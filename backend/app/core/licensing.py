"""Offline license verification (open-core).

The product (Community/EE) verifies a signed license JWT with an embedded Ed25519
**public** key — fully offline, air-gapped-safe, no call-home. It NEVER holds a
private key. The
license is a **feature-gate, not DRM**: the AGPL core is recompilable, so the real
protection is that EE code is absent from the Community artifact.
This module exists for legitimate customers, compliance and billing — not anti-piracy.

Algorithm: EdDSA (Ed25519) JWT. The token header carries ``kid`` so the verifier
selects the public key from a *keyring* — enabling key rotation without breaking
already-issued tokens (and revoking a leaked key by dropping it from the keyring in
the next product update). pyjwt validates the signature, ``exp`` and ``nbf``; this
module selects the key by ``kid``, requires ``exp``+``sub``, and normalizes every
failure into a :class:`LicenseError` so callers can fail-closed deterministically.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from typing import Mapping, Optional

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

#: Only EdDSA is accepted. Pinning the algorithm prevents an ``alg`` confusion /
#: downgrade attack (e.g. a forged ``alg=none`` or HMAC token verified with a public
#: key as the HMAC secret). NEVER widen this list.
ALLOWED_ALGORITHMS = ["EdDSA"]


class LicenseError(Exception):
    """Base class for every license-verification failure."""


class MalformedLicense(LicenseError):
    """Token is not a well-formed/complete EdDSA JWT (or missing required claims)."""


class UnknownKeyId(LicenseError):
    """The token's ``kid`` is not present in the keyring (unknown/rotated-out key)."""


class InvalidLicenseSignature(LicenseError):
    """Signature does not verify against the selected public key."""


class ExpiredLicense(LicenseError):
    """``exp`` is in the past (beyond leeway)."""


class NotYetValidLicense(LicenseError):
    """``nbf`` is in the future (beyond leeway)."""


class RevokedLicense(LicenseError):
    """Token's ``jti`` is in the offline revocation list (vendor-revoked before exp)."""


def _to_utc(value: Optional[int]) -> Optional[datetime]:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (ValueError, OverflowError, OSError, TypeError) as exc:
        # Out-of-range/garbage timestamp from a (validly-signed) token must NOT
        # escape as a raw ValueError/OverflowError — normalize to a LicenseError so
        # callers fail-closed instead of crashing.
        raise MalformedLicense(f"invalid timestamp claim {value!r}") from exc


@dataclasses.dataclass(frozen=True)
class LicenseClaims:
    """Validated, immutable view of a license token's claims.

    ``subject`` is the COMMERCIAL customer id (``sub``) — NOT a product-local
    ``Organization``; the issuer does not know the per-deploy org at issuance
    time, so the gate is per-deploy/per-customer, not per-internal-org.
    """

    subject: str
    plan: str
    features: frozenset[str]
    seats: Optional[int]
    issued_at: Optional[datetime]
    not_before: Optional[datetime]
    expires_at: Optional[datetime]
    key_id: str
    token_id: Optional[str]  # jti
    # Teto de organizações (root) do tier — Starter = single-tenant = 1; MSSP/Enterprise
    # = None (ilimitado, ou controlado por PartnerProgram.max_child_orgs). Enforçado na
    # criação de org. Default None mantém compat com tokens/tests sem o claim.
    max_organizations: Optional[int] = None
    # Janela de carência ASSINADA (dias) — quantos dias após o ``exp`` o produto pode
    # honrar a licença antes de reverter p/ Community. É a fronteira de confiança REAL: o
    # cliente só consegue ENCURTAR via env, nunca estender além do que o vendor assinou
    # (o env-knob no core aberto não é enforcement — o ``exp`` assinado + este claim são).
    # Default None = sem carência assinada (o env-knob, limitado por um teto de código, vale).
    grace_days: Optional[int] = None

    def has_feature(self, name: str) -> bool:
        return name in self.features

    @classmethod
    def _from_payload(cls, payload: Mapping[str, object], kid: str) -> "LicenseClaims":
        raw_features = payload.get("features") or []
        if not isinstance(raw_features, (list, tuple)):
            raise MalformedLicense("'features' claim must be a list")
        if not all(isinstance(f, str) for f in raw_features):
            raise MalformedLicense("'features' must be a list of strings")
        seats = payload.get("seats")
        # bool is an int subclass — exclude it so seats=True is not a seat count.
        if seats is not None and (not isinstance(seats, int) or isinstance(seats, bool)):
            raise MalformedLicense("'seats' claim must be an integer")
        max_orgs = payload.get("max_organizations")
        if max_orgs is not None and (
            not isinstance(max_orgs, int) or isinstance(max_orgs, bool) or max_orgs < 1
        ):
            raise MalformedLicense("'max_organizations' claim must be a positive integer")
        grace_days = payload.get("grace_days")
        if grace_days is not None and (
            not isinstance(grace_days, int) or isinstance(grace_days, bool) or grace_days < 0
        ):
            raise MalformedLicense("'grace_days' claim must be a non-negative integer")
        return cls(
            subject=str(payload.get("sub") or ""),
            plan=str(payload.get("plan") or ""),
            features=frozenset(raw_features),
            seats=seats,
            issued_at=_to_utc(payload.get("iat")),  # type: ignore[arg-type]
            not_before=_to_utc(payload.get("nbf")),  # type: ignore[arg-type]
            expires_at=_to_utc(payload.get("exp")),  # type: ignore[arg-type]
            key_id=kid,
            token_id=(str(payload["jti"]) if payload.get("jti") is not None else None),
            max_organizations=max_orgs,
            grace_days=grace_days,
        )


def verify_license(
    token: str,
    keyring: Mapping[str, Ed25519PublicKey],
    *,
    leeway_seconds: int = 60,
    revoked_token_ids: frozenset[str] = frozenset(),
) -> LicenseClaims:
    """Verify a license JWT **offline** and return its claims, or raise LicenseError.

    ``keyring`` maps ``kid`` -> Ed25519 public key. The algorithm is pinned to EdDSA;
    ``exp`` and ``sub`` are required; ``exp``/``nbf`` are validated with ``leeway``.
    No network access, no private key.

    ``revoked_token_ids`` is an optional set of revoked ``jti`` (from a signed offline
    revocation list — see ``edition.load_revocation_list``): a token whose ``jti`` is in
    it raises :class:`RevokedLicense` (a ``LicenseError`` → the resolver fails closed to
    Community). The signature/expiry are still checked FIRST, so revocation only applies
    to an otherwise-valid token.
    """
    if not isinstance(token, str) or not token:
        raise MalformedLicense("empty or non-string token")

    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as exc:  # malformed header/segments
        raise MalformedLicense(f"unreadable token header: {exc}") from exc

    if header.get("alg") not in ALLOWED_ALGORITHMS:
        # Defense-in-depth: reject before key selection. jwt.decode also enforces
        # algorithms=, but failing fast on a downgrade attempt is clearer.
        raise MalformedLicense(f"unexpected alg: {header.get('alg')!r}")

    kid = header.get("kid")
    if not kid:
        raise MalformedLicense("missing 'kid' in token header")
    public_key = keyring.get(kid)
    if public_key is None:
        raise UnknownKeyId(f"unknown key id: {kid!r}")
    if not isinstance(public_key, Ed25519PublicKey):
        # Misconfigured keyring (e.g. PEM bytes / RSA key) would make pyjwt raise
        # InvalidKeyError (NOT an InvalidTokenError). Reject as a LicenseError so the
        # caller fails-closed instead of crashing.
        raise MalformedLicense(
            f"keyring entry for kid {kid!r} is not an Ed25519 public key"
        )

    try:
        payload = jwt.decode(
            token,
            public_key,
            algorithms=ALLOWED_ALGORITHMS,
            leeway=leeway_seconds,
            options={"require": ["exp", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise ExpiredLicense(str(exc)) from exc
    except jwt.ImmatureSignatureError as exc:
        raise NotYetValidLicense(str(exc)) from exc
    except jwt.InvalidSignatureError as exc:
        raise InvalidLicenseSignature(str(exc)) from exc
    except jwt.InvalidTokenError as exc:  # missing required claim, bad format, etc.
        raise MalformedLicense(str(exc)) from exc
    except Exception as exc:  # normalize ANY other pyjwt/crypto error -> LicenseError
        raise MalformedLicense(f"license decode failed: {exc}") from exc

    # _from_payload may raise MalformedLicense (bad timestamp / non-string features) —
    # already a LicenseError, so it propagates correctly to fail-closed callers.
    claims = LicenseClaims._from_payload(payload, str(kid))

    # Offline revocation: a signature-valid, unexpired token can still be revoked by the
    # vendor (refund/cancel/leak). Checked LAST — only a token that would otherwise be
    # honored is rejected here.
    if revoked_token_ids and claims.token_id and claims.token_id in revoked_token_ids:
        raise RevokedLicense(f"license token id {claims.token_id!r} is revoked")

    return claims
