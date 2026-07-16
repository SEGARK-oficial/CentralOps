"""Tests for the edition runtime loader + GET /api/edition (open-core).

Public keys are written to a temp dir as ``<kid>.pem`` and a token is signed with the
matching EPHEMERAL private key (never in the repo). Covers keyring loading (incl.
skipping bad/non-Ed25519 keys), token from env and from file, fail-closed defaults,
cache refresh, and the endpoint serialization (which must not leak the customer id).
"""
from __future__ import annotations

import logging
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key

from backend.app.core import edition
from backend.app.routers import edition as edition_api


@pytest.fixture(autouse=True)
def _reset_edition(monkeypatch):
    # Isolate every test from ambient license env + the module cache.
    for var in ("CENTRALOPS_LICENSE_TOKEN", "CENTRALOPS_LICENSE_TOKEN_FILE", "CENTRALOPS_LICENSE_KEYS_DIR"):
        monkeypatch.delenv(var, raising=False)
    edition.reset_cache()
    yield
    edition.reset_cache()


def _write_pubkey(dir_path, kid, pub):
    pem = pub.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    (dir_path / f"{kid}.pem").write_bytes(pem)


def _sign(priv, *, kid="k1", **claims):
    now = int(time.time())
    payload = {"sub": "cust_42", "plan": "enterprise", "iat": now, "exp": now + 3600}
    payload.update(claims)
    return jwt.encode(payload, priv, algorithm="EdDSA", headers={"kid": kid})


# ── load_keyring ───────────────────────────────────────────────────────────────

def test_load_keyring_reads_pem_by_kid(tmp_path):
    priv = Ed25519PrivateKey.generate()
    _write_pubkey(tmp_path, "billing-2026", priv.public_key())
    keyring = edition.load_keyring(tmp_path)
    assert set(keyring) == {"billing-2026"}


def test_load_keyring_absent_dir_is_empty(tmp_path):
    assert edition.load_keyring(tmp_path / "does-not-exist") == {}


def test_load_keyring_skips_non_ed25519_and_garbage(tmp_path):
    priv = Ed25519PrivateKey.generate()
    _write_pubkey(tmp_path, "good", priv.public_key())
    # RSA public key -> skipped (not Ed25519)
    rsa_pub = generate_private_key(public_exponent=65537, key_size=2048).public_key()
    (tmp_path / "rsa.pem").write_bytes(
        rsa_pub.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    (tmp_path / "garbage.pem").write_bytes(b"not a pem")
    keyring = edition.load_keyring(tmp_path)
    assert set(keyring) == {"good"}  # rsa + garbage skipped, no crash


# ── load_keyring: logging de diagnóstico (dedup por mudança de kids) ───────────

def test_load_keyring_logs_info_on_kids_change_and_debug_always(tmp_path, caplog):
    """INFO só quando o conjunto de kids MUDA (1º load, chave nova); DEBUG sempre."""
    priv = Ed25519PrivateKey.generate()
    _write_pubkey(tmp_path, "key.prod", priv.public_key())

    with caplog.at_level(logging.DEBUG, logger="backend.app.core.edition"):
        edition.load_keyring(tmp_path)
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 1  # 1º load = mudança (None -> {key.prod})
    assert "loaded 1 key(s)" in infos[0].getMessage()
    assert "key.prod" in infos[0].getMessage() and str(tmp_path) in infos[0].getMessage()

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="backend.app.core.edition"):
        edition.load_keyring(tmp_path)  # mesmos kids -> sem INFO (dedup), DEBUG presente
    assert [r for r in caplog.records if r.levelno == logging.INFO] == []
    debugs = [
        r for r in caplog.records
        if r.levelno == logging.DEBUG and "license keyring" in r.getMessage()
    ]
    assert debugs, "o load deve SEMPRE logar em DEBUG"

    caplog.clear()
    _write_pubkey(tmp_path, "key.prod2", priv.public_key())  # kids mudaram -> INFO de novo
    with caplog.at_level(logging.INFO, logger="backend.app.core.edition"):
        edition.load_keyring(tmp_path)
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 1 and "loaded 2 key(s)" in infos[0].getMessage()


def test_load_keyring_logs_explicit_message_when_empty(tmp_path, caplog):
    """Transição p/ VAZIO (dir ausente/sem .pem) loga INFO auto-explicativa — é o
    estado que produz o 'unknown key id' na ativação."""
    with caplog.at_level(logging.INFO, logger="backend.app.core.edition"):
        assert edition.load_keyring(tmp_path / "does-not-exist") == {}
    assert "license keyring: 0 keys loaded from" in caplog.text
    assert "will fail with unknown key id" in caplog.text


def test_refresh_warns_once_when_token_present_but_keyring_empty(tmp_path, monkeypatch, caplog):
    """Token configurado + keyring vazio = incidente clássico do overlay ausente:
    WARNING claro no refresh, deduplicado (não spamma a cada TTL), sem vazar o token."""
    secret_token = "tok-secret-abcdef-1234567890"
    monkeypatch.setenv("CENTRALOPS_LICENSE_KEYS_DIR", str(tmp_path))  # dir vazio
    monkeypatch.setenv("CENTRALOPS_LICENSE_TOKEN", secret_token)
    edition.reset_cache()

    with caplog.at_level(logging.DEBUG, logger="backend.app.core.edition"):
        fs = edition.refresh()
    assert fs.edition == edition.COMMUNITY  # fail-closed preservado
    warnings = [
        r for r in caplog.records
        if "license token present but public keyring is empty" in r.getMessage()
    ]
    assert len(warnings) == 1
    assert str(tmp_path) in warnings[0].getMessage()
    assert secret_token not in caplog.text  # decisão: NUNCA logar o token

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="backend.app.core.edition"):
        edition.refresh()  # mesmo estado persiste -> deduplicado
    assert "license token present but public keyring is empty" not in caplog.text


# ── current()/refresh() via env + file ─────────────────────────────────────────

def test_current_is_community_without_token_or_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("CENTRALOPS_LICENSE_KEYS_DIR", str(tmp_path))  # empty dir
    edition.reset_cache()
    assert edition.current().edition == edition.COMMUNITY


def test_current_enterprise_from_env_token(tmp_path, monkeypatch):
    priv = Ed25519PrivateKey.generate()
    _write_pubkey(tmp_path, "k1", priv.public_key())
    token = _sign(priv, features=["multi_tenant", "federated_search"], seats=25)
    monkeypatch.setenv("CENTRALOPS_LICENSE_KEYS_DIR", str(tmp_path))
    monkeypatch.setenv("CENTRALOPS_LICENSE_TOKEN", token)
    edition.reset_cache()
    fs = edition.current()
    assert fs.is_enterprise and fs.seats == 25
    assert edition.feature_enabled("multi_tenant")
    assert not edition.feature_enabled("audit_compliance")


def test_token_from_file(tmp_path, monkeypatch):
    priv = Ed25519PrivateKey.generate()
    _write_pubkey(tmp_path, "k1", priv.public_key())
    token_file = tmp_path / "license.jwt"
    token_file.write_text(_sign(priv, features=["reseller"]) + "\n")  # trailing ws tolerated
    monkeypatch.setenv("CENTRALOPS_LICENSE_KEYS_DIR", str(tmp_path))
    monkeypatch.setenv("CENTRALOPS_LICENSE_TOKEN_FILE", str(token_file))
    edition.reset_cache()
    assert edition.current().is_enterprise


def test_invalid_token_env_fails_closed_to_community(tmp_path, monkeypatch):
    priv = Ed25519PrivateKey.generate()
    other = Ed25519PrivateKey.generate()
    _write_pubkey(tmp_path, "k1", other.public_key())  # keyring has the WRONG key
    monkeypatch.setenv("CENTRALOPS_LICENSE_KEYS_DIR", str(tmp_path))
    monkeypatch.setenv("CENTRALOPS_LICENSE_TOKEN", _sign(priv))
    edition.reset_cache()
    assert edition.current().edition == edition.COMMUNITY


def test_expired_token_env_fails_closed_to_community(tmp_path, monkeypatch):
    monkeypatch.setenv("CENTRALOPS_LICENSE_GRACE_DAYS", "0")  # estrito (sem carência)
    priv = Ed25519PrivateKey.generate()
    _write_pubkey(tmp_path, "k1", priv.public_key())
    now = int(time.time())
    token = _sign(priv, iat=now - 7200, exp=now - 3600)  # expired 1h ago
    monkeypatch.setenv("CENTRALOPS_LICENSE_KEYS_DIR", str(tmp_path))
    monkeypatch.setenv("CENTRALOPS_LICENSE_TOKEN", token)
    edition.reset_cache()
    assert edition.current().edition == edition.COMMUNITY


def test_current_is_cached_and_refresh_rereads(tmp_path, monkeypatch):
    priv = Ed25519PrivateKey.generate()
    _write_pubkey(tmp_path, "k1", priv.public_key())
    monkeypatch.setenv("CENTRALOPS_LICENSE_KEYS_DIR", str(tmp_path))
    edition.reset_cache()
    assert edition.current().edition == edition.COMMUNITY  # no token yet -> cached community
    monkeypatch.setenv("CENTRALOPS_LICENSE_TOKEN", _sign(priv, features=["ha"]))
    assert edition.current().edition == edition.COMMUNITY  # still cached
    assert edition.refresh().is_enterprise  # explicit re-read picks up the new token


# ── GET /api/edition endpoint serialization ────────────────────────────────────

def test_endpoint_community_default():
    out = edition_api.get_edition()
    assert out.edition == edition.COMMUNITY and out.features == []
    assert out.plan is None and out.expires_at is None


def test_endpoint_enterprise_serialization_excludes_customer(tmp_path, monkeypatch):
    priv = Ed25519PrivateKey.generate()
    _write_pubkey(tmp_path, "k1", priv.public_key())
    monkeypatch.setenv("CENTRALOPS_LICENSE_KEYS_DIR", str(tmp_path))
    monkeypatch.setenv("CENTRALOPS_LICENSE_TOKEN", _sign(priv, features=["b", "a"], seats=10))
    edition.reset_cache()
    out = edition_api.get_edition()
    assert out.edition == edition.ENTERPRISE
    assert out.features == ["a", "b"]  # sorted
    assert out.seats == 10 and out.expires_at is not None
    # the commercial customer id (sub) must NOT be exposed by the endpoint
    assert "customer" not in out.model_dump() and "cust_42" not in str(out.model_dump())


def test_router_registers_edition_path():
    assert any(r.path == "/edition" for r in edition_api.router.routes)


def test_enterprise_integrity_ok_for_community(monkeypatch):
    """Community (sem features pagas) é sempre íntegra — não depende de seam do EE."""
    monkeypatch.setattr(edition, "_cached_feature_set", edition.FeatureSet.community())
    monkeypatch.setattr(edition, "_cache_resolved_at", edition._monotonic())
    assert edition.enterprise_integrity_problem() is None


def test_enterprise_integrity_problem_when_multitenant_but_resolver_absent(monkeypatch):
    """licença concede multi_tenant mas o scope resolver do EE não
    está registrado (pacote não ativou) → degradação silenciosa p/ FLAT → fail-loud."""
    from backend.app.core import ee_hooks

    fs = edition.FeatureSet(edition=edition.ENTERPRISE, features=frozenset({"multi_tenant"}))
    monkeypatch.setattr(edition, "_cached_feature_set", fs)
    monkeypatch.setattr(edition, "_cache_resolved_at", edition._monotonic())
    ee_hooks.reset_scope_resolver()  # EE não ativou
    problem = edition.enterprise_integrity_problem()
    assert problem is not None and "centralops_ee" in problem


def test_enterprise_integrity_ok_when_resolver_registered(monkeypatch):
    from backend.app.core import ee_hooks

    fs = edition.FeatureSet(edition=edition.ENTERPRISE, features=frozenset({"reseller"}))
    monkeypatch.setattr(edition, "_cached_feature_set", fs)
    monkeypatch.setattr(edition, "_cache_resolved_at", edition._monotonic())
    ee_hooks.register_scope_resolver(lambda user, session: None)  # conftest reseta após
    assert edition.enterprise_integrity_problem() is None


def test_enterprise_integrity_ok_when_feature_does_not_need_subtree(monkeypatch):
    """Uma licença Enterprise que NÃO concede multi_tenant/reseller não exige o seam de
    subárvore → íntegra mesmo sem resolver registrado."""
    fs = edition.FeatureSet(edition=edition.ENTERPRISE, features=frozenset({"audit_compliance"}))
    monkeypatch.setattr(edition, "_cached_feature_set", fs)
    monkeypatch.setattr(edition, "_cache_resolved_at", edition._monotonic())
    assert edition.enterprise_integrity_problem() is None


def test_worker_init_warms_edition_cache():
    """o boot do worker (sinal worker_init) aquece o cache de edição.

    O worker nunca importa main.py (onde a API faz refresh()); sem este wiring o
    cache ficava frio até a 1ª task chamar feature_enabled(). Sem token no
    ambiente de teste → Community; o ponto é que o cache deixa de ser None.
    Restaura os handlers do root logger ao fim (worker_init também dispara o
    handler de logging JSON do worker)."""
    import logging as _logging

    from celery.signals import worker_init

    import backend.app.collectors.celery_app  # noqa: F401 — registra o receiver

    root = _logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    try:
        edition.reset_cache()
        assert edition._cached_feature_set is None
        worker_init.send(sender=None)
        assert edition._cached_feature_set is not None
        assert edition._cached_feature_set.edition == edition.COMMUNITY
    finally:
        root.handlers[:], root.level = saved_handlers, saved_level
