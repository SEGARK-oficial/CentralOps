"""DB-backed, encrypted license activation.

Covers the new persistence seam: an operator-activated token is stored ENCRYPTED in
the ``license_config`` singleton and read DB-first by the edition resolver (env/file
remain as fallback). Crypto uses an ephemeral Ed25519 keypair generated in-test — the
private key never touches the repo. The DB is an in-memory sqlite; ``SessionLocal`` is
patched (the store imports it at call time, so attribute patching takes effect).
"""
from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.core import edition, license_store
from backend.app.db import database as db_module
from backend.app.db.database import Base
from backend.app.db.models import LicenseConfig


@pytest.fixture()
def test_db(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(db_module, "SessionLocal", TestingSessionLocal)
    # never let a stray env token leak into these tests; DB is the source under test
    monkeypatch.delenv("CENTRALOPS_LICENSE_TOKEN", raising=False)
    monkeypatch.delenv("CENTRALOPS_LICENSE_TOKEN_FILE", raising=False)
    edition.reset_cache()
    yield TestingSessionLocal
    edition.reset_cache()
    Base.metadata.drop_all(bind=engine)


def _keypair():
    priv = Ed25519PrivateKey.generate()
    return priv, priv.public_key()


def _sign(priv, *, kid="k1", **claims):
    now = int(time.time())
    payload = {"sub": "cust_1", "plan": "enterprise", "iat": now, "exp": now + 3600}
    payload.update(claims)
    return jwt.encode(payload, priv, algorithm="EdDSA", headers={"kid": kid})


def _write_keyring(tmp_path, pub, kid="k1"):
    (tmp_path / f"{kid}.pem").write_bytes(
        pub.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    )


def test_token_is_persisted_encrypted_and_round_trips(test_db):
    license_store.save_token("opaque-license-token", actor="admin@centralops.io")

    with test_db() as db:
        row = db.get(LicenseConfig, 1)
        assert row is not None and row.license_token
        # stored ciphertext must NOT be the plaintext (encrypted at rest)
        assert row.license_token != "opaque-license-token"
        assert row.license_token.startswith("enc::")
        assert row.activated_by == "admin@centralops.io"

    assert license_store.load_active_token() == "opaque-license-token"
    info = license_store.activation_info()
    assert info["source"] == "database" and info["activated_by"] == "admin@centralops.io"


def test_load_token_prefers_db_over_env(test_db, monkeypatch):
    priv, _ = _keypair()
    token = _sign(priv)
    license_store.save_token(token, actor="admin")
    # an env token is present but the DB activation must win ("sempre lê do banco")
    monkeypatch.setenv("CENTRALOPS_LICENSE_TOKEN", "env-fallback-token")
    assert edition._load_token() == token


def test_falls_back_to_env_when_db_empty(test_db, monkeypatch):
    monkeypatch.setenv("CENTRALOPS_LICENSE_TOKEN", "env-fallback-token")
    assert edition._load_token() == "env-fallback-token"


def test_activation_flips_edition_to_enterprise(test_db, monkeypatch, tmp_path):
    priv, pub = _keypair()
    _write_keyring(tmp_path, pub)
    monkeypatch.setenv("CENTRALOPS_LICENSE_KEYS_DIR", str(tmp_path))
    token = _sign(priv, plan="enterprise", features=["multi_tenant", "federated_search"], seats=50)

    license_store.save_token(token, actor="admin")
    fs = edition.refresh()  # re-resolves DB-first against the keyring

    assert fs.is_enterprise and fs.plan == "enterprise"
    assert fs.feature_enabled("federated_search") and fs.seats == 50


def test_deactivation_reverts_to_community(test_db, monkeypatch, tmp_path):
    priv, pub = _keypair()
    _write_keyring(tmp_path, pub)
    monkeypatch.setenv("CENTRALOPS_LICENSE_KEYS_DIR", str(tmp_path))
    license_store.save_token(_sign(priv, features=["multi_tenant"]), actor="admin")
    assert edition.refresh().is_enterprise

    license_store.clear_token(actor="admin")
    fs = edition.refresh()
    assert not fs.is_enterprise and fs.edition == edition.COMMUNITY
    assert license_store.activation_info()["source"] is None


def test_expired_stored_token_downgrades_to_community(test_db, monkeypatch, tmp_path):
    """Expiração → downgrade: um token que ERA válido e foi ativado (persistido no banco)
    resolve para Community na próxima resolução depois de vencer — fail-closed. O token
    permanece no banco (não é apagado), mas ``refresh()`` o re-verifica (``exp`` passou →
    ExpiredLicense → Community). É assim que o downgrade acontece no boot/refresh."""
    import time

    monkeypatch.setenv("CENTRALOPS_LICENSE_GRACE_DAYS", "0")  # estrito (a carência tem suíte própria)
    priv, pub = _keypair()
    _write_keyring(tmp_path, pub)
    monkeypatch.setenv("CENTRALOPS_LICENSE_KEYS_DIR", str(tmp_path))

    now = int(time.time())
    expired = _sign(priv, plan="enterprise", features=["multi_tenant"], iat=now - 7200, exp=now - 3600)
    license_store.save_token(expired, actor="admin")

    fs = edition.refresh()
    assert not fs.is_enterprise and fs.edition == edition.COMMUNITY  # downgrade
    # o token continua no banco (fonte 'database'), mas não concede nada expirado
    assert license_store.load_active_token() == expired
