"""Okta System Log — collector de identity logs.

Vendor novo = 1 módulo, ZERO core. IdP #1 em SOC — destrava detecção de identidade
(impossible travel, MFA fatigue).

**Auth: API token SSWS** (estático, sem refresh) — ``Authorization: SSWS <token>``.
Como o Wazuh (basic auth), o collector é AUTO-CONTIDO: lê o org URL (``base_url``)
e o token do store no ``collect()`` e monta o header; o ``refresh_fn`` é no-op.

**Endpoint:** ``GET {org}/api/v1/logs`` em modo POLLING (``sortOrder=ASCENDING``,
sem ``until``) — ordena por persistence time e captura eventos atrasados sem perda.
**Paginação:** segue o header HTTP ``Link; rel="next"`` (a Okta proíbe paginar
manualmente por ``since``/``until``). O cursor persistido é a **URL inteira do next
link** (carrega o cursor ``after`` opaco). ``since`` só no cold start. Dedupe: ``uuid``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict, Optional
from urllib.parse import urlparse

from ..base import BaseCollector
from ..metrics import API_LATENCY
from ._rate_limit import VendorRateLimitedError

logger = logging.getLogger(__name__)

_PAGE_SIZE = 200

# Teto de páginas por CICLO Celery (50 × 200 = 10.000 eventos/ciclo). Sem este guard,
# um backlog grande é drenado num ÚNICO run — o while abaixo segue Link após Link até
# exaurir a Okta — estourando o ``task_soft_time_limit`` (720s). No soft-timeout o
# pipeline reverte o cursor para ``cursor_before`` e solta TODAS as claims → loop sem
# progresso (não coleta). Ao atingir o teto, o cursor já aponta para a PRÓXIMA página
# (``next_url`` resumível — o token ``after`` opaco) e devolvemos o slot do worker; o
# próximo ciclo retoma daí (a borda re-buscada é deduplicada por ``uuid``). NÃO caímos
# em nenhuma escrita final de watermark. Espelha ``_MAX_PAGES_PER_CYCLE`` dos coletores
# de detections do Wazuh/Sophos.
_MAX_PAGES_PER_CYCLE = 50


class OktaRateLimitedError(VendorRateLimitedError):
    def __init__(self, retry_after: int) -> None:
        super().__init__(retry_after, vendor="okta")


class OktaSystemLogCollector(BaseCollector):
    """Pull do System Log da Okta (paginação por ``Link: rel=next``)."""

    platform = "okta"
    stream = "system_log"
    event_type = "okta.system_log"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._base: Optional[str] = None
        self._host: Optional[str] = None
        self._token: Optional[str] = None

    @property
    def domain(self) -> str:
        return self._host or "okta.com"

    def _load_conn(self) -> Dict[str, Any]:
        """Org URL (base) + SSWS token do store (sync, em thread)."""
        from ...core.url_policy import normalize_service_url
        from ...db import database, models
        from ...services import integration_secrets

        with database.SessionLocal() as db:
            integ = db.get(models.Integration, self.ctx.integration_id)
            if integ is None:
                raise RuntimeError(f"okta: integração {self.ctx.integration_id} não encontrada")
            base = normalize_service_url(integ.base_url or "")
            if not base:
                raise RuntimeError(
                    f"okta: integração {self.ctx.integration_id} sem base_url (org Okta)"
                )
            token = integration_secrets.read_secret(integ, "api_token") or ""
            if not token:
                raise RuntimeError(
                    f"okta: integração {self.ctx.integration_id} sem api_token (SSWS)"
                )
            return {"base_url": base.rstrip("/"), "token": token}

    async def collect(self) -> AsyncIterator[Dict[str, Any]]:
        conn = await asyncio.to_thread(self._load_conn)
        self._base = conn["base_url"]
        self._host = urlparse(self._base).hostname or "okta.com"
        self._token = conn["token"]
        headers = {
            "Authorization": f"SSWS {self._token}",
            "Accept": "application/json",
        }

        cursor = self.ctx.cursor or {}
        # Retoma da URL do next link salvo; no cold start, monta a janela polling.
        url: Optional[str] = cursor.get("next_url")
        if not url:
            since = cursor.get("since") or _default_lookback_iso()
            url = (
                f"{self._base}/api/v1/logs?since={since}"
                f"&sortOrder=ASCENDING&limit={_PAGE_SIZE}"
            )

        pages = 0
        while True:
            await self.ctx.rate_limiter.acquire(self.ctx.integration_id, self.platform)

            started = time.monotonic()
            async with self.ctx.domain_limiter.slot(self.domain):
                async with self.ctx.session.get(url, headers=headers) as resp:
                    if resp.status == 429:
                        retry_after = _parse_rate_limit_reset(resp.headers)
                        await self.ctx.rate_limiter.backoff(self.platform, retry_after)
                        raise OktaRateLimitedError(retry_after)
                    resp.raise_for_status()
                    events = await resp.json()
                    next_url = _next_link(resp.headers)

            API_LATENCY.labels(vendor=self.platform, stream=self.stream).observe(
                time.monotonic() - started
            )

            if not events:
                # Modo polling: o next link sempre existe; array vazio = sem novos
                # eventos → encerra o ciclo retomando deste ponto na próxima coleta.
                if next_url:
                    self.ctx.cursor = {"next_url": next_url}
                break

            for ev in events:
                yield ev

            if not next_url:
                break
            url = next_url
            self.ctx.cursor = {"next_url": next_url}

            # Teto por ciclo: o cursor acima já aponta p/ a PRÓXIMA página (``next_url``,
            # resumível). Encerra o run graciosamente e devolve o slot do worker; o
            # próximo ciclo retoma daqui (borda re-buscada é deduplicada por ``uuid``).
            # Ver _MAX_PAGES_PER_CYCLE — NÃO caímos em escrita final de watermark.
            pages += 1
            if self.ctx.bounded_per_cycle and pages >= _MAX_PAGES_PER_CYCLE:
                # Sobrou backlog. O cursor da Okta é o ``next_url`` opaco (sem
                # instante traduzível), então este sinal é a única evidência de
                # atraso que este stream consegue produzir.
                self.mark_cycle_capped()
                logger.info(
                    "okta system_log: teto de %d páginas/ciclo atingido — cursor em "
                    "next_url p/ próximo ciclo (integration=%s)",
                    _MAX_PAGES_PER_CYCLE, self.ctx.integration_id,
                )
                break

    def extract_message_id(self, event: Dict[str, Any]) -> str:
        return str(event.get("uuid") or "")


def _default_lookback_iso() -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=1)
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _next_link(headers) -> Optional[str]:
    """URL do ``Link; rel="next"``. Okta manda Links separados (self/next)."""
    raws = []
    getall = getattr(headers, "getall", None)
    if callable(getall):
        try:
            raws = list(headers.getall("Link"))
        except Exception:  # noqa: BLE001
            raws = []
    if not raws:
        single = headers.get("Link")
        if single:
            raws = [single]
    for raw in raws:
        for seg in raw.split(","):
            s = seg.strip()
            if 'rel="next"' in s and "<" in s and ">" in s:
                return s[s.find("<") + 1 : s.find(">")]
    return None


def _parse_rate_limit_reset(headers) -> int:
    """``X-Rate-Limit-Reset`` da Okta é epoch UTC (s); converte p/ delta."""
    raw = headers.get("X-Rate-Limit-Reset") or headers.get("Retry-After")
    if not raw:
        return 5
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return 5
    if val > 100000:  # epoch → delta
        return max(1, val - int(time.time()))
    return max(1, val)


# ── Self-registration (refresher no-op — SSWS, não OAuth) ──────────────


async def _okta_refresher(integration_id: int) -> Dict[str, object]:
    """No-op p/ o framework: Okta usa SSWS (basic-token estático), não OAuth. O
    collector lê o token do store no collect() e monta o header SSWS."""
    return {"access_token": "", "expires_in": 3600}


async def _okta_probe(cfg: Dict[str, Any]):
    """Teste STATELESS pré-save: GET /api/v1/logs?limit=1 com o SSWS digitado."""
    import aiohttp

    from ..output.base import TestResult

    base = (cfg.get("base_url") or "").rstrip("/")
    token = cfg.get("api_token") or ""
    if not base or not token:
        return TestResult.failed("Informe a Org URL e o API Token (SSWS).")
    t0 = time.perf_counter()
    timeout = aiohttp.ClientTimeout(total=20)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                f"{base}/api/v1/logs?limit=1",
                headers={"Authorization": f"SSWS {token}", "Accept": "application/json"},
            ) as r:
                ms = (time.perf_counter() - t0) * 1000.0
                if r.status == 200:
                    return TestResult.passed("Conexão OK — token válido.", latency_ms=ms)
                hint = "Verifique o API Token." if r.status in (401, 403) else "Verifique a Org URL."
                return TestResult.failed(f"Falha (HTTP {r.status}). {hint}")
    except Exception as exc:  # noqa: BLE001
        return TestResult.failed(f"Não foi possível conectar: {exc}")


def _register() -> None:
    from datetime import timedelta as _td

    from ..queues import Q_BULK, T_COLLECT_BULK
    from ..registry import (
        AuthField,
        CollectorRegistration,
        PlatformRegistration,
        register,
        register_platform,
    )

    register_platform(
        PlatformRegistration(
            platform="okta",
            display_name="Okta",
            category="Identity",
            description="Okta System Log — eventos de identidade (logins, MFA, lifecycle).",
            icon_id="okta",
            docs_url="https://developer.okta.com/docs/reference/system-log-query/",
            order=26,
            test_fn=_okta_probe,
            required_secrets=("api_token",),
            capabilities=frozenset({"catalog", "auth:test", "collect:system_log"}),
            auth_fields=(
                AuthField(key="base_url", label="Org URL", type="url", required=True,
                          help_text="URL da sua org Okta (ex: https://acme.okta.com)"),
                AuthField(key="api_token", label="API Token (SSWS)", type="secret", required=True,
                          help_text="Token de API da Okta (Security > API > Tokens). Scope de leitura."),
            ),
        )
    )

    register(
        CollectorRegistration(
            platform=OktaSystemLogCollector.platform,
            stream=OktaSystemLogCollector.stream,
            collector_cls=OktaSystemLogCollector,
            refresh_fn=_okta_refresher,
            schedule=_td(minutes=2),
            queue=Q_BULK,
            task_name=T_COLLECT_BULK,
        )
    )


_register()
