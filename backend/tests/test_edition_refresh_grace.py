"""Janela de carência de expiração + TTL de re-resolução da edição.

Fecha a lacuna "downgrade só no restart": ``current()`` agora tem TTL
(``CENTRALOPS_EDITION_REFRESH_SECONDS``) — expirado o intervalo, a próxima consulta
re-resolve a licença (DB→env→arquivo), então EXPIRAÇÃO (downgrade) e ATIVAÇÃO nova
acontecem em runtime, uniformemente na API e nos workers, sem beat task. E a
``CENTRALOPS_LICENSE_GRACE_DAYS`` dá a UX de renovação atrasada: token vencido é
honrado por N dias com ``expired_in_grace=True`` + ERROR no log — SÓ o ``exp`` ganha
tolerância; assinatura/kid/claims seguem estritos. O relógio do TTL é injetável
(``edition._monotonic``) para os testes não dormirem.
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


@pytest.fixture()
def test_db(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(db_module, "SessionLocal", TestingSessionLocal)
    monkeypatch.delenv("CENTRALOPS_LICENSE_TOKEN", raising=False)
    monkeypatch.delenv("CENTRALOPS_LICENSE_TOKEN_FILE", raising=False)
    edition.reset_cache()
    yield TestingSessionLocal
    edition.reset_cache()
    Base.metadata.drop_all(bind=engine)


class _Clock:
    """Relógio monotônico controlável (sem sleep nos testes de TTL)."""

    def __init__(self) -> None:
        self.t = 1_000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture()
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(edition, "_monotonic", c)
    return c


def _keypair():
    priv = Ed25519PrivateKey.generate()
    return priv, priv.public_key()


def _sign(priv, *, kid="k1", **claims):
    now = int(time.time())
    payload = {"sub": "cust_1", "plan": "enterprise", "iat": now, "exp": now + 3600}
    payload.update(claims)
    return jwt.encode(payload, priv, algorithm="EdDSA", headers={"kid": kid})


def _keyring_env(tmp_path, monkeypatch, pub, kid="k1"):
    (tmp_path / f"{kid}.pem").write_bytes(
        pub.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    )
    monkeypatch.setenv("CENTRALOPS_LICENSE_KEYS_DIR", str(tmp_path))


# ── Janela de carência ─────────────────────────────────────────────────────────

def test_expired_within_grace_stays_enterprise_flagged(test_db, monkeypatch, tmp_path):
    """Vencida há 1h com carência de 7d → Enterprise, marcada expired_in_grace."""
    monkeypatch.setenv("CENTRALOPS_LICENSE_GRACE_DAYS", "7")
    priv, pub = _keypair()
    _keyring_env(tmp_path, monkeypatch, pub)
    now = int(time.time())
    license_store.save_token(
        _sign(priv, features=["multi_tenant"], iat=now - 7200, exp=now - 3600),
        actor="admin",
    )
    fs = edition.refresh()
    assert fs.is_enterprise and fs.expired_in_grace is True
    assert fs.feature_enabled("multi_tenant")  # features seguem ativas na carência


def test_expired_beyond_grace_downgrades_to_community(test_db, monkeypatch, tmp_path):
    """Vencida há 8 dias com carência de 7d → Community (hard)."""
    monkeypatch.setenv("CENTRALOPS_LICENSE_GRACE_DAYS", "7")
    priv, pub = _keypair()
    _keyring_env(tmp_path, monkeypatch, pub)
    now = int(time.time())
    eight_days = 8 * 86400
    license_store.save_token(
        _sign(priv, iat=now - eight_days - 3600, exp=now - eight_days), actor="admin"
    )
    fs = edition.refresh()
    assert not fs.is_enterprise and fs.expired_in_grace is False


def test_grace_zero_is_strict(test_db, monkeypatch, tmp_path):
    """Carência 0 → venceu (além do leeway de 60s), caiu."""
    monkeypatch.setenv("CENTRALOPS_LICENSE_GRACE_DAYS", "0")
    priv, pub = _keypair()
    _keyring_env(tmp_path, monkeypatch, pub)
    now = int(time.time())
    license_store.save_token(_sign(priv, iat=now - 7200, exp=now - 300), actor="admin")
    assert edition.refresh().edition == edition.COMMUNITY


def test_grace_does_not_weaken_signature_checks(test_db, monkeypatch, tmp_path):
    """A carência tolera SÓ o exp: token vencido assinado pela chave ERRADA continua
    rejeitado mesmo com carência ampla (não é bypass de verificação)."""
    monkeypatch.setenv("CENTRALOPS_LICENSE_GRACE_DAYS", "30")
    _, pub = _keypair()               # keyring publica a chave A…
    attacker_priv, _ = _keypair()     # …mas o token é assinado pela chave B
    _keyring_env(tmp_path, monkeypatch, pub)
    now = int(time.time())
    license_store.save_token(
        _sign(attacker_priv, iat=now - 7200, exp=now - 3600), actor="x"
    )
    assert edition.refresh().edition == edition.COMMUNITY


def test_grace_invalid_env_falls_back_to_default(test_db, monkeypatch, tmp_path):
    """Knob inválido não quebra o boot: cai no default (fail-safe)."""
    monkeypatch.setenv("CENTRALOPS_LICENSE_GRACE_DAYS", "not-a-number")
    priv, pub = _keypair()
    _keyring_env(tmp_path, monkeypatch, pub)
    now = int(time.time())
    license_store.save_token(_sign(priv, iat=now - 7200, exp=now - 3600), actor="admin")
    # default 7d > 1h vencida → em carência
    assert edition.refresh().expired_in_grace is True


# ── Endurecimento: o env-knob (código aberto/recompilável) NÃO é a trava ─────────
#
# A pergunta do dono: ter CENTRALOPS_LICENSE_GRACE_DAYS num core AGPL recompilável não
# vira bypass? Defesa em profundidade: (1) o env só ENCURTA a carência, (2) um claim
# ASSINADO ``grace_days`` (controlado pelo vendor) encurta mais, (3) um teto de código
# ``_MAX_GRACE_DAYS`` limita tudo. O relógio real e inamovível é o ``exp`` assinado.

def test_signed_grace_days_caps_below_env(test_db, monkeypatch, tmp_path):
    """env pede 30d de carência, mas a licença ASSINA grace_days=2 → efetivo = 2d.
    Vencida há 5d → além dos 2d assinados → Community (o vendor manda, não a env)."""
    monkeypatch.setenv("CENTRALOPS_LICENSE_GRACE_DAYS", "30")
    priv, pub = _keypair()
    _keyring_env(tmp_path, monkeypatch, pub)
    now = int(time.time())
    five_days = 5 * 86400
    license_store.save_token(
        _sign(priv, grace_days=2, iat=now - five_days - 3600, exp=now - five_days),
        actor="admin",
    )
    assert edition.refresh().edition == edition.COMMUNITY


def test_signed_grace_days_honored_within_window(test_db, monkeypatch, tmp_path):
    """env=30d, licença assina grace_days=10, vencida há 5d → dentro dos 10d assinados
    → Enterprise em carência (o claim assinado é a fonte de verdade da janela)."""
    monkeypatch.setenv("CENTRALOPS_LICENSE_GRACE_DAYS", "30")
    priv, pub = _keypair()
    _keyring_env(tmp_path, monkeypatch, pub)
    now = int(time.time())
    five_days = 5 * 86400
    license_store.save_token(
        _sign(priv, grace_days=10, features=["multi_tenant"],
              iat=now - five_days - 3600, exp=now - five_days),
        actor="admin",
    )
    fs = edition.refresh()
    assert fs.is_enterprise and fs.expired_in_grace is True


def test_code_hard_cap_limits_absurd_env(test_db, monkeypatch, tmp_path):
    """Alguém edita a env para 3650 dias (10 anos) de carência no core recompilável.
    O teto de código (_MAX_GRACE_DAYS) limita: vencida além do teto → Community, sem
    depender de um claim assinado. A env não consegue esticar a janela além do teto."""
    monkeypatch.setenv(
        "CENTRALOPS_LICENSE_GRACE_DAYS", str(edition._MAX_GRACE_DAYS + 3650)
    )
    priv, pub = _keypair()
    _keyring_env(tmp_path, monkeypatch, pub)
    now = int(time.time())
    overdue = (edition._MAX_GRACE_DAYS + 5) * 86400  # vencida 5d além do teto
    license_store.save_token(
        _sign(priv, iat=now - overdue - 3600, exp=now - overdue), actor="admin"
    )
    assert edition.refresh().edition == edition.COMMUNITY


# ── TTL de re-resolução em runtime ────────────────────────────────────────────

def test_ttl_downgrades_expired_license_at_runtime(test_db, monkeypatch, tmp_path, clock):
    """O caso que motivou o TTL: licença expira COM O PROCESSO RODANDO → passado o
    intervalo, o próprio ``current()`` re-resolve e faz o downgrade, sem restart."""
    monkeypatch.setenv("CENTRALOPS_EDITION_REFRESH_SECONDS", "300")
    monkeypatch.setenv("CENTRALOPS_LICENSE_GRACE_DAYS", "0")
    priv, pub = _keypair()
    _keyring_env(tmp_path, monkeypatch, pub)
    license_store.save_token(_sign(priv, features=["multi_tenant"]), actor="admin")
    assert edition.current().is_enterprise  # resolve + aquece o cache

    # a licença "expira" enquanto o processo roda: troca o token armazenado por um vencido
    now = int(time.time())
    license_store.save_token(_sign(priv, iat=now - 7200, exp=now - 300), actor="admin")
    assert edition.current().is_enterprise          # dentro do TTL → cache (enterprise)
    clock.advance(301)                               # passa o intervalo
    assert edition.current().edition == edition.COMMUNITY  # re-resolveu → downgrade


def test_ttl_picks_up_new_activation_at_runtime(test_db, monkeypatch, tmp_path, clock):
    """O inverso: deploy Community ganha licença ativada → workers/API viram Enterprise
    no próximo intervalo, sem restart (importante p/ o worker, que nunca chama activate)."""
    monkeypatch.setenv("CENTRALOPS_EDITION_REFRESH_SECONDS", "300")
    priv, pub = _keypair()
    _keyring_env(tmp_path, monkeypatch, pub)
    assert edition.current().edition == edition.COMMUNITY  # sem licença

    license_store.save_token(_sign(priv, features=["federated_search"]), actor="admin")
    assert edition.current().edition == edition.COMMUNITY  # ainda no cache
    clock.advance(301)
    assert edition.current().is_enterprise                  # pegou a ativação


def test_ttl_zero_disables_auto_refresh(test_db, monkeypatch, tmp_path, clock):
    """TTL 0 = comportamento antigo (cache eterno até refresh()/restart explícito)."""
    monkeypatch.setenv("CENTRALOPS_EDITION_REFRESH_SECONDS", "0")
    monkeypatch.setenv("CENTRALOPS_LICENSE_GRACE_DAYS", "0")
    priv, pub = _keypair()
    _keyring_env(tmp_path, monkeypatch, pub)
    license_store.save_token(_sign(priv), actor="admin")
    assert edition.current().is_enterprise

    now = int(time.time())
    license_store.save_token(_sign(priv, iat=now - 7200, exp=now - 300), actor="admin")
    clock.advance(10_000_000)
    assert edition.current().is_enterprise  # cache eterno — só refresh() explícito muda
    assert edition.refresh().edition == edition.COMMUNITY
