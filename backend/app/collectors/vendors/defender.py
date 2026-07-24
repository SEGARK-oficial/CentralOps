"""Microsoft Defender (Graph Security API) — incidents collector.

Endpoint: ``GET https://graph.microsoft.com/v1.0/security/incidents``

Paginação via ``@odata.nextLink`` (URL completa para a próxima página).
Filtro delta via ``$filter=lastUpdateDateTime ge <iso>``.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict

from ..base import BaseCollector
from ..capabilities import (
    CAP_QUERY_KQL,
    DIALECT_KQL,
    QUERY_MODE_LIVE,
    SPEC_PASSTHROUGH,
    SPEC_SIGMA,
    QueryCapability,
)
from ..metrics import API_LATENCY

logger = logging.getLogger(__name__)


from ._rate_limit import VendorRateLimitedError

# query KQL ao vivo (Graph advanced hunting / runHuntingQuery).
# Síncrono. Teto de 30d + rate-limit ~45/min/tenant (limites do Graph) — ENFORCED
# no QueryService a partir daqui (Invariante #5).
DEFENDER_QUERY_CAPABILITY = QueryCapability(
    dialect=DIALECT_KQL,
    modes=(QUERY_MODE_LIVE,),
    supports_async=False,
    max_window=timedelta(days=30),
    rate_limit="45/min/tenant",
    required_secrets=("client_secret",),
    ocsf_mapping_version="1",
    # kql tem backend pySigma (microsoft365defender) → aceita spec_kind=sigma.
    spec_kinds=(SPEC_PASSTHROUGH, SPEC_SIGMA),
)


def _defender_provider(integration):
    """Factory tardia do ``DefenderProvider`` rico (query KQL)."""
    from ...providers.defender.provider import DefenderProvider

    return DefenderProvider(integration)


class DefenderRateLimitedError(VendorRateLimitedError):
    def __init__(self, retry_after: int) -> None:
        super().__init__(retry_after, vendor="graph")


# Teto de páginas por CICLO Celery (50 × 100 = 5.000 incidentes/ciclo). Sem este
# guard, um backlog grande é drenado num ÚNICO run — o while abaixo segue o
# ``@odata.nextLink`` página após página até exaurir o vendor — estourando o
# ``task_soft_time_limit`` (720s). No soft-timeout o pipeline reverte o cursor para
# cursor_before e solta TODAS as claims → loop sem progresso (não coleta). Escritas de
# cursor no MEIO do laço não sobrevivem ao revert; só um return gracioso faz o pipeline
# COMMITAR o cursor pelo caminho de sucesso. Ao atingir o teto, salvamos o cursor
# RESUMÍVEL (o ``nextLink`` da PRÓXIMA página, com ``lastUpdateDateTime`` preservado em
# ``last_ts``) e devolvemos o slot do worker; o próximo ciclo retoma exatamente deste
# ponto. Espelha ``_MAX_PAGES_PER_CYCLE`` dos coletores de detections da Sophos/Wazuh.
_MAX_PAGES_PER_CYCLE = 50


class DefenderIncidentsCollector(BaseCollector):
    platform = "microsoft_defender"
    stream = "incidents"
    event_type = "defender.incident"
    domain = "graph.microsoft.com"

    async def collect(self) -> AsyncIterator[Dict[str, Any]]:
        cursor = self.ctx.cursor or {}
        last_ts: str = cursor.get("lastUpdateDateTime") or _default_lookback_iso()
        next_link: str | None = cursor.get("@odata.nextLink")

        latest_seen = last_ts

        if next_link:
            url = next_link
            params: Dict[str, Any] = {}
        else:
            url = "https://graph.microsoft.com/v1.0/security/incidents"
            params = {
                "$filter": f"lastUpdateDateTime ge {last_ts}",
                "$orderby": "lastUpdateDateTime asc",
                "$top": 100,
            }

        page_count = 0
        while True:
            # Teto por ciclo: encerra o run e retoma no próximo ciclo (ver
            # _MAX_PAGES_PER_CYCLE). Salvamos o cursor RESUMÍVEL — o ``next_link`` da
            # PRÓXIMA página, com ``lastUpdateDateTime`` preservado em ``last_ts`` (NÃO
            # ``latest_seen``) — e retornamos ANTES do próximo fetch. NÃO caímos na
            # escrita FINAL do cursor (que avança o watermark p/ latest_seen e zera o
            # nextLink), pois isso DESCARTARIA as páginas ainda não lidas. Na 1ª iteração
            # o guard nunca dispara (page_count=1), então ``next_link`` aqui é sempre o
            # token da página seguinte (setado no fim da iteração anterior). O próximo
            # ciclo retoma deste nextLink; dedup id@lastUpdateDateTime absorve a borda.
            page_count += 1
            if self.ctx.bounded_per_cycle and page_count > _MAX_PAGES_PER_CYCLE:
                self.ctx.cursor = {
                    "lastUpdateDateTime": last_ts,
                    "@odata.nextLink": next_link,
                }
                # Sobrou backlog: o watermark fica em ``last_ts`` de propósito, e
                # sem este sinal esse "parado" é lido como tenant sem incidentes.
                self.mark_cycle_capped()
                logger.info(
                    "defender incidents: teto de %d páginas/ciclo atingido — cursor "
                    "resumível em nextLink p/ próximo ciclo (integration=%s)",
                    _MAX_PAGES_PER_CYCLE, self.ctx.integration_id,
                )
                return

            await self.ctx.rate_limiter.acquire(
                self.ctx.integration_id, self.platform
            )

            started = time.monotonic()
            async with self.ctx.domain_limiter.slot(self.domain):
                async with self.ctx.session.get(
                    url, headers=self.ctx.headers, params=params or None
                ) as resp:
                    if resp.status == 429:
                        retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                        await self.ctx.rate_limiter.backoff(
                            self.platform, retry_after
                        )
                        raise DefenderRateLimitedError(retry_after)
                    resp.raise_for_status()
                    payload = await resp.json()

            API_LATENCY.labels(vendor=self.platform, stream=self.stream).observe(
                time.monotonic() - started
            )

            for ev in payload.get("value", []) or []:
                ts = ev.get("lastUpdateDateTime") or latest_seen
                if isinstance(ts, str) and ts > latest_seen:
                    latest_seen = ts
                yield ev

            next_link = payload.get("@odata.nextLink")
            if not next_link:
                break

            url = next_link
            params = {}  # nextLink já contém todos os filtros

            # Cursor intermediário para retomada.
            self.ctx.cursor = {
                "lastUpdateDateTime": last_ts,
                "@odata.nextLink": next_link,
            }

        self.ctx.cursor = {
            "lastUpdateDateTime": latest_seen,
            "@odata.nextLink": None,
        }

    def extract_message_id(self, event: Dict[str, Any]) -> str:
        # incidentes são MUTÁVEIS — este collector existe
        # para captar transições de estado (active→resolved) via
        # ``lastUpdateDateTime ge {cursor}``. Com a dedup key = id CRU + TTL 7d, a
        # 1ª emissão reclamava a chave e o UPDATE era descartado (perda de
        # correção). Compor a key com lastUpdateDateTime: cada estado distinto do
        # incidente é uma chave distinta (dups verdadeiros — mesmo id+update — ainda
        # deduplicam, ex.: a borda inclusiva do filtro `ge`).
        base = str(event.get("id") or event.get("incidentId") or "")
        updated = str(
            event.get("lastUpdateDateTime") or event.get("lastModifiedDateTime") or ""
        )
        return f"{base}@{updated}" if (base and updated) else base

    @classmethod
    def watermark_at(cls, cursor: Optional[Dict[str, Any]]) -> Optional[datetime]:
        """``lastUpdateDateTime`` — o piso do ``$filter`` no Graph.

        Incidente é MUTÁVEL e o stream é ordenado por hora de ATUALIZAÇÃO, então o
        watermark mede até que ponto da linha do tempo de mudanças chegamos — que
        é exatamente o que atrasa quando o teto por ciclo não vence o volume.
        """
        return cls.watermark_from_iso(cursor, "lastUpdateDateTime")


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


# ── Self-registration ────────────────────────────────────────────────

def _register() -> None:
    from datetime import timedelta as _td
    from ..auth.probes import defender_probe as _defender_probe
    from ..auth.refreshers import defender_refresher
    from ..queues import Q_PRIORITY, T_COLLECT_PRIORITY
    from ..registry import (
        AuthField,
        CollectorRegistration,
        PlatformRegistration,
        register,
        register_platform,
    )

    # Catálogo da UI (dono do platform "microsoft_defender" — incidents + alerts).
    register_platform(
        PlatformRegistration(
            platform="microsoft_defender",
            display_name="Microsoft Defender",
            category="EDR / XDR",
            description="Microsoft Defender via Graph Security API (incidentes e alertas).",
            icon_id="microsoft",
            docs_url="https://learn.microsoft.com/en-us/defender-endpoint/",
            order=20,
            test_fn=_defender_probe,
            provider_factory=_defender_provider,
            capabilities=frozenset({"catalog", "auth:test", CAP_QUERY_KQL}),
            query_capabilities=(DEFENDER_QUERY_CAPABILITY,),
            auth_fields=(
                AuthField(key="tenant_id", label="Tenant ID", type="string", required=True,
                          help_text="Azure AD Tenant ID"),
                AuthField(key="client_id", label="Client ID", type="string", required=True,
                          help_text="Azure AD App Registration Client ID"),
                AuthField(key="client_secret", label="Client Secret", type="secret", required=True),
            ),
        )
    )

    register(
        CollectorRegistration(
            platform=DefenderIncidentsCollector.platform,
            stream=DefenderIncidentsCollector.stream,
            collector_cls=DefenderIncidentsCollector,
            refresh_fn=defender_refresher,
            schedule=_td(minutes=2),
            queue=Q_PRIORITY,
            task_name=T_COLLECT_PRIORITY,
        )
    )


_register()
