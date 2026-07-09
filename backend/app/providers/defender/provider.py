"""Microsoft Defender — provider de query KQL (advanced hunting).

Zero-core: provider rico resolvido via ``provider_factory`` na registration. O
``run_query`` recebe um KQL (passthrough) e executa o advanced
hunting do Graph: ``POST https://graph.microsoft.com/v1.0/security/runHuntingQuery``
com ``{"query": "<KQL>"}`` → ``{"results": [...], "schema": [...]}``.

Auth: OAuth2 client_credentials SÍNCRONO no Azure AD
(``POST {login}/{tenant}/oauth2/v2.0/token``, scope ``…/.default``). HTTP via seam
mockável ``_client()``. O teto de janela (30d) é ENFORCED no ``QueryService`` a
partir da ``QueryCapability.max_window`` (não aqui — o KQL controla o range).
"""

from __future__ import annotations

import logging
from typing import List

from ..base import BaseProvider, HealthResult, QueryResult
from ..errors import ProviderConfigurationError, ProviderError, ProviderQueryError
from ...services import integration_secrets

logger = logging.getLogger(__name__)

_GRAPH = "https://graph.microsoft.com/v1.0"
_LOGIN = "https://login.microsoftonline.com"
_SCOPE = "https://graph.microsoft.com/.default"


class DefenderProvider(BaseProvider):
    platform = "microsoft_defender"

    def capabilities(self) -> List[str]:
        return ["health:check"] + self._query_capability_keys()

    # ── seams ────────────────────────────────────────────────────────────
    def _client(self):
        """Seam HTTP mockável (httpx.Client sync). Testes patcham ESTE método."""
        import httpx

        return httpx.Client(timeout=60.0)

    def _credentials(self) -> tuple[str, str, str]:
        tenant = (self.integration.tenant_id or "").strip()
        client_id = (self.integration.client_id or "").strip()
        secret = integration_secrets.read_secret(self.integration, "client_secret") or ""
        if not (tenant and client_id and secret):
            raise ProviderConfigurationError(
                "Defender integration sem tenant_id/client_id/client_secret",
                code="DEFENDER_NO_CREDS",
            )
        return tenant, client_id, secret

    def _get_token(self, client) -> str:
        tenant, client_id, secret = self._credentials()
        resp = client.post(
            f"{_LOGIN}/{tenant}/oauth2/v2.0/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": secret,
                "scope": _SCOPE,
            },
        )
        if resp.status_code not in (200, 201):
            raise ProviderConfigurationError(
                f"Defender OAuth falhou (HTTP {resp.status_code})",
                code="DEFENDER_AUTH_FAILED",
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
        if not (statement or "").strip():
            raise ProviderQueryError("KQL vazio", code="DEFENDER_EMPTY_QUERY")
        try:
            with self._client() as c:
                token = self._get_token(c)
                resp = c.post(
                    f"{_GRAPH}/security/runHuntingQuery",
                    json={"query": statement},
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                )
                if resp.status_code != 200:
                    raise ProviderQueryError(
                        f"Defender runHuntingQuery falhou (HTTP {resp.status_code})",
                        code="DEFENDER_QUERY_FAILED",
                    )
                payload = resp.json()
                items = payload.get("results") or []
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ProviderQueryError(
                f"Defender query erro: {exc}", code="DEFENDER_QUERY_ERROR"
            ) from exc

        return QueryResult(items=list(items), total=len(items))
