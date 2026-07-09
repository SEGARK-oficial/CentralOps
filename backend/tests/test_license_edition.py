"""Tests for offline license verification + edition resolution (open-core).

Crypto is exercised end-to-end with an EPHEMERAL Ed25519 keypair generated INSIDE the
test — the private key never touches the repo. Covers: happy path, signature/expiry/nbf/kid/
malformed failures, the alg-pinning downgrade guard, and the fail-closed edition
resolution.
"""
from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from backend.app.core import edition, licensing


def _keypair():
    priv = Ed25519PrivateKey.generate()
    return priv, priv.public_key()


def _sign(priv, *, kid="k1", alg="EdDSA", **claims):
    now = int(time.time())
    payload = {"sub": "cust_1", "plan": "enterprise", "iat": now, "exp": now + 3600}
    payload.update(claims)
    return jwt.encode(payload, priv, algorithm=alg, headers={"kid": kid})


# ── licensing.verify_license ───────────────────────────────────────────────────

def test_valid_token_returns_claims():
    priv, pub = _keypair()
    token = _sign(priv, features=["multi_tenant", "federated_search"], seats=50, jti="t1")
    claims = licensing.verify_license(token, {"k1": pub})
    assert claims.subject == "cust_1"
    assert claims.plan == "enterprise"
    assert claims.features == frozenset({"multi_tenant", "federated_search"})
    assert claims.seats == 50
    assert claims.token_id == "t1"
    assert claims.key_id == "k1"
    assert claims.has_feature("federated_search") and not claims.has_feature("nope")
    assert claims.expires_at is not None and claims.issued_at is not None


def test_max_organizations_claim_roundtrips_to_featureset():
    """Starter single-tenant: o claim max_organizations chega ao FeatureSet."""
    priv, pub = _keypair()
    token = _sign(priv, max_organizations=1)
    claims = licensing.verify_license(token, {"k1": pub})
    assert claims.max_organizations == 1
    assert edition.FeatureSet.from_claims(claims).max_organizations == 1


def test_max_organizations_absent_is_none():
    priv, pub = _keypair()
    claims = licensing.verify_license(_sign(priv), {"k1": pub})
    assert claims.max_organizations is None  # ilimitado quando ausente


@pytest.mark.parametrize("bad", [0, -1, True])
def test_max_organizations_invalid_rejected(bad):
    priv, pub = _keypair()
    token = _sign(priv, max_organizations=bad)
    with pytest.raises(licensing.MalformedLicense):
        licensing.verify_license(token, {"k1": pub})


def test_signed_grace_days_claim_roundtrips():
    """A janela de carência ASSINADA (grace_days) chega às claims — é a fronteira de
    confiança do env-knob no core aberto (o vendor assina, o produto não pode esticar)."""
    priv, pub = _keypair()
    claims = licensing.verify_license(_sign(priv, grace_days=3), {"k1": pub})
    assert claims.grace_days == 3


def test_grace_days_absent_is_none():
    priv, pub = _keypair()
    claims = licensing.verify_license(_sign(priv), {"k1": pub})
    assert claims.grace_days is None  # sem carência assinada → env (limitada por teto)


@pytest.mark.parametrize("bad", [-1, True, "5", 1.5])
def test_grace_days_invalid_rejected(bad):
    """grace_days deve ser inteiro não-negativo; bool (subclasse de int) é rejeitado."""
    priv, pub = _keypair()
    token = _sign(priv, grace_days=bad)
    with pytest.raises(licensing.MalformedLicense):
        licensing.verify_license(token, {"k1": pub})


def test_grace_days_zero_is_valid():
    """grace_days=0 é válido (vendor declara: sem carência) — distinto de ausente."""
    priv, pub = _keypair()
    claims = licensing.verify_license(_sign(priv, grace_days=0), {"k1": pub})
    assert claims.grace_days == 0


def test_wrong_key_fails_signature():
    priv, _pub = _keypair()
    _other_priv, other_pub = _keypair()
    token = _sign(priv)
    with pytest.raises(licensing.InvalidLicenseSignature):
        licensing.verify_license(token, {"k1": other_pub})


def test_unknown_kid_rejected():
    priv, pub = _keypair()
    token = _sign(priv, kid="rotated-out")
    with pytest.raises(licensing.UnknownKeyId):
        licensing.verify_license(token, {"k1": pub})


def test_expired_token_rejected():
    priv, pub = _keypair()
    now = int(time.time())
    token = _sign(priv, iat=now - 7200, exp=now - 3600)  # expired 1h ago
    with pytest.raises(licensing.ExpiredLicense):
        licensing.verify_license(token, {"k1": pub})


def test_not_yet_valid_rejected():
    priv, pub = _keypair()
    now = int(time.time())
    token = _sign(priv, nbf=now + 3600, exp=now + 7200)  # valid only in 1h
    with pytest.raises(licensing.NotYetValidLicense):
        licensing.verify_license(token, {"k1": pub})


def test_missing_exp_is_malformed():
    priv, pub = _keypair()
    now = int(time.time())
    # exp is required; build a token without it
    token = jwt.encode({"sub": "c", "iat": now}, priv, algorithm="EdDSA", headers={"kid": "k1"})
    with pytest.raises(licensing.MalformedLicense):
        licensing.verify_license(token, {"k1": pub})


def test_missing_kid_is_malformed():
    priv, pub = _keypair()
    now = int(time.time())
    token = jwt.encode({"sub": "c", "exp": now + 3600}, priv, algorithm="EdDSA")  # no kid header
    with pytest.raises(licensing.MalformedLicense):
        licensing.verify_license(token, {"k1": pub})


def test_garbage_token_is_malformed():
    _priv, pub = _keypair()
    with pytest.raises(licensing.MalformedLicense):
        licensing.verify_license("not.a.jwt", {"k1": pub})


def test_empty_token_is_malformed():
    _priv, pub = _keypair()
    with pytest.raises(licensing.MalformedLicense):
        licensing.verify_license("", {"k1": pub})


def test_alg_downgrade_is_rejected():
    # An attacker re-signs with HS256 using the public key bytes as the HMAC secret.
    # Our verifier pins EdDSA and must reject before/at decode.
    _priv, pub = _keypair()
    pub_bytes = pub.public_bytes_raw()
    now = int(time.time())
    forged = jwt.encode(
        {"sub": "attacker", "exp": now + 3600}, pub_bytes, algorithm="HS256",
        headers={"kid": "k1"},
    )
    with pytest.raises(licensing.LicenseError):
        licensing.verify_license(forged, {"k1": pub})


def test_features_must_be_list():
    priv, pub = _keypair()
    token = _sign(priv, features="multi_tenant")  # string, not list
    with pytest.raises(licensing.MalformedLicense):
        licensing.verify_license(token, {"k1": pub})


# ── edition.resolve_edition (fail-closed) ──────────────────────────────────────

def test_no_token_is_community():
    fs = edition.resolve_edition(None, {})
    assert fs.edition == edition.COMMUNITY
    assert not fs.is_enterprise
    assert fs.features == frozenset()
    assert not fs.feature_enabled("federated_search")


def test_empty_keyring_is_community():
    priv, _pub = _keypair()
    token = _sign(priv)
    assert edition.resolve_edition(token, {}).edition == edition.COMMUNITY


def test_valid_token_is_enterprise():
    priv, pub = _keypair()
    token = _sign(priv, plan="mssp", features=["multi_tenant", "reseller"], seats=10)
    fs = edition.resolve_edition(token, {"k1": pub})
    assert fs.is_enterprise
    assert fs.plan == "mssp" and fs.seats == 10 and fs.customer == "cust_1"
    assert fs.feature_enabled("multi_tenant") and fs.feature_enabled("reseller")
    assert not fs.feature_enabled("audit_compliance")


def test_invalid_token_fails_closed_to_community():
    priv, _pub = _keypair()
    _other, other_pub = _keypair()
    token = _sign(priv)  # signed by priv, verified against other_pub -> bad signature
    assert edition.resolve_edition(token, {"k1": other_pub}).edition == edition.COMMUNITY


def test_expired_token_fails_closed_to_community():
    priv, pub = _keypair()
    now = int(time.time())
    token = _sign(priv, iat=now - 7200, exp=now - 3600)
    assert edition.resolve_edition(token, {"k1": pub}).edition == edition.COMMUNITY


# ── hardening: fail-closed against non-LicenseError paths (audit follow-ups) ────

def test_out_of_range_timestamp_is_malformed_and_fails_closed():
    # A validly-signed token with a ms-vs-s exp bug (exp ~ now*1000 -> year out of
    # range) must normalize to MalformedLicense, and resolve_edition must NOT crash.
    priv, pub = _keypair()
    token = _sign(priv, exp=int(time.time()) * 1000)
    with pytest.raises(licensing.MalformedLicense):
        licensing.verify_license(token, {"k1": pub})
    assert edition.resolve_edition(token, {"k1": pub}).edition == edition.COMMUNITY


def test_wrong_keyring_value_type_is_malformed_and_fails_closed():
    # Misconfigured keyring (raw bytes instead of an Ed25519PublicKey).
    priv, _pub = _keypair()
    token = _sign(priv)
    with pytest.raises(licensing.MalformedLicense):
        licensing.verify_license(token, {"k1": b"not-a-key"})
    assert edition.resolve_edition(token, {"k1": b"not-a-key"}).edition == edition.COMMUNITY


def test_non_mapping_keyring_fails_closed_without_crashing():
    # A non-empty list is truthy -> passes the guard -> keyring.get raises
    # AttributeError inside verify_license; resolve_edition's broad except must
    # fail-closed to Community and never propagate.
    priv, _pub = _keypair()
    token = _sign(priv)
    assert edition.resolve_edition(token, ["not-a-mapping"]).edition == edition.COMMUNITY


def test_non_string_feature_element_is_malformed():
    priv, pub = _keypair()
    token = _sign(priv, features=["ok", {"nested": "dict"}, 123])
    with pytest.raises(licensing.MalformedLicense):
        licensing.verify_license(token, {"k1": pub})


def test_bool_seats_is_rejected():
    priv, pub = _keypair()
    token = _sign(priv, seats=True)  # bool is an int subclass — must be rejected
    with pytest.raises(licensing.MalformedLicense):
        licensing.verify_license(token, {"k1": pub})
