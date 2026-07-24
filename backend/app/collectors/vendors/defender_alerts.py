"""Microsoft Defender — Alerts v2 (substitui ``/security/alerts`` legado).

Endpoint: ``GET https://graph.microsoft.com/v1.0/security/alerts_v2``

**Por que v2**: a API legada ``/security/alerts`` foi **deprecated em
abril/2026** pela Microsoft. Código novo usa alerts_v2
exclusivamente. Para ingestão consolidada de incidents+alerts, é
geralmente melhor usar ``/security/incidents?$expand=alerts`` em vez
deste endpoint separado — mas alerts_v2 é útil para alertas que não
estão anexados a nenhum incident (raro mas possível).

**Delta time**: ``$filter=lastUpdateDateTime gt <ISO>``. Usar
``lastUpdateDateTime`` (e não ``createdDateTime``) é **crítico** —
ele captura re-classificações e mudanças de status feitas pelo SOC
ou automação do Defender XDR.

**Paginação**: padrão Graph ``@odata.nextLink``.

**Dedupe**: por ``id`` (string tipo
``"da637551227677560813_-961444813"``).

Permissão Graph: ``SecurityAlert.Read.All`` (application).
Docs: https://learn.microsoft.com/en-us/graph/api/security-list-alerts_v2
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict

from ..base import BaseCollector
from ..metrics import API_LATENCY

logger = logging.getLogger(__name__)


from ._rate_limit import VendorRateLimitedError


# Teto de páginas por CICLO Celery (50 × 100 = 5.000 eventos/ciclo). Sem este guard,
# um backlog grande é drenado num ÚNICO run — o while abaixo segue ``@odata.nextLink``
# página após página até exaurir o Graph — estourando o ``task_soft_time_limit`` (720s).
# No soft-timeout o pipeline reverte o cursor (para ``cursor_before``) e solta TODAS as
# claims → loop sem progresso (não coleta). Ao atingir o teto, salvamos o cursor RESUMÍVEL
# (o ``@odata.nextLink`` da PRÓXIMA página + o ``lastUpdateDateTime`` de INÍCIO do run) e
# damos return gracioso ANTES da escrita final que avança o watermark — assim o próximo
# ciclo retoma exatamente da página não lida. Espelha ``_MAX_PAGES_PER_CYCLE`` dos
# coletores de detections da Sophos e do Wazuh.
_MAX_PAGES_PER_CYCLE = 50


class DefenderAlertsRateLimitedError(VendorRateLimitedError):
    def __init__(self, retry_after: int) -> None:
        super().__init__(retry_after, vendor="graph-alerts")


class DefenderAlertsV2Collector(BaseCollector):
    platform = "microsoft_defender"
    stream = "alerts"
    event_type = "defender.alert"
    domain = "graph.microsoft.com"

    async def collect(self) -> AsyncIterator[Dict[str, Any]]:
        cursor = self.ctx.cursor or {}
        last_update: str = cursor.get("lastUpdateDateTime") or _default_lookback_iso()
        next_link: str | None = cursor.get("@odata.nextLink")
        latest_seen = last_update

        if next_link:
            url = next_link
            params: Dict[str, Any] = {}
        else:
            url = "https://graph.microsoft.com/v1.0/security/alerts_v2"
            params = {
                "$filter": f"lastUpdateDateTime gt {last_update}",
                "$orderby": "lastUpdateDateTime asc",
                "$top": 100,  # Graph caps alerts_v2 em 100 por página
            }

        page_count = 0
        while True:
            # Teto por ciclo: encerra o run e retoma no próximo ciclo (ver
            # _MAX_PAGES_PER_CYCLE). Contado no topo do while.
            page_count += 1

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
                        raise DefenderAlertsRateLimitedError(retry_after)
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
            params = {}  # nextLink já carrega os filtros

            # Cursor intermediário.
            self.ctx.cursor = {
                "lastUpdateDateTime": last_update,
                "@odata.nextLink": next_link,
            }

            # Teto por ciclo atingido: salva o cursor RESUMÍVEL (o nextLink da
            # PRÓXIMA página + ``last_update`` = INÍCIO do run, NÃO ``latest_seen``)
            # e retorna ANTES da escrita final que avança o watermark e zera o
            # nextLink. Usar ``last_update`` é deliberado: se o skiptoken expirar,
            # o próximo ciclo cai no fallback ``$filter=lastUpdateDateTime gt
            # last_update`` e re-varre o run inteiro sem perder páginas (a dedupe
            # por ``id::lastUpdateDateTime`` absorve a sobreposição).
            if self.ctx.bounded_per_cycle and page_count >= _MAX_PAGES_PER_CYCLE:
                self.ctx.cursor = {
                    "lastUpdateDateTime": last_update,
                    "@odata.nextLink": next_link,
                }
                # Sobrou backlog: o watermark fica em ``last_update`` de propósito,
                # e sem este sinal esse "parado" é lido como tenant sem alertas.
                self.mark_cycle_capped()
                logger.info(
                    "defender alerts: teto de %d páginas/ciclo atingido — cursor "
                    "resumível em @odata.nextLink p/ próximo ciclo (integration=%s)",
                    _MAX_PAGES_PER_CYCLE, self.ctx.integration_id,
                )
                return

        self.ctx.cursor = {
            "lastUpdateDateTime": latest_seen,
            "@odata.nextLink": None,
        }

    def extract_message_id(self, event: Dict[str, Any]) -> str:
        """Dedupe por ``id::lastUpdateDateTime`` — rastreia cada update.

        O mesmo alert pode ser atualizado (classificação, status,
        determinação) após a criação; queremos propagar cada
        atualização ao Wazuh como um evento distinto.
        """
        alert_id = event.get("id") or ""
        updated = event.get("lastUpdateDateTime") or ""
        if alert_id and updated:
            return f"{alert_id}::{updated}"
        return str(alert_id or event.get("providerAlertId") or "")

    @classmethod
    def watermark_at(cls, cursor: Optional[Dict[str, Any]]) -> Optional[datetime]:
        """``lastUpdateDateTime`` — mesmo contrato do coletor de incidentes."""
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
    from ..auth.refreshers import defender_refresher
    from ..queues import Q_PRIORITY, T_COLLECT_PRIORITY
    from ..registry import CollectorRegistration, register

    register(
        CollectorRegistration(
            platform=DefenderAlertsV2Collector.platform,
            stream=DefenderAlertsV2Collector.stream,
            collector_cls=DefenderAlertsV2Collector,
            refresh_fn=defender_refresher,
            schedule=_td(minutes=2),
            queue=Q_PRIORITY,
            task_name=T_COLLECT_PRIORITY,
        )
    )


_register()
