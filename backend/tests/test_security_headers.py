"""Testes do middleware de security headers — F4-S4.

Verifica que os headers de segurança são adicionados corretamente a todas
as respostas, com comportamento distinto para produção vs. desenvolvimento.

Estratégia de teste:
- Monkey-patch em ``backend.app.main.settings.APP_ENV`` para simular produção.
- O middleware lê ``settings.APP_ENV`` em runtime, então o patch funciona
  sem reiniciar a aplicação.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import backend.app.main as main_module
from backend.app.db.database import Base, get_session
from backend.app.main import app


# ── Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture()
def test_client():
    """TestClient com banco in-memory. Não autentica — usa endpoint público."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_session] = override
    client = TestClient(app)
    yield client
    client.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def production_client(test_client, monkeypatch):
    """TestClient com APP_ENV=production."""
    monkeypatch.setattr(main_module.settings, "APP_ENV", "production")
    yield test_client


@pytest.fixture()
def dev_client(test_client, monkeypatch):
    """TestClient com APP_ENV=dev."""
    monkeypatch.setattr(main_module.settings, "APP_ENV", "dev")
    yield test_client


# Endpoint público acessível sem autenticação
_PUBLIC_ENDPOINT = "/api/auth/status"


# ── Testes: headers universais ────────────────────────────────────────

def test_security_headers_present_in_response(test_client) -> None:
    """Todos os headers de segurança mandatórios estão presentes em qualquer resposta."""
    r = test_client.get(_PUBLIC_ENDPOINT)
    # 200 ou 404 — o endpoint pode não existir, mas os headers devem estar presentes
    assert "x-content-type-options" in r.headers, "X-Content-Type-Options ausente"
    assert "x-frame-options" in r.headers, "X-Frame-Options ausente"
    assert "referrer-policy" in r.headers, "Referrer-Policy ausente"
    assert "permissions-policy" in r.headers, "Permissions-Policy ausente"
    assert "content-security-policy" in r.headers, "Content-Security-Policy ausente"


def test_x_content_type_options_nosniff(test_client) -> None:
    r = test_client.get(_PUBLIC_ENDPOINT)
    assert r.headers.get("x-content-type-options") == "nosniff"


def test_x_frame_options_deny_always(test_client) -> None:
    """X-Frame-Options deve ser DENY em qualquer ambiente."""
    r = test_client.get(_PUBLIC_ENDPOINT)
    assert r.headers.get("x-frame-options") == "DENY"


def test_x_frame_options_deny_in_production(production_client) -> None:
    """X-Frame-Options deve ser DENY também em produção."""
    r = production_client.get(_PUBLIC_ENDPOINT)
    assert r.headers.get("x-frame-options") == "DENY"


def test_referrer_policy_strict(test_client) -> None:
    r = test_client.get(_PUBLIC_ENDPOINT)
    assert r.headers.get("referrer-policy") == "strict-origin-when-cross-origin"


def test_permissions_policy_restricts_sensors(test_client) -> None:
    policy = test_client.get(_PUBLIC_ENDPOINT).headers.get("permissions-policy", "")
    assert "geolocation=()" in policy
    assert "microphone=()" in policy
    assert "camera=()" in policy


# ── Testes: HSTS somente em produção ─────────────────────────────────

def test_hsts_only_in_production(production_client) -> None:
    """Strict-Transport-Security deve aparecer somente em produção."""
    r = production_client.get(_PUBLIC_ENDPOINT)
    hsts = r.headers.get("strict-transport-security", "")
    assert "max-age=" in hsts, f"HSTS ausente em produção: {hsts!r}"
    assert "includeSubDomains" in hsts


def test_hsts_absent_in_dev(dev_client) -> None:
    """Strict-Transport-Security NÃO deve aparecer em desenvolvimento."""
    r = dev_client.get(_PUBLIC_ENDPOINT)
    assert "strict-transport-security" not in r.headers, (
        f"HSTS não deve estar presente em dev: {r.headers.get('strict-transport-security')}"
    )


def test_hsts_absent_in_test(test_client) -> None:
    """Em test (APP_ENV=test), HSTS não deve aparecer."""
    r = test_client.get(_PUBLIC_ENDPOINT)
    assert "strict-transport-security" not in r.headers


# ── Testes: CSP ambiente-específica ──────────────────────────────────

def test_csp_strict_in_production(production_client) -> None:
    """Em produção, CSP não deve permitir unsafe-eval nem ws:// para scripts."""
    r = production_client.get(_PUBLIC_ENDPOINT)
    csp = r.headers.get("content-security-policy", "")
    assert "unsafe-eval" not in csp, f"CSP de produção não deve conter unsafe-eval: {csp}"
    assert "ws://" not in csp, f"CSP de produção não deve conter ws://: {csp}"
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp


def test_csp_permissive_in_dev(dev_client) -> None:
    """Em dev, CSP deve permitir unsafe-eval e ws:// para Vite HMR."""
    r = dev_client.get(_PUBLIC_ENDPOINT)
    csp = r.headers.get("content-security-policy", "")
    assert "unsafe-eval" in csp, f"CSP de dev deve conter unsafe-eval: {csp}"
    assert "ws://localhost:*" in csp, f"CSP de dev deve conter ws://localhost:*: {csp}"


def test_csp_script_src_self_in_production(production_client) -> None:
    """script-src em produção deve ser apenas 'self'."""
    r = production_client.get(_PUBLIC_ENDPOINT)
    csp = r.headers.get("content-security-policy", "")
    assert "script-src 'self'" in csp


def test_csp_form_action_self_in_production(production_client) -> None:
    """form-action em produção deve ser apenas 'self'."""
    r = production_client.get(_PUBLIC_ENDPOINT)
    csp = r.headers.get("content-security-policy", "")
    assert "form-action 'self'" in csp


# ── Testes: headers em respostas não-2xx ─────────────────────────────

def test_security_headers_on_404(test_client) -> None:
    """Headers de segurança devem aparecer mesmo em respostas 404."""
    r = test_client.get("/api/this-endpoint-does-not-exist")
    # 404 de API ou redirect para SPA — headers de segurança devem estar presentes
    assert "x-frame-options" in r.headers
    assert "x-content-type-options" in r.headers
