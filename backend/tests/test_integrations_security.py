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
from backend.app.providers.base import HealthResult
from backend.app.providers.wazuh.provider import resolve_alert_index
from backend.app.providers.wazuh.query_builder import build_agent_query


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


def test_resolve_alert_index_defaults_cleanly():
    """resolve_alert_index segue vivo — a busca federada (run_query) resolve o
    índice do Indexer por ele (a superfície de alerts foi removida)."""
    assert resolve_alert_index(None) == "wazuh-alerts-*"
    assert resolve_alert_index({}) == "wazuh-alerts-*"
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

    dashboard_response = user_client.get("/api/dashboard/summary")
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

    dashboard_response = user_client.get("/api/dashboard/summary")
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


def test_integration_overview_exposes_component_statuses_without_alerts_preview(client_factory, monkeypatch):
    """O overview expõe health por componente; a preview de alertas (superfície
    Wazuh-only removida) NÃO existe mais no payload."""
    admin_client = client_factory()
    bootstrap_admin(admin_client)
    organization = create_organization(admin_client, "Org Overview")
    integration = create_wazuh_integration(admin_client, organization_id=organization["id"], include_indexer=True)

    class OverviewProvider:
        def __init__(self, integration_obj) -> None:
            self.integration = integration_obj
            self.platform = "wazuh"

        def capabilities(self) -> list[str]:
            return ["health:check"]

        def health_check(self) -> HealthResult:
            return HealthResult(
                status="degraded",
                details={
                    "manager": {"status": "unreachable", "message": "Unable to connect"},
                    "indexer": {"status": "healthy", "cluster_status": "green"},
                },
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
    assert "alerts_preview" not in payload
    assert "alerts_preview_error" not in payload


def test_alerts_surface_endpoints_are_gone(client_factory):
    """As rotas da superfície de alerts foram REMOVIDAS (sem shim/legado)."""
    admin_client = client_factory()
    bootstrap_admin(admin_client)
    organization = create_organization(admin_client, "Org No Alerts")
    integration = create_wazuh_integration(admin_client, organization_id=organization["id"])

    assert admin_client.get("/api/integrations/alerts/aggregate").status_code == 404
    assert admin_client.get(f"/api/integrations/{integration['id']}/alerts").status_code == 404
    assert admin_client.get(f"/api/integrations/{integration['id']}/alerts/abc").status_code == 404
    assert admin_client.post(f"/api/integrations/{integration['id']}/alerts/search?query=x").status_code == 404


def test_dashboard_summary_scoped_organization_counts(client_factory):
    """O summary (payload único v2) escopa organizations/integrations por org."""
    admin_client = client_factory()
    bootstrap_admin(admin_client)
    org_alpha = create_organization(admin_client, "Org Dash Alpha")
    org_beta = create_organization(admin_client, "Org Dash Beta")
    create_wazuh_integration(admin_client, organization_id=org_alpha["id"], name="Alpha Wazuh")
    create_wazuh_integration(admin_client, organization_id=org_beta["id"], name="Beta Wazuh")

    response = admin_client.get(
        "/api/dashboard/summary",
        params={"organization_id": org_alpha["id"], "days": 7},
    )
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["schema_version"] == 2
    assert payload["organizations"]["total"] == 1
    assert payload["integrations"]["total"] == 1
    assert payload["integrations"]["by_platform"] == {"wazuh": 1}
    assert payload["integrations"]["health"]["degraded"] == 0
    assert "alerts" not in payload


def test_dashboard_summary_platform_scope_counts(client_factory):
    """Filtro por plataforma restringe as contagens no payload único v2."""
    admin_client = client_factory()
    bootstrap_admin(admin_client)
    org_alpha = create_organization(admin_client, "Org Priority Alpha")
    org_beta = create_organization(admin_client, "Org Priority Beta")
    create_wazuh_integration(admin_client, organization_id=org_alpha["id"], name="Alpha Wazuh")
    create_wazuh_integration(admin_client, organization_id=org_beta["id"], name="Beta Wazuh")

    response = admin_client.get(
        "/api/dashboard/summary",
        params={"platform": "wazuh", "days": 7},
    )
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["schema_version"] == 2
    assert payload["organizations"]["total"] == 2
    assert payload["integrations"]["total"] == 2
    assert payload["integrations"]["by_platform"] == {"wazuh": 2}
    assert "comparison" in payload["integrations"]
    assert payload["integrations"]["comparison"]["degraded_integrations"]["trend"] in ("up", "down", "stable")


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
