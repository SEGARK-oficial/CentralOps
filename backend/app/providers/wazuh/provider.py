"""Wazuh provider with separated Manager and Indexer credentials."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from ...db.models import Integration
from ...services import integration_secrets
from ..base import (
    AlertDetailSummary,
    AlertSummary,
    BaseProvider,
    HealthResult,
    PaginatedAlertsResult,
    QueryResult,
)
from ..errors import (
    ProviderConfigurationError,
    ProviderConnectivityError,
    ProviderInvalidRequestError,
    ProviderQueryError,
)
from .alert_query_builder import (
    SEVERITY_KEYS,
    build_alert_aggregation_body,
    build_alert_search_body,
)
from .indexer_client import WazuhIndexerClient
from .manager_client import WazuhManagerClient
from .query_builder import build_agent_query

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
    def _as_list(value: Any) -> list[Any]:
        if isinstance(value, list):
            return value
        if value in (None, ""):
            return []
        return [value]

    @staticmethod
    def _as_string_list(value: Any) -> list[str]:
        result: list[str] = []
        for item in WazuhProvider._as_list(value):
            if item in (None, ""):
                continue
            result.append(str(item))
        return result

    @staticmethod
    def _first_non_empty(*values: Any) -> Optional[str]:
        for value in values:
            if value in (None, "", [], {}):
                continue
            return str(value)
        return None

    @staticmethod
    def _join_groups(*values: Any) -> Optional[str]:
        groups: list[str] = []
        for value in values:
            for item in WazuhProvider._as_string_list(value):
                if item not in groups:
                    groups.append(item)
        if not groups:
            return None
        return ", ".join(groups)

    @staticmethod
    def _severity_from_level(level: int) -> str:
        if level >= 15:
            return "critical"
        if level >= 12:
            return "high"
        if level >= 7:
            return "medium"
        if level >= 4:
            return "low"
        return "info"

    @staticmethod
    def _extract_bucket_summary(aggregations: Dict[str, Any], key: str) -> list[dict[str, Any]]:
        buckets = aggregations.get(key, {}).get("buckets", [])
        result: list[dict[str, Any]] = []
        for bucket in buckets:
            bucket_key = bucket.get("key")
            if bucket_key in (None, ""):
                continue
            label = None
            if key == "top_rules":
                descriptions = bucket.get("description", {}).get("buckets", [])
                if descriptions:
                    label = descriptions[0].get("key")
            result.append(
                {
                    "key": str(bucket_key),
                    "label": str(label) if label else None,
                    "count": int(bucket.get("doc_count", 0) or 0),
                    "integration_id": None,
                    "integration_name": None,
                    "organization_id": None,
                    "organization_name": None,
                }
            )
        return result

    def _serialize_alert_hit(self, hit: Dict[str, Any]) -> AlertSummary:
        source = hit.get("_source", {})
        rule = source.get("rule", {})
        level = int(rule.get("level", 0) or 0)
        severity = self._severity_from_level(level)

        mitre = rule.get("mitre", {})
        agent = source.get("agent", {})
        data = source.get("data", {})
        decoder = source.get("decoder", {})
        manager = source.get("manager", {})
        input_data = source.get("input", {})
        syscheck = source.get("syscheck", {})
        mitre_ids = mitre.get("id", []) if isinstance(mitre, dict) else []
        mitre_tactics = mitre.get("tactic", []) if isinstance(mitre, dict) else []
        mitre_techniques = mitre.get("technique", []) if isinstance(mitre, dict) else []
        highlights = hit.get("highlight", {})

        return AlertSummary(
            alert_id=hit.get("_id", ""),
            title=rule.get("description", ""),
            severity=severity,
            platform="wazuh",
            timestamp=source.get("timestamp"),
            hostname=agent.get("name"),
            rule_id=str(rule.get("id", "")),
            rule_level=level,
            rule_groups=self._as_string_list(rule.get("groups")),
            rule_firedtimes=rule.get("firedtimes"),
            mitre_ids=self._as_string_list(mitre_ids),
            mitre_tactics=self._as_string_list(mitre_tactics),
            mitre_techniques=self._as_string_list(mitre_techniques),
            decoder_name=self._first_non_empty(decoder.get("name"), source.get("decoder", {}).get("parent")),
            agent_id=self._first_non_empty(agent.get("id")),
            agent_name=self._first_non_empty(agent.get("name")),
            agent_ip=self._first_non_empty(agent.get("ip")),
            agent_group=self._join_groups(agent.get("groups"), agent.get("group"), agent.get("labels", {}).get("group")),
            agent_labels=agent.get("labels", {}) if isinstance(agent.get("labels"), dict) else {},
            manager_name=self._first_non_empty(manager.get("name")),
            location=self._first_non_empty(source.get("location")),
            full_log=self._first_non_empty(source.get("full_log")),
            src_ip=self._first_non_empty(data.get("srcip")),
            dst_ip=self._first_non_empty(data.get("dstip")),
            src_user=self._first_non_empty(data.get("srcuser"), data.get("user")),
            dst_user=self._first_non_empty(data.get("dstuser")),
            input_type=self._first_non_empty(input_data.get("type")),
            syscheck_path=self._first_non_empty(syscheck.get("path")),
            data_fields=data if isinstance(data, dict) else {},
            highlights={key: self._as_string_list(value) for key, value in highlights.items() if isinstance(value, list)},
            source_index=self._first_non_empty(hit.get("_index")),
            integration_id=self.integration.id,
            integration_name=self.integration.name,
            organization_id=self.integration.organization_id,
            organization_name=self.integration.organization.name if self.integration.organization else None,
            raw=source,
        )

    @staticmethod
    def _extract_total_hits(total: Any, *, fallback: int) -> int:
        if isinstance(total, dict):
            value = total.get("value")
            if value is not None:
                return int(value)
        if total is None:
            return fallback
        try:
            return int(total)
        except (TypeError, ValueError):
            return fallback

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
        # "alerts:*" são SUPORTE da plataforma (Wazuh é SIEM) —
        # capability ≠ configuração. Indexer ausente é erro de RUNTIME
        # (ProviderConfigurationError em list_alerts/search), não ausência de
        # capability. Assim o router gateia a preview de alertas puramente por
        # "alerts:list" (sem ``if platform == "wazuh"``), e wazuh-sem-indexer
        # ainda surfacia o erro de config no call-time.
        # a capability de query (``query:opensearch_dsl``) é DERIVADA
        # da ``query_capability()`` (catálogo), garantindo runtime↔catálogo alinhados
        # — substitui o legado ``investigations:run`` (que não tinha dono no gate).
        return [
            "health:check", "alerts:list", "alerts:detail", "alerts:search",
        ] + self._query_capability_keys()

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

    def list_alerts(self, **filters) -> PaginatedAlertsResult:
        size = filters.get("limit", 100)
        from_ = filters.get("offset", 0)
        query_body = build_alert_search_body(
            {
                "severity": filters.get("severity"),
                "level": filters.get("level"),
                "hostname": filters.get("hostname"),
                "agent_id": filters.get("agent_id"),
                "rule_id": filters.get("rule_id"),
                "rule_group": filters.get("rule_group"),
                "decoder": filters.get("decoder"),
                "src_ip": filters.get("src_ip"),
                "dst_ip": filters.get("dst_ip"),
                "username": filters.get("username"),
                "description": filters.get("description"),
                "description_mode": filters.get("description_mode"),
                "query": filters.get("query"),
                "time_from": filters.get("time_from"),
                "time_to": filters.get("time_to"),
            },
            size=size,
            offset=from_,
        )

        data = self._search_alert_index(body=query_body, filters=filters, operation="list_alerts")
        hits = data.get("hits", {}).get("hits", [])
        items = [self._serialize_alert_hit(hit) for hit in hits]
        total = self._extract_total_hits(data.get("hits", {}).get("total"), fallback=len(items))
        return PaginatedAlertsResult(
            items=items,
            total=total,
            limit=size,
            offset=from_,
            has_more=from_ + len(items) < total,
        )

    def get_alert(self, alert_id: str, **filters) -> Optional[AlertDetailSummary]:
        body = {
            "size": 1,
            "track_total_hits": False,
            "query": {
                "ids": {
                    "values": [alert_id],
                }
            },
        }

        data = self._search_alert_index(body=body, filters=filters, operation="get_alert")
        hits = data.get("hits", {}).get("hits", [])
        if not hits:
            return None
        summary = self._serialize_alert_hit(hits[0])
        return AlertDetailSummary(**summary.__dict__)

    def search_alerts(self, query: str, **filters) -> PaginatedAlertsResult:
        size = filters.get("limit", 100)
        body = {
            "size": size,
            "from": 0,
            "track_total_hits": True,
            "sort": [{"timestamp": {"order": "desc"}}],
            "query": {"query_string": {"query": query}},
            "highlight": {
                "pre_tags": ["<em>"],
                "post_tags": ["</em>"],
                "fields": {
                    "rule.description": {"number_of_fragments": 2, "fragment_size": 180},
                    "full_log": {"number_of_fragments": 1, "fragment_size": 240},
                },
            },
        }

        data = self._search_alert_index(body=body, filters=filters, operation="search_alerts")
        hits = data.get("hits", {}).get("hits", [])
        items = [self._serialize_alert_hit(hit) for hit in hits]
        total = self._extract_total_hits(data.get("hits", {}).get("total"), fallback=len(items))
        return PaginatedAlertsResult(
            items=items,
            total=total,
            limit=size,
            offset=0,
            has_more=len(items) < total,
        )

    def get_alert_statistics(self, **filters) -> Dict[str, Any]:
        body = build_alert_aggregation_body(
            {
                "severity": filters.get("severity"),
                "level": filters.get("level"),
                "hostname": filters.get("hostname"),
                "agent_id": filters.get("agent_id"),
                "rule_id": filters.get("rule_id"),
                "rule_group": filters.get("rule_group"),
                "decoder": filters.get("decoder"),
                "src_ip": filters.get("src_ip"),
                "dst_ip": filters.get("dst_ip"),
                "username": filters.get("username"),
                "description": filters.get("description"),
                "description_mode": filters.get("description_mode"),
                "query": filters.get("query"),
                "time_from": filters.get("time_from"),
                "time_to": filters.get("time_to"),
            }
        )

        data = self._search_alert_index(body=body, filters=filters, operation="get_alert_statistics")
        total = data.get("hits", {}).get("total", {})
        total_count = total.get("value", 0) if isinstance(total, dict) else int(total or 0)
        aggregations = data.get("aggregations", {})
        severity_buckets = aggregations.get("severity", {}).get("buckets", {})
        timeline_buckets = aggregations.get("timeline", {}).get("buckets", [])

        by_severity = {
            key: int(severity_buckets.get(key, {}).get("doc_count", 0))
            for key in SEVERITY_KEYS
        }
        latest_timestamp = aggregations.get("latest_timestamp", {}).get("value_as_string")
        top_hosts = self._extract_bucket_summary(aggregations, "top_hosts")
        top_rules = self._extract_bucket_summary(aggregations, "top_rules")
        top_mitre_ids = self._extract_bucket_summary(aggregations, "top_mitre_ids")
        top_agent_groups = self._extract_bucket_summary(aggregations, "top_agent_groups")

        for bucket_list in (top_hosts, top_rules, top_mitre_ids, top_agent_groups):
            for bucket in bucket_list:
                bucket["integration_id"] = self.integration.id
                bucket["integration_name"] = self.integration.name
                bucket["organization_id"] = self.integration.organization_id
                bucket["organization_name"] = self.integration.organization.name if self.integration.organization else None

        trend: list[dict[str, Any]] = []
        for bucket in timeline_buckets:
            bucket_severity = bucket.get("severity", {}).get("buckets", {})
            trend.append(
                {
                    "timestamp": bucket.get("key_as_string"),
                    "total": int(bucket.get("doc_count", 0)),
                    **{
                        key: int(bucket_severity.get(key, {}).get("doc_count", 0))
                        for key in SEVERITY_KEYS
                    },
                }
            )

        return {
            "total": total_count,
            "by_severity": by_severity,
            "trend": trend,
            "top_hosts": top_hosts,
            "top_rules": top_rules,
            "top_mitre_ids": top_mitre_ids,
            "top_agent_groups": top_agent_groups,
            "latest_timestamp": latest_timestamp,
        }

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
