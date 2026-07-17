"""Wazuh provider with separated Manager and Indexer credentials."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from ...db.models import Integration
from ...services import integration_secrets
from ..base import (
    BaseProvider,
    HealthResult,
    QueryResult,
)
from ..errors import (
    ProviderConfigurationError,
    ProviderConnectivityError,
    ProviderInvalidRequestError,
    ProviderQueryError,
)
from .indexer_client import WazuhIndexerClient
from .manager_client import WazuhManagerClient

logger = logging.getLogger(__name__)
DEFAULT_ALERT_INDEX = "wazuh-alerts-*"


def resolve_alert_index(filters: dict[str, Any] | None) -> str:
    if not filters:
        return DEFAULT_ALERT_INDEX

    raw_index = filters.get("index")
    if raw_index is None:
        return DEFAULT_ALERT_INDEX

    normalized = str(raw_index).strip()
    if normalized.lower() in {"", "none", "null", "undefined"}:
        return DEFAULT_ALERT_INDEX
    return normalized


class WazuhProvider(BaseProvider):
    platform = "wazuh"

    def __init__(self, integration: Integration) -> None:
        super().__init__(integration)
        self._manager: Optional[WazuhManagerClient] = None
        self._indexer: Optional[WazuhIndexerClient] = None

    def _read_cred(self, logical_name: str) -> str:
        """Lê um segredo do store ``integration_credentials``.

        Retorna ``""`` (falsy) quando ausente — preserva o contrato ``str`` que os
        presence-checks (_has_*_config) e os clients esperam. Funciona com a row
        detached (relationship ``credentials`` carregada com ``lazy="selectin"``)."""
        return integration_secrets.read_secret(self.integration, logical_name) or ""

    def _manager_username(self) -> str:
        return self._read_cred("manager_api_username")

    def _manager_password(self) -> str:
        return self._read_cred("manager_api_password")

    def _indexer_username(self) -> str:
        return self._read_cred("indexer_username")

    def _indexer_password(self) -> str:
        return self._read_cred("indexer_password")

    def _has_manager_config(self) -> bool:
        return bool(self.integration.manager_url and self._manager_username() and self._manager_password())

    def _has_indexer_config(self) -> bool:
        return bool(self.integration.indexer_url and self._indexer_username() and self._indexer_password())

    def _get_manager(self) -> WazuhManagerClient:
        if not self._has_manager_config():
            raise NotImplementedError("Manager configuration is incomplete for this integration")
        if not self._manager:
            self._manager = WazuhManagerClient(
                base_url=self.integration.manager_url or "",
                username=self._manager_username(),
                password=self._manager_password(),
                verify_ssl=self.integration.verify_ssl if self.integration.verify_ssl is not None else True,
            )
        return self._manager

    def _get_indexer(self) -> WazuhIndexerClient:
        if not self._has_indexer_config():
            raise ProviderConfigurationError(
                "Wazuh indexer configuration is incomplete for this integration",
                code="INDEXER_NOT_CONFIGURED",
            )
        if not self._indexer:
            self._indexer = WazuhIndexerClient(
                base_url=self.integration.indexer_url or "",
                username=self._indexer_username(),
                password=self._indexer_password(),
                verify_ssl=self.integration.verify_ssl if self.integration.verify_ssl is not None else True,
            )
        return self._indexer

    def _component_error(self, component: str, exc: Exception) -> Dict[str, Any]:
        logger.warning("Wazuh %s request failed for integration %s: %s", component, self.integration.id, exc)

        if isinstance(exc, httpx.HTTPStatusError):
            status_code = exc.response.status_code
            if status_code == 401:
                status = "auth_error"
                message = "Authentication failed"
            elif status_code == 403:
                status = "forbidden"
                message = "Authorization failed"
            else:
                status = "upstream_error"
                message = f"HTTP {status_code}"
        elif isinstance(exc, httpx.TimeoutException):
            status = "unreachable"
            message = "Request timed out"
        elif isinstance(exc, httpx.RequestError):
            status = "unreachable"
            message = "Unable to connect"
        else:
            status = "error"
            message = "Unexpected error"

        return {"status": status, "message": message}

    def _component_not_configured(self, message: str | None = None) -> Dict[str, Any]:
        result = {"status": "not_configured"}
        if message:
            result["message"] = message
        return result

    def _overall_status(self, details: Dict[str, Any]) -> str:
        expected_statuses = []
        if self._has_indexer_config():
            expected_statuses.append(details.get("indexer", {}).get("status"))
        if self._has_manager_config():
            expected_statuses.append(details.get("manager", {}).get("status"))

        if not expected_statuses:
            return "error"
        healthy_components = sum(status == "healthy" for status in expected_statuses)
        if healthy_components == len(expected_statuses):
            return "healthy"
        if healthy_components > 0:
            return "degraded"
        return "error"

    @staticmethod
    def _extract_manager_total(data: Dict[str, Any], *, fallback: int) -> int:
        total = data.get("data", {}).get("total_affected_items")
        try:
            return int(total)
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _indexer_error_type(exc: httpx.HTTPStatusError) -> str | None:
        try:
            payload = exc.response.json()
        except ValueError:
            return None

        if not isinstance(payload, dict):
            return None

        error_data = payload.get("error")
        if isinstance(error_data, dict):
            error_type = error_data.get("type")
            if error_type:
                return str(error_type)
            reason = error_data.get("reason")
            if reason:
                return str(reason)
        if isinstance(error_data, str):
            return error_data
        return None

    def _log_alert_operation(self, *, operation: str, resolved_index: str) -> None:
        logger.info(
            "provider=wazuh integration=%s operation=%s resolved_index=%s",
            self.integration.id,
            operation,
            resolved_index,
        )

    def _raise_indexer_query_error(
        self,
        exc: Exception,
        *,
        operation: str,
        resolved_index: str,
    ) -> None:
        details = {
            "operation": operation,
            "resolved_index": resolved_index,
        }

        if isinstance(exc, httpx.TimeoutException):
            raise ProviderConnectivityError(
                "Timed out while connecting to the Wazuh indexer",
                code="INDEXER_UNAVAILABLE",
                details=details,
            ) from exc

        if isinstance(exc, httpx.RequestError):
            raise ProviderConnectivityError(
                "Unable to connect to the Wazuh indexer",
                code="INDEXER_UNAVAILABLE",
                details=details,
            ) from exc

        if isinstance(exc, httpx.HTTPStatusError):
            upstream_status = exc.response.status_code
            details["upstream_status"] = upstream_status
            error_type = self._indexer_error_type(exc)
            if error_type:
                details["upstream_error_type"] = error_type

            if upstream_status in {401, 403}:
                raise ProviderConfigurationError(
                    "Wazuh indexer authentication failed",
                    code="INDEXER_AUTH_FAILED",
                    details=details,
                ) from exc

            if error_type == "index_not_found_exception":
                raise ProviderInvalidRequestError(
                    "Alert index pattern is not configured or does not match any index",
                    code="ALERT_INDEX_INVALID",
                    details=details,
                ) from exc

            if upstream_status in {400, 404}:
                raise ProviderInvalidRequestError(
                    "Wazuh indexer rejected the alert query",
                    code="ALERT_QUERY_INVALID",
                    details=details,
                ) from exc

            raise ProviderQueryError(
                "Wazuh indexer query failed",
                code="INDEXER_QUERY_FAILED",
                details=details,
            ) from exc

        raise ProviderQueryError(
            "Unexpected Wazuh indexer query failure",
            code="INDEXER_QUERY_FAILED",
            details=details,
        ) from exc

    def _search_alert_index(
        self,
        *,
        body: Dict[str, Any],
        filters: dict[str, Any] | None,
        operation: str,
    ) -> Dict[str, Any]:
        indexer = self._get_indexer()
        resolved_index = resolve_alert_index(filters)
        self._log_alert_operation(operation=operation, resolved_index=resolved_index)
        try:
            return indexer.search(index=resolved_index, body=body)
        except Exception as exc:
            self._raise_indexer_query_error(exc, operation=operation, resolved_index=resolved_index)
            raise AssertionError("unreachable")

    def capabilities(self) -> List[str]:
        # a capability de query (``query:opensearch_dsl``) é DERIVADA
        # da ``query_capability()`` (catálogo), garantindo runtime↔catálogo alinhados
        # — substitui o legado ``investigations:run`` (que não tinha dono no gate).
        # NB: as capabilities ``alerts:*`` (superfície de visualização Wazuh-only)
        # foram REMOVIDAS — a busca federada (query:opensearch_dsl) cobre o caso.
        return ["health:check"] + self._query_capability_keys()

    def test_connection(self) -> HealthResult:
        details: Dict[str, Any] = {}

        if self._has_manager_config():
            try:
                manager_info = self._get_manager().get_manager_info()
                details["manager"] = {
                    "status": "healthy",
                    "version": manager_info.get("data", {}).get("api_version", "unknown"),
                }
            except Exception as exc:  # pragma: no cover - converted into structured status
                details["manager"] = self._component_error("manager", exc)
        else:
            details["manager"] = self._component_not_configured("Manager URL and credentials are required")

        if self.integration.indexer_url:
            if self._has_indexer_config():
                try:
                    indexer_info = self._get_indexer().get_cluster_health()
                    details["indexer"] = {
                        "status": "healthy",
                        "cluster_status": indexer_info.get("status", "unknown"),
                    }
                except Exception as exc:  # pragma: no cover - converted into structured status
                    details["indexer"] = self._component_error("indexer", exc)
            else:
                details["indexer"] = self._component_not_configured("Indexer credentials are required")
        else:
            details["indexer"] = self._component_not_configured()

        return HealthResult(status=self._overall_status(details), details=details)

    def health_check(self) -> HealthResult:
        details: Dict[str, Any] = {}

        if self._has_manager_config():
            try:
                manager = self._get_manager()
                status = manager.get_manager_status()
                details["manager"] = {
                    "status": "healthy",
                    "daemons": status.get("data", {}).get("affected_items", []),
                }

                try:
                    summary = manager.get_agents_summary()
                    details["agents"] = summary.get("data", {})
                except Exception as exc:  # pragma: no cover - best-effort extra details
                    details["agents"] = self._component_error("manager-agents", exc)

                try:
                    cluster = manager.get_cluster_status()
                    details["cluster"] = cluster.get("data", {})
                except Exception as exc:  # pragma: no cover - best-effort extra details
                    details["cluster"] = self._component_error("manager-cluster", exc)
            except Exception as exc:  # pragma: no cover - converted into structured status
                details["manager"] = self._component_error("manager", exc)
        else:
            details["manager"] = self._component_not_configured("Manager URL and credentials are required")

        if self.integration.indexer_url:
            if self._has_indexer_config():
                try:
                    health = self._get_indexer().get_cluster_health()
                    details["indexer"] = {
                        "status": "healthy",
                        "cluster_status": health.get("status", "unknown"),
                        "node_count": health.get("number_of_nodes", 0),
                    }
                except Exception as exc:  # pragma: no cover - converted into structured status
                    details["indexer"] = self._component_error("indexer", exc)
            else:
                details["indexer"] = self._component_not_configured("Indexer credentials are required")
        else:
            details["indexer"] = self._component_not_configured()

        return HealthResult(status=self._overall_status(details), details=details)

    def get_health_metrics(self) -> List[Any]:  # List[HealthMetric]
        """Return v2 HealthMetric list derived from the same calls as health_check().

        Calls the Wazuh Manager/Indexer APIs and maps the response to the
        data-driven HealthMetric shape. Falls back to severity="unknown" when
        a component is not configured or the call fails — never raises.
        """
        from ...schemas.health import HealthMetric  # local import avoids circular

        metrics: List[HealthMetric] = []

        # ── Manager status ────────────────────────────────────────────────
        if self._has_manager_config():
            try:
                manager = self._get_manager()
                manager.get_manager_status()
                metrics.append(HealthMetric(
                    id="manager_status",
                    label="Manager",
                    value="healthy",
                    severity="ok",
                    icon_id="server",
                    group="manager",
                    hint="API do Wazuh Manager respondeu com sucesso",
                ))
            except Exception:
                metrics.append(HealthMetric(
                    id="manager_status",
                    label="Manager",
                    value="error",
                    severity="critical",
                    icon_id="x",
                    group="manager",
                    hint="Não foi possível contatar o Wazuh Manager",
                ))
        else:
            metrics.append(HealthMetric(
                id="manager_status",
                label="Manager",
                value="not_configured",
                severity="unknown",
                icon_id="info",
                group="manager",
                hint="Manager URL ou credenciais não configurados",
            ))

        # ── Indexer status ────────────────────────────────────────────────
        if self.integration.indexer_url:
            if self._has_indexer_config():
                try:
                    health = self._get_indexer().get_cluster_health()
                    cluster_status = health.get("status", "unknown")
                    sev = "ok" if cluster_status == "green" else ("warn" if cluster_status == "yellow" else "critical")
                    metrics.append(HealthMetric(
                        id="indexer_status",
                        label="Indexer",
                        value=cluster_status,
                        severity=sev,
                        icon_id="database",
                        group="indexer",
                        hint=f"Cluster status: {cluster_status}",
                    ))
                except Exception:
                    metrics.append(HealthMetric(
                        id="indexer_status",
                        label="Indexer",
                        value="error",
                        severity="critical",
                        icon_id="x",
                        group="indexer",
                        hint="Não foi possível contatar o Wazuh Indexer",
                    ))
            else:
                metrics.append(HealthMetric(
                    id="indexer_status",
                    label="Indexer",
                    value="not_configured",
                    severity="unknown",
                    icon_id="info",
                    group="indexer",
                    hint="Credenciais do Indexer não configuradas",
                ))
        else:
            metrics.append(HealthMetric(
                id="indexer_status",
                label="Indexer",
                value="not_configured",
                severity="unknown",
                icon_id="info",
                group="indexer",
                hint="Indexer URL não configurado — alertas indisponíveis",
            ))

        # ── Cluster status ────────────────────────────────────────────────
        if self._has_manager_config():
            try:
                manager = self._get_manager()
                cluster = manager.get_cluster_status()
                enabled = cluster.get("data", {}).get("enabled", "no")
                cluster_value = "enabled" if enabled == "yes" else "disabled"
                metrics.append(HealthMetric(
                    id="cluster_status",
                    label="Cluster",
                    value=cluster_value,
                    severity="ok" if enabled == "yes" else "unknown",
                    icon_id="activity",
                    group="cluster",
                    hint="Modo cluster do Wazuh Manager",
                ))
            except Exception:
                metrics.append(HealthMetric(
                    id="cluster_status",
                    label="Cluster",
                    value="unknown",
                    severity="unknown",
                    icon_id="info",
                    group="cluster",
                    hint="Não foi possível obter status do cluster",
                ))

        # ── Agent counts ──────────────────────────────────────────────────
        if self._has_manager_config():
            try:
                manager = self._get_manager()
                summary = manager.get_agents_summary()
                agent_data = summary.get("data", {})
                connection = agent_data.get("connection", {})
                active = int(connection.get("active", 0) or 0)
                disconnected = int(connection.get("disconnected", 0) or 0)

                metrics.append(HealthMetric(
                    id="agents_active",
                    label="Agentes ativos",
                    value=active,
                    unit="agentes",
                    severity="ok" if active > 0 else "warn",
                    icon_id="server",
                    group="agents",
                    hint="Agentes com conexão ativa ao Manager",
                ))
                metrics.append(HealthMetric(
                    id="agents_disconnected",
                    label="Agentes desconectados",
                    value=disconnected,
                    unit="agentes",
                    severity="warn" if disconnected > 0 else "ok",
                    icon_id="x",
                    group="agents",
                    hint="Agentes que perderam conexão com o Manager",
                ))
            except Exception:
                metrics.append(HealthMetric(
                    id="agents_active",
                    label="Agentes ativos",
                    value="unknown",
                    severity="unknown",
                    icon_id="info",
                    group="agents",
                    hint="Não foi possível obter contagem de agentes",
                ))

        return metrics

    def run_query(self, statement: str, from_ts: str, to_ts: str, **kwargs) -> QueryResult:
        size = kwargs.get("limit", 500)
        body = {
            "size": size,
            "sort": [{"timestamp": {"order": "desc"}}],
            "query": {
                "bool": {
                    "must": [
                        {"query_string": {"query": statement}},
                        {"range": {"timestamp": {"gte": from_ts, "lte": to_ts}}},
                    ]
                }
            },
        }

        data = self._search_alert_index(body=body, filters=kwargs, operation="run_query")
        hits = data.get("hits", {}).get("hits", [])
        total = data.get("hits", {}).get("total", {})
        total_count = total.get("value", len(hits)) if isinstance(total, dict) else len(hits)
        items = [hit.get("_source", {}) for hit in hits]
        return QueryResult(items=items, total=total_count)

    def close(self) -> None:
        if self._manager:
            self._manager.close()
        if self._indexer:
            self._indexer.close()
