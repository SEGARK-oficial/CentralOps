"""NinjaOne — coleta de activities com paginação por ``after``/id crescente.

Endpoint: ``GET https://app.ninjarmm.com/v2/activities``

Paginação: o vendor retorna uma lista ordenada por id crescente; para a
próxima página passamos ``?after=<maior_id_visto>``. Delta time via
``?activityTimeAfter=<unix_seconds>``.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict

from ..base import BaseCollector
from ..metrics import API_LATENCY

logger = logging.getLogger(__name__)

_PAGE_SIZE = 200

# Teto de páginas por CICLO Celery (13 × 200 = 2.600 eventos/ciclo). Sem este guard,
# um backlog grande é drenado num ÚNICO run — o while abaixo pagina após página até
# exaurir o vendor — estourando o ``task_soft_time_limit`` (720s). No soft-timeout o
# pipeline reverte o cursor e solta TODAS as claims → loop sem progresso (não coleta).
# Ao atingir o teto salvamos o cursor keyset RESUMÍVEL (``after`` exclusivo → o próximo
# ciclo retoma de ``?after=after_id`` sem pular nem duplicar) e devolvemos o slot do
# worker; NÃO avançamos o piso temporal. Espelha ``_MAX_PAGES_PER_CYCLE`` dos coletores
# de detections da Sophos/Wazuh.
_MAX_PAGES_PER_CYCLE = 13


from ._rate_limit import VendorRateLimitedError


class NinjaOneRateLimitedError(VendorRateLimitedError):
    def __init__(self, retry_after: int) -> None:
        super().__init__(retry_after, vendor="ninjaone")


class NinjaOneActivitiesCollector(BaseCollector):
    platform = "ninjaone"
    stream = "activities"
    event_type = "ninjaone.activity"
    domain = "app.ninjarmm.com"

    async def collect(self) -> AsyncIterator[Dict[str, Any]]:
        cursor = self.ctx.cursor or {}
        after_id: int | None = cursor.get("after_id")
        activity_time_after: int = (
            int(cursor.get("activity_time_after"))
            if cursor.get("activity_time_after") is not None
            else _default_lookback_unix()
        )

        latest_id = after_id or 0
        page_count = 0

        while True:
            # Teto por ciclo: encerra o run e retoma no próximo ciclo (ver
            # _MAX_PAGES_PER_CYCLE). O cursor keyset já reflete todas as páginas
            # anteriores (``after_id``/``latest_id`` = maior id emitido), então salvá-lo
            # aqui NÃO perde nem pula eventos — ``after`` é exclusivo. Salvamos o cursor
            # RESUMÍVEL explicitamente e damos ``return`` ANTES da escrita de cursor final
            # (linha ~114): não dependemos das gravações intermediárias sobreviverem ao
            # soft-timeout, e NÃO avançamos o piso temporal (activity_time_after fixo).
            page_count += 1
            if self.ctx.bounded_per_cycle and page_count > _MAX_PAGES_PER_CYCLE:
                self.ctx.cursor = {
                    "after_id": latest_id,
                    "activity_time_after": activity_time_after,
                }
                # Sobrou backlog. Aqui o sinal é a ÚNICA evidência disponível: o
                # cursor é keyset (``after_id``) e ``activity_time_after`` é um
                # piso fixo, então este stream não reporta ``watermark_at`` — sem
                # o teto sinalizado, nada distingue backlog de silêncio.
                self.mark_cycle_capped()
                logger.info(
                    "ninjaone activities: teto de %d páginas/ciclo atingido — cursor em "
                    "after_id=%s p/ próximo ciclo (integration=%s)",
                    _MAX_PAGES_PER_CYCLE, latest_id, self.ctx.integration_id,
                )
                return

            await self.ctx.rate_limiter.acquire(
                self.ctx.integration_id, self.platform
            )

            params: Dict[str, Any] = {
                "pageSize": _PAGE_SIZE,
                "activityTimeAfter": activity_time_after,
            }
            if after_id:
                params["after"] = after_id

            started = time.monotonic()
            async with self.ctx.domain_limiter.slot(self.domain):
                async with self.ctx.session.get(
                    f"https://{self.domain}/v2/activities",
                    headers=self.ctx.headers,
                    params=params,
                ) as resp:
                    if resp.status == 429:
                        retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                        await self.ctx.rate_limiter.backoff(
                            self.platform, retry_after
                        )
                        raise NinjaOneRateLimitedError(retry_after)
                    resp.raise_for_status()
                    payload = await resp.json()

            API_LATENCY.labels(vendor=self.platform, stream=self.stream).observe(
                time.monotonic() - started
            )

            items = payload if isinstance(payload, list) else payload.get("activities", [])
            if not items:
                break

            for ev in items:
                ev_id = ev.get("id")
                if isinstance(ev_id, int) and ev_id > latest_id:
                    latest_id = ev_id
                yield ev

            if len(items) < _PAGE_SIZE:
                # Página incompleta → chegamos ao fim.
                break

            after_id = latest_id
            # Cursor intermediário.
            self.ctx.cursor = {
                "after_id": after_id,
                "activity_time_after": activity_time_after,
            }

        # Cursor final: próximo ciclo começa depois de ``latest_id`` e, por
        # segurança, mantém o ``activity_time_after`` apertado ao ts do
        # último id para evitar replay de milhões de eventos se o backend
        # resetar ids.
        self.ctx.cursor = {
            "after_id": latest_id or after_id or 0,
            "activity_time_after": activity_time_after,
        }

    def extract_message_id(self, event: Dict[str, Any]) -> str:
        return str(event.get("id") or event.get("activityId") or "")


def _default_lookback_unix() -> int:
    dt = datetime.now(timezone.utc) - timedelta(hours=1)
    return int(dt.timestamp())


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
    from ..auth.probes import ninjaone_probe as _ninjaone_probe
    from ..auth.refreshers import ninjaone_refresher
    from ..queues import Q_BULK, T_COLLECT_BULK
    from ..registry import (
        AuthField,
        CollectorRegistration,
        PlatformRegistration,
        register,
        register_platform,
    )

    # Catálogo da UI (self-describing — sem hardcode em providers.py/frontend).
    register_platform(
        PlatformRegistration(
            platform="ninjaone",
            display_name="NinjaOne",
            category="RMM",
            description="NinjaOne RMM — atividades e alertas de endpoints.",
            icon_id="ninjaone",
            docs_url="https://app.ninjarmm.com/apidocs-beta/",
            order=40,
            test_fn=_ninjaone_probe,
            auth_fields=(
                AuthField(key="client_id", label="Client ID", type="string", required=True,
                          help_text="Client ID da aplicação OAuth NinjaOne"),
                AuthField(key="client_secret", label="Client Secret", type="secret", required=True),
                AuthField(key="base_url", label="Base URL", type="url", required=True,
                          help_text="URL base do NinjaOne (ex: https://app.ninjarmm.com)"),
            ),
        )
    )

    register(
        CollectorRegistration(
            platform=NinjaOneActivitiesCollector.platform,
            stream=NinjaOneActivitiesCollector.stream,
            collector_cls=NinjaOneActivitiesCollector,
            refresh_fn=ninjaone_refresher,
            schedule=_td(minutes=5),
            queue=Q_BULK,
            task_name=T_COLLECT_BULK,
        )
    )


_register()
