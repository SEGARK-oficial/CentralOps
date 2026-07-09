from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.core.url_policy import normalize_service_url
from backend.app.db.database import Base, get_session
from backend.app.main import _redact_audit_payload, app
from backend.app.providers.errors import ProviderConnectivityError, ProviderQueryError
from backend.app.providers.base import AlertSummary, HealthResult, PaginatedAlertsResult
from backend.app.providers.wazuh.alert_query_builder import build_alert_search_body
from backend.app.providers.wazuh.provider import resolve_alert_index
from backend.app.providers.wazuh.query_builder import build_agent_query
from backend.app.routers.integrations import _collect_alert_filters


@pytest.fixture()
def client_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    testing_session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_get_session():
        db = testing_session_local()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_session] = override_get_session
    clients: list[TestClient] = []

    def factory() -> TestClient:
        client = TestClient(app)
        clients.append(client)
        return client

    yield factory

    for client in clients:
        client.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


def bootstrap_admin(client: TestClient, username: str = "admin", password: str = "AdminPassword123!") -> dict[str, Any]:
    response = client.post(
        "/api/auth/bootstrap",
        json={"username": username, "password": password, "display_name": "Administrator"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def login(client: TestClient, username: str, password: str) -> dict[str, Any]:
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response.json()


def create_organization(client: TestClient, name: str) -> dict[str, Any]:
    response = client.post("/api/organizations/", json={"name": name})
    assert response.status_code == 200, response.text
    return response.json()


def create_user(
    client: TestClient,
    *,
    username: str,
    password: str = "UserPassword123!",
    role: str = "user",
    organization_id: int | None = None,
) -> dict[str, Any]:
    response = client.post(
        "/api/auth/users",
        json={
            "username": username,
            "password": password,
            "display_name": username.title(),
            "role": role,
            "organization_id": organization_id,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def create_wazuh_integration(
    client: TestClient,
    *,
    organization_id: int,
    name: str = "Wazuh Primary",
    include_indexer: bool = True,
    include_manager: bool = True,
) -> dict[str, Any]:
    """Cria integração Wazuh. Indexer é sempre obrigatório; Manager é opcional.

    ``include_indexer`` foi mantido por compatibilidade de chamadas existentes —
    agora é sempre True (Indexer obrigatório); se passado False, é ignorado e
    Indexer ainda é incluído. ``include_manager`` controla o add-on opcional.
    """
    payload: dict[str, Any] = {
        "organization_id": organization_id,
        "name": name,
        "platform": "wazuh",
        "indexer_url": "https://indexer.example.com:9200",
        "indexer_username": "indexer-user",
        "indexer_password": "indexer-pass-123",
        "verify_ssl": True,
    }
    if include_manager:
        payload.update(
            {
                "manager_url": "https://manager.example.com:55000",
                "manager_api_username": "manager-user",
                "manager_api_password": "manager-pass-123",
            }
        )

    response = client.post("/api/integrations/", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def test_build_agent_query_supports_simple_mode_and_wql_válidation():
    assert build_agent_query("web-01 production") == (
        "(id~web-01,name~web-01,ip~web-01,os.name~web-01,version~web-01,group~web-01);"
        "(id~production,name~production,ip~production,os.name~production,version~production,group~production)"
    )
    assert build_agent_query("name~web-01;status=active", mode="wql") == "name~web-01;status=active"

    with pytest.raises(ValueError, match="must contain at least one valid operator"):
        build_agent_query("hostname only", mode="wql")

    with pytest.raises(ValueError, match="letters or numbers"):
        build_agent_query("###", mode="simple")


def test_build_alert_search_body_supports_smart_description_rule_wildcards_and_highlight():
    body = build_alert_search_body(
        {
            "severity": "high",
            "level": "12",
            "hostname": "web-01",
            "agent_id": "001",
            "rule_id": "57*",
            "description": "*bruteforce*",
            "description_mode": "smart",
            "query": "agent.name:web*",
            "time_from": "2026-01-01T00:00:00Z",
            "time_to": "2026-01-02T00:00:00Z",
        },
        size=50,
        offset=10,
    )

    assert body["size"] == 50
    assert body["from"] == 10
    assert body["track_total_hits"] is True

    bool_query = body["query"]["bool"]
    assert {"terms": {"rule.level": [12, 13, 14]}} in bool_query["filter"]
    assert {"term": {"rule.level": 12}} in bool_query["filter"]
    assert {"range": {"timestamp": {"gte": "2026-01-01T00:00:00Z", "lte": "2026-01-02T00:00:00Z"}}} in bool_query["filter"]

    must_clauses = bool_query["must"]
    assert {"match": {"agent.name": {"query": "web-01", "operator": "and"}}} in must_clauses
    assert {
        "bool": {
            "should": [
                {"term": {"agent.id.keyword": "001"}},
                {"term": {"agent.id": "001"}},
            ],
            "minimum_should_match": 1,
        }
    } in must_clauses
    rule_clause = next(clause for clause in must_clauses if "bool" in clause and clause["bool"]["should"][0].get("wildcard", {}).get("rule.id.keyword"))
    assert rule_clause["bool"]["should"][0]["wildcard"]["rule.id.keyword"]["value"] == "57*"
    description_clause = next(clause for clause in must_clauses if "bool" in clause and clause["bool"]["should"][0].get("wildcard", {}).get("rule.description.keyword"))
    assert description_clause["bool"]["should"][0]["wildcard"]["rule.description.keyword"]["value"] == "*bruteforce*"
    assert any("query_string" in clause and clause["query_string"]["query"] == "agent.name:web*" for clause in must_clauses)
    assert body["highlight"]["fields"]["rule.description"]["fragment_size"] == 180
    assert body["highlight"]["fields"]["full_log"]["number_of_fragments"] == 1


def test_build_alert_search_body_supports_exact_description_mode_without_wildcards():
    body = build_alert_search_body({"description": "Privilege escalation", "description_mode": "exact"})

    clause = body["query"]["bool"]["must"][0]
    should = clause["bool"]["should"]
    assert any("match_phrase" in item for item in should)
    assert any("term" in item and "rule.description.keyword" in item["term"] for item in should)
    assert not any("multi_match" in item for item in should)


def test_build_alert_search_body_supports_question_mark_wildcards_and_user_clause():
    body = build_alert_search_body(
        {
            "description": "failed log?in",
            "description_mode": "contains",
            "username": "svc-admin",
            "src_ip": "10.0.0.*",
        }
    )

    must = body["query"]["bool"]["must"]
    description_clause = next(clause for clause in must if "bool" in clause and clause["bool"]["should"][0].get("wildcard", {}).get("rule.description.keyword"))
    assert description_clause["bool"]["should"][0]["wildcard"]["rule.description.keyword"]["value"] == "*failed log?in*"

    user_clause = next(clause for clause in must if "bool" in clause and len(clause["bool"]["should"]) == 3)
    assert any("data.srcuser" in str(item) for item in user_clause["bool"]["should"])
    src_ip_clause = next(clause for clause in must if "bool" in clause and clause["bool"]["should"][0].get("wildcard", {}).get("data.srcip.keyword"))
    assert src_ip_clause["bool"]["should"][0]["wildcard"]["data.srcip.keyword"]["value"] == "10.0.0.*"


def test_build_alert_search_body_rejects_invalid_description_mode_and_level():
    with pytest.raises(ValueError, match="Unsupported description_mode"):
        build_alert_search_body({"description": "teste", "description_mode": "dsl"})

    with pytest.raises(ValueError, match="level must be a number"):
        build_alert_search_body({"level": "critico"})


def test_collect_alert_filters_omits_none_and_alert_index_defaults_cleanly():
    filters = _collect_alert_filters(
        index=None,
        severity="high",
        level=None,
        hostname=None,
        agent_id=None,
        rule_id=None,
        rule_group=None,
        decoder=None,
        src_ip=None,
        dst_ip=None,
        username=None,
        description=None,
        description_mode="smart",
        query=None,
        time_from=None,
        time_to=None,
    )

    assert "index" not in filters
    assert "level" not in filters
    assert filters["severity"] == "high"
    assert resolve_alert_index(filters) == "wazuh-alerts-*"
    assert resolve_alert_index({"index": None}) == "wazuh-alerts-*"
    assert resolve_alert_index({"index": "None"}) == "wazuh-alerts-*"
    assert resolve_alert_index({"index": "wazuh-archives-*"}) == "wazuh-archives-*"


def test_url_policy_normalizes_urls_and_rejects_embedded_credentials():
    assert normalize_service_url("manager.example.com:55000") == "https://manager.example.com:55000"
    assert normalize_service_url("https://MANAGER.EXAMPLE.COM/") == "https://manager.example.com"

    with pytest.raises(ValueError, match="Credentials must not be embedded"):
        normalize_service_url("https://user:pass@manager.example.com")


def test_audit_redacts_wazuh_credentials_and_legacy_api_fields():
    payload = {
        "manager_api_username": "manager-user",
        "manager_api_password": "manager-pass",
        "nested": {"indexer_password": "indexer-pass"},
        "items": [{"api_username": "legacy-user"}, {"api_password": "legacy-pass"}],
    }

    redacted = _redact_audit_payload(payload)

    assert redacted["manager_api_username"] == "[REDACTED]"
    assert redacted["manager_api_password"] == "[REDACTED]"
    assert redacted["nested"]["indexer_password"] == "[REDACTED]"
    assert redacted["items"][0]["api_username"] == "[REDACTED]"
    assert redacted["items"][1]["api_password"] == "[REDACTED]"


def test_wazuh_integration_update_supports_split_credentials_rotation_and_clear(client_factory):
    """Rotação de credenciais do Indexer e limpeza opcional do Manager."""
    admin_client = client_factory()
    bootstrap_admin(admin_client)
    organization = create_organization(admin_client, "Org Alpha")
    # Cria com Indexer (obrigatório) + Manager (opcional).
    integration = create_wazuh_integration(admin_client, organization_id=organization["id"], include_manager=True)

    assert integration["indexer_username"] == "indexer-user"
    assert integration["indexer_password_configured"] is True
    assert integration["manager_api_username"] == "manager-user"
    assert integration["manager_api_password_configured"] is True
    assert integration["manager_url"] == "https://manager.example.com:55000"

    # Rotaciona credenciais do Indexer e limpa o Manager (add-on opcional).
    response = admin_client.put(
        f"/api/integrations/{integration['id']}",
        json={
            "indexer_username": "indexer-user-rotated",
            "indexer_password": "indexer-pass-rotated",
            "manager_url": None,
            "manager_api_username": None,
            "manager_api_password": None,
        },
    )
    assert response.status_code == 200, response.text

    updated = response.json()
    assert updated["indexer_username"] == "indexer-user-rotated"
    assert updated["indexer_password_configured"] is True
    assert updated["manager_url"] is None
    assert updated["manager_api_username"] is None
    assert updated["manager_api_password_configured"] is False
    assert updated["auth_status"] == "unknown"


def test_inactive_integration_is_soft_deleted_and_blocked_from_operational_routes(client_factory):
    admin_client = client_factory()
    bootstrap_admin(admin_client)
    organization = create_organization(admin_client, "Org Soft Delete")
    integration = create_wazuh_integration(admin_client, organization_id=organization["id"])

    delete_response = admin_client.delete(f"/api/integrations/{integration['id']}")
    assert delete_response.status_code == 200, delete_response.text
    assert delete_response.json()["detail"] == "Integration deactivated"

    get_response = admin_client.get(f"/api/integrations/{integration['id']}")
    assert get_response.status_code == 200, get_response.text
    assert get_response.json()["is_active"] is False

    health_response = admin_client.get(f"/api/integrations/{integration['id']}/health")
    assert health_response.status_code == 409, health_response.text
    assert health_response.json()["error"]["code"] == "integration.inactive"


def test_non_admin_visibility_is_scoped_to_assigned_organization(client_factory):
    admin_client = client_factory()
    bootstrap_admin(admin_client)
    org_alpha = create_organization(admin_client, "Org Scoped Alpha")
    org_beta = create_organization(admin_client, "Org Scoped Beta")
    integration_alpha = create_wazuh_integration(admin_client, organization_id=org_alpha["id"], name="Scoped A")
    integration_beta = create_wazuh_integration(admin_client, organization_id=org_beta["id"], name="Scoped B")
    create_user(admin_client, username="scoped-user", organization_id=org_alpha["id"])

    user_client = client_factory()
    login(user_client, "scoped-user", "UserPassword123!")

    organizations_response = user_client.get("/api/organizations/")
    assert organizations_response.status_code == 200, organizations_response.text
    visible_orgs = organizations_response.json()
    assert [organization["id"] for organization in visible_orgs] == [org_alpha["id"]]

    integrations_response = user_client.get("/api/integrations/")
    assert integrations_response.status_code == 200, integrations_response.text
    visible_integrations = integrations_response.json()
    assert [integration["id"] for integration in visible_integrations] == [integration_alpha["id"]]

    forbidden_org_response = user_client.get(f"/api/organizations/{org_beta['id']}")
    assert forbidden_org_response.status_code == 403, forbidden_org_response.text

    forbidden_integration_response = user_client.get(f"/api/integrations/{integration_beta['id']}")
    assert forbidden_integration_response.status_code == 403, forbidden_integration_response.text

    dashboard_response = user_client.get(
        "/api/dashboard/summary",
        headers={"Accept": "application/vnd.centralops.v1+json"},
    )
    assert dashboard_response.status_code == 200, dashboard_response.text
    summary = dashboard_response.json()
    assert summary["organizations"]["total"] == 1
    assert summary["integrations"]["total"] == 1


def test_non_admin_without_organization_assignment_is_fail_closed(client_factory):
    admin_client = client_factory()
    bootstrap_admin(admin_client)
    organization = create_organization(admin_client, "Org Unassigned")
    integration = create_wazuh_integration(admin_client, organization_id=organization["id"])
    create_user(admin_client, username="orgless-user", organization_id=None)

    user_client = client_factory()
    login(user_client, "orgless-user", "UserPassword123!")

    organizations_response = user_client.get("/api/organizations/")
    assert organizations_response.status_code == 200, organizations_response.text
    assert organizations_response.json() == []

    integrations_response = user_client.get("/api/integrations/")
    assert integrations_response.status_code == 200, integrations_response.text
    assert integrations_response.json() == []

    forbidden_integration_response = user_client.get(f"/api/integrations/{integration['id']}")
    assert forbidden_integration_response.status_code == 403, forbidden_integration_response.text

    dashboard_response = user_client.get(
        "/api/dashboard/summary",
        headers={"Accept": "application/vnd.centralops.v1+json"},
    )
    assert dashboard_response.status_code == 200, dashboard_response.text
    summary = dashboard_response.json()
    assert summary["organizations"]["total"] == 0
    assert summary["integrations"]["total"] == 0


def test_test_connection_persists_auth_status_and_last_error(client_factory, monkeypatch):
    admin_client = client_factory()
    bootstrap_admin(admin_client)
    organization = create_organization(admin_client, "Org Health")
    integration = create_wazuh_integration(admin_client, organization_id=organization["id"], include_indexer=True)

    class StubProvider:
        def test_connection(self) -> HealthResult:
            return HealthResult(
                status="degraded",
                details={
                    "manager": {"status": "healthy", "version": "4.9.0"},
                    "indexer": {"status": "error", "message": "Authentication failed"},
                },
            )

        def close(self) -> None:
            return None

    monkeypatch.setattr("backend.app.routers.integrations.get_provider", lambda integration: StubProvider())

    response = admin_client.post(f"/api/integrations/{integration['id']}/test-connection")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "degraded"

    integration_response = admin_client.get(f"/api/integrations/{integration['id']}")
    assert integration_response.status_code == 200, integration_response.text
    data = integration_response.json()
    assert data["auth_status"] == "degraded"
    assert data["is_authenticated"] is True
    assert "indexer: Authentication failed" in data["last_error"]
    assert data["last_checked_at"] is not None


def test_alerts_endpoint_passes_description_and_query_filters_to_provider(client_factory, monkeypatch):
    admin_client = client_factory()
    bootstrap_admin(admin_client)
    organization = create_organization(admin_client, "Org Alerts")
    integration = create_wazuh_integration(admin_client, organization_id=organization["id"], include_indexer=True)
    captured: dict[str, Any] = {}

    class AlertProvider:
        def capabilities(self) -> list[str]:
            return ["alerts:list", "alerts:detail"]

        def list_alerts(self, **filters):
            captured.update(filters)
            return PaginatedAlertsResult(
                items=[
                    AlertSummary(
                        alert_id="alert-1",
                        title="Suspicious login",
                        severity="high",
                        platform="wazuh",
                        hostname="web-01",
                        rule_id="5710",
                        rule_level=12,
                        src_ip="10.0.0.5",
                        decoder_name="sshd",
                        integration_id=integration["id"],
                        integration_name=integration["name"],
                    )
                ],
                total=87,
                limit=100,
                offset=0,
                has_more=False,
            )

        def get_alert(self, alert_id: str, **filters):
            assert alert_id == "alert-1"
            return AlertSummary(
                alert_id=alert_id,
                title="Suspicious login",
                severity="high",
                platform="wazuh",
                hostname="web-01",
                rule_id="5710",
                rule_level=12,
                full_log="failed password for root",
                src_ip="10.0.0.5",
                decoder_name="sshd",
                integration_id=integration["id"],
                integration_name=integration["name"],
                organization_id=organization["id"],
                organization_name=organization["name"],
                raw={"rule": {"id": "5710"}},
            )

        def close(self) -> None:
            return None

    monkeypatch.setattr("backend.app.routers.integrations.get_provider", lambda integration: AlertProvider())

    response = admin_client.get(
        f"/api/integrations/{integration['id']}/alerts",
        params={
            "index": "wazuh-archives-*",
            "severity": "critical",
            "level": "12",
            "hostname": "web-01",
            "agent_id": "001",
            "rule_id": "5710",
            "rule_group": "authentication_failed",
            "decoder": "sshd",
            "src_ip": "10.0.0.5",
            "dst_ip": "10.0.0.10",
            "username": "root",
            "description": "*bruteforce*",
            "description_mode": "contains",
            "query": "agent.name:web*",
            "time_from": "2026-01-01T00:00:00Z",
            "time_to": "2026-01-02T00:00:00Z",
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["total"] == 87
    assert payload["limit"] == 100
    assert payload["offset"] == 0
    assert payload["has_more"] is False
    assert payload["items"][0]["rule_level"] == 12
    assert payload["items"][0]["src_ip"] == "10.0.0.5"
    assert payload["items"][0]["integration_id"] == integration["id"]
    assert captured["severity"] == "critical"
    assert captured["level"] == "12"
    assert captured["hostname"] == "web-01"
    assert captured["agent_id"] == "001"
    assert captured["rule_id"] == "5710"
    assert captured["rule_group"] == "authentication_failed"
    assert captured["decoder"] == "sshd"
    assert captured["src_ip"] == "10.0.0.5"
    assert captured["dst_ip"] == "10.0.0.10"
    assert captured["username"] == "root"
    assert captured["description"] == "*bruteforce*"
    assert captured["description_mode"] == "contains"
    assert captured["query"] == "agent.name:web*"
    assert captured["time_from"] == "2026-01-01T00:00:00Z"
    assert captured["time_to"] == "2026-01-02T00:00:00Z"
    assert captured["index"] == "wazuh-archives-*"

    detail_response = admin_client.get(f"/api/integrations/{integration['id']}/alerts/alert-1")
    assert detail_response.status_code == 200, detail_response.text
    detail = detail_response.json()
    assert detail["full_log"] == "failed password for root"
    assert detail["decoder_name"] == "sshd"
    assert detail["organization_name"] == organization["name"]


def test_alerts_endpoint_returns_structured_error_when_indexer_is_not_configured(client_factory):
    """Simula runtime onde o Indexer não está configurado (credenciais ausentes no store).

    Indexer é obrigatório no create — portanto criamos com Indexer e simulamos a
    ausência de configuração em runtime via mock de _has_indexer_config.
    """
    from unittest.mock import patch

    admin_client = client_factory()
    bootstrap_admin(admin_client)
    organization = create_organization(admin_client, "Org Missing Indexer")
    # Indexer obrigatório no create; runtime sem config é simulado abaixo.
    integration = create_wazuh_integration(admin_client, organization_id=organization["id"], include_manager=False)

    with patch(
        "backend.app.providers.wazuh.provider.WazuhProvider._has_indexer_config",
        return_value=False,
    ):
        response = admin_client.get(f"/api/integrations/{integration['id']}/alerts")

    assert response.status_code == 422, response.text
    payload = response.json()
    assert payload["error"]["code"] == "INDEXER_NOT_CONFIGURED"
    assert payload["error"]["integration_id"] == integration["id"]


def test_alerts_endpoint_returns_structured_connectivity_error(client_factory, monkeypatch):
    admin_client = client_factory()
    bootstrap_admin(admin_client)
    organization = create_organization(admin_client, "Org Alert Error")
    integration = create_wazuh_integration(admin_client, organization_id=organization["id"], include_indexer=True)

    class ErrorProvider:
        def __init__(self, integration_obj) -> None:
            self.integration = integration_obj
            self.platform = "wazuh"

        def capabilities(self) -> list[str]:
            return ["alerts:list"]

        def list_alerts(self, **filters):
            raise ProviderConnectivityError(
                "Unable to connect to the Wazuh indexer",
                code="INDEXER_UNAVAILABLE",
                details={"resolved_index": "wazuh-alerts-*"},
            )

        def close(self) -> None:
            return None

    monkeypatch.setattr("backend.app.routers.integrations.get_provider", lambda integration: ErrorProvider(integration))

    response = admin_client.get(f"/api/integrations/{integration['id']}/alerts")

    assert response.status_code == 503, response.text
    payload = response.json()
    assert payload["error"]["code"] == "INDEXER_UNAVAILABLE"
    assert payload["error"]["integration_id"] == integration["id"]
    assert payload["error"]["details"]["resolved_index"] == "wazuh-alerts-*"


def test_alerts_aggregate_endpoint_merges_and_paginates_provider_results(client_factory, monkeypatch):
    admin_client = client_factory()
    bootstrap_admin(admin_client)
    organization = create_organization(admin_client, "Org Aggregate Alerts")
    create_wazuh_integration(admin_client, organization_id=organization["id"], name="Alpha", include_indexer=True)
    create_wazuh_integration(admin_client, organization_id=organization["id"], name="Beta", include_indexer=True)

    class AggregateProvider:
        def __init__(self, integration_payload: dict[str, Any]) -> None:
            self.integration_payload = integration_payload

        def capabilities(self) -> list[str]:
            return ["alerts:list"]

        def list_alerts(self, **filters):
            assert filters["limit"] == 2
            integration_name = self.integration_payload["name"]
            if integration_name == "Alpha":
                return PaginatedAlertsResult(
                    items=[
                        AlertSummary(
                            alert_id="alpha-2",
                            title="Newest alpha",
                            severity="critical",
                            platform="wazuh",
                            timestamp="2026-01-02T12:00:00Z",
                            integration_id=self.integration_payload["id"],
                            integration_name=integration_name,
                        ),
                        AlertSummary(
                            alert_id="alpha-1",
                            title="Older alpha",
                            severity="high",
                            platform="wazuh",
                            timestamp="2026-01-01T12:00:00Z",
                            integration_id=self.integration_payload["id"],
                            integration_name=integration_name,
                        ),
                    ],
                    total=2,
                    limit=2,
                    offset=0,
                    has_more=False,
                )
            return PaginatedAlertsResult(
                items=[
                    AlertSummary(
                        alert_id="beta-1",
                        title="Newest beta",
                        severity="medium",
                        platform="wazuh",
                        timestamp="2026-01-02T10:00:00Z",
                        integration_id=self.integration_payload["id"],
                        integration_name=integration_name,
                    )
                ],
                total=1,
                limit=2,
                offset=0,
                has_more=False,
            )

        def close(self) -> None:
            return None

    def provider_factory(integration_obj):
        payload = {"id": integration_obj.id, "name": integration_obj.name}
        return AggregateProvider(payload)

    monkeypatch.setattr("backend.app.routers.integrations.get_provider", provider_factory)

    response = admin_client.get(
        "/api/integrations/alerts/aggregate",
        params={
            "organization_id": organization["id"],
            "limit": 2,
            "offset": 0,
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["total"] == 3
    assert payload["has_more"] is True
    assert payload["is_sampled"] is False
    assert [item["alert_id"] for item in payload["items"]] == ["alpha-2", "beta-1"]


def test_integration_overview_exposes_partial_alert_preview_failure_and_component_statuses(client_factory, monkeypatch):
    admin_client = client_factory()
    bootstrap_admin(admin_client)
    organization = create_organization(admin_client, "Org Overview Alerts")
    integration = create_wazuh_integration(admin_client, organization_id=organization["id"], include_indexer=True)

    class OverviewProvider:
        def __init__(self, integration_obj) -> None:
            self.integration = integration_obj
            self.platform = "wazuh"

        def capabilities(self) -> list[str]:
            return ["health:check", "alerts:list"]

        def health_check(self) -> HealthResult:
            return HealthResult(
                status="degraded",
                details={
                    "manager": {"status": "unreachable", "message": "Unable to connect"},
                    "indexer": {"status": "healthy", "cluster_status": "green"},
                },
            )

        def list_alerts(self, **filters):
            raise ProviderQueryError(
                "Failed to load alerts preview",
                code="INDEXER_QUERY_FAILED",
                details={"resolved_index": "wazuh-alerts-*"},
            )

        def close(self) -> None:
            return None

    monkeypatch.setattr("backend.app.routers.integrations.get_provider", lambda integration: OverviewProvider(integration))

    response = admin_client.get(f"/api/integrations/{integration['id']}/overview")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["health"]["status"] == "degraded"
    assert payload["health"]["manager_status"] == "unreachable"
    assert payload["health"]["indexer_status"] == "healthy"
    assert payload["alerts_preview"] is None
    assert payload["alerts_preview_error"]["code"] == "INDEXER_QUERY_FAILED"
    assert payload["alerts_preview_error"]["integration_id"] == integration["id"]


def test_dashboard_summary_aggregates_alerts_for_scoped_organization(client_factory, monkeypatch):
    admin_client = client_factory()
    bootstrap_admin(admin_client)
    org_alpha = create_organization(admin_client, "Org Dash Alpha")
    org_beta = create_organization(admin_client, "Org Dash Beta")
    integration_alpha = create_wazuh_integration(admin_client, organization_id=org_alpha["id"], name="Alpha Wazuh", include_indexer=True)
    create_wazuh_integration(admin_client, organization_id=org_beta["id"], name="Beta Wazuh", include_indexer=True)

    class DashboardProvider:
        def __init__(self, integration_id: int) -> None:
            self.integration_id = integration_id

        def capabilities(self) -> list[str]:
            return ["alerts:list"]

        def get_alert_statistics(self, **filters):
            assert filters["time_from"]
            assert filters["time_to"]
            if self.integration_id == integration_alpha["id"]:
                return {
                    "total": 7,
                    "by_severity": {"critical": 2, "high": 3, "medium": 1, "low": 1, "info": 0},
                    "trend": [
                        {"timestamp": "2026-01-01T00:00:00.000Z", "total": 3, "critical": 1, "high": 1, "medium": 1, "low": 0, "info": 0},
                        {"timestamp": "2026-01-02T00:00:00.000Z", "total": 4, "critical": 1, "high": 2, "medium": 0, "low": 1, "info": 0},
                    ],
                    "top_hosts": [{"key": "web-01", "count": 4, "integration_id": integration_alpha["id"], "integration_name": "Alpha Wazuh", "organization_id": org_alpha["id"], "organization_name": org_alpha["name"]}],
                    "top_rules": [{"key": "5710", "label": "Suspicious login", "count": 3, "integration_id": integration_alpha["id"], "integration_name": "Alpha Wazuh", "organization_id": org_alpha["id"], "organization_name": org_alpha["name"]}],
                    "top_mitre_ids": [{"key": "T1110", "count": 2}],
                    "top_agent_groups": [{"key": "linux", "count": 4}],
                    "latest_timestamp": "2026-01-02T12:00:00Z",
                }
            return {
                "total": 5,
                "by_severity": {"critical": 0, "high": 1, "medium": 2, "low": 1, "info": 1},
                "trend": [],
                "top_hosts": [{"key": "db-01", "count": 2}],
                "top_rules": [{"key": "1002", "label": "Archive event", "count": 2}],
                "top_mitre_ids": [{"key": "T1059", "count": 1}],
                "top_agent_groups": [{"key": "windows", "count": 2}],
                "latest_timestamp": "2026-01-02T08:00:00Z",
            }

        def close(self) -> None:
            return None

    monkeypatch.setattr("backend.app.routers.dashboard.get_provider", lambda integration: DashboardProvider(integration.id))

    response = admin_client.get(
        "/api/dashboard/summary",
        params={"organization_id": org_alpha["id"], "days": 7},
        headers={"Accept": "application/vnd.centralops.v1+json"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["organizations"]["total"] == 1
    assert payload["integrations"]["total"] == 1
    assert payload["alerts"]["total"] == 7
    assert payload["alerts"]["by_severity"]["critical"] == 2
    assert payload["alerts"]["by_severity"]["high"] == 3
    assert payload["alerts"]["sources"][0]["integration_name"] == "Alpha Wazuh"
    assert len(payload["alerts"]["trend"]) == 2
    assert payload["alerts"]["top_hosts"][0]["key"] == "web-01"
    assert payload["alerts"]["top_hosts"][0]["organization_name"] == org_alpha["name"]
    assert payload["alerts"]["top_rules"][0]["label"] == "Suspicious login"
    assert payload["alerts"]["top_rules"][0]["integration_name"] == "Alpha Wazuh"
    assert payload["alerts"]["latest_timestamp"] == "2026-01-02T12:00:00Z"
    assert payload["alerts"]["unsupported_sources"] == 0
    assert payload["integrations"]["health"]["degraded"] == 0


def test_dashboard_summary_includes_comparison_priority_and_platform_scope(client_factory, monkeypatch):
    admin_client = client_factory()
    bootstrap_admin(admin_client)
    org_alpha = create_organization(admin_client, "Org Priority Alpha")
    org_beta = create_organization(admin_client, "Org Priority Beta")
    integration_alpha = create_wazuh_integration(admin_client, organization_id=org_alpha["id"], name="Alpha Wazuh", include_indexer=True)
    create_wazuh_integration(admin_client, organization_id=org_beta["id"], name="Beta Wazuh", include_indexer=True)

    class DashboardProvider:
        def __init__(self, integration_id: int) -> None:
            self.integration_id = integration_id

        def capabilities(self) -> list[str]:
            return ["alerts:list"]

        def get_alert_statistics(self, **filters):
            if filters["time_to"] == "2026-01-08T00:00:00Z":
                if self.integration_id == integration_alpha["id"]:
                    return {
                        "total": 9,
                        "by_severity": {"critical": 4, "high": 2, "medium": 1, "low": 1, "info": 1},
                        "trend": [],
                        "top_hosts": [],
                        "top_rules": [],
                        "top_mitre_ids": [{"key": "T1110", "count": 4}],
                        "top_agent_groups": [{"key": "linux", "count": 5}],
                        "latest_timestamp": "2026-01-08T00:00:00Z",
                    }
                return {
                    "total": 5,
                    "by_severity": {"critical": 1, "high": 2, "medium": 1, "low": 1, "info": 0},
                    "trend": [],
                    "top_hosts": [],
                    "top_rules": [],
                    "top_mitre_ids": [{"key": "T1059", "count": 2}],
                    "top_agent_groups": [{"key": "windows", "count": 3}],
                    "latest_timestamp": "2026-01-07T18:00:00Z",
                }
            if self.integration_id == integration_alpha["id"]:
                return {
                    "total": 3,
                    "by_severity": {"critical": 1, "high": 1, "medium": 1, "low": 0, "info": 0},
                    "trend": [],
                    "top_hosts": [],
                    "top_rules": [],
                    "top_mitre_ids": [],
                    "top_agent_groups": [],
                    "latest_timestamp": None,
                }
            return {
                "total": 2,
                "by_severity": {"critical": 0, "high": 1, "medium": 1, "low": 0, "info": 0},
                "trend": [],
                "top_hosts": [],
                "top_rules": [],
                "top_mitre_ids": [],
                "top_agent_groups": [],
                "latest_timestamp": None,
            }

        def close(self) -> None:
            return None

    monkeypatch.setattr("backend.app.routers.dashboard.get_provider", lambda integration: DashboardProvider(integration.id))
    monkeypatch.setattr("backend.app.routers.dashboard.datetime", type("FrozenDateTime", (), {
        "now": staticmethod(lambda tz=None: __import__("datetime").datetime(2026, 1, 8, 0, 0, tzinfo=tz)),
        "utcnow": staticmethod(lambda: __import__("datetime").datetime(2026, 1, 8, 0, 0)),
    }))

    response = admin_client.get(
        "/api/dashboard/summary",
        params={"platform": "wazuh", "days": 7},
        headers={"Accept": "application/vnd.centralops.v1+json"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["organizations"]["total"] == 2
    assert payload["integrations"]["total"] == 2
    assert payload["alerts"]["comparison"]["total_alerts"]["current"] == 14
    assert payload["alerts"]["comparison"]["total_alerts"]["previous"] == 5
    assert payload["alerts"]["comparison"]["total_alerts"]["trend"] == "up"
    assert payload["alerts"]["comparison"]["critical_alerts"]["current"] == 5
    assert payload["alerts"]["comparison"]["critical_alerts"]["previous"] == 1
    assert payload["alerts"]["most_critical_client"]["organization_name"] == org_alpha["name"]
    assert payload["alerts"]["most_critical_integration"]["integration_name"] == integration_alpha["name"]
    assert payload["alerts"]["applied_platform"] == "wazuh"


def test_non_admin_integration_serialization_hides_wazuh_urls(client_factory):
    admin_client = client_factory()
    bootstrap_admin(admin_client)
    organization = create_organization(admin_client, "Org Hidden URLs")
    integration = create_wazuh_integration(admin_client, organization_id=organization["id"], include_indexer=True)
    create_user(admin_client, username="hidden-user", organization_id=organization["id"])

    user_client = client_factory()
    login(user_client, "hidden-user", "UserPassword123!")

    response = user_client.get(f"/api/integrations/{integration['id']}")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["manager_url"] is None
    assert payload["indexer_url"] is None
    assert payload["verify_ssl"] is None


def test_user_creation_rejects_inactive_organization_assignment(client_factory):
    admin_client = client_factory()
    bootstrap_admin(admin_client)
    organization = create_organization(admin_client, "Org Inactive User Scope")

    deactivate_response = admin_client.put(
        f"/api/organizations/{organization['id']}",
        json={"is_active": False},
    )
    assert deactivate_response.status_code == 200, deactivate_response.text

    create_response = admin_client.post(
        "/api/auth/users",
        json={
            "username": "inactive-org-user",
            "password": "UserPassword123!",
            "display_name": "Inactive Org User",
            "role": "user",
            "organization_id": organization["id"],
        },
    )
    assert create_response.status_code == 409, create_response.text
    assert create_response.json()["error"]["code"] == "org.inactive"
