"""Sophos Central — Cases (MDR/XDR incident management).

Endpoint: ``GET /cases/v1/cases`` em ``api-{region}.central.sophos.com``.

**Parâmetros aceitos** (confirmados na Postman collection oficial em
``docs/Sophos Central APIs.postman_collection.json``):

- Delta time: ``createdAfter``/``createdBefore`` (``updatedAfter`` **não
  existe** — Sophos devolve 400 se enviado).
- Paginação: ``page`` (1-indexed) + ``pageSize`` (offset-based). Cases
  **NÃO usa** ``pageFromKey`` como alerts.
- Filtros adicionais: ``managedBy``, ``type``, ``severity``, ``status``,
  ``assignee``, ``escalated``, ``verdict``.
- ``sort`` é aceito aqui (ex: ``sort=createdAt:asc``).

**Resposta**:

    {
      "items": [{"id": "...", "createdAt": "...", ...}],
      "pages": {"current": N, "size": S, "total": T, "items": K, "maxSize": M}
    }

Fim de paginação: quando ``len(items) < pageSize``.

**Dedupe**: ``{id}::{updatedAt}`` — permite propagar updates de status/
assignee sem perder como duplicatas.

Permissões: Service Principal Read-Only é suficiente.
Docs: https://developer.sophos.com/docs/cases-v1/1/routes/cases/get
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict

from ..base import BaseCollector
from ._sophos_common import resolve_sophos_domain
from .sophos import _normalize_ts  # normalização de timestamp compartilhada
from ..metrics import API_LATENCY

logger = logging.getLogger(__name__)

# Cases API: ``pageSize`` válido = 1..50 (comportamento distinto da
# ``Common Alerts API`` que aceita até 1000). Confirmado via validation
# error do vendor: ``"Page size must be between 1 and 50."``
_PAGE_SIZE = 50


from ._rate_limit import VendorRateLimitedError


class SophosCasesRateLimitedError(VendorRateLimitedError):
    def __init__(self, retry_after: int) -> None:
        super().__init__(retry_after, vendor="sophos-cases")


class SophosCasesCollector(BaseCollector):
    platform = "sophos"
    stream = "cases"
    event_type = "sophos.case"

    @property
    def domain(self) -> str:
        # Preferimos ``X-Api-Host``; fallback estrito de ``X-Region`` (só aceita
        # slug de datacenter — ver ``_sophos_common.resolve_sophos_domain``).
        return resolve_sophos_domain(
            self.ctx.headers, integration_id=getattr(self.ctx, "integration_id", None)
        )

    async def collect(self) -> AsyncIterator[Dict[str, Any]]:
        cursor = self.ctx.cursor or {}
        # Precedência da janela inferior:
        #   1. cursor["created_after"] — retomada de coleta agendada / mid-loop
        #   2. cursor["backfill_from_ts"] — janela explícita do backfill
        #      (gravada por collect_backfill_job em backfill_tasks.py:240-244)
        #   3. _default_lookback_iso() — cold-start sem cursor (1h atrás)
        # Sophos rejeita timestamps com microsegundos (ver sophos.py).
        created_after: str = _normalize_ts(
            cursor.get("created_after")
            or cursor.get("backfill_from_ts")
            or _default_lookback_iso()
        )
        # Janela superior — só populada em backfill. Limita a coleta ao
        # to_ts solicitado em vez de paginar até o presente.
        backfill_to_ts = cursor.get("backfill_to_ts")
        created_before = _normalize_ts(backfill_to_ts) if backfill_to_ts else None
        # Paginação offset-based (``page`` 1-indexed + ``pageSize``) — é o
        # que a Postman collection oficial documenta. Cases **NÃO** usa
        # ``pageFromKey`` como alerts.
        page: int = int(cursor.get("page") or 1)
        latest_updated = created_after
        total_collected = 0

        base_url = f"https://{self.domain}/cases/v1/cases"

        while True:
            await self.ctx.rate_limiter.acquire(
                self.ctx.integration_id, self.platform
            )
            params: Dict[str, Any] = {
                "createdAfter": created_after,
                "page": page,
                "pageSize": _PAGE_SIZE,
                "sort": "createdAt:asc",  # ordem estável para offset paging
            }
            if created_before:
                params["createdBefore"] = created_before

            started = time.monotonic()
            async with self.ctx.domain_limiter.slot(self.domain):
                async with self.ctx.session.get(
                    base_url, headers=self.ctx.headers, params=params
                ) as resp:
                    if resp.status == 429:
                        retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                        await self.ctx.rate_limiter.backoff(
                            self.platform, retry_after
                        )
                        raise SophosCasesRateLimitedError(retry_after)
                    if 400 <= resp.status < 500 and resp.status != 401:
                        # Mesmo pattern que sophos alerts: em 4xx (menos 401
                        # que é tratado como recovery de token), log o body
                        # para saber exatamente o que o vendor reclamou.
                        body_preview = (await resp.text())[:500]
                        logger.warning(
                            "sophos cases: HTTP %s params=%s body=%s",
                            resp.status, params, body_preview,
                        )
                    resp.raise_for_status()
                    payload = await resp.json()

            API_LATENCY.labels(vendor=self.platform, stream=self.stream).observe(
                time.monotonic() - started
            )

            items = payload.get("items") or []
            total_collected += len(items)
            for ev in items:
                raw_updated = ev.get("updatedAt") or ev.get("createdAt") or latest_updated
                updated = _normalize_ts(raw_updated) if isinstance(raw_updated, str) else latest_updated
                if updated > latest_updated:
                    latest_updated = updated
                yield ev

            # Fim de paginação: página incompleta → acabaram os cases
            # no recorte pedido.
            if len(items) < _PAGE_SIZE:
                break

            page += 1
            # Cursor intermediário — retomada segura se o worker morre
            # mid-loop (voltamos na mesma página).
            self.ctx.cursor = {"created_after": created_after, "page": page}

        if total_collected == 0:
            # Distingue 'vendor retornou 200 com items=[]' de 'API quebrada'.
            # Cenário comum: tenant sem MDR/XDR licenciado — o endpoint
            # /cases/v1/cases responde 200 vazio em vez de 403.
            logger.info(
                "sophos cases: 0 events collected window=[%s, %s) — "
                "tenant may lack MDR/XDR licensing or window is empty",
                created_after,
                created_before or "now",
            )

        # Cursor final — próxima janela começa do updatedAt mais recente
        # visto (captura updates dentro da janela anterior); paginação
        # volta ao início.
        self.ctx.cursor = {"created_after": latest_updated, "page": 1}

    def extract_message_id(self, event: Dict[str, Any]) -> str:
        """Dedupe por ``id::updatedAt`` — permite rastrear updates do
        mesmo case ao longo do ciclo de vida (não só a criação)."""
        case_id = event.get("id") or ""
        updated = event.get("updatedAt") or event.get("createdAt") or ""
        if case_id and updated:
            return f"{case_id}::{updated}"
        return str(case_id)


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
    from ..auth.refreshers import sophos_refresher
    from ..queues import Q_PRIORITY, T_COLLECT_PRIORITY
    from ..registry import CollectorRegistration, register

    register(
        CollectorRegistration(
            platform=SophosCasesCollector.platform,
            stream=SophosCasesCollector.stream,
            collector_cls=SophosCasesCollector,
            refresh_fn=sophos_refresher,
            # Cases mudam menos que alerts. 3min é balanço entre frescor
            # e carga no endpoint que tem rate limit compartilhado.
            schedule=_td(minutes=3),
            queue=Q_PRIORITY,
            task_name=T_COLLECT_PRIORITY,
        )
    )


_register()
