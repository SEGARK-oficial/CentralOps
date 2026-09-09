"""Desacoplamento do IRIS do caminho de entrega.

Cobre: o mapping genérico (destination_customer_mappings) como fonte da verdade
do customer id externo, a resolução inbound via mapping, o validator de
DFIR_IRIS_URL (fail-fast) e o backfill idempotente do iris_customer_id legado.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import database, models
from backend.app.db.models import Base
from backend.app.db.repository import (
    DestinationCustomerIdConflictError,
    DestinationCustomerMappingRepository,
    OrganizationRepository,
)


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    with Session() as session:
        yield session
    engine.dispose()


def _org(db, name="Acme", slug="acme", iris_customer_id=None):
    org = models.Organization(
        name=name, slug=slug, is_active=True, iris_customer_id=iris_customer_id
    )
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


# ── Mapping repo: fonte da verdade do customer id externo ───────────────────
def test_mapping_set_get_roundtrip(db):
    org = _org(db)
    repo = DestinationCustomerMappingRepository(db)
    repo.set(org.id, "iris", 4242)
    assert repo.get_external_id(org.id, "iris") == "4242"  # armazenado como string
    assert repo.get_external_id(org.id, "thehive") is None


def test_mapping_set_is_idempotent_upsert(db):
    org = _org(db)
    repo = DestinationCustomerMappingRepository(db)
    repo.set(org.id, "iris", 1)
    repo.set(org.id, "iris", 2)  # upsert, não duplica
    rows = repo.list_for_org(org.id)
    assert len(rows) == 1
    assert rows[0].external_customer_id == "2"


def test_mapping_generaliza_para_outros_destinos(db):
    org = _org(db)
    repo = DestinationCustomerMappingRepository(db)
    repo.set(org.id, "iris", 10)
    repo.set(org.id, "thehive", "th-abc")  # id não-inteiro (String cobre)
    kinds = {m.destination_kind for m in repo.list_for_org(org.id)}
    assert kinds == {"iris", "thehive"}


def test_find_organization_id_by_external(db):
    org = _org(db)
    repo = DestinationCustomerMappingRepository(db)
    repo.set(org.id, "iris", 777)
    assert repo.find_organization_id("iris", "777") == org.id
    assert repo.find_organization_id("iris", "999") is None


def test_external_id_is_globally_unique_per_kind(db):
    """Um customer id externo pertence a no MÁXIMO uma org por destino — a
    resolução inversa nunca cruza tenant por colisão de id (uq kind+extid).

    set() detecta a colisão (INSERT: org2 ainda sem mapping) e levanta
    DestinationCustomerIdConflictError já com rollback feito — não deixa o
    IntegrityError cru escapar pro caller (era isso que virava 500 sem
    tratamento no router antes do fix)."""
    org1 = _org(db, name="O1", slug="o1")
    org2 = _org(db, name="O2", slug="o2")
    repo = DestinationCustomerMappingRepository(db)
    repo.set(org1.id, "iris", 42)
    with pytest.raises(DestinationCustomerIdConflictError) as excinfo:
        repo.set(org2.id, "iris", 42)  # MESMO id externo p/ outra org → rejeitado
    assert excinfo.value.conflicting_organization_id == org1.id
    # A sessão já está usável (rollback interno) sem esforço do caller.
    assert repo.find_organization_id("iris", "42") == org1.id


def test_external_id_conflict_on_update_is_caught(db):
    """Mesma colisão, mas via UPDATE (org2 já tem mapping 'iris' e faz
    force-resync pro id que org1 já possui) — era o branch SEM NENHUM
    try/except antes do fix. Reproduz o cenário real: IRIS reprovisionado do
    zero reatribui um customer_id que já está preso a um mapping órfão de
    outra org."""
    org1 = _org(db, name="O1", slug="o1")
    org2 = _org(db, name="O2", slug="o2")
    repo = DestinationCustomerMappingRepository(db)
    repo.set(org1.id, "iris", 42)
    repo.set(org2.id, "iris", 99)  # org2 já mapeada (kind='iris')
    with pytest.raises(DestinationCustomerIdConflictError) as excinfo:
        repo.set(org2.id, "iris", 42)  # force-resync pro id que org1 já tem
    assert excinfo.value.conflicting_organization_id == org1.id
    # UPDATE falhou antes de gravar — org2 mantém o mapping anterior intacto.
    assert repo.get_external_id(org2.id, "iris") == "99"


def test_integrity_error_de_outra_causa_nao_vira_conflito(db):
    """IntegrityError que NÃO é colisão de (kind, external_id) sobe cru.

    Guarda contra mascarar qualquer falha de integridade como "customer_id
    já usado": um erro enganoso manda quem depura pro caminho errado. Aqui a
    violação é NOT NULL em destination_kind — ninguém detém o external_id,
    então set() re-levanta o IntegrityError original em vez de inventar um
    conflito com organization_id=None."""
    from sqlalchemy.exc import IntegrityError

    org = _org(db)
    repo = DestinationCustomerMappingRepository(db)
    with pytest.raises(IntegrityError):
        repo.set(org.id, None, 42)  # destination_kind NOT NULL → viola
    db.rollback()


# ── Resolução INBOUND via mapping (não mais pela coluna) ────────────────────
def test_find_by_iris_customer_id_resolves_via_mapping(db):
    org = _org(db, iris_customer_id=None)  # coluna VAZIA de propósito
    DestinationCustomerMappingRepository(db).set(org.id, "iris", 555)

    org_repo = OrganizationRepository(db)
    found = org_repo.find_by_iris_customer_id(555)
    assert found is not None and found.id == org.id
    # Sem mapping → None (não resolve pela coluna deprecada).
    assert org_repo.find_by_iris_customer_id(123456) is None


# ── Backfill idempotente: iris_customer_id legado → mapping ─────────────────
def test_backfill_migrates_legacy_iris_customer_id(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(database, "DATABASE_URL", "sqlite:///:memory:")
    import backend.app.db.models  # noqa: F401 — popula metadata

    database._run_schema_init()  # create_all + lightweight (inclui o backfill)

    Session = sessionmaker(bind=engine)
    with Session() as s:
        # Insere uma org legada com iris_customer_id na coluna, SEM mapping.
        s.execute(text(
            "INSERT INTO organizations(name, slug, is_active, created_at, updated_at, "
            "iris_customer_id) VALUES ('Legacy', 'legacy', 1, datetime('now'), "
            "datetime('now'), 9001)"
        ))
        s.commit()

    # Re-roda as migrações leves → backfilla a org legada.
    database._run_lightweight_migrations()

    repo_session = Session()
    try:
        org = OrganizationRepository(repo_session).get_by_slug("legacy")
        ext = DestinationCustomerMappingRepository(repo_session).get_external_id(
            org.id, "iris"
        )
        assert ext == "9001"
        # Idempotente: re-rodar não duplica.
        database._run_lightweight_migrations()
        rows = DestinationCustomerMappingRepository(repo_session).list_for_org(org.id)
        assert len([r for r in rows if r.destination_kind == "iris"]) == 1
    finally:
        repo_session.close()
    engine.dispose()


# ── validator de DFIR_IRIS_URL (fail-fast no boot) ──────────────────────
def test_iris_url_validator_rejects_malformed():
    from backend.app.core.config import Settings

    with pytest.raises(Exception):  # ValidationError (URL sem scheme/host)
        Settings(DFIR_IRIS_URL="iris.exemplo.com")  # sem http(s)://


def test_iris_url_validator_accepts_valid_and_strips_trailing_slash():
    from backend.app.core.config import Settings

    s = Settings(DFIR_IRIS_URL="https://iris.exemplo.com/")
    assert s.DFIR_IRIS_URL == "https://iris.exemplo.com"


def test_iris_url_validator_allows_empty():
    from backend.app.core.config import Settings

    assert Settings(DFIR_IRIS_URL=None).DFIR_IRIS_URL is None
    assert Settings(DFIR_IRIS_URL="").DFIR_IRIS_URL is None
