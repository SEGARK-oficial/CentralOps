"""Offline license revocation.

A signature-valid, unexpired token can still be REVOKED by the vendor via a signed
offline revocation list (JWS, same keyring). The product verifies the list offline and
rejects revoked jtis — fail-closed to Community, fail-safe on a bad/tampered list.

Imports use ``backend.app.*`` (compiled .so dual-root gotcha).
"""
from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
)

from backend.app.core import edition, licensing


def _kp():
    priv = Ed25519PrivateKey.generate()
    return priv, priv.public_key()


def _token(priv, *, jti="JTI_A", kid="k1", exp_delta=3600, sub="cust"):
    now = int(time.time())
    return jwt.encode(
        {"sub": sub, "plan": "enterprise", "features": ["multi_tenant"],
         "iat": now, "exp": now + exp_delta, "jti": jti},
        priv, algorithm="EdDSA", headers={"kid": kid},
    )


def _revlist(priv, jtis, *, kid="k1", exp_delta=90 * 86400):
    now = int(time.time())
    return jwt.encode(
        {"typ": "revocation", "revoked_jti": list(jtis), "iat": now, "exp": now + exp_delta},
        priv, algorithm="EdDSA", headers={"kid": kid},
    )


# ── verify_license ──────────────────────────────────────────────────────────

def test_verify_raises_revoked_when_jti_listed():
    priv, pub = _kp()
    tok = _token(priv, jti="X")
    with pytest.raises(licensing.RevokedLicense):
        licensing.verify_license(tok, {"k1": pub}, revoked_token_ids=frozenset({"X"}))


def test_verify_ok_when_other_jti_revoked():
    priv, pub = _kp()
    tok = _token(priv, jti="X")
    claims = licensing.verify_license(tok, {"k1": pub}, revoked_token_ids=frozenset({"Y"}))
    assert claims.token_id == "X"


def test_revocation_checked_after_signature_and_expiry():
    # an EXPIRED revoked token raises ExpiredLicense (signature/expiry first), not Revoked
    priv, pub = _kp()
    tok = _token(priv, jti="X", exp_delta=-100)
    with pytest.raises(licensing.ExpiredLicense):
        licensing.verify_license(tok, {"k1": pub}, revoked_token_ids=frozenset({"X"}))


# ── resolve_edition ─────────────────────────────────────────────────────────

def test_resolve_revoked_token_is_community():
    priv, pub = _kp()
    tok = _token(priv, jti="X")
    fs = edition.resolve_edition(tok, {"k1": pub}, revoked_token_ids=frozenset({"X"}))
    assert fs.edition == edition.COMMUNITY


def test_resolve_unrevoked_token_is_enterprise():
    priv, pub = _kp()
    tok = _token(priv, jti="X")
    fs = edition.resolve_edition(tok, {"k1": pub}, revoked_token_ids=frozenset({"Z"}))
    assert fs.is_enterprise


def test_revocation_beats_grace():
    # revoked AND expired within the grace window → still Community (revocation wins)
    priv, pub = _kp()
    tok = _token(priv, jti="X", exp_delta=-100)
    fs = edition.resolve_edition(
        tok, {"k1": pub}, grace_seconds=7 * 86400, revoked_token_ids=frozenset({"X"})
    )
    assert fs.edition == edition.COMMUNITY


# ── load_revocation_list ────────────────────────────────────────────────────

def test_load_revocation_list_verifies_and_extracts(monkeypatch):
    priv, pub = _kp()
    monkeypatch.setenv("CENTRALOPS_LICENSE_REVOCATIONS", _revlist(priv, ["A", "B", ""]))
    assert edition.load_revocation_list({"k1": pub}) == frozenset({"A", "B"})


def test_load_revocation_list_ignores_tampered(monkeypatch):
    priv, pub = _kp()
    attacker, _ = _kp()  # product only knows `pub`
    monkeypatch.setenv("CENTRALOPS_LICENSE_REVOCATIONS", _revlist(attacker, ["A"]))
    assert edition.load_revocation_list({"k1": pub}) == frozenset()


def test_load_revocation_list_ignores_unknown_kid(monkeypatch):
    priv, pub = _kp()
    monkeypatch.setenv("CENTRALOPS_LICENSE_REVOCATIONS", _revlist(priv, ["A"], kid="other"))
    assert edition.load_revocation_list({"k1": pub}) == frozenset()


def test_load_revocation_list_absent_and_garbage(monkeypatch):
    priv, pub = _kp()
    monkeypatch.delenv("CENTRALOPS_LICENSE_REVOCATIONS", raising=False)
    assert edition.load_revocation_list({"k1": pub}) == frozenset()
    monkeypatch.setenv("CENTRALOPS_LICENSE_REVOCATIONS", "not-a-jws")
    assert edition.load_revocation_list({"k1": pub}) == frozenset()


def test_expired_revocation_list_still_applies(monkeypatch):
    # a stale (expired) revocation list must NOT silently un-revoke → still applied
    priv, pub = _kp()
    monkeypatch.setenv("CENTRALOPS_LICENSE_REVOCATIONS", _revlist(priv, ["A"], exp_delta=-10))
    assert edition.load_revocation_list({"k1": pub}) == frozenset({"A"})


# ── refresh() end-to-end via env ────────────────────────────────────────────

def test_refresh_downgrades_revoked_token(monkeypatch, tmp_path):
    priv, pub = _kp()
    (tmp_path / "k1.pem").write_bytes(
        pub.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    )
    tok = _token(priv, jti="REVME")
    monkeypatch.setenv("CENTRALOPS_LICENSE_KEYS_DIR", str(tmp_path))
    monkeypatch.setenv("CENTRALOPS_LICENSE_TOKEN", tok)
    monkeypatch.setenv("CENTRALOPS_LICENSE_REVOCATIONS", _revlist(priv, ["REVME"]))
    try:
        assert edition.refresh().edition == edition.COMMUNITY
    finally:
        edition.reset_cache()
