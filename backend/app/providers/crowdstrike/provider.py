"""CrowdStrike Falcon — provider de query FQL ao vivo.

Zero-core: novo provider rico (alerts/query) resolvido via ``provider_factory`` na
registration do vendor — não toca pipeline/registry-core. ``run_query`` é o ponto
canônico: recebe um filtro FQL (passthrough) e busca alertas via
``POST {base}/alerts/combined/alerts/v1`` (cursor ``after``), combinando o filtro
do analista com a janela ``created_timestamp`` do job.

Auth: OAuth2 client_credentials SÍNCRONO (``POST {base}/oauth2/token``) — o provider
roda no worker/threadpool, não em corrotina. HTTP via seam mockável ``_client()``
(testes patcham; nunca tocam a rede real).

NB: o contrato FQL do Falcon não é 100% público — validar contra um tenant real
antes do GA (mesma ressalva do collector de detecções).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..base import BaseProvider, HealthResult, QueryResult
from ..errors import ProviderConfigurationError, ProviderError, ProviderQueryError
from ...services import integration_secrets

logger = logging.getLogger(__name__)

_DEFAULT_BASE = "https://api.crowdstrike.com"
_PAGE_MAX = 1000


class CrowdStrikeProvider(BaseProvider):
    platform = "crowdstrike"

    def capabilities(self) -> List[str]:
        return ["health:check"] + self._query_capability_keys()

    # ── seams ────────────────────────────────────────────────────────────
    def _base_url(self) -> str:
        return (self.integration.base_url or _DEFAULT_BASE).rstrip("/")

    def _client(self):
        """Seam HTTP mockável (httpx.Client sync). Testes patcham ESTE método."""
        import httpx

        return httpx.Client(timeout=30.0)

    def _credentials(self) -> tuple[str, str]:
        client_id = (self.integration.client_id or "").strip()
        secret = integration_secrets.read_secret(self.integration, "client_secret") or ""
        if not client_id or not secret:
            raise ProviderConfigurationError(
                "CrowdStrike integration sem client_id/client_secret",
                code="CROWDSTRIKE_NO_CREDS",
            )
        return client_id, secret

    def _get_token(self, client) -> str:
        client_id, secret = self._credentials()
        resp = client.post(
            f"{self._base_url()}/oauth2/token",
            data={"client_id": client_id, "client_secret": secret},
        )
        if resp.status_code not in (200, 201):
            raise ProviderConfigurationError(
                f"CrowdStrike OAuth falhou (HTTP {resp.status_code})",
                code="CROWDSTRIKE_AUTH_FAILED",
            )
        return resp.json().get("access_token", "")

    # ── contrato ─────────────────────────────────────────────────────────
    def test_connection(self) -> HealthResult:
        try:
            with self._client() as c:
                token = self._get_token(c)
            return HealthResult(status="healthy" if token else "error",
                                details={"auth": "ok" if token else "no token"})
        except Exception as exc:  # pragma: no cover - convertido em status
            return HealthResult(status="error", details={"error": str(exc)})

    def health_check(self) -> HealthResult:
        return self.test_connection()

    def run_query(self, statement: str, from_ts: str, to_ts: str, **kwargs) -> QueryResult:
        limit = int(kwargs.get("limit", _PAGE_MAX))
        flt = (statement or "").strip()
        # FQL: AND é '+'. Combina o filtro do analista com a janela do job.
        window = f"created_timestamp:>='{from_ts}'+created_timestamp:<='{to_ts}'"
        full = f"({flt})+{window}" if flt else window

        items: List[Dict[str, Any]] = []
        after: Optional[str] = None
        try:
            with self._client() as c:
                headers = {"Authorization": f"Bearer {self._get_token(c)}"}
                while len(items) < limit:
                    body: Dict[str, Any] = {
                        "filter": full,
                        "sort": "created_timestamp|asc",
                        "limit": min(_PAGE_MAX, limit - len(items)),
                    }
                    if after:
                        body["after"] = after
                    resp = c.post(
                        f"{self._base_url()}/alerts/combined/alerts/v1",
                        json=body, headers=headers,
                    )
                    if resp.status_code != 200:
                        raise ProviderQueryError(
                            f"CrowdStrike FQL falhou (HTTP {resp.status_code})",
                            code="CROWDSTRIKE_QUERY_FAILED",
                        )
                    payload = resp.json()
                    resources = payload.get("resources") or []
                    items.extend(resources)
                    after = ((payload.get("meta") or {}).get("pagination") or {}).get("after")
                    if not after or not resources:
                        break
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ProviderQueryError(
                f"CrowdStrike query erro: {exc}", code="CROWDSTRIKE_QUERY_ERROR"
            ) from exc

        items = items[:limit]
        return QueryResult(items=items, total=len(items))
