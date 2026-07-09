"""Fundação do contrato de query (QueryCapability + endpoint).

Cobre:
- ``QueryCapability`` (dataclass + ``capability_key()``) e ``FederatedQueryResult``.
- Cada vendor declara o dialeto certo no catálogo (``query_capabilities``) E o
  deriva no runtime (``capabilities()``) — runtime↔catálogo alinhados, sem o legado
  ``investigations:run`` (catálogo e runtime ⊆ mesmo vocabulário).
- Regra por kind: Sophos partner/organization NÃO rodam query (capability None).
- ``integration_query_capability`` tolerante (plataforma só-catálogo ⇒ None).
- ``GET /api/providers/query-capabilities`` agrega por dialeto com ``supported_by``.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.collectors import registry
from backend.app.collectors.capabilities import (
    CAP_QUERY_OPENSEARCH_DSL,
    CAP_QUERY_XDR_DATA_LAKE,
    QueryCapability,
    is_valid_capability,
    query_capability_key,
)
from backend.app.db import models
from backend.app.providers.base import FederatedQueryResult, FederatedSourceResult


def _integ(**kw):
    base = dict(name="x", organization_id=1, kind="tenant", platform="sophos")
    base.update(kw)
    return models.Integration(**base)


# ── QueryCapability (dataclass) ───────────────────────────────────────


def test_query_capability_key_helper():
    assert query_capability_key("opensearch_dsl") == "query:opensearch_dsl"
    assert is_valid_capability("query:opensearch_dsl")
    assert is_valid_capability("query:xdr_data_lake")


def test_legacy_investigations_run_is_retired():
    """A key síncrona legada ``investigations:run`` foi aposentada em favor
    de ``query:<dialect>``. Travada fora do vocabulário (e de todo runtime) para não
    reaparecer — back-compat puro, não flag forward-looking."""
    from backend.app.collectors.capabilities import EXACT_CAPABILITIES

    assert "investigations:run" not in EXACT_CAPABILITIES
    assert is_valid_capability("investigations:run") is False
    for integ in (_integ(platform="wazuh", kind="tenant"),
                  _integ(platform="sophos", kind="tenant"),
                  _integ(platform="sophos", kind="partner")):
        assert "investigations:run" not in registry.get_provider(integ).capabilities()


def test_query_capability_defaults_and_key():
    qc = QueryCapability(dialect="kql")
    assert qc.capability_key() == "query:kql"
    assert qc.modes == ("live",)
    assert qc.supports_async is False
    assert qc.max_window is None
    assert qc.spec_kinds == ("passthrough",)
    # frozen → imutável
    with pytest.raises(Exception):
        qc.dialect = "fql"  # type: ignore[misc]


# ── FederatedQueryResult (agregador) ──────────────────────────────────


def test_federated_query_result_aggregation():
    f = FederatedQueryResult(allow_partial_results=True)
    f.add_source(FederatedSourceResult(integration_id=1, status="answered", count=5))
    f.add_source(
        FederatedSourceResult(integration_id=2, status="failed", error="boom", partial=True)
    )
    assert f.sources_queried == 2
    assert f.sources_answered == 1          # só o "answered"
    assert f.partial is True                # uma fonte falhou → resultado incompleto


def test_federated_query_result_all_answered_not_partial():
    f = FederatedQueryResult()
    f.add_source(FederatedSourceResult(integration_id=1, status="answered", count=3))
    f.add_source(FederatedSourceResult(integration_id=2, status="answered", count=7))
    assert f.sources_answered == 2
    assert f.partial is False


def test_federated_add_source_replaces_same_integration():
    f = FederatedQueryResult()
    f.add_source(FederatedSourceResult(integration_id=1, status="failed"))
    f.add_source(FederatedSourceResult(integration_id=1, status="answered", count=2))
    assert f.sources_queried == 1
    assert f.per_source[1].status == "answered"


# ── Catálogo: query_capabilities declarado ────────────────────────────


def test_wazuh_catalog_declares_opensearch_dsl():
    reg = registry.get_platform("wazuh")
    assert reg is not None
    dialects = {qc.dialect for qc in reg.query_capabilities}
    assert dialects == {"opensearch_dsl"}
    assert CAP_QUERY_OPENSEARCH_DSL in reg.capabilities
    qc = reg.query_capabilities[0]
    assert qc.supports_async is False
    assert qc.modes == ("live",)


def test_sophos_catalog_declares_xdr_data_lake_with_30d_window():
    reg = registry.get_platform("sophos")
    assert reg is not None
    dialects = {qc.dialect for qc in reg.query_capabilities}
    assert dialects == {"xdr_data_lake"}
    assert CAP_QUERY_XDR_DATA_LAKE in reg.capabilities
    qc = reg.query_capabilities[0]
    assert qc.supports_async is True
    assert qc.max_window is not None
    assert qc.max_window.days == 30


def test_sophos_mssp_variants_have_no_query():
    for variant in ("sophos_partner", "sophos_organization"):
        reg = registry.get_platform(variant)
        assert reg is not None, variant
        assert reg.query_capabilities == ()
        assert not any(c.startswith("query:") for c in reg.capabilities), variant


# ── Catálogo↔runtime: alinhamento total (anti-drift) ─────────


def test_catalog_query_keys_match_query_capabilities_for_all_platforms():
    """As keys ``query:<dialect>`` de ``capabilities`` == dialetos de
    ``query_capabilities`` em TODA plataforma registrada."""
    offenders = {}
    for plat in registry.all_platforms():
        cat = {c.split(":", 1)[1] for c in plat.capabilities if c.startswith("query:")}
        struct = {qc.dialect for qc in plat.query_capabilities}
        if cat != struct:
            offenders[plat.platform] = (sorted(cat), sorted(struct))
    assert not offenders, f"dialetos divergentes catálogo↔struct: {offenders}"


@pytest.mark.parametrize(
    "integration,expected_query_keys",
    [
        (_integ(platform="wazuh", kind="tenant"), ["query:opensearch_dsl"]),
        (_integ(platform="sophos", kind="tenant"), ["query:xdr_data_lake"]),
        (_integ(platform="sophos", kind="tenant", parent_integration_id=9),
         ["query:xdr_data_lake"]),
        (_integ(platform="sophos", kind="partner"), []),
        (_integ(platform="sophos", kind="organization"), []),
    ],
    ids=["wazuh", "sophos-tenant", "sophos-child", "sophos-partner", "sophos-org"],
)
def test_runtime_capabilities_derive_query_key(integration, expected_query_keys):
    caps = registry.get_provider(integration).capabilities()
    query_keys = [c for c in caps if c.startswith("query:")]
    assert query_keys == expected_query_keys
    # o legado investigations:run não aparece mais no runtime
    assert "investigations:run" not in caps


def test_runtime_query_key_is_subset_of_catalog():
    """Para cada integração com provider rico, o ``query:<dialect>`` de runtime ⊆
    o que o catálogo declara para a plataforma (nunca inventa dialeto)."""
    for integ in (_integ(platform="wazuh", kind="tenant"),
                  _integ(platform="sophos", kind="tenant")):
        plat = registry.get_platform(integ.platform)
        catalog_query = {c for c in plat.capabilities if c.startswith("query:")}
        runtime_query = {c for c in registry.get_provider(integ).capabilities()
                         if c.startswith("query:")}
        assert runtime_query <= catalog_query, integ.platform


# ── integration_query_capability (instance-aware + tolerante) ─────────


def test_integration_query_capability_resolves_dialect():
    qc = registry.integration_query_capability(_integ(platform="wazuh", kind="tenant"))
    assert qc is not None and qc.dialect == "opensearch_dsl"
    qc2 = registry.integration_query_capability(_integ(platform="sophos", kind="tenant"))
    assert qc2 is not None and qc2.dialect == "xdr_data_lake"


def test_integration_query_capability_none_for_partner_and_catalog_only():
    # Sophos partner/org: regra por kind → None
    assert registry.integration_query_capability(
        _integ(platform="sophos", kind="partner")
    ) is None
    # plataforma só-catálogo/coleta (sem provider rico) → None, sem levantar
    assert registry.integration_query_capability(
        _integ(platform="ninjaone", kind="tenant")
    ) is None


# ── Endpoint GET /api/providers/query-capabilities ────────────────────


@pytest.fixture()
def client_factory():
    from backend.app.db.database import Base, get_session
    from backend.app.main import app

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
    clients: list[TestClient] = []

    def factory() -> TestClient:
        client = TestClient(app)
        clients.append(client)
        return client

    yield factory

    for c in clients:
        c.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


def _bootstrap_admin(client: TestClient) -> None:
    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPass1!", "display_name": "Admin"},
    )
    assert r.status_code == 200, r.text
    r2 = client.post("/api/auth/login", json={"username": "admin", "password": "AdminPass1!"})
    assert r2.status_code == 200, r2.text


def test_query_capabilities_endpoint_requires_auth(client_factory):
    client = client_factory()
    r = client.get("/api/providers/query-capabilities")
    assert r.status_code in (401, 403)


def test_query_capabilities_endpoint_lists_dialects(client_factory):
    client = client_factory()
    _bootstrap_admin(client)
    r = client.get("/api/providers/query-capabilities")
    assert r.status_code == 200, r.text
    by_dialect = {row["dialect"]: row for row in r.json()}

    assert "opensearch_dsl" in by_dialect
    assert "xdr_data_lake" in by_dialect

    osd = by_dialect["opensearch_dsl"]
    assert osd["capability"] == "query:opensearch_dsl"
    assert "wazuh" in osd["supported_by"]
    assert osd["supports_async"] is False
    assert osd["max_window_seconds"] is None

    xdr = by_dialect["xdr_data_lake"]
    assert "sophos" in xdr["supported_by"]
    assert xdr["supports_async"] is True
    assert xdr["max_window_seconds"] == 30 * 24 * 3600
    assert "data_lake" in xdr["modes"]


def test_query_capabilities_endpoint_schema_shape(client_factory):
    client = client_factory()
    _bootstrap_admin(client)
    r = client.get("/api/providers/query-capabilities")
    for row in r.json():
        for key in ("dialect", "capability", "modes", "supports_async",
                    "required_secrets", "ocsf_mapping_version", "spec_kinds",
                    "supported_by"):
            assert key in row, key
        assert row["capability"] == f"query:{row['dialect']}"
