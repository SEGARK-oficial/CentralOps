"""CrowdStrike Falcon — collector de detecções/alertas.

Vendor novo = 1 módulo self-registering, ZERO core (não toca pipeline/beat/
routing/registry). EDR tier-1 de 3ª parte — fecha a paridade de endpoint.

**API (Alerts API v2, moderna — a Detects v1 foi decomissionada em 09/2025):**
pull incremental via ``POST {base}/alerts/combined/alerts/v1`` (1 passo: query +
detalhe juntos, paginação por cursor ``after`` — sem o teto de 10k do offset da
``/alerts/queries/alerts/v2``). Cursor de tempo = ``created_timestamp`` (imutável
→ paginação estável; o CrowdStrike alerta que ordenar por campo MUTÁVEL duplica/
perde registros entre páginas). Dedupe por ``composite_id``.

**Auth:** OAuth2 client_credentials → ``POST {base}/oauth2/token`` (form
``client_id``/``client_secret``; sem ``grant_type``/``scope`` explícitos no
contrato CrowdStrike). Token ``Authorization: Bearer`` (≈30 min). Encaixa no
framework OAuth do collector (``refresh_fn`` → ``oauth_cache`` → ``ctx.headers``).

**Base region-aware** (api.crowdstrike.com / api.us-2 / api.eu-1 / GOV) é
POR-INTEGRAÇÃO — a base errada dá erro de auth, não redirect. O collector
carrega ``base_url`` do banco (não dá p/ inferir); o token vem do framework.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict, Optional
from urllib.parse import urlparse

import aiohttp

from ..base import BaseCollector
from ..capabilities import (
    CAP_QUERY_FQL,
    DIALECT_FQL,
    QUERY_MODE_LIVE,
    QueryCapability,
)
from ..metrics import API_LATENCY
from ._rate_limit import VendorRateLimitedError

logger = logging.getLogger(__name__)

_DEFAULT_BASE = "https://api.crowdstrike.com"
_PAGE_SIZE = 1000  # máx do combined/alerts/v1

# Teto de páginas por CICLO Celery (20 × 1000 = ~20.000 eventos/ciclo). Sem este
# guard, um backlog grande é drenado num ÚNICO run — o while abaixo pagina via
# cursor ``after`` até exaurir o vendor — estourando o ``task_soft_time_limit``
# (720s). No soft-timeout o pipeline reverte o cursor e solta TODAS as claims →
# loop sem progresso (não coleta). Ao atingir o teto, salvamos o cursor RESUMÍVEL
# (o token ``after`` da PRÓXIMA página, com o MESMO ``created_after``) e devolvemos
# o slot do worker; o próximo ciclo retoma exatamente daí (filter/sort imutáveis →
# paginação estável). CRÍTICO: NÃO caímos no cursor final {latest_seen, after:None}
# no caminho do teto — isso avançaria o watermark ``created_after`` e descartaria o
# ``after``, jogando fora as páginas ainda não lidas. Espelha ``_MAX_PAGES_PER_CYCLE``
# dos coletores de detections da Sophos/Wazuh.
_MAX_PAGES_PER_CYCLE = 20

# query FQL ao vivo (Falcon Alerts API v2). Síncrono. Teto de janela
# de 7d (FQL não documenta limite — evita poison-query unbounded, Invariante #5).
CROWDSTRIKE_QUERY_CAPABILITY = QueryCapability(
    dialect=DIALECT_FQL,
    modes=(QUERY_MODE_LIVE,),
    supports_async=False,
    max_window=timedelta(days=7),
    required_secrets=("client_secret",),
    ocsf_mapping_version="1",
)


def _crowdstrike_provider(integration):
    """Factory tardia do ``CrowdStrikeProvider`` rico (query FQL)."""
    from ...providers.crowdstrike.provider import CrowdStrikeProvider

    return CrowdStrikeProvider(integration)


class CrowdStrikeRateLimitedError(VendorRateLimitedError):
    def __init__(self, retry_after: int) -> None:
        super().__init__(retry_after, vendor="crowdstrike")


class CrowdStrikeDetectionsCollector(BaseCollector):
    """Pull de detecções do Falcon (Alerts API v2, cursor ``after``)."""

    platform = "crowdstrike"
    stream = "detections"
    event_type = "crowdstrike.detection"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._base: Optional[str] = None
        self._host: Optional[str] = None

    @property
    def domain(self) -> str:
        return self._host or "api.crowdstrike.com"

    def _load_base_url(self) -> str:
        """Base region-aware da integração (sync, em thread). Token vem do framework."""
        from ...core.url_policy import normalize_service_url
        from ...db import database, models

        with database.SessionLocal() as db:
            integ = db.get(models.Integration, self.ctx.integration_id)
            if integ is None:
                raise RuntimeError(
                    f"crowdstrike: integração {self.ctx.integration_id} não encontrada"
                )
            base = normalize_service_url(integ.base_url or _DEFAULT_BASE) or _DEFAULT_BASE
            return base.rstrip("/")

    async def collect(self) -> AsyncIterator[Dict[str, Any]]:
        self._base = await asyncio.to_thread(self._load_base_url)
        self._host = urlparse(self._base).hostname or "api.crowdstrike.com"
        url = f"{self._base}/alerts/combined/alerts/v1"

        cursor = self.ctx.cursor or {}
        created_after: str = cursor.get("created_after") or _default_lookback_iso()
        after: Optional[str] = cursor.get("after")
        latest_seen = created_after
        page_count = 0

        while True:
            # Teto por ciclo: encerra o run e retoma no próximo ciclo via ``after``
            # (filter/sort imutáveis = paginação estável). ``after`` aqui é o token
            # da PRÓXIMA página (setado no fim da iteração anterior, junto ao cursor
            # intermediário). NÃO caímos no cursor final {latest_seen, after:None} —
            # isso avançaria ``created_after`` e descartaria o ``after``, jogando fora
            # as páginas ainda não lidas. Ver _MAX_PAGES_PER_CYCLE.
            page_count += 1
            if self.ctx.bounded_per_cycle and page_count > _MAX_PAGES_PER_CYCLE:
                self.ctx.cursor = {"created_after": created_after, "after": after}
                # Sobrou backlog: como ``created_after`` fica parado de propósito,
                # este é o único sinal que distingue "tenant sem detecções" de
                # "não damos conta do volume" na Saúde do Pipeline.
                self.mark_cycle_capped()
                logger.info(
                    "crowdstrike detections: teto de %d páginas/ciclo atingido — cursor "
                    "resumível em created_after=%s after=%s p/ próximo ciclo (integration=%s)",
                    _MAX_PAGES_PER_CYCLE, created_after, after, self.ctx.integration_id,
                )
                return

            await self.ctx.rate_limiter.acquire(self.ctx.integration_id, self.platform)

            body: Dict[str, Any] = {
                # Imutável → paginação estável. Filtro incremental por janela.
                "filter": f"created_timestamp:>'{created_after}'",
                "sort": "created_timestamp|asc",
                "limit": _PAGE_SIZE,
            }
            if after:
                body["after"] = after

            started = time.monotonic()
            async with self.ctx.domain_limiter.slot(self.domain):
                async with self.ctx.session.post(
                    url, json=body, headers=self.ctx.headers
                ) as resp:
                    if resp.status == 429:
                        retry_after = _parse_retry_after(resp.headers)
                        await self.ctx.rate_limiter.backoff(self.platform, retry_after)
                        raise CrowdStrikeRateLimitedError(retry_after)
                    resp.raise_for_status()
                    payload = await resp.json()

            API_LATENCY.labels(vendor=self.platform, stream=self.stream).observe(
                time.monotonic() - started
            )

            resources = payload.get("resources") or []
            if not resources:
                break

            for ev in resources:
                ts = ev.get("created_timestamp")
                if isinstance(ts, str) and ts > latest_seen:
                    latest_seen = ts
                yield ev

            after = ((payload.get("meta") or {}).get("pagination") or {}).get("after")
            if not after:
                break
            # Cursor intermediário p/ retomada (filter estável dentro do ciclo).
            self.ctx.cursor = {"created_after": created_after, "after": after}

        self.ctx.cursor = {"created_after": latest_seen, "after": None}

    def extract_message_id(self, event: Dict[str, Any]) -> str:
        return str(event.get("composite_id") or event.get("id") or "")

    @classmethod
    def watermark_at(cls, cursor: Optional[Dict[str, Any]]) -> Optional[datetime]:
        """``created_after`` — o piso do filtro FQL, em ISO com milissegundos.

        É o watermark real: o ciclo capado o preserva (e guarda o ``after``), e o
        ciclo drenado o avança para o ``created_timestamp`` mais recente visto.
        """
        return cls.watermark_from_iso(cursor, "created_after")


def _default_lookback_iso() -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=1)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_retry_after(headers) -> int:
    """``X-RateLimit-RetryAfter`` do CrowdStrike é um EPOCH UTC (não 'segundos a
    esperar'); converte p/ delta. Fallback no ``Retry-After`` padrão."""
    raw = headers.get("X-RateLimit-RetryAfter") or headers.get("Retry-After")
    if not raw:
        return 5
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return 5
    if val > 100000:  # parece epoch → delta até lá
        return max(1, val - int(time.time()))
    return max(1, val)


# ── Auth INLINE (zero edição em refreshers.py/probes.py) ───────────────


async def _crowdstrike_refresher(integration_id: int) -> Dict[str, object]:
    """OAuth2 client_credentials no token endpoint region-aware. Lê client_secret
    do store e base_url da integração."""

    def _load() -> tuple[str, str, str]:
        from ...db import database, models
        from ...services import integration_secrets

        with database.SessionLocal() as db:
            integ = db.get(models.Integration, integration_id)
            if integ is None:
                raise RuntimeError(f"crowdstrike: integração id={integration_id} não encontrada")
            base = (integ.base_url or _DEFAULT_BASE).rstrip("/")
            client_id = (integ.client_id or "").strip()
            client_secret = integration_secrets.read_secret(integ, "client_secret") or ""
            return base, client_id, client_secret

    base, client_id, client_secret = await asyncio.to_thread(_load)
    if not client_id or not client_secret:
        raise RuntimeError(
            f"crowdstrike: integração id={integration_id} sem client_id/client_secret"
        )

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            f"{base}/oauth2/token",
            data={"client_id": client_id, "client_secret": client_secret},
        ) as r:
            r.raise_for_status()
            payload = await r.json()

    return {
        "access_token": payload["access_token"],
        "expires_in": int(payload.get("expires_in", 1799)),
    }


async def _crowdstrike_probe(cfg: Dict[str, Any]):
    """Teste de conexão STATELESS pré-save — OAuth no token endpoint region-aware."""
    from ..auth.probes import oauth_client_credentials_probe

    base = (cfg.get("base_url") or _DEFAULT_BASE).rstrip("/")
    return await oauth_client_credentials_probe(
        f"{base}/oauth2/token",
        cfg.get("client_id", ""),
        cfg.get("client_secret", ""),
        scope="",  # CrowdStrike não usa scope no client_credentials
    )


# ── Self-registration ─────────────────────────────────────────────────


def _register() -> None:
    from datetime import timedelta as _td

    from ..queues import Q_PRIORITY, T_COLLECT_PRIORITY
    from ..registry import (
        AuthField,
        CollectorRegistration,
        PlatformRegistration,
        register,
        register_platform,
    )

    register_platform(
        PlatformRegistration(
            platform="crowdstrike",
            display_name="CrowdStrike Falcon",
            category="EDR / XDR",
            description="CrowdStrike Falcon — detecções/alertas (Alerts API v2).",
            icon_id="crowdstrike",
            docs_url="https://developer.crowdstrike.com/api-reference/collections/alerts/",
            order=15,
            test_fn=_crowdstrike_probe,
            provider_factory=_crowdstrike_provider,
            required_secrets=("client_secret",),
            capabilities=frozenset({"catalog", "auth:test", "collect:detections", CAP_QUERY_FQL}),
            query_capabilities=(CROWDSTRIKE_QUERY_CAPABILITY,),
            auth_fields=(
                AuthField(key="client_id", label="Client ID", type="string", required=True,
                          help_text="Client ID da API CrowdStrike (Falcon > API Clients & Keys)"),
                AuthField(key="client_secret", label="Client Secret", type="secret", required=True),
                AuthField(key="base_url", label="Base URL (região)", type="url", required=True,
                          help_text="Base region-aware: https://api.crowdstrike.com (US-1), "
                                    "https://api.us-2.crowdstrike.com, https://api.eu-1.crowdstrike.com"),
            ),
        )
    )

    register(
        CollectorRegistration(
            platform=CrowdStrikeDetectionsCollector.platform,
            stream=CrowdStrikeDetectionsCollector.stream,
            collector_cls=CrowdStrikeDetectionsCollector,
            refresh_fn=_crowdstrike_refresher,
            schedule=_td(minutes=2),
            queue=Q_PRIORITY,
            task_name=T_COLLECT_PRIORITY,
        )
    )


_register()
