"""A trilha de auditoria não pode vazar entre tenants, nem gravar segredo.

Dois defeitos que andavam juntos e vieram da mesma auditoria competitiva.

**Vazamento cross-tenant.** ``AuditLog`` não tinha ``organization_id``, então
não havia por onde escopar. O endpoint ``/api/history/audit`` passa
``include_all=True``, e o único filtro do repositório era ``if viewer and not
include_all`` — pulado. Um admin de org enxergava a atividade administrativa de
TODOS os tenants: usuário, IP, endpoint, horário. Num MSSP com clientes
concorrentes, é reconhecimento de graça.

**Segredo em texto claro.** A redação do payload comparava o nome do campo por
IGUALDADE contra uma lista. ``token`` estava lá, ``hec_token`` não, e igualdade
não pega composto. O token do Splunk HEC ia inteiro para
``audit_logs.request_payload``.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import models
from backend.app.db.database import Base
from backend.app.db.repository import AuditLogRepository


@pytest.fixture()
def db():
    """SQLite em memória, mesmo padrão dos demais testes de backend/tests."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def _usuario(org, *, role="admin", uid=1, nome="u"):
    """AppUser leve. ``has_global_scope`` decide por role + organization_id."""
    u = models.AppUser(id=uid, username=nome, role=role, organization_id=org)
    return u


def _semear(db) -> None:
    for i, (org, nome) in enumerate(
        [(1, "alice_org1"), (1, "bob_org1"), (2, "carol_org2"), (None, "plataforma")],
        start=1,
    ):
        db.add(
            models.AuditLog(
                user_id=i,
                username=nome,
                user_role="admin",
                organization_id=org,
                action="login",
                endpoint="/api/auth/login",
                ip_address=f"198.51.100.{i}",
            )
        )
    db.commit()


# ── escopo de tenant ──────────────────────────────────────────────────

def test_admin_de_org_nao_ve_a_atividade_de_outro_tenant(db) -> None:
    """O vazamento. include_all=True não pode significar "todos os tenants"."""
    _semear(db)
    repo = AuditLogRepository(db)

    linhas = repo.list(viewer=_usuario(1), include_all=True)

    orgs = {l.organization_id for l in linhas}
    assert orgs == {1}, f"vazou para fora da org 1: {orgs}"
    assert {l.username for l in linhas} == {"alice_org1", "bob_org1"}


def test_admin_de_org_nao_ve_linha_sem_org(db) -> None:
    """NULL cobre ação de plataforma e linha pré-migração. Fail-closed nos dois."""
    _semear(db)
    repo = AuditLogRepository(db)

    linhas = repo.list(viewer=_usuario(2), include_all=True)

    assert [l.username for l in linhas] == ["carol_org2"]
    assert all(l.organization_id is not None for l in linhas)


def test_escopado_sem_org_resolvida_nao_ve_nada(db) -> None:
    """Configuração incompleta não pode virar acesso total."""
    _semear(db)
    repo = AuditLogRepository(db)

    assert repo.list(viewer=_usuario(None, role="viewer"), include_all=True) == []


def test_os_dois_eixos_sao_independentes(db) -> None:
    """include_all continua decidindo QUAIS USUÁRIOS, sem tocar no tenant."""
    _semear(db)
    repo = AuditLogRepository(db)
    alice = _usuario(1, uid=1, nome="alice_org1")

    proprias = repo.list(viewer=alice, include_all=False)
    da_org = repo.list(viewer=alice, include_all=True)

    assert {l.username for l in proprias} == {"alice_org1"}
    assert {l.username for l in da_org} == {"alice_org1", "bob_org1"}


# ── redação de segredo ────────────────────────────────────────────────

@pytest.mark.parametrize(
    "campo",
    [
        "hec_token",          # o caso real que vazou
        "clickhouse_password",
        "ingest_secret",
        "service_account_key",
        "vault_credential",
        "bootstrap_passphrase",
        "HEC_TOKEN",          # caixa alta
    ],
)
def test_nome_composto_de_credencial_e_redactado(campo) -> None:
    """Sufixo em vez de igualdade: fecha a CLASSE, não o caso.

    A lista exata perdia todo nome composto que ninguém tinha previsto, e
    cadastrar um por um é jogo de acertar a toupeira: o próximo destino traz o
    próximo nome.
    """
    from backend.app.main import _redact_audit_payload

    saida = _redact_audit_payload({campo: "VALOR-SECRETO"})

    assert saida[campo] == "[REDACTED]"
    assert "VALOR-SECRETO" not in str(saida)


def test_a_redacao_alcanca_estrutura_aninhada() -> None:
    from backend.app.main import _redact_audit_payload

    saida = _redact_audit_payload(
        {"config": {"url": "http://h:8123", "itens": [{"api_token": "X"}]}}
    )

    assert saida["config"]["itens"][0]["api_token"] == "[REDACTED]"
    assert saida["config"]["url"] == "http://h:8123"


@pytest.mark.parametrize("campo", ["token_bucket", "keyboard", "monkey", "descricao"])
def test_campo_inofensivo_nao_e_redactado(campo) -> None:
    """O sufixo casa no FIM do nome. ``keyboard`` contém "key" e não é segredo."""
    from backend.app.main import _redact_audit_payload

    assert _redact_audit_payload({campo: "valor"})[campo] == "valor"
