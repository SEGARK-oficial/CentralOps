"""Pais de AWS Organization: descoberta de contas e regra de cada filha.

O Core lista as contas e diz COMO cada filha nasce (``child_spec``); criar as
filhas é do EE. Aqui trava o lado do Core: catálogo, criação do pai como
``kind=organization`` (sem coleta própria), disparo da descoberta pelo seam do EE
e a regra de credencial por fonte.

Imports usam ``backend.app.*`` (gotcha .so dual-root).
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Generator, List
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.collectors import registry as reg
from backend.app.collectors.auth.aws import AwsAuth
from backend.app.collectors.vendors import aws_organization as org_mod
from backend.app.core import ee_hooks
from backend.app.db import models
from backend.app.db.database import Base, get_session
from backend.app.main import app
from backend.app.providers.aws import provider as prov
from backend.app.providers.aws.provider import AwsOrganizationProvider, member_role_arn

_MGMT_ROLE = "arn:aws:iam::111122223333:role/CentralOpsReadOnly"


class _Org:
    """Fake do client ``organizations`` com paginação."""

    def __init__(self, pages: List[Dict[str, Any]]) -> None:
        self.pages = list(pages)
        self.calls: List[Dict[str, Any]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def list_accounts(self, **kw):
        self.calls.append(kw)
        return self.pages.pop(0)


def _integration(platform: str, client_id: str = _MGMT_ROLE, base_url: str | None = None):
    return models.Integration(
        id=42, organization_id=7, name="org", platform=platform, kind="organization",
        client_id=client_id, base_url=base_url,
    )


def _provider(platform: str, fake: _Org, **kw) -> AwsOrganizationProvider:
    p = AwsOrganizationProvider(_integration(platform, **kw))
    p._auth = lambda: AwsAuth.access_key("AKIA", "s")  # type: ignore[method-assign]
    p._client = lambda kwargs: fake  # type: ignore[method-assign]
    return p


# ── descoberta ───────────────────────────────────────────────────────────


def test_discover_lists_every_page_and_keeps_only_active_accounts() -> None:
    fake = _Org([
        {"Accounts": [
            {"Id": "111122223333", "Name": "management", "Status": "ACTIVE"},
            {"Id": "444455556666", "Name": "prod", "State": "ACTIVE"},
        ], "NextToken": "p2"},
        {"Accounts": [
            {"Id": "777788889999", "Name": "antiga", "Status": "SUSPENDED"},
            {"Id": "000011112222", "Name": "saindo", "State": "PENDING_CLOSURE"},
            {"Id": "333344445555", "Name": "dev", "Status": "ACTIVE"},
        ]},
    ])
    out = _provider("aws_guardduty_org", fake).discover_tenants()
    assert [a["id"] for a in out] == ["111122223333", "444455556666", "333344445555"]
    assert out[1]["name"] == "prod"
    assert fake.calls[1]["NextToken"] == "p2"


def test_discover_works_inside_a_running_event_loop() -> None:
    """Router async ou teste async: ``asyncio.run`` direto levantaria."""
    fake = _Org([{"Accounts": [{"Id": "444455556666", "Name": "prod", "Status": "ACTIVE"}]}])
    p = _provider("aws_securityhub_org", fake)

    async def _inside():
        return p.discover_tenants()

    assert [a["id"] for a in asyncio.run(_inside())] == ["444455556666"]


def test_connection_reports_safe_error() -> None:
    class _Denied(Exception):
        response = {"Error": {"Code": "AWSOrganizationsNotInUseException", "Message": "no org"}}

    class _Boom(_Org):
        async def list_accounts(self, **kw):
            raise _Denied()

    result = _provider("aws_guardduty_org", _Boom([])).test_connection()
    assert result.status == "error"
    assert "ListAccounts: AWSOrganizationsNotInUseException" in result.details["error"]


# ── regra de cada filha ──────────────────────────────────────────────────


def test_member_role_keeps_name_and_path() -> None:
    assert member_role_arn("arn:aws:iam::111122223333:role/sec/CentralOps", "444455556666") == (
        "arn:aws:iam::444455556666:role/sec/CentralOps"
    )
    assert member_role_arn("arn:aws-us-gov:iam::111122223333:role/R", "444455556666").startswith(
        "arn:aws-us-gov:iam::444455556666:"
    )


def test_findings_child_assumes_the_role_in_the_member_account() -> None:
    p = AwsOrganizationProvider(_integration("aws_guardduty_org"))
    spec = p.child_spec("444455556666", "prod")
    assert spec == {
        "platform": "aws_guardduty",
        "name": "prod (444455556666)",
        "client_id": "arn:aws:iam::444455556666:role/CentralOpsReadOnly",
        "tenant_id": "444455556666",
        "region": "*",
        "base_url": None,
    }


def test_cloudtrail_child_reuses_parent_role_and_central_bucket() -> None:
    p = AwsOrganizationProvider(_integration("aws_cloudtrail_org", base_url="org-trail-bucket/p"))
    spec = p.child_spec("444455556666", "prod")
    assert spec["platform"] == "aws_cloudtrail"
    assert spec["client_id"] == _MGMT_ROLE  # o bucket central é lido pelo papel do pai
    assert spec["tenant_id"] == "444455556666"  # só muda a conta do caminho
    assert spec["base_url"] == "org-trail-bucket/p"


def test_access_key_parent_is_refused() -> None:
    p = AwsOrganizationProvider(_integration("aws_cloudtrail_org", client_id="AKIAEXAMPLE"))
    with pytest.raises(ValueError):
        p.child_spec("444455556666", "prod")


def test_probe_refuses_access_key_before_network() -> None:
    result = asyncio.run(org_mod._probe({"client_id": "AKIAEXAMPLE", "organization_id": 1}))
    assert result.ok is False
    assert "ARN de um papel" in result.detail


@pytest.mark.parametrize("platform", ["aws_cloudtrail_org", "aws_guardduty_org", "aws_securityhub_org"])
def test_catalog_declares_organization_parents(platform) -> None:
    plat = reg.get_platform(platform)
    assert plat.variant == "organization"
    assert plat.base_platform == ""
    assert "discover:children" in plat.capabilities
    assert plat.provider_factory is not None
    assert plat.auth_setup == "aws_assume_role"
    # Pai não coleta: nenhum collector registrado na plataforma do pai.
    assert list(reg.iter_for_platform(platform)) == []
    child_platform, _ = prov.ORG_PLATFORMS[platform]
    assert reg.get_platform(child_platform) is not None


# ── criação pelo router ──────────────────────────────────────────────────


@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_session] = override
    c = TestClient(app)
    r = c.post("/api/auth/bootstrap", json={"username": "root", "password": "AdminPassword123!", "display_name": "Root"})
    assert r.status_code in (200, 201), r.text
    r = c.post("/api/organizations", json={"name": "Cliente", "slug": "cliente"})
    assert r.status_code in (200, 201), r.text
    c.org_id = r.json()["id"]  # type: ignore[attr-defined]
    yield c
    c.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)
    ee_hooks.reset_partner_sync_dispatcher()


def _create(c: TestClient, platform: str, **extra):
    body = {"name": "AWS Org", "platform": platform, "organization_id": c.org_id, "client_id": _MGMT_ROLE}
    body.update(extra)
    return c.post("/api/integrations/", json=body)


def test_parent_is_created_as_organization_and_dispatches_discovery(client) -> None:
    dispatched: List[int] = []
    ee_hooks.register_partner_sync_dispatcher(dispatched.append)
    with patch("backend.app.collectors.scheduler.register_integration_in_beat") as beat:
        r = _create(client, "aws_cloudtrail_org", base_url="org-trail-bucket")
    assert r.status_code in (200, 201), r.text
    body = r.json()
    assert body["platform"] == "aws_cloudtrail_org"
    assert body["kind"] == "organization"
    assert dispatched == [body["id"]]
    beat.assert_not_called()  # o pai não coleta


def test_community_creates_parent_without_children(client) -> None:
    ee_hooks.reset_partner_sync_dispatcher()
    r = _create(client, "aws_guardduty_org")
    assert r.status_code in (200, 201), r.text
    r = client.post(f"/api/integrations/{r.json()['id']}/sync-tenants")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "enterprise_required"
