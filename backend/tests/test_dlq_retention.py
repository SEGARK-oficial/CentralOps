"""A DLQ de destino era a única tabela sem poda, e a que mais cresce.

Ela cresce por definição durante incidente de entrega: uma linha por evento não
entregue, cada uma carregando o envelope canônico COMPLETO em ``payload``. Num
incidente observado, ~2 mil linhas em poucas horas, no mesmo Postgres que serve
a API. Todas as outras tabelas de crescimento ilimitado já estavam no
``prune_all``; esta ficou de fora.

O detalhe que quase escapa: as demais tasks de purge iteram organizações e
filtram por elas, o que deixaria de fora exatamente as linhas de destino GLOBAL
(``organization_id IS NULL``), que num MSSP costumam ser a maioria do volume.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.collectors import retention_tasks
from backend.app.db import models
from backend.app.db.database import Base

DEST = "dest-x"


@pytest.fixture()
def sessao(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(retention_tasks.database, "SessionLocal", Session)
    yield Session
    Base.metadata.drop_all(bind=engine)


def _linha(Session, *, org, dias_atras, eid):
    with Session() as s:
        s.add(
            models.DestinationDeadLetter(
                destination_id=DEST,
                event_id=eid,
                organization_id=org,
                error_kind="breaker_open",
                error_detail="x",
                payload='{"grande": "envelope"}',
                created_at=datetime.utcnow() - timedelta(days=dias_atras),
            )
        )
        s.commit()


def _restantes(Session) -> set[str]:
    with Session() as s:
        return {str(r.event_id) for r in s.query(models.DestinationDeadLetter).all()}


def _org(Session, oid: int):
    with Session() as s:
        s.add(
            models.Organization(
                id=oid, name=f"org{oid}", slug=f"org{oid}", is_active=True
            )
        )
        s.commit()


def test_poda_linha_expirada_da_org_e_preserva_a_recente(sessao) -> None:
    _org(sessao, 1)
    _linha(sessao, org=1, dias_atras=90, eid="velha")
    _linha(sessao, org=1, dias_atras=2, eid="nova")

    retention_tasks.prune_expired_destination_dlq.run()

    assert _restantes(sessao) == {"nova"}


def test_poda_TAMBEM_a_linha_de_destino_global(sessao) -> None:
    """O caso que as outras tasks de purge deixariam passar.

    Iterar organizações nunca alcança ``organization_id IS NULL``, e num MSSP
    o destino compartilhado costuma ser o de maior volume. Sem esta passada, a
    poda existiria no papel e a tabela continuaria crescendo.
    """
    _linha(sessao, org=None, dias_atras=90, eid="global_velha")
    _linha(sessao, org=None, dias_atras=1, eid="global_nova")

    retention_tasks.prune_expired_destination_dlq.run()

    assert _restantes(sessao) == {"global_nova"}


def test_org_inativa_nao_bloqueia_a_poda_global(sessao) -> None:
    """A passada global roda independente do estado das organizações."""
    _org(sessao, 1)
    _linha(sessao, org=None, dias_atras=200, eid="global_velha")

    retention_tasks.prune_expired_destination_dlq.run()

    assert _restantes(sessao) == set()


def test_a_poda_entra_no_prune_all(sessao, monkeypatch) -> None:
    """Sem estar no wrapper, a task existiria e nunca seria agendada.

    Esse foi exatamente o defeito: a função de purge de search_results também
    já existia antes de alguém notar que ninguém a chamava.

    A verificação é COMPORTAMENTAL, não por leitura de código-fonte. A primeira
    versão usava ``inspect.getsource(prune_all)`` e casava a string do nome, o
    que tem dois problemas: quebra na imagem compilada (Cython não devolve
    fonte, e o gate reprovou por isso) e passaria com o nome aparecendo só num
    comentário. Chamar o wrapper e exigir a invocação real resolve os dois.
    """
    chamou = []
    original = retention_tasks.prune_expired_destination_dlq.run

    def _espiao():
        chamou.append(1)
        return original()

    monkeypatch.setattr(
        retention_tasks.prune_expired_destination_dlq, "run", _espiao
    )

    resultado = retention_tasks.prune_all.run()

    assert chamou == [1], "prune_all não invocou a poda do DLQ"
    assert "destination_dlq" in resultado, (
        f"a chave do DLQ não apareceu no relatório: {sorted(resultado)}"
    )


def test_a_retencao_do_dlq_e_mais_curta_que_a_de_auditoria(sessao) -> None:
    """Decisão consciente, travada.

    O envelope completo por evento é caro, e o valor forense de um evento não
    entregue cai rápido: ou foi reprocessado em dias, ou a causa raiz mudou.
    Auditoria é o oposto, tem valor de compliance por muito tempo.
    """
    assert retention_tasks._DEFAULT_DLQ_DAYS < retention_tasks._DEFAULT_AUDIT_LOG_DAYS
    assert retention_tasks._DEFAULT_DLQ_DAYS >= 7, "curto demais destrói evidência de incidente"
