"""Onboarding do papel AWS cross-account: External ID, escopo de tenant e teste pré-save.

O External ID é a fronteira entre clientes (confused deputy): estes testes travam
que ele é gerado pelo servidor, estável, único por org e invisível para quem não
administra integrações daquela org — e que o teste pré-save não age em nome de
uma org alheia.

Imports usam ``backend.app.*`` (gotcha .so dual-root).
"""

from __future__ import annotations

import dataclasses
from typing import Any, Dict, Generator, List
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.collectors.auth import aws as aws_auth
from backend.app.db import models
from backend.app.db.database import Base, get_session
from backend.app.main import app

_ROLE = "arn:aws:iam::123456789012:role/CentralOpsReadOnly"
_BROKER = "arn:aws:sts::111122223333:assumed-role/centralops-broker/i-0abc"


@pytest.fixture()
def env(monkeypatch) -> Generator[Dict[str, Any], None, None]:
    aws_auth.reset_caches()

    async def _ident():
        return {"Arn": _BROKER}

    # Sem rede: a identidade do servidor vem deste seam.
    monkeypatch.setattr(aws_auth, "_get_caller_identity", _ident)

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_get_session():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_session] = override_get_session
    clients: List[TestClient] = []

    def factory() -> TestClient:
        c = TestClient(app)
        clients.append(c)
        return c

    ga = factory()
    r = ga.post(
        "/api/auth/bootstrap",
        json={"username": "root", "password": "AdminPassword123!", "display_name": "Root"},
    )
    assert r.status_code in (200, 201), r.text

    def create_org(name: str) -> int:
        r = ga.post("/api/organizations", json={"name": name, "slug": name.lower().replace(" ", "-")})
        assert r.status_code in (200, 201), r.text
        return r.json()["id"]

    org_a, org_b = create_org("Org A"), create_org("Org B")

    def login_as(username: str, role: str, org_id: int) -> TestClient:
        r = ga.post(
            "/api/auth/users",
            json={"username": username, "password": "SomePassword123!", "role": role, "organization_id": org_id},
        )
        assert r.status_code in (200, 201), r.text
        c = factory()
        r = c.post("/api/auth/login", json={"username": username, "password": "SomePassword123!"})
        assert r.status_code == 200, r.text
        return c

    yield {
        "global_admin": ga,
        "scoped": login_as("orgadmin", "admin", org_a),
        "viewer": login_as("leitor", "viewer", org_a),
        "org_a": org_a,
        "org_b": org_b,
        "Session": TestingSession,
    }

    for c in clients:
        c.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)
    aws_auth.reset_caches()


def _setup(client: TestClient, org_id: int):
    return client.get("/api/providers/aws/role-setup", params={"organization_id": org_id})


def test_external_id_is_server_generated_stable_and_unique_per_org(env) -> None:
    ga = env["global_admin"]
    first = _setup(ga, env["org_a"])
    assert first.status_code == 200, first.text
    body = first.json()
    ext_a = body["external_id"]
    assert ext_a.startswith("centralops-")
    # Estável: a segunda chamada devolve o MESMO valor (o papel do cliente depende dele).
    assert _setup(ga, env["org_a"]).json()["external_id"] == ext_a
    # Único por org.
    assert _setup(ga, env["org_b"]).json()["external_id"] != ext_a
    # Persistido na org.
    with env["Session"]() as db:
        assert db.get(models.Organization, env["org_a"]).aws_external_id == ext_a


def test_setup_returns_principal_and_prefilled_template(env) -> None:
    body = _setup(env["global_admin"], env["org_a"]).json()
    assert body["trust_principal_arn"] == "arn:aws:iam::111122223333:role/centralops-broker"
    assert body["principal_error"] is None
    params = body["template"]["Parameters"]
    assert params["ExternalId"]["Default"] == body["external_id"]
    assert params["TrustedPrincipalArn"]["Default"] == body["trust_principal_arn"]


def test_scoped_admin_cannot_read_other_org_external_id(env) -> None:
    assert _setup(env["scoped"], env["org_b"]).status_code == 403
    assert _setup(env["scoped"], env["org_a"]).status_code == 200
    # E o 403 não gerou ID para a org alheia como efeito colateral.
    with env["Session"]() as db:
        assert db.get(models.Organization, env["org_b"]).aws_external_id is None


def test_viewer_without_integration_write_cannot_open_onboarding(env) -> None:
    assert _setup(env["viewer"], env["org_a"]).status_code == 403


def test_unknown_org_is_404(env) -> None:
    assert _setup(env["global_admin"], 999_999).status_code == 404


def test_existing_external_id_is_never_replaced(env) -> None:
    with env["Session"]() as db:
        org = db.get(models.Organization, env["org_a"])
        org.aws_external_id = "centralops-ja-existente"
        db.commit()
    assert _setup(env["global_admin"], env["org_a"]).json()["external_id"] == "centralops-ja-existente"


# ── Teste pré-save age em nome de qual org? ──────────────────────────────


def _with_spy_probe(platform: str = "aws_cloudtrail"):
    from backend.app.collectors import registry as reg
    from backend.app.collectors.output.base import TestResult

    seen: List[Dict[str, Any]] = []
    orig_get = reg.get_platform
    orig = orig_get(platform)

    async def _spy(cfg):
        seen.append(dict(cfg))
        return TestResult.passed("ok", latency_ms=1.0)

    fake = dataclasses.replace(orig, test_fn=_spy)
    return seen, patch.object(reg, "get_platform", lambda p: fake if p == platform else orig_get(p))


def test_probe_refuses_org_outside_user_scope(env) -> None:
    seen, patcher = _with_spy_probe()
    with patcher:
        r = env["scoped"].post(
            "/api/providers/aws_cloudtrail/test-connection",
            json={"config": {"client_id": _ROLE, "base_url": "b", "organization_id": env["org_b"]}},
        )
    assert r.status_code == 403
    assert seen == []  # o probe nem rodou


def test_probe_receives_validated_integer_org(env) -> None:
    seen, patcher = _with_spy_probe()
    with patcher:
        r = env["scoped"].post(
            "/api/providers/aws_cloudtrail/test-connection",
            json={"config": {"client_id": _ROLE, "base_url": "b", "organization_id": str(env["org_a"])}},
        )
    assert r.status_code == 200, r.text
    assert seen[0]["organization_id"] == env["org_a"]


@pytest.mark.parametrize("bad", ["abc", -3, 0, True])
def test_probe_drops_invalid_org_instead_of_forwarding(env, bad) -> None:
    seen, patcher = _with_spy_probe()
    with patcher:
        r = env["global_admin"].post(
            "/api/providers/aws_cloudtrail/test-connection",
            json={"config": {"client_id": _ROLE, "organization_id": bad}},
        )
    assert r.status_code == 200, r.text
    assert "organization_id" not in seen[0]


# ── Integração salva com papel ───────────────────────────────────────────


def test_integration_with_role_needs_no_secret_and_resolves_org_external_id(env) -> None:
    ga = env["global_admin"]
    ext = _setup(ga, env["org_a"]).json()["external_id"]
    r = ga.post(
        "/api/integrations/",
        json={
            "name": "CloudTrail via papel",
            "platform": "aws_cloudtrail",
            "organization_id": env["org_a"],
            "client_id": _ROLE,
            "base_url": "logs-bucket",
            "region": "us-east-1",
        },
    )
    assert r.status_code in (200, 201), r.text
    integ_id = r.json()["id"]

    with env["Session"]() as db:
        integ = db.get(models.Integration, integ_id)
        auth = aws_auth.auth_for_integration(db, integ)
    assert auth.mode == aws_auth.MODE_ROLE
    assert auth.role_arn == _ROLE
    assert auth.external_id == ext
    assert auth.session_name == f"centralops-o{env['org_a']}-i{integ_id}"


def test_catalog_exposes_aws_onboarding_key(env) -> None:
    by = {p["platform"]: p for p in env["global_admin"].get("/api/providers/platforms").json()}
    assert by["aws_cloudtrail"]["auth_setup"] == "aws_assume_role"
    assert by["aws_cloudwatch"]["auth_setup"] == "aws_assume_role"
    assert by["sophos"]["auth_setup"] == ""
