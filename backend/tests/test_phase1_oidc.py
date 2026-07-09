"""Fase 1 (Entra) — login OIDC (Authorization Code + PKCE).

Atualizado na Fase 2: o ``oidc`` opera sobre um ``IdentitySnapshot`` (``cfg``).
Os unit tests constroem o cfg via ``_cfg()``; os E2E usam a fixture
``sso_config`` (monkeypatch de settings), que o ``identity_config.load`` lê
como fallback quando não há linha no banco.

Cobertura: PKCE/state/nonce, map_role/map_identity, email allowlist,
build_authorization_url, validação REAL de id_token (RSA + JWKS), state repo,
/status, /sso/login e /callback (JIT, conflito, not_provisioned, etc.).
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.core import oidc
from backend.app.core.config import settings
from backend.app.core.identity_config import IdentitySnapshot
from backend.app.db import models
from backend.app.db.database import Base, get_session
from backend.app.db.repository import OidcAuthStateRepository, UserRepository
from backend.app.main import app


_TENANT = "11111111-2222-3333-4444-555555555555"
_CLIENT = "client-app-id"
_ISSUER = f"https://login.microsoftonline.com/{_TENANT}/v2.0"


def _cfg(**overrides) -> IdentitySnapshot:
    base = dict(
        entra_enabled=True,
        entra_tenant_id=_TENANT,
        entra_client_id=_CLIENT,
        entra_client_secret="super-secret",
        entra_redirect_uri="https://app.test/api/auth/sso/callback",
        entra_authority="https://login.microsoftonline.com",
        entra_scopes="openid profile email",
        entra_role_map={"CentralOpsAdmin": "admin", "CentralOpsOperator": "operator"},
        entra_default_role="viewer",
        entra_default_is_global=False,
        entra_jit_provisioning=True,
        entra_allowed_email_domains=[],
        entra_button_label="Entrar com Microsoft",
        entra_post_login_redirect="/",
    )
    base.update(overrides)
    return IdentitySnapshot(**base)


# ── Config fixture (E2E — via settings/from_settings fallback) ────────


@pytest.fixture()
def sso_config(monkeypatch):
    monkeypatch.setattr(settings, "ENTRA_ENABLED", True)
    monkeypatch.setattr(settings, "ENTRA_TENANT_ID", _TENANT)
    monkeypatch.setattr(settings, "ENTRA_CLIENT_ID", _CLIENT)
    monkeypatch.setattr(settings, "ENTRA_CLIENT_SECRET", "super-secret")
    monkeypatch.setattr(settings, "ENTRA_REDIRECT_URI", "https://app.test/api/auth/sso/callback")
    monkeypatch.setattr(settings, "ENTRA_AUTHORITY", "https://login.microsoftonline.com")
    monkeypatch.setattr(settings, "ENTRA_SCOPES", "openid profile email")
    monkeypatch.setattr(settings, "ENTRA_ROLE_MAP", {"CentralOpsAdmin": "admin", "CentralOpsOperator": "operator"})
    monkeypatch.setattr(settings, "ENTRA_DEFAULT_ROLE", "viewer")
    monkeypatch.setattr(settings, "ENTRA_DEFAULT_IS_GLOBAL", False)
    monkeypatch.setattr(settings, "ENTRA_JIT_PROVISIONING", True)
    monkeypatch.setattr(settings, "ENTRA_ALLOWED_EMAIL_DOMAINS", [])
    monkeypatch.setattr(settings, "ENTRA_POST_LOGIN_REDIRECT", "/")
    monkeypatch.setattr(settings, "ENTRA_BUTTON_LABEL", "Entrar com Microsoft")
    oidc.reset_caches()
    yield
    oidc.reset_caches()


# ── Unit: helpers puros (cfg-based) ───────────────────────────────────


def test_is_enabled_requires_all_fields():
    assert oidc.is_enabled(_cfg(entra_tenant_id=None)) is False


def test_is_enabled_true_when_configured():
    assert oidc.is_enabled(_cfg()) is True


def test_pkce_pair_is_url_safe_and_distinct():
    v, c = oidc.generate_pkce_pair()
    assert v != c
    assert "=" not in v and "=" not in c
    assert "+" not in c and "/" not in c


def test_map_role_picks_highest_privilege():
    cfg = _cfg()
    assert oidc.map_role(cfg, ["CentralOpsOperator", "CentralOpsAdmin"]) == "admin"
    assert oidc.map_role(cfg, ["CentralOpsOperator"]) == "operator"
    assert oidc.map_role(cfg, []) == "viewer"
    assert oidc.map_role(cfg, ["UnknownRole"]) == "viewer"


def test_map_identity_admin_is_global():
    ident = oidc.map_identity(_cfg(), {"oid": "o1", "email": "A@X.com", "name": "A", "roles": ["CentralOpsAdmin"]})
    assert ident.subject == "o1"
    assert ident.email == "a@x.com"
    assert ident.role == "admin"
    assert ident.is_global is True


def test_map_identity_default_is_global_flag():
    ident = oidc.map_identity(_cfg(entra_default_is_global=True), {"oid": "o2", "preferred_username": "b@x.com", "roles": []})
    assert ident.role == "viewer"
    assert ident.is_global is True


def test_email_domain_allowlist():
    cfg = _cfg(entra_allowed_email_domains=["empresa.com"])
    assert oidc.email_domain_allowed(cfg, "user@empresa.com") is True
    assert oidc.email_domain_allowed(cfg, "user@other.com") is False
    assert oidc.email_domain_allowed(cfg, None) is False
    assert oidc.email_domain_allowed(_cfg(entra_allowed_email_domains=[]), "anyone@anywhere.com") is True


def test_build_authorization_url(monkeypatch):
    monkeypatch.setattr(oidc, "discover", lambda cfg: {"authorization_endpoint": "https://login.test/authorize"})
    url = oidc.build_authorization_url(_cfg(), state="st", nonce="no", code_challenge="ch")
    assert url.startswith("https://login.test/authorize?")
    for fragment in ("client_id=client-app-id", "code_challenge=ch", "code_challenge_method=S256", "state=st", "nonce=no", "response_type=code"):
        assert fragment in url


# ── Unit: validação real de id_token ──────────────────────────────────


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _make_id_token(key, **overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": _ISSUER,
        "aud": _CLIENT,
        "sub": "subject-123",
        "oid": "oid-123",
        "tid": _TENANT,
        "nonce": "the-nonce",
        "email": "user@empresa.com",
        "name": "User Example",
        "roles": ["CentralOpsOperator"],
        "iat": now,
        "exp": now + 3600,
    }
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-key"})


@pytest.fixture()
def patched_jwks(rsa_key, monkeypatch):
    pub = rsa_key.public_key()
    monkeypatch.setattr(oidc, "_jwks_client", lambda cfg: SimpleNamespace(
        get_signing_key_from_jwt=lambda token: SimpleNamespace(key=pub)
    ))
    return rsa_key


def test_validate_id_token_happy_path(patched_jwks):
    token = _make_id_token(patched_jwks)
    claims = oidc.validate_id_token(_cfg(), token, nonce="the-nonce")
    assert claims["oid"] == "oid-123"


def test_validate_id_token_rejects_bad_nonce(patched_jwks):
    with pytest.raises(oidc.OidcError):
        oidc.validate_id_token(_cfg(), _make_id_token(patched_jwks), nonce="WRONG")


def test_validate_id_token_rejects_wrong_audience(patched_jwks):
    with pytest.raises(oidc.OidcError):
        oidc.validate_id_token(_cfg(), _make_id_token(patched_jwks, aud="some-other-app"), nonce="the-nonce")


def test_validate_id_token_rejects_wrong_tenant(patched_jwks):
    token = _make_id_token(patched_jwks, tid="99999999-0000-0000-0000-000000000000")
    with pytest.raises(oidc.OidcError):
        oidc.validate_id_token(_cfg(), token, nonce="the-nonce")


def test_validate_id_token_rejects_bad_issuer(patched_jwks):
    with pytest.raises(oidc.OidcError):
        oidc.validate_id_token(_cfg(), _make_id_token(patched_jwks, iss="https://evil.example.com/"), nonce="the-nonce")


def test_validate_id_token_rejects_expired(patched_jwks):
    past = int(time.time()) - 10
    token = _make_id_token(patched_jwks, exp=past, iat=past - 3600)
    with pytest.raises(oidc.OidcError):
        oidc.validate_id_token(_cfg(), token, nonce="the-nonce")


# ── DB fixture + repository ───────────────────────────────────────────


@pytest.fixture()
def client_factory():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_get_session():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_session] = override_get_session
    clients: list[TestClient] = []

    def factory() -> TestClient:
        c = TestClient(app)
        clients.append(c)
        return c

    yield factory, TestingSessionLocal

    for c in clients:
        c.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


def test_oidc_state_consume_is_single_use(client_factory):
    _, Session = client_factory
    with Session() as s:
        OidcAuthStateRepository(s).create(state="st", nonce="n", code_verifier="v", ttl_seconds=600)
    with Session() as s:
        repo = OidcAuthStateRepository(s)
        assert repo.consume("st") == ("n", "v", None)
        assert repo.consume("st") is None


def test_oidc_state_consume_rejects_expired(client_factory):
    _, Session = client_factory
    with Session() as s:
        s.add(models.OidcAuthState(
            state="old", nonce="n", code_verifier="v",
            created_at=datetime.utcnow() - timedelta(minutes=30),
            expires_at=datetime.utcnow() - timedelta(minutes=20),
        ))
        s.commit()
    with Session() as s:
        assert OidcAuthStateRepository(s).consume("old") is None


# ── /status ───────────────────────────────────────────────────────────


def test_status_sso_disabled_by_default(client_factory):
    factory, _ = client_factory
    r = factory().get("/api/auth/status")
    assert r.status_code == 200
    assert r.json()["sso_enabled"] is False


def test_status_sso_enabled_when_configured(client_factory, sso_config):
    factory, _ = client_factory
    r = factory().get("/api/auth/status")
    body = r.json()
    assert body["sso_enabled"] is True
    assert body["sso_button_label"] == "Entrar com Microsoft"


# ── /sso/login ────────────────────────────────────────────────────────


def test_sso_login_disabled_redirects_to_error(client_factory):
    factory, _ = client_factory
    r = factory().get("/api/auth/sso/login", follow_redirects=False)
    assert r.status_code == 303
    assert "sso_error=sso_disabled" in r.headers["location"]


def test_sso_login_redirects_to_idp_and_persists_state(client_factory, sso_config, monkeypatch):
    factory, Session = client_factory
    monkeypatch.setattr(oidc, "discover", lambda cfg: {"authorization_endpoint": "https://login.test/authorize"})
    r = factory().get("/api/auth/sso/login", follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith("https://login.test/authorize?")
    assert "code_challenge_method=S256" in loc
    with Session() as s:
        assert s.query(models.OidcAuthState).count() == 1


# ── /sso/callback ─────────────────────────────────────────────────────


def _seed_state(Session, *, state="st-1", nonce="n-1", verifier="v-1"):
    with Session() as s:
        OidcAuthStateRepository(s).create(state=state, nonce=nonce, code_verifier=verifier, ttl_seconds=600)


def _patch_token_flow(monkeypatch, claims):
    monkeypatch.setattr(oidc, "exchange_code", lambda cfg, **kw: {"id_token": "fake.jwt.token"})
    monkeypatch.setattr(oidc, "validate_id_token", lambda cfg, token, nonce: dict(claims))


def test_callback_jit_provisions_user_and_sets_session(client_factory, sso_config, monkeypatch):
    factory, Session = client_factory
    _seed_state(Session)
    _patch_token_flow(monkeypatch, {
        "oid": "oid-new", "email": "novo@empresa.com", "name": "Novo Analista",
        "roles": ["CentralOpsOperator"],
    })
    r = factory().get("/api/auth/sso/callback?code=authcode&state=st-1", follow_redirects=False)
    assert r.status_code == 303, r.text
    assert r.headers["location"] == "/"
    assert "set-cookie" in {k.lower() for k in r.headers}
    with Session() as s:
        user = UserRepository(s).get_by_external_subject("entra", "oid-new")
        assert user is not None
        assert user.auth_provider == "entra"
        assert user.email == "novo@empresa.com"
        assert user.role == "operator"
        assert user.password_hash is None


def test_callback_invalid_state(client_factory, sso_config, monkeypatch):
    factory, _ = client_factory
    _patch_token_flow(monkeypatch, {"oid": "x"})
    r = factory().get("/api/auth/sso/callback?code=c&state=does-not-exist", follow_redirects=False)
    assert r.status_code == 303
    assert "sso_error=invalid_state" in r.headers["location"]


def test_callback_provider_error(client_factory, sso_config):
    factory, _ = client_factory
    r = factory().get("/api/auth/sso/callback?error=access_denied&error_description=nope", follow_redirects=False)
    assert "sso_error=provider_error" in r.headers["location"]


def test_callback_email_conflict_blocks_takeover(client_factory, sso_config, monkeypatch):
    factory, Session = client_factory
    with Session() as s:
        s.add(models.AppUser(username="local-admin", email="taken@empresa.com", password_hash="x", role="admin", is_active=True))
        s.commit()
    _seed_state(Session)
    _patch_token_flow(monkeypatch, {"oid": "oid-z", "email": "taken@empresa.com", "roles": []})
    r = factory().get("/api/auth/sso/callback?code=c&state=st-1", follow_redirects=False)
    assert "sso_error=email_conflict" in r.headers["location"]


def test_callback_not_provisioned_when_jit_off(client_factory, sso_config, monkeypatch):
    factory, Session = client_factory
    monkeypatch.setattr(settings, "ENTRA_JIT_PROVISIONING", False)
    _seed_state(Session)
    _patch_token_flow(monkeypatch, {"oid": "oid-unknown", "email": "ghost@empresa.com", "roles": []})
    r = factory().get("/api/auth/sso/callback?code=c&state=st-1", follow_redirects=False)
    assert "sso_error=not_provisioned" in r.headers["location"]


def test_callback_reconciles_existing_user(client_factory, sso_config, monkeypatch):
    factory, Session = client_factory
    with Session() as s:
        s.add(models.AppUser(
            username="existing", email="old@empresa.com", auth_provider="entra",
            external_subject="oid-exist", password_hash=None, role="viewer", is_active=True,
        ))
        s.commit()
    _seed_state(Session)
    _patch_token_flow(monkeypatch, {
        "oid": "oid-exist", "email": "old@empresa.com", "name": "Promoted",
        "roles": ["CentralOpsAdmin"],
    })
    r = factory().get("/api/auth/sso/callback?code=c&state=st-1", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    with Session() as s:
        user = UserRepository(s).get_by_external_subject("entra", "oid-exist")
        assert user.role == "admin"
        assert user.is_global is True
        assert user.display_name == "Promoted"


def test_callback_inactive_user_blocked(client_factory, sso_config, monkeypatch):
    factory, Session = client_factory
    with Session() as s:
        s.add(models.AppUser(
            username="disabled", auth_provider="entra", external_subject="oid-dis",
            password_hash=None, role="viewer", is_active=False,
        ))
        s.commit()
    _seed_state(Session)
    _patch_token_flow(monkeypatch, {"oid": "oid-dis", "email": "d@empresa.com", "roles": []})
    r = factory().get("/api/auth/sso/callback?code=c&state=st-1", follow_redirects=False)
    assert "sso_error=user_inactive" in r.headers["location"]
