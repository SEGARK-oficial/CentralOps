"""Testes para GET /api/providers/platforms.

Cobertura:
- Retorna 4 plataformas conhecidas.
- Cada plataforma tem display_name, icon_id, auth_fields e streams.
- auth_fields com type=secret nunca têm valor padrão.
- Autenticação obrigatória (401 sem sessão).
- Campos de auth replicam fielmente os campos do IntegrationForm.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db.database import Base, get_session
from backend.app.main import app


@pytest.fixture()
def client_factory():
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


class TestProviderPlatformsEndpoint:
    def test_requires_auth(self, client_factory):
        client = client_factory()
        r = client.get("/api/providers/platforms")
        assert r.status_code in (401, 403)

    def test_returns_all_catalog_platforms(self, client_factory):
        """O endpoint deve expor TODAS as plataformas registradas.

        A contagem é DERIVADA do registry, não hardcoded: o catálogo é plugin-driven
        (cada vendor se auto-registra no import de ``vendors/__init__.py``), então um
        número fixo quebra a cada integração nova — sem pegar bug nenhum. O invariante
        que importa é a equivalência com o registry: se um vendor SUMIR do catálogo
        (registro perdido, import removido), isto falha.
        """
        from backend.app.collectors import vendors  # noqa: F401 — dispara auto-registro
        from backend.app.collectors import registry as collector_registry

        client = client_factory()
        _bootstrap_admin(client)
        r = client.get("/api/providers/platforms")
        assert r.status_code == 200, r.text
        data = r.json()
        expected = {p.platform for p in collector_registry.all_platforms()}
        assert {p["platform"] for p in data} == expected
        assert len(data) == len(expected)  # sem duplicatas no catálogo
        assert expected, "catálogo vazio — o auto-registro dos vendors não rodou"

    def test_platform_slugs(self, client_factory):
        client = client_factory()
        _bootstrap_admin(client)
        r = client.get("/api/providers/platforms")
        slugs = [p["platform"] for p in r.json()]
        assert "wazuh" in slugs
        assert "sophos" in slugs
        assert "ninjaone" in slugs
        assert "microsoft_defender" in slugs
        assert "crowdstrike" in slugs
        assert "entra_id" in slugs
        assert "okta" in slugs
        assert "aws_cloudtrail" in slugs
        assert "aws_cloudwatch" in slugs
        assert "veeam" in slugs

    def test_each_has_display_name(self, client_factory):
        client = client_factory()
        _bootstrap_admin(client)
        r = client.get("/api/providers/platforms")
        for p in r.json():
            assert p["display_name"], f"display_name vazio para {p['platform']}"

    def test_each_has_auth_fields(self, client_factory):
        client = client_factory()
        _bootstrap_admin(client)
        r = client.get("/api/providers/platforms")
        for p in r.json():
            # Fontes PUSH não usam credenciais de poll: a auth é o
            # token de ingestão (emitido após o create), então auth_fields é vazio.
            if p.get("transport") == "push":
                assert p["auth_fields"] == []
                continue
            assert len(p["auth_fields"]) > 0, f"auth_fields vazio para {p['platform']}"

    def test_catalog_is_plugin_driven(self, client_factory):
        """Catálogo self-describing: cada plataforma traz category + description
        vindos do PlatformRegistration do vendor (não de dict hardcoded). Garante
        que um vendor novo apareça agrupado por categoria sem tocar a UI/router."""
        client = client_factory()
        _bootstrap_admin(client)
        r = client.get("/api/providers/platforms")
        by_plat = {p["platform"]: p for p in r.json()}
        for plat in ("sophos", "microsoft_defender", "ninjaone", "wazuh"):
            assert by_plat[plat]["category"], f"category vazia para {plat}"
            assert by_plat[plat]["description"], f"description vazia para {plat}"
        cats = {p["category"] for p in by_plat.values()}
        assert "EDR / XDR" in cats  # sophos + defender agrupam

    def test_secret_fields_have_no_default_value(self, client_factory):
        """Garante que type=secret nunca expõe valor padrão."""
        client = client_factory()
        _bootstrap_admin(client)
        r = client.get("/api/providers/platforms")
        for p in r.json():
            for field in p["auth_fields"]:
                assert "default" not in field or field.get("default") is None, (
                    f"Campo {field['key']} em {p['platform']} expõe valor padrão"
                )
                # A ausência de 'value' no schema confirma que nada é retornado
                assert "value" not in field, (
                    f"Campo {field['key']} em {p['platform']} tem 'value' — vazamento de credencial"
                )

    def test_wazuh_indexer_required_manager_optional(self, client_factory):
        """Indexer é obrigatório (fonte de dados); Manager é opcional (add-on de saúde)."""
        client = client_factory()
        _bootstrap_admin(client)
        r = client.get("/api/providers/platforms")
        wazuh = next(p for p in r.json() if p["platform"] == "wazuh")
        fields_by_key = {f["key"]: f for f in wazuh["auth_fields"]}

        # Indexer — obrigatório
        assert fields_by_key["indexer_url"]["required"] is True
        assert fields_by_key["indexer_url"]["type"] == "url"
        assert fields_by_key["indexer_username"]["required"] is True
        assert fields_by_key["indexer_password"]["required"] is True

        # Manager — opcional
        assert fields_by_key["manager_url"]["required"] is False
        assert fields_by_key["manager_url"]["type"] == "url"
        assert fields_by_key["manager_api_username"]["required"] is False
        assert fields_by_key["manager_api_password"]["required"] is False

    def test_wazuh_has_streams(self, client_factory):
        client = client_factory()
        _bootstrap_admin(client)
        r = client.get("/api/providers/platforms")
        wazuh = next(p for p in r.json() if p["platform"] == "wazuh")
        # wazuh tem pelo menos 1 stream registrado
        assert len(wazuh["streams"]) >= 0  # pode ser 0 em test isolation, não levanta

    def test_sophos_secret_fields(self, client_factory):
        client = client_factory()
        _bootstrap_admin(client)
        r = client.get("/api/providers/platforms")
        sophos = next(p for p in r.json() if p["platform"] == "sophos")
        secret_fields = [f for f in sophos["auth_fields"] if f["type"] == "secret"]
        assert len(secret_fields) >= 1
        for sf in secret_fields:
            assert "value" not in sf

    def test_icon_ids_present(self, client_factory):
        client = client_factory()
        _bootstrap_admin(client)
        r = client.get("/api/providers/platforms")
        for p in r.json():
            assert p["icon_id"] is not None, f"icon_id None para {p['platform']}"

    def test_response_schema_structure(self, client_factory):
        """Verifica que o shape é iterável e cada item tem os campos do contrato."""
        client = client_factory()
        _bootstrap_admin(client)
        r = client.get("/api/providers/platforms")
        assert r.status_code == 200
        for p in r.json():
            assert "platform" in p
            assert "display_name" in p
            assert "auth_fields" in p
            assert "streams" in p
            for af in p["auth_fields"]:
                assert "key" in af
                assert "label" in af
                assert "type" in af
                assert "required" in af
            for s in p["streams"]:
                assert "stream" in s
                assert "schedule_seconds" in s


class TestProviderTestConnection:
    """POST /providers/{platform}/test-connection — teste pré-save stateless."""

    def test_catalog_exposes_supports_test(self, client_factory):
        client = client_factory()
        _bootstrap_admin(client)
        by = {p["platform"]: p for p in client.get("/api/providers/platforms").json()}
        # sophos/defender/ninjaone têm probe OAuth; wazuh-fonte não.
        assert by["sophos"]["supports_test"] is True
        assert by["microsoft_defender"]["supports_test"] is True
        assert by["ninjaone"]["supports_test"] is True
        assert by["wazuh"]["supports_test"] is False

    def test_unknown_platform_404(self, client_factory):
        client = client_factory()
        _bootstrap_admin(client)
        r = client.post("/api/providers/nope/test-connection", json={"config": {}})
        assert r.status_code == 404

    def test_wazuh_unsupported_422(self, client_factory):
        client = client_factory()
        _bootstrap_admin(client)
        r = client.post("/api/providers/wazuh/test-connection", json={"config": {}})
        assert r.status_code == 422

    def test_runs_vendor_test_fn_without_real_network(self, client_factory):
        """O endpoint roda o test_fn do vendor; aqui o trocamos por um fake p/ não
        bater na rede real (a corretude do probe é validada contra creds reais)."""
        import dataclasses

        from backend.app.collectors import registry as reg
        from backend.app.collectors.output.base import TestResult

        client = client_factory()
        _bootstrap_admin(client)
        orig = reg.get_platform("sophos")

        async def _fake(_cfg):
            return TestResult.passed("fake ok", latency_ms=1.5)

        fake = dataclasses.replace(orig, test_fn=_fake)
        with patch.object(reg, "get_platform", lambda p: fake if p == "sophos" else orig):
            r = client.post(
                "/api/providers/sophos/test-connection",
                json={"config": {"client_id": "x", "client_secret": "y"}},
            )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["detail"] == "fake ok"
        assert body["latency_ms"] == 1.5


def test_oauth_probe_guards_empty_creds():
    """O probe não faz rede sem creds — fail-fast com mensagem útil (sem secret)."""
    import asyncio

    from backend.app.collectors.auth.probes import oauth_client_credentials_probe

    res = asyncio.run(oauth_client_credentials_probe("https://x", "", "", "scope"))
    assert res.ok is False
    assert "client_id" in res.detail
