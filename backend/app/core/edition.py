"""Edition resolution (open-core).

Derives the running **edition** (``community`` | ``enterprise``) and its
``FeatureSet`` from a verified license token. **Fail-closed to Community**: any
absence, expiry, bad signature, unknown key, or malformed token resolves to
Community with NO features — never raises into the boot path, never grants on doubt.

The edition is **per-deploy** (the license binds to a commercial customer, not to a
product-local ``Organization``). Consumers ask
``feature_enabled("federated_search")`` — the actual EE *code* for a gated feature
only exists in the ``centralops_ee`` artifact, so this gate is the
runtime half of the seam, not the protection itself.

This module is pure (no env/DB/IO): :func:`resolve_edition` takes the token + keyring
explicitly. Boot wiring (reading the token from env/file, loading the public keyring,
caching the current edition) lands in a later increment.
"""
from __future__ import annotations

import dataclasses
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Mapping, Optional

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from .licensing import (
    ALLOWED_ALGORITHMS,
    ExpiredLicense,
    LicenseClaims,
    LicenseError,
    RevokedLicense,
    verify_license,
)

logger = logging.getLogger(__name__)

COMMUNITY = "community"
ENTERPRISE = "enterprise"

# Teto ABSOLUTO da janela de carência (dias), no CÓDIGO. Um deploy Community (AGPL,
# recompilável) pode setar ``CENTRALOPS_LICENSE_GRACE_DAYS`` alto ou editar o código —
# mas o env-knob é TUNING, não enforcement: aqui ele é limitado a este teto e, se a
# licença trouxer um ``grace_days`` ASSINADO, o efetivo = min(env, assinado, teto). A
# fronteira real de confiança é o ``exp`` assinado (não move sem a chave privada) + o
# claim assinado; este teto só impede que uma env mal configurada crie um bypass grande.
_MAX_GRACE_DAYS = 45


@dataclasses.dataclass(frozen=True)
class FeatureSet:
    """Immutable description of what the running edition may do."""

    edition: str
    features: frozenset[str]
    plan: Optional[str] = None
    seats: Optional[int] = None
    # Teto de organizações (root) do tier. None = ilimitado (Community e tiers sem teto).
    # Starter = 1 (single-tenant). Enforçado na criação de org.
    max_organizations: Optional[int] = None
    expires_at: Optional[datetime] = None
    # Commercial customer id (the license ``sub``) — INTERNAL only. Never serialize to
    # client-facing APIs (GET /api/edition deliberately omits it).
    customer: Optional[str] = None
    # True quando o ``exp`` já passou mas a licença ainda é honrada pela JANELA DE
    # CARÊNCIA (CENTRALOPS_LICENSE_GRACE_DAYS) — UX de renovação atrasada. A UI usa
    # isto p/ alertar "expirada, em carência"; após a carência → Community (hard).
    expired_in_grace: bool = False

    @classmethod
    def community(cls) -> "FeatureSet":
        """The default, safe edition: no paid features.

        ``max_organizations`` is INTENTIONALLY left at its ``None`` default — the
        AGPL Community core has NO org cap (the cap is a paid Starter feature).
        Do not set it here: a non-None value would silently impose a tier limit on
        every Community install (breaks the fail-closed-to-Community invariant)."""
        return cls(edition=COMMUNITY, features=frozenset())

    @classmethod
    def from_claims(cls, claims: LicenseClaims) -> "FeatureSet":
        return cls(
            edition=ENTERPRISE,
            features=claims.features,
            plan=claims.plan or None,
            seats=claims.seats,
            max_organizations=claims.max_organizations,
            expires_at=claims.expires_at,
            customer=claims.subject or None,
        )

    @property
    def is_enterprise(self) -> bool:
        return self.edition == ENTERPRISE

    def feature_enabled(self, name: str) -> bool:
        """True only when running Enterprise AND the feature is granted."""
        return name in self.features


def activate_enterprise(app) -> bool:
    """Discover and activate the proprietary Enterprise package, if installed.

    Optional discovery seam (open-core): imports ``centralops_ee`` and calls
    its ``activate(app)`` so it can register routers/services/resolvers on top of the
    Community core. Returns ``True`` when the EE package is present and activated,
    ``False`` for Community (package absent).

    Only ``ImportError`` (absence) is swallowed. If the EE package IS present but its
    ``activate`` raises, that error propagates — a misconfigured paid edition must
    fail loud, not silently degrade to Community. The Core never imports the EE by
    name beyond this single guarded hook; the dependency arrow is always EE -> Core.
    Call once at app construction, in the uvicorn process only (the Celery worker and
    the migrate step never import ``main``).
    """
    try:
        import centralops_ee  # type: ignore  # noqa: PLC0415 (lazy, optional EE)
    except ImportError:
        logger.debug("centralops_ee not installed; running Community edition")
        return False
    centralops_ee.activate(app)
    logger.info("centralops_ee activated (Enterprise edition)")
    return True


def resolve_edition(
    token: Optional[str],
    keyring: Mapping[str, Ed25519PublicKey],
    *,
    grace_seconds: int = 0,
    revoked_token_ids: frozenset[str] = frozenset(),
) -> FeatureSet:
    """Resolve a :class:`FeatureSet` from a license token. **Fail-closed to Community.**

    - No token or empty keyring (e.g. Community install, or no public key shipped) ->
      Community.
    - Any verification error (expired/invalid/unknown-kid/malformed/REVOKED) -> Community,
      logged at WARNING. Never raises.
    - Valid token -> Enterprise with the claimed features.
    - ``grace_seconds`` (janela de carência de renovação): um token EXPIRADO — e apenas
      expirado; assinatura/kid/claims continuam estritos — ainda é honrado por até
      ``grace_seconds`` após o ``exp``, marcado ``expired_in_grace=True`` e logado em
      ERROR. Passada a carência → Community (hard). ``0`` = sem carência.
    - ``revoked_token_ids``: jtis da lista de revogação assinada (offline). Um token
      revogado — mesmo íntegro/vigente — cai para Community. Revogação vence a carência.
    """
    if not token or not keyring:
        return FeatureSet.community()
    try:
        claims = verify_license(token, keyring, revoked_token_ids=revoked_token_ids)
    except ExpiredLicense as exc:
        if grace_seconds <= 0:
            logger.warning(
                "license verification failed; running as Community edition: %s", exc
            )
            return FeatureSet.community()
        cap_seconds = _MAX_GRACE_DAYS * 86400
        try:
            # Re-verifica tolerando o ``exp`` até o TETO de código — SÓ o exp ganha
            # tolerância; assinatura/alg/kid/claims/REVOGAÇÃO seguem estritos (um token
            # revogado E expirado cai para Community: revogação vence a carência).
            claims = verify_license(
                token, keyring, leeway_seconds=60 + cap_seconds,
                revoked_token_ids=revoked_token_ids,
            )
        except RevokedLicense as rexc:
            # Revogação vence a carência — e é security-relevant, então loga distinto.
            logger.warning(
                "license is REVOKED (was also expired); running as Community edition: %s",
                rexc,
            )
            return FeatureSet.community()
        except LicenseError:
            logger.warning(
                "license expired beyond the max grace cap (%dd); running as Community "
                "edition: %s", _MAX_GRACE_DAYS, exc,
            )
            return FeatureSet.community()
        except Exception as inner:  # noqa: BLE001 — fail-closed, never raises
            logger.error(
                "unexpected error during grace re-verification; running as "
                "Community edition: %r", inner,
            )
            return FeatureSet.community()
        # Carência EFETIVA: o env-knob só ENCURTA; um ``grace_days`` ASSINADO na licença
        # encurta mais ainda (vendor manda); tudo limitado pelo teto de código. Assim a
        # env do core aberto nunca cria bypass grande — o exp assinado é o relógio real.
        allowed = min(grace_seconds, cap_seconds)
        if claims.grace_days is not None:
            allowed = min(allowed, claims.grace_days * 86400)
        exp = claims.expires_at
        overdue = (
            (datetime.now(timezone.utc) - exp).total_seconds() if exp else float("inf")
        )
        if overdue > allowed:
            logger.warning(
                "license expired %.1fd ago, beyond the effective grace window (%.1fd); "
                "running as Community edition", overdue / 86400.0, allowed / 86400.0,
            )
            return FeatureSet.community()
        fs = dataclasses.replace(FeatureSet.from_claims(claims), expired_in_grace=True)
        logger.error(
            "LICENÇA EXPIRADA — em JANELA DE CARÊNCIA (vencida há %.1fd, carência %.1fd): "
            "renove para não perder os recursos Enterprise; após a carência o sistema "
            "reverte para Community. plan=%s expirou_em=%s",
            overdue / 86400.0, allowed / 86400.0, fs.plan,
            fs.expires_at.isoformat() if fs.expires_at else "?",
        )
        return fs
    except LicenseError as exc:
        logger.warning(
            "license verification failed; running as Community edition: %s", exc
        )
        return FeatureSet.community()
    except Exception as exc:  # noqa: BLE001 — fail-closed: license handling must
        # NEVER crash the boot/request path. Any unexpected error (misconfigured
        # keyring, library bug, non-Mapping keyring) degrades to Community.
        logger.error(
            "unexpected error verifying license; running as Community edition: %r",
            exc,
        )
        return FeatureSet.community()
    return FeatureSet.from_claims(claims)


# ── runtime loading: bundled public keyring + token from env/file ──────────────
# Production embeds the Ed25519 PUBLIC key(s) under ``license_keys/``
# as ``<kid>.pem``; the directory ships empty (-> Community). The license token is
# pasted by the operator via env/file (never call-home). All loading is fail-safe.

_ENV_TOKEN = "CENTRALOPS_LICENSE_TOKEN"
_ENV_TOKEN_FILE = "CENTRALOPS_LICENSE_TOKEN_FILE"
_ENV_KEYS_DIR = "CENTRALOPS_LICENSE_KEYS_DIR"
# Lista de revogação offline ASSINADA (JWS EdDSA emitido com a mesma
# chave do token). Entregue junto do
# keyring pelo operador (env com o JWS, ou arquivo). Ausente → sem revogações.
_ENV_REVOCATIONS = "CENTRALOPS_LICENSE_REVOCATIONS"
_ENV_REVOCATIONS_FILE = "CENTRALOPS_LICENSE_REVOCATIONS_FILE"
# TTL do cache da edição (s). A cada intervalo o próximo ``current()`` re-resolve —
# assim EXPIRAÇÃO (downgrade) e ATIVAÇÃO nova são pegas em runtime, sem restart e sem
# task de beat (o TTL cobre uniformemente API + workers). 0 = desliga (cache eterno,
# comportamento antigo). O custo é 1 re-resolução (query DB + verify local, ~ms) por
# intervalo por processo — não por request.
_ENV_REFRESH_SECONDS = "CENTRALOPS_EDITION_REFRESH_SECONDS"
_DEFAULT_REFRESH_SECONDS = 300
# Janela de carência pós-``exp`` (dias): honra a licença expirada por N dias com alerta
# ERROR + ``expired_in_grace=True`` (UX de renovação atrasada — procurement é lento),
# depois reverte hard para Community. 0 = estrito. SÓ o exp ganha carência; assinatura/
# kid/claims continuam estritos (não é bypass de verificação).
_ENV_GRACE_DAYS = "CENTRALOPS_LICENSE_GRACE_DAYS"
_DEFAULT_GRACE_DAYS = 7
_BUNDLED_KEYS_DIR = Path(__file__).resolve().parent / "license_keys"

_cache_lock = threading.Lock()
_cached_feature_set: Optional[FeatureSet] = None
_cache_resolved_at: Optional[float] = None
# Indireção p/ testes congelarem/avançarem o relógio do TTL sem sleep.
_monotonic = time.monotonic

# ── Diagnóstico do keyring (dedup de logs) ─────────────────────────────────────
# ``load_keyring`` roda a cada ativação e a cada refresh TTL — logar INFO toda vez
# viraria spam. Guardamos o ÚLTIMO conjunto de kids carregado (module-level) e só
# logamos INFO quando ele MUDA (inclui o 1º load e a transição p/ vazio). O WARNING
# "token presente + keyring vazio" (refresh) usa dedup por transição de estado.
_last_keyring_kids: Optional[frozenset[str]] = None
_warned_token_without_keyring = False


def _env_int(name: str, default: int) -> int:
    """Knob inteiro de env, fail-safe: valor inválido/negativo → default."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        logger.warning("%s inválido (%r) — usando default %s", name, raw, default)
        return default
    return value if value >= 0 else default


def _refresh_interval_seconds() -> int:
    return _env_int(_ENV_REFRESH_SECONDS, _DEFAULT_REFRESH_SECONDS)


def _grace_seconds() -> int:
    return _env_int(_ENV_GRACE_DAYS, _DEFAULT_GRACE_DAYS) * 86400


def _keys_dir() -> Path:
    override = os.environ.get(_ENV_KEYS_DIR)
    return Path(override) if override else _BUNDLED_KEYS_DIR


def load_keyring(directory: Optional[Path] = None) -> Dict[str, Ed25519PublicKey]:
    """Load the Ed25519 public-key keyring from ``<kid>.pem`` files in a directory.

    The file stem is the ``kid``. Non-Ed25519 or unreadable files are skipped with a
    WARNING (fail-safe — a bad key is never trusted and never crashes boot). Returns
    an empty keyring when the directory is absent (-> Community).

    Diagnóstico: SEMPRE loga o resultado em DEBUG (N chaves + kids + dir) e loga em
    INFO quando o conjunto de kids MUDOU desde o último load — inclui o 1º load e a
    transição para vazio (o clássico "unknown key id" por keyring vazio/overlay
    ausente). Sem spam no refresh TTL: kids iguais → só DEBUG.
    """
    global _last_keyring_kids
    directory = directory or _keys_dir()
    keyring: Dict[str, Ed25519PublicKey] = {}
    if directory.is_dir():
        for pem_path in sorted(directory.glob("*.pem")):
            try:
                key = load_pem_public_key(pem_path.read_bytes())
            except Exception as exc:  # noqa: BLE001
                logger.warning("skipping unreadable license public key %s: %r", pem_path.name, exc)
                continue
            if not isinstance(key, Ed25519PublicKey):
                logger.warning("skipping non-Ed25519 license public key %s", pem_path.name)
                continue
            keyring[pem_path.stem] = key
    kids = frozenset(keyring)
    logger.debug(
        "license keyring: loaded %d key(s) %s from %s",
        len(keyring), sorted(kids), directory,
    )
    if kids != _last_keyring_kids:
        if keyring:
            logger.info(
                "license keyring: loaded %d key(s) %s from %s",
                len(keyring), sorted(kids), directory,
            )
        else:
            logger.info(
                "license keyring: 0 keys loaded from %s — activation/verification "
                "will fail with unknown key id", directory,
            )
        _last_keyring_kids = kids
    return keyring


def _load_revocation_jws() -> Optional[str]:
    raw = os.environ.get(_ENV_REVOCATIONS)
    if raw and raw.strip():
        return raw.strip()
    path_env = os.environ.get(_ENV_REVOCATIONS_FILE)
    if path_env:
        path = Path(path_env)
        if path.is_file():
            try:
                return path.read_text(encoding="utf-8").strip() or None
            except OSError as exc:
                logger.warning("could not read revocation list file %s: %r", path_env, exc)
    return None


def load_revocation_list(keyring: Mapping[str, Ed25519PublicKey]) -> frozenset[str]:
    """Carrega + VERIFICA a lista de revogação offline assinada → conjunto de ``jti``.

    A lista é um JWS assinado com a MESMA chave do vendor (``kid`` → keyring), verificado
    OFFLINE (EdDSA pinado, assinatura conferida). O ``exp`` DA LISTA é IGNORADO de
    propósito — uma lista expirada não pode DES-revogar em silêncio (frescor é
    responsabilidade operacional; a fronteira real ainda é o ``exp`` de cada token).
    **Fail-safe:** ausente/não-assinada/adulterada/kid-desconhecido → conjunto VAZIO
    (sem revogações), logado. Nunca levanta — nunca revoga por engano nem crasha o boot.
    """
    jws = _load_revocation_jws()
    if not jws or not keyring:
        return frozenset()
    try:
        header = jwt.get_unverified_header(jws)
        if header.get("alg") not in ALLOWED_ALGORITHMS:
            logger.warning("revocation list: alg inesperado %r; ignorando", header.get("alg"))
            return frozenset()
        kid = header.get("kid")
        public_key = keyring.get(kid) if kid else None
        if public_key is None or not isinstance(public_key, Ed25519PublicKey):
            logger.warning("revocation list: kid desconhecido/inválido %r; ignorando", kid)
            return frozenset()
        payload = jwt.decode(
            jws, public_key, algorithms=ALLOWED_ALGORITHMS, options={"verify_exp": False}
        )
    except Exception as exc:  # noqa: BLE001 — fail-safe: sem revogações, nunca crasha
        logger.warning(
            "revocation list verification failed; ignoring (no revocations): %r", exc
        )
        return frozenset()
    raw = payload.get("revoked_jti")
    if not isinstance(raw, (list, tuple)):
        return frozenset()
    return frozenset(str(j) for j in raw if isinstance(j, str) and j)


def _load_token() -> Optional[str]:
    # DB-first: a license activated via the UI is persisted ENCRYPTED in the DB
    # (``license_config`` singleton) and is the source of truth — it survives restarts
    # and carries an audit trail. Env/file remain as fallback for bootstrap / air-gapped
    # / compose deploys. Fail-safe: any DB/crypto error (e.g. table absent at early boot)
    # falls through to env/file and never breaks edition resolution.
    try:
        from .license_store import load_active_token  # noqa: PLC0415 — lazy: no DB import at module load
        db_token = load_active_token()
        if db_token and db_token.strip():
            return db_token.strip()
    except Exception as exc:  # noqa: BLE001 — fail-safe to env/file
        logger.warning("DB license lookup failed; falling back to env/file: %r", exc)
    token = os.environ.get(_ENV_TOKEN)
    if token and token.strip():
        return token.strip()
    token_file = os.environ.get(_ENV_TOKEN_FILE)
    if token_file:
        path = Path(token_file)
        if path.is_file():
            try:
                return path.read_text(encoding="utf-8").strip() or None
            except OSError as exc:
                logger.warning("could not read license token file %s: %r", token_file, exc)
    return None


def refresh() -> FeatureSet:
    """(Re)resolve the current edition from the DB/env/file token + keyring and cache
    it. Fail-closed (resolve_edition never raises). Call at boot, after a license
    update, and automatically via the TTL in :func:`current`."""
    global _cached_feature_set, _cache_resolved_at, _warned_token_without_keyring
    keyring = load_keyring()
    token = _load_token()
    if token and not keyring:
        # Diagnóstico do incidente clássico "unknown key id": há token configurado
        # (DB/env/arquivo) mas NENHUMA chave pública carregou (overlay/mount/permissão).
        # Dedup por transição de estado — loga quando o estado surge, não a cada TTL.
        if not _warned_token_without_keyring:
            logger.warning(
                "license token present but public keyring is empty (dir: %s) — "
                "cannot verify, resolving Community", _keys_dir(),
            )
            _warned_token_without_keyring = True
    else:
        _warned_token_without_keyring = False
    feature_set = resolve_edition(
        token,
        keyring,
        grace_seconds=_grace_seconds(),
        revoked_token_ids=load_revocation_list(keyring),
    )
    with _cache_lock:
        _cached_feature_set = feature_set
        _cache_resolved_at = _monotonic()
    if feature_set.is_enterprise:
        logger.info(
            "edition=enterprise plan=%s features=%d%s",
            feature_set.plan, len(feature_set.features),
            " (EXPIRADA — em carência)" if feature_set.expired_in_grace else "",
        )
    else:
        logger.info("edition=community")
    return feature_set


def current() -> FeatureSet:
    """Return the cached current :class:`FeatureSet`, resolving lazily on first use.

    O cache tem TTL (``CENTRALOPS_EDITION_REFRESH_SECONDS``, default 300s): expirado o
    intervalo, o próximo call re-resolve — é o que faz o DOWNGRADE por expiração (e a
    ativação nova) acontecer em runtime, sem restart, uniformemente na API e nos
    workers. Corrida benigna: dois threads no limiar re-resolvem duas vezes (idempotente).
    """
    cached = _cached_feature_set
    if cached is None:
        return refresh()
    interval = _refresh_interval_seconds()
    if interval > 0:
        resolved_at = _cache_resolved_at
        if resolved_at is None or (_monotonic() - resolved_at) >= interval:
            return refresh()
    return cached


def feature_enabled(name: str) -> bool:
    """Convenience gate over the current edition."""
    return current().feature_enabled(name)


def max_organizations() -> Optional[int]:
    """Teto de organizações do tier corrente (``None`` = ilimitado). Community e tiers
    sem o claim → ``None``. Consultado na criação de org p/ a trava single-tenant
    (Starter). Fail-closed-to-Community: licença ilegível → None → sem teto (o core
    AGPL é irrestrito; a trava é uma feature do tier PAGO Starter)."""
    return current().max_organizations


#: Features pagas cujo COMPORTAMENTO é provido por um seam do EE (o resolver de
#: subárvore). Se a licença concede uma destas mas o seam não está registrado, o
#: produto degradaria em SILÊNCIO para FLAT — o cliente pagou por multi-tenant e veria
#: só a própria org.
_SUBTREE_DEPENDENT_FEATURES = ("multi_tenant", "reseller")


def enterprise_integrity_problem() -> Optional[str]:
    """Detecta uma edição Enterprise MAL-CONFIGURADA e retorna o motivo (ou ``None`` se
    íntegra). Caso: a licença concede uma feature que EXIGE um seam do EE (subtree
    scope) mas o seam NÃO está registrado — i.e. o pacote ``centralops_ee`` não ativou
    (ausente/erro). Sem este gate, o produto serviria silenciosamente como Community
    (FLAT) com uma licença paga. Community pura (sem essas features) → sempre íntegra."""
    fs = current()
    if not any(fs.feature_enabled(f) for f in _SUBTREE_DEPENDENT_FEATURES):
        return None  # não depende do seam → nada a verificar
    from . import ee_hooks  # noqa: PLC0415 — lazy p/ evitar ciclo de import

    if ee_hooks.get_scope_resolver() is None:
        return (
            "licença concede multi_tenant/reseller, mas o scope resolver do EE não está "
            "registrado — o pacote centralops_ee não ativou (degradação silenciosa p/ FLAT)"
        )
    return None


def reset_cache() -> None:
    """Clear the cached FeatureSet (and the keyring-log dedup state). For tests /
    after a license update."""
    global _cached_feature_set, _cache_resolved_at
    global _last_keyring_kids, _warned_token_without_keyring
    with _cache_lock:
        _cached_feature_set = None
        _cache_resolved_at = None
        _last_keyring_kids = None
        _warned_token_without_keyring = False
