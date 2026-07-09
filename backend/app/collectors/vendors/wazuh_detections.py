"""Wazuh como FONTE de detecções.

Faz pull das detecções do **Wazuh Indexer** (índice ``wazuh-alerts-*``, API
OpenSearch ``_search``) e as entrega ao pipeline como eventos crus ``wazuh.detection``
(stream ``detections``). É o lado FONTE do Wazuh — distinto do lado DESTINO, que é
**syslog** (``wazuh-default`` entrega via syslog ao manager). Ver a exclusão de loop
em ``routing/engine.py``.

**Auth:** o Indexer usa **basic auth** (não OAuth/bearer). Diferente dos vendors
OAuth, este collector é AUTO-CONTIDO: carrega ``indexer_url`` + credenciais do
store ``integration_credentials`` no início do ``collect()`` e monta o header
``Authorization: Basic ...`` ele mesmo (ignora ``ctx.headers``). O ``refresh_fn``
registrado é um no-op só para satisfazer o contrato do ``oauth_cache``.

**Paginação incremental:** janela temporal + ``from``/``size`` (não ``search_after``
— evita depender de um campo tiebreaker sortável/com fielddata no Indexer). O
cursor é ``{"from_ts": <iso>}``; a query é ``timestamp >= from_ts`` (gte, inclusivo)
— a sobreposição de borda é absorvida pela dedupe por ``message_id`` do pipeline.
Ao bater o ``max_result_window`` (10k), a janela avança para o último
timestamp visto e reinicia o offset.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict, Optional
from urllib.parse import urlparse

from ..base import BaseCollector
from ..metrics import API_LATENCY
from ._rate_limit import VendorRateLimitedError

logger = logging.getLogger(__name__)

DEFAULT_ALERT_INDEX = "wazuh-alerts-*"
_PAGE_SIZE = 200
_MAX_RESULT_WINDOW = 10000  # OpenSearch ``index.max_result_window`` default.


class WazuhRateLimitedError(VendorRateLimitedError):
    def __init__(self, retry_after: int) -> None:
        super().__init__(retry_after, vendor="wazuh")


class WazuhDetectionsCollector(BaseCollector):
    """Pull de detecções do Wazuh Indexer (basic auth, OpenSearch DSL)."""

    platform = "wazuh"
    stream = "detections"
    event_type = "wazuh.detection"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._conn: Optional[Dict[str, Any]] = None
        self._host: Optional[str] = None

    @property
    def domain(self) -> str:
        # Host do Indexer (resolvido em _load_conn). Fallback estável p/ o semáforo
        # por domínio caso seja lido antes do load (não ocorre no fluxo atual).
        return self._host or "wazuh-indexer"

    # ── Conexão (basic auth + indexer_url do store) ────────────────────

    def _load_conn(self) -> Dict[str, Any]:
        """Carrega indexer_url + credenciais do Indexer do store (sync, em thread)."""
        from ...core.url_policy import normalize_service_url
        from ...db import database, models
        from ...services import integration_secrets

        with database.SessionLocal() as db:
            integ = db.get(models.Integration, self.ctx.integration_id)
            if integ is None:
                raise RuntimeError(
                    f"wazuh detections: integração {self.ctx.integration_id} não encontrada"
                )
            base = normalize_service_url(integ.indexer_url or "")
            if not base:
                raise RuntimeError(
                    f"wazuh detections: integração {self.ctx.integration_id} sem indexer_url"
                )
            username = integration_secrets.read_secret(integ, "indexer_username") or ""
            password = integration_secrets.read_secret(integ, "indexer_password") or ""
            if not username or not password:
                raise RuntimeError(
                    f"wazuh detections: integração {self.ctx.integration_id} sem "
                    "credenciais do Indexer (indexer_username/indexer_password)"
                )
            verify_ssl = integ.verify_ssl if integ.verify_ssl is not None else True
            return {
                "base_url": base.rstrip("/"),
                "username": username,
                "password": password,
                "verify_ssl": bool(verify_ssl),
            }

    def _auth_headers(self) -> Dict[str, str]:
        token = base64.b64encode(
            f"{self._conn['username']}:{self._conn['password']}".encode("utf-8")
        ).decode("ascii")
        return {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    @staticmethod
    def _search_body(from_ts: str, offset: int) -> Dict[str, Any]:
        return {
            "size": _PAGE_SIZE,
            "from": offset,
            "track_total_hits": False,
            "sort": [{"timestamp": {"order": "asc"}}],
            "query": {"range": {"timestamp": {"gte": from_ts}}},
        }

    # ── Coleta ─────────────────────────────────────────────────────────

    async def collect(self) -> AsyncIterator[Dict[str, Any]]:
        self._conn = await asyncio.to_thread(self._load_conn)
        self._host = urlparse(self._conn["base_url"]).hostname or "wazuh-indexer"
        headers = self._auth_headers()
        ssl_opt: Any = None if self._conn["verify_ssl"] else False
        search_url = f"{self._conn['base_url']}/{DEFAULT_ALERT_INDEX}/_search"

        cursor = self.ctx.cursor or {}
        from_ts: str = cursor.get("from_ts") or _default_lookback_iso()
        latest_seen = from_ts
        offset = 0

        while True:
            await self.ctx.rate_limiter.acquire(self.ctx.integration_id, self.platform)

            started = time.monotonic()
            async with self.ctx.domain_limiter.slot(self.domain):
                async with self.ctx.session.post(
                    search_url,
                    json=self._search_body(from_ts, offset),
                    headers=headers,
                    ssl=ssl_opt,
                ) as resp:
                    if resp.status == 429:
                        retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                        await self.ctx.rate_limiter.backoff(self.platform, retry_after)
                        raise WazuhRateLimitedError(retry_after)
                    resp.raise_for_status()
                    payload = await resp.json()

            API_LATENCY.labels(vendor=self.platform, stream=self.stream).observe(
                time.monotonic() - started
            )

            hits = (payload.get("hits") or {}).get("hits") or []
            if not hits:
                break

            for hit in hits:
                src = hit.get("_source") or {}
                # Preserva o id único do doc p/ dedupe quando o alerta não
                # traz ``id`` nativo do Wazuh.
                if not src.get("id") and hit.get("_id"):
                    src["id"] = hit["_id"]
                ts = src.get("timestamp")
                if isinstance(ts, str) and ts > latest_seen:
                    latest_seen = ts
                yield src

            offset += len(hits)
            if len(hits) < _PAGE_SIZE:
                break
            if offset >= _MAX_RESULT_WINDOW:
                # Estoura o max_result_window: avança a janela p/ o último timestamp
                # visto e reinicia o offset (a borda re-buscada é deduplicada).
                from_ts = latest_seen
                offset = 0
            # Cursor intermediário p/ retomada idempotente.
            self.ctx.cursor = {"from_ts": from_ts}

        self.ctx.cursor = {"from_ts": latest_seen}

    def extract_message_id(self, event: Dict[str, Any]) -> str:
        return str(event.get("id") or "")


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


# ── Self-registration ─────────────────────────────────────────────────
# A PlatformRegistration do wazuh já existe em ``vendors/wazuh.py`` (catálogo +
# provider). Aqui registramos APENAS o collector (vendor, stream) — o refresher
# é no-op (basic auth lido no collect()). Capability ``collect:detections`` é
# declarada na registration do wazuh.py.


def _register() -> None:
    from datetime import timedelta as _td

    from ..auth.refreshers import wazuh_indexer_refresher
    from ..queues import Q_BULK, T_COLLECT_BULK
    from ..registry import CollectorRegistration, register

    register(
        CollectorRegistration(
            platform=WazuhDetectionsCollector.platform,
            stream=WazuhDetectionsCollector.stream,
            collector_cls=WazuhDetectionsCollector,
            refresh_fn=wazuh_indexer_refresher,
            schedule=_td(minutes=2),
            queue=Q_BULK,
            task_name=T_COLLECT_BULK,
        )
    )


_register()
