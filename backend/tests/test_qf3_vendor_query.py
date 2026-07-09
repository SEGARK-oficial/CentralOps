"""Providers de query CrowdStrike (FQL) + Defender (KQL).

Zero-core: providers ricos novos resolvidos via provider_factory. run_query usa um
seam HTTP mockável (``_client()``) — testes patcham, nunca tocam a rede real.
Cobre: query OK (request shape + paginação), erro HTTP → ProviderQueryError,
sem credencial → ProviderConfigurationError, e a capability declarada no catálogo.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import pytest

from backend.app.collectors import registry
from backend.app.db import models
from backend.app.providers.crowdstrike.provider import CrowdStrikeProvider
from backend.app.providers.defender.provider import DefenderProvider
from backend.app.providers.errors import ProviderConfigurationError, ProviderQueryError


class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class _Client:
    """httpx.Client fake — context manager + .post() devolvendo respostas em ordem."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, **kw):
        self.calls.append((url, kw))
        return self._responses.pop(0)


def _cs_provider(**kw):
    base = dict(name="cs", organization_id=1, kind="tenant", platform="crowdstrike",
                client_id="cid", base_url="https://api.crowdstrike.com")
    base.update(kw)
    return CrowdStrikeProvider(models.Integration(**base))


def _def_provider(**kw):
    base = dict(name="d", organization_id=1, kind="tenant", platform="microsoft_defender",
                client_id="cid", tenant_id="tid")
    base.update(kw)
    return DefenderProvider(models.Integration(**base))


# ── CrowdStrike FQL ────────────────────────────────────────────────────


def test_crowdstrike_run_query_shape_and_pagination(monkeypatch):
    p = _cs_provider()
    fake = _Client([
        _Resp(200, {"access_token": "tok"}),
        _Resp(200, {"resources": [{"composite_id": "a"}],
                    "meta": {"pagination": {"after": "CUR"}}}),
        _Resp(200, {"resources": [{"composite_id": "b"}],
                    "meta": {"pagination": {"after": None}}}),
    ])
    monkeypatch.setattr(p, "_client", lambda: fake)
    monkeypatch.setattr(p, "_credentials", lambda: ("cid", "secret"))

    res = p.run_query("severity:>50", "2026-06-21T00:00:00Z", "2026-06-22T00:00:00Z")
    assert res.total == 2
    assert [i["composite_id"] for i in res.items] == ["a", "b"]
    # token POST + 2 páginas de alertas
    assert fake.calls[0][0].endswith("/oauth2/token")
    alerts_body = fake.calls[1][1]["json"]
    assert "alerts/combined/alerts/v1" in fake.calls[1][0]
    assert "severity:>50" in alerts_body["filter"]
    assert "created_timestamp:>='2026-06-21T00:00:00Z'" in alerts_body["filter"]
    assert fake.calls[2][1]["json"]["after"] == "CUR"  # 2ª página usa o cursor


def test_crowdstrike_http_error_raises_query_error(monkeypatch):
    p = _cs_provider()
    fake = _Client([_Resp(200, {"access_token": "tok"}), _Resp(400, {"errors": ["bad fql"]})])
    monkeypatch.setattr(p, "_client", lambda: fake)
    monkeypatch.setattr(p, "_credentials", lambda: ("cid", "secret"))
    with pytest.raises(ProviderQueryError):
        p.run_query("bad(", "2026-06-21T00:00:00Z", "2026-06-22T00:00:00Z")


def test_crowdstrike_no_credentials_raises_config_error(monkeypatch):
    p = _cs_provider(client_id="")  # sem client_id
    monkeypatch.setattr("backend.app.services.integration_secrets.read_secret",
                        lambda integ, name: None)
    with pytest.raises(ProviderConfigurationError):
        p.run_query("x", "2026-06-21T00:00:00Z", "2026-06-22T00:00:00Z")


# ── Defender KQL ───────────────────────────────────────────────────────


def test_defender_run_query_kql(monkeypatch):
    p = _def_provider()
    fake = _Client([
        _Resp(200, {"access_token": "tok"}),
        _Resp(200, {"results": [{"DeviceName": "pc1"}, {"DeviceName": "pc2"}],
                    "schema": [{"Name": "DeviceName", "Type": "String"}]}),
    ])
    monkeypatch.setattr(p, "_client", lambda: fake)
    monkeypatch.setattr(p, "_credentials", lambda: ("tid", "cid", "secret"))

    res = p.run_query("DeviceEvents | take 10", "2026-06-21T00:00:00Z", "2026-06-22T00:00:00Z")
    assert res.total == 2
    assert res.items[0]["DeviceName"] == "pc1"
    # token no Azure AD + runHuntingQuery no Graph com o KQL
    assert "login.microsoftonline.com/tid/oauth2/v2.0/token" in fake.calls[0][0]
    assert fake.calls[1][0].endswith("/security/runHuntingQuery")
    assert fake.calls[1][1]["json"]["query"] == "DeviceEvents | take 10"


def test_defender_http_error_raises_query_error(monkeypatch):
    p = _def_provider()
    fake = _Client([_Resp(200, {"access_token": "tok"}), _Resp(400, {"error": "bad kql"})])
    monkeypatch.setattr(p, "_client", lambda: fake)
    monkeypatch.setattr(p, "_credentials", lambda: ("tid", "cid", "secret"))
    with pytest.raises(ProviderQueryError):
        p.run_query("bad |", "2026-06-21T00:00:00Z", "2026-06-22T00:00:00Z")


def test_defender_empty_kql_rejected(monkeypatch):
    p = _def_provider()
    monkeypatch.setattr(p, "_credentials", lambda: ("tid", "cid", "secret"))
    with pytest.raises(ProviderQueryError):
        p.run_query("   ", "2026-06-21T00:00:00Z", "2026-06-22T00:00:00Z")


# ── Catálogo / capability ──────────────────────────────────────────────


def test_crowdstrike_declares_fql_capability():
    reg = registry.get_platform("crowdstrike")
    assert reg.provider_factory is not None
    assert {qc.dialect for qc in reg.query_capabilities} == {"fql"}
    assert "query:fql" in reg.capabilities
    qc = reg.query_capabilities[0]
    assert qc.supports_async is False and qc.max_window.days == 7


def test_defender_declares_kql_capability():
    reg = registry.get_platform("microsoft_defender")
    assert reg.provider_factory is not None
    assert {qc.dialect for qc in reg.query_capabilities} == {"kql"}
    assert "query:kql" in reg.capabilities
    qc = reg.query_capabilities[0]
    assert qc.max_window.days == 30 and qc.rate_limit == "45/min/tenant"


def test_qf3_integration_query_capability_resolves():
    for platform, dialect in (("crowdstrike", "fql"), ("microsoft_defender", "kql")):
        integ = models.Integration(name="x", organization_id=1, kind="tenant", platform=platform)
        qc = registry.integration_query_capability(integ)
        assert qc is not None and qc.dialect == dialect
