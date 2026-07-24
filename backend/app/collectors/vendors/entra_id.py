"""Microsoft Entra ID (Azure AD) — sign-in + directory audit logs.

Vendor novo = 1 módulo, ZERO core. Identity #1 em ambientes Microsoft (par do
Defender já existente). REUSA o OAuth Graph do Defender (mesmo
``defender_refresher``/``defender_probe`` — client_credentials no tenant Azure AD,
scope ``graph/.default``) — zero refresher/probe novos.

**Endpoints (Graph v1.0):**
- ``GET /auditLogs/signIns`` — stream ``signins`` (event_type ``entra_id.signin``).
- ``GET /auditLogs/directoryAudits`` — stream ``audit`` (event_type ``entra_id.audit``).

**Paginação:** ``value[]`` + ``@odata.nextLink`` (segue o link opaco até sumir).
**Cursor incremental:** ``$filter=<ts> ge '<cursor>'`` + dedupe por ``id`` (a doc
exemplifica ``ge``, não ``gt``; signIns não suporta ``$orderby`` em v1.0 e já vem
ordenado por ``createdDateTime`` desc — coletamos tudo e guardamos o max).

Perms da app: ``AuditLog.Read.All`` (cobre os 2 streams). Sign-in logs exigem
Entra ID P1/P2 no tenant. Throttle 429 → respeita ``Retry-After`` (segundos).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict, Optional

from ..base import BaseCollector
from ..metrics import API_LATENCY
from ._rate_limit import VendorRateLimitedError

logger = logging.getLogger(__name__)

_GRAPH_DOMAIN = "graph.microsoft.com"

# Teto de páginas por CICLO Celery. Com ``$top=1000``, 25 × 1000 = 25.000 eventos/ciclo.
# Sign-in logs são ALTO volume: sem este guard, um backlog grande é drenado num ÚNICO run
# — o ``while`` abaixo segue ``@odata.nextLink`` página após página até o Graph parar de
# devolver o link — estourando o ``task_soft_time_limit`` (720s). No soft-timeout o
# pipeline reverte o cursor p/ ``cursor_before`` e solta TODAS as claims → loop sem
# progresso (a coleta trava). Ao atingir o teto salvamos o cursor RESUMÍVEL (o
# ``@odata.nextLink`` da PRÓXIMA página, preservando ``last_ts`` — NÃO o watermark
# ``latest_seen``) e retornamos ANTES da escrita final (a que zera o nextLink e avança o
# watermark, descartando o backlog não lido). O próximo ciclo retoma exatamente desse
# nextLink; a borda ``ge`` re-buscada é deduplicada por ``id`` no pipeline. Espelha
# ``_MAX_PAGES_PER_CYCLE`` de ``wazuh_detections`` / ``sophos_detections``.
_MAX_PAGES_PER_CYCLE = 25


class EntraRateLimitedError(VendorRateLimitedError):
    def __init__(self, retry_after: int) -> None:
        super().__init__(retry_after, vendor="entra_id")


class EntraSignInsCollector(BaseCollector):
    """Sign-in logs via Graph (``auditLogs/signIns``). Cursor: ``createdDateTime``."""

    platform = "entra_id"
    stream = "signins"
    event_type = "entra_id.signin"
    domain = _GRAPH_DOMAIN

    _ENDPOINT = "https://graph.microsoft.com/v1.0/auditLogs/signIns"
    _CURSOR_FIELD = "createdDateTime"

    async def collect(self) -> AsyncIterator[Dict[str, Any]]:
        cursor = self.ctx.cursor or {}
        last_ts: str = cursor.get(self._CURSOR_FIELD) or _default_lookback_iso()
        next_link: Optional[str] = cursor.get("@odata.nextLink")
        latest_seen = last_ts

        if next_link:
            url, params = next_link, None
        else:
            url = self._ENDPOINT
            params = {"$filter": f"{self._CURSOR_FIELD} ge {last_ts}", "$top": 1000}

        page_count = 0
        while True:
            # Teto por ciclo: encerra o run e retoma no próximo ciclo (ver
            # _MAX_PAGES_PER_CYCLE). Salva o cursor RESUMÍVEL — o ``@odata.nextLink`` da
            # PRÓXIMA página, preservando ``last_ts`` — e retorna ANTES da escrita final
            # (linha ~:96, que zera o nextLink e avança o watermark p/ ``latest_seen``,
            # descartando as páginas não lidas). ``next_link`` aqui já é o token gravado
            # no cursor pela iteração anterior; a borda ``ge`` re-buscada é deduplicada.
            page_count += 1
            if self.ctx.bounded_per_cycle and page_count > _MAX_PAGES_PER_CYCLE:
                self.ctx.cursor = {self._CURSOR_FIELD: last_ts, "@odata.nextLink": next_link}
                # Sobrou backlog: sign-in log é o stream de maior volume da frota e
                # o que mais bate o teto — sem este sinal, o atraso cresce com a
                # Saúde do Pipeline verde.
                self.mark_cycle_capped()
                logger.info(
                    "entra_id %s: teto de %d páginas/ciclo atingido — cursor no "
                    "@odata.nextLink p/ próximo ciclo (integration=%s)",
                    self.stream, _MAX_PAGES_PER_CYCLE, self.ctx.integration_id,
                )
                return

            await self.ctx.rate_limiter.acquire(self.ctx.integration_id, self.platform)

            started = time.monotonic()
            async with self.ctx.domain_limiter.slot(self.domain):
                async with self.ctx.session.get(
                    url, headers=self.ctx.headers, params=params
                ) as resp:
                    if resp.status == 429:
                        retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                        await self.ctx.rate_limiter.backoff(self.platform, retry_after)
                        raise EntraRateLimitedError(retry_after)
                    resp.raise_for_status()
                    payload = await resp.json()

            API_LATENCY.labels(vendor=self.platform, stream=self.stream).observe(
                time.monotonic() - started
            )

            for ev in payload.get("value", []) or []:
                ts = ev.get(self._CURSOR_FIELD)
                if isinstance(ts, str) and ts > latest_seen:
                    latest_seen = ts
                yield ev

            next_link = payload.get("@odata.nextLink")
            if not next_link:
                break
            url, params = next_link, None
            self.ctx.cursor = {self._CURSOR_FIELD: last_ts, "@odata.nextLink": next_link}

        self.ctx.cursor = {self._CURSOR_FIELD: latest_seen, "@odata.nextLink": None}

    def extract_message_id(self, event: Dict[str, Any]) -> str:
        return str(event.get("id") or "")

    @classmethod
    def watermark_at(cls, cursor: Optional[Dict[str, Any]]) -> Optional[datetime]:
        """Lê ``cls._CURSOR_FIELD`` — que é OUTRO campo em cada subclasse.

        ``signIns`` usa ``createdDateTime`` e ``directoryAudits`` usa
        ``activityDateTime``. Fixar o nome aqui faria o stream de auditoria
        devolver ``None`` para sempre e sumir do indicador de atraso — o mesmo
        ponto cego, só que num stream a menos.
        """
        return cls.watermark_from_iso(cursor, cls._CURSOR_FIELD)


class EntraDirectoryAuditCollector(EntraSignInsCollector):
    """Directory audit logs (``auditLogs/directoryAudits``). Cursor: ``activityDateTime``."""

    stream = "audit"
    event_type = "entra_id.audit"
    _ENDPOINT = "https://graph.microsoft.com/v1.0/auditLogs/directoryAudits"
    _CURSOR_FIELD = "activityDateTime"


def _default_lookback_iso() -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=1)
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_retry_after(value: str | None) -> int:
    if not value:
        return 5
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 5


# ── Self-registration (reusa OAuth Graph do Defender) ──────────────────


def _register() -> None:
    from datetime import timedelta as _td

    from ..auth.probes import defender_probe  # mesmo Graph client_credentials
    from ..auth.refreshers import defender_refresher
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
            platform="entra_id",
            display_name="Microsoft Entra ID",
            category="Identity",
            description="Microsoft Entra ID (Azure AD) — sign-in e directory audit logs (Graph).",
            icon_id="microsoft",
            docs_url="https://learn.microsoft.com/en-us/graph/api/resources/azure-ad-auditlog-overview",
            order=25,
            test_fn=defender_probe,
            required_secrets=("client_secret",),
            capabilities=frozenset({"catalog", "auth:test", "collect:signins", "collect:audit"}),
            auth_fields=(
                AuthField(key="tenant_id", label="Tenant ID", type="string", required=True,
                          help_text="Azure AD Tenant ID"),
                AuthField(key="client_id", label="Client ID", type="string", required=True,
                          help_text="App Registration Client ID (perm AuditLog.Read.All)"),
                AuthField(key="client_secret", label="Client Secret", type="secret", required=True),
            ),
        )
    )

    for collector_cls in (EntraSignInsCollector, EntraDirectoryAuditCollector):
        register(
            CollectorRegistration(
                platform=collector_cls.platform,
                stream=collector_cls.stream,
                collector_cls=collector_cls,
                refresh_fn=defender_refresher,
                schedule=_td(minutes=5),
                queue=Q_PRIORITY,
                task_name=T_COLLECT_PRIORITY,
            )
        )


_register()
