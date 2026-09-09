"""Filha de Partner/Organization herda o estado de auth do pai.

Bug original: ``/schedules`` ficava travado em "Cadastro pendente" num deploy
Sophos Partner, por mais queries que o operador cadastrasse. A tela monta a
lista de clientes com::

    availableClients = clients.filter(c => c.is_authenticated && c.tenant_id)

e num Partner NINGUÉM satisfazia as duas condições ao mesmo tempo:

  * o pai (kind=organization) é ``auth_status='healthy'`` mas tem
    ``tenant_id=NULL`` — não é um tenant;
  * as filhas (kind=tenant) têm ``tenant_id`` mas nascem
    ``auth_status='unknown'`` (``IntegrationRepository.create_child``) e nunca
    saem disso: não têm credencial própria por design (o OAuth vive no pai),
    ``record_auth_state`` não tem chamador e não há health check periódico.

Resultado: ``availableClients == []`` → ``readyToCreate == False`` → o form
some atrás do aviso, com o botão de submit desabilitado.

O fix trata a raiz: ``is_authenticated`` de uma filha reflete o pai, já que é
lá que a credencial vive.
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
from backend.app.db.models import Base


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


def _org(db, name="ViaConnect MSP", slug="viaconnect"):
    org = models.Organization(name=name, slug=slug, is_active=True)
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


def _parent(db, org, auth_status="healthy"):
    """Pai MSSP: autenticado, mas SEM tenant_id (não é um tenant)."""
    parent = models.Integration(
        organization_id=org.id,
        name="ViaConnect MSP",
        platform="sophos",
        kind="organization",
        auth_status=auth_status,
        is_active=True,
    )
    db.add(parent)
    db.commit()
    db.refresh(parent)
    return parent


def _child(db, org, parent, tenant_id="033dc4dd-4894-4669-aec0-9a76090e7d73"):
    """Filha auto-provisionada: tenant_id preenchido, auth_status 'unknown'
    (exatamente como ``IntegrationRepository.create_child`` a cria)."""
    child = models.Integration(
        organization_id=org.id,
        name="SIM Rede de Postos Ltda (Sophos)",
        platform="sophos",
        kind="tenant",
        parent_integration_id=parent.id,
        external_id=tenant_id,
        tenant_id=tenant_id,
        auto_managed=True,
        auth_status="unknown",
        is_active=True,
    )
    db.add(child)
    db.commit()
    db.refresh(child)
    return child


def test_filha_unknown_herda_auth_do_pai(db):
    """Regressão direta do "Cadastro pendente": filha 'unknown' com pai
    'healthy' conta como autenticada."""
    org = _org(db)
    parent = _parent(db, org, auth_status="healthy")
    child = _child(db, org, parent)

    assert child.auth_status == "unknown"  # a coluna NÃO muda
    assert child.is_authenticated is True  # o estado derivado, sim


def test_filha_passa_no_filtro_do_schedules(db):
    """O predicado exato do SchedulesPage (is_authenticated && tenant_id)
    seleciona a filha e descarta o pai — que é o comportamento correto: o pai
    não é um tenant consultável."""
    org = _org(db)
    parent = _parent(db, org, auth_status="healthy")
    child = _child(db, org, parent)

    integrations = [parent, child]
    available = [i for i in integrations if i.is_authenticated and bool(i.tenant_id)]

    assert available == [child]


@pytest.mark.parametrize("parent_status", ["healthy", "degraded"])
def test_pai_degradado_tambem_habilita(db, parent_status):
    """'degraded' é estado autenticado (mesma regra do próprio integration)."""
    org = _org(db)
    parent = _parent(db, org, auth_status=parent_status)
    child = _child(db, org, parent)
    assert child.is_authenticated is True


@pytest.mark.parametrize("parent_status", ["unknown", "error", "unauthorized"])
def test_pai_nao_autenticado_nao_habilita_filha(db, parent_status):
    """A herança não pode virar um 'sempre True': pai sem auth mantém a filha
    fora — é o sinal legítimo de que aquele tenant não é consultável."""
    org = _org(db)
    parent = _parent(db, org, auth_status=parent_status)
    child = _child(db, org, parent)
    assert child.is_authenticated is False


def test_integracao_sem_pai_mantem_regra_original(db):
    """Standalone (sem parent_integration_id) não herda nada: continua valendo
    exclusivamente o próprio auth_status."""
    org = _org(db)
    standalone = models.Integration(
        organization_id=org.id,
        name="Wazuh standalone",
        platform="wazuh",
        kind="tenant",
        tenant_id="t-1",
        auth_status="unknown",
        is_active=True,
    )
    db.add(standalone)
    db.commit()
    db.refresh(standalone)

    assert standalone.is_authenticated is False
    standalone.auth_status = "healthy"
    assert standalone.is_authenticated is True


def test_filha_com_auth_propria_nao_depende_do_pai(db):
    """Se a filha tiver estado próprio saudável, ele vence sem consultar o pai
    (o curto-circuito evita o lazy-load)."""
    org = _org(db)
    parent = _parent(db, org, auth_status="error")
    child = _child(db, org, parent)
    child.auth_status = "healthy"
    db.commit()

    assert child.is_authenticated is True


def test_row_destacada_nao_estoura(db):
    """Providers chamam ``db.expunge_all()`` e leem a row destacada. Sem sessão
    o lazy-load do pai é impossível — degrada pro estado próprio em vez de
    levantar DetachedInstanceError."""
    org = _org(db)
    parent = _parent(db, org, auth_status="healthy")
    child = _child(db, org, parent)
    child_id = child.id

    db.expunge_all()
    detached = db.get(models.Integration, child_id)
    db.expunge(detached)

    # Não deve levantar; sem sessão, cai no próprio auth_status ('unknown').
    assert detached.is_authenticated is False
