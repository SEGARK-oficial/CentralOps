"""O dreno da DLQ não pode atropelar a ingestão viva nem perder evento.

Estes testes existem por causa de um comportamento observado em produção que é
contraintuitivo: clicar em "reprocessar" fazia o contador da DLQ **subir**.

O mecanismo tinha três partes, e cada teste aqui trava uma delas.

1. O dreno despachava UM evento por requisição. Com ida e volta de ~0,3s,
   alguns milhares de eventos viravam dezenas de minutos, e o
   ``task_time_limit`` de 15 min matava a task no meio do caminho.
2. Não havia teto por execução, apesar de o docstring do repositório afirmar
   que "o endpoint limita a 500 linhas". Esse limite nunca existiu.
3. O disjuntor é POR DESTINO e compartilhado com a ingestão normal. O dreno
   saturava o destino, os lotes de tráfego vivo batiam no disjuntor aberto e
   viravam ``breaker_open`` mais rápido do que o dreno drenava. Reprocessar
   aumentava a fila em vez de diminuir.

O quarto teste cobre o que o conserto NÃO pode quebrar: a linha só é apagada
depois de confirmado que o destino aceitou.
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

from unittest.mock import patch

import pytest
from sqlalchemy.orm import sessionmaker

from backend.app.collectors import circuit_breaker
from backend.app.collectors import tasks as tasks_mod
from backend.app.db import models
from backend.app.db.database import Base
from backend.app.db.repository import DestinationRepository

DEST = "dest-dreno"


@pytest.fixture()
def db_estatico(threadsafe_sqlite_engine):
    """SQLite compartilhado, com ``database.SessionLocal`` apontado para ele."""
    engine = threadsafe_sqlite_engine
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    import backend.app.db.database as db_module

    original = db_module.SessionLocal
    db_module.SessionLocal = Session
    try:
        yield Session
    finally:
        db_module.SessionLocal = original


def _semear(Session, quantos: int, *, prefixo: str = "evt") -> list[str]:
    """Cria ``quantos`` linhas de DLQ com envelope válido. Devolve os event_ids."""
    ids = []
    with Session() as s:
        for i in range(quantos):
            eid = f"{prefixo}-{i}"
            ids.append(eid)
            s.add(
                models.DestinationDeadLetter(
                    destination_id=DEST,
                    event_id=eid,
                    organization_id=7,
                    error_kind="breaker_open",
                    error_detail="disjuntor aberto",
                    payload=json.dumps(
                        {"_centralops": {"event_id": eid, "organization_id": 7},
                         "normalized": {"class_uid": 2004}}
                    ),
                )
            )
        s.commit()
    return ids


def _contar(Session) -> int:
    with Session() as s:
        return s.query(models.DestinationDeadLetter).filter_by(destination_id=DEST).count()


def _kinds(Session) -> dict:
    with Session() as s:
        return {
            str(r.event_id): str(r.error_kind)
            for r in s.query(models.DestinationDeadLetter).filter_by(destination_id=DEST).all()
        }


# ── 1. lote, não um-por-um ────────────────────────────────────────────

def test_o_dreno_despacha_em_LOTE_e_nao_um_evento_por_requisicao(db_estatico) -> None:
    """250 eventos devem virar 3 despachos de 100/100/50, não 250 despachos.

    Era o defeito nº 1: a ida e volta por evento fazia a task estourar o
    ``task_time_limit`` antes de terminar, e a DLQ nunca esvaziava.
    """
    _semear(db_estatico, 250)
    tamanhos = []

    async def _fake(dest_id, batch):
        tamanhos.append(len(batch))

    with patch.object(tasks_mod, "dispatch_batch_to_destination", _fake), \
         patch.object(tasks_mod.drain_destination_dlq, "apply_async"):
        r = tasks_mod.drain_destination_dlq(DEST, None, None, True)

    assert tamanhos == [100, 100, 50], f"esperava lotes, veio {len(tamanhos)} despachos"
    assert r["delivered"] == 250
    assert _contar(db_estatico) == 0


# ── 2. teto por execução, com continuação ─────────────────────────────

def test_o_dreno_respeita_teto_por_execucao_e_reagenda_o_resto(db_estatico) -> None:
    """650 eventos: 500 nesta execução, 150 reagendados.

    O docstring do repositório afirmava um teto de 500 no endpoint que nunca
    existiu. Agora o teto existe de verdade, e é a task que o aplica.
    """
    _semear(db_estatico, 650)

    async def _ok(dest_id, batch):
        return None

    with patch.object(tasks_mod, "dispatch_batch_to_destination", _ok), \
         patch.object(tasks_mod.drain_destination_dlq, "apply_async") as reagenda:
        r = tasks_mod.drain_destination_dlq(DEST, None, None, True)

    assert r["delivered"] == 500
    assert r["remaining"] == 150
    assert _contar(db_estatico) == 150, "as 150 sobrando têm que continuar na DLQ"

    reagenda.assert_called_once()
    kwargs = reagenda.call_args.kwargs
    assert len(kwargs["kwargs"]["event_ids"]) == 150
    assert kwargs["countdown"] == tasks_mod._DRENO_ESPERA_S


# ── 3. cede a vez ao tráfego vivo ─────────────────────────────────────

def test_o_dreno_CEDE_no_primeiro_disjuntor_aberto_em_vez_de_insistir(db_estatico) -> None:
    """É o conserto do defeito que fazia a DLQ crescer durante o reprocesso.

    Insistir contra um disjuntor aberto roubava a vez da ingestão normal, cujos
    lotes viravam ``breaker_open`` novos. Parar na primeira recusa e reagendar
    com espera MAIOR que o cooldown devolve a prioridade ao tráfego real.
    """
    _semear(db_estatico, 300)
    chamadas = []

    async def _sempre_breaker(dest_id, batch):
        chamadas.append(len(batch))
        raise circuit_breaker.BreakerOpen("aberto")

    with patch.object(tasks_mod, "dispatch_batch_to_destination", _sempre_breaker), \
         patch.object(tasks_mod.drain_destination_dlq, "apply_async") as reagenda:
        r = tasks_mod.drain_destination_dlq(DEST, None, None, True)

    assert len(chamadas) == 1, "tinha que parar no PRIMEIRO, não varrer os 3 lotes"
    assert r["yielded_to_breaker"] is True
    assert r["delivered"] == 0
    assert _contar(db_estatico) == 300, "nada pode ser perdido ao ceder"

    assert reagenda.call_args.kwargs["countdown"] == tasks_mod._DRENO_ESPERA_DISJUNTOR_S
    assert (
        tasks_mod._DRENO_ESPERA_DISJUNTOR_S > 30
    ), "a espera precisa passar do cooldown de 30s do disjuntor"


def test_ao_ceder_a_linha_volta_a_dizer_breaker_open_e_nao_fica_com_o_sentinela(
    db_estatico,
) -> None:
    """O carimbo é detalhe interno; o operador tem que ver o motivo real."""
    _semear(db_estatico, 5)

    async def _breaker(dest_id, batch):
        raise circuit_breaker.BreakerOpen("aberto")

    with patch.object(tasks_mod, "dispatch_batch_to_destination", _breaker), \
         patch.object(tasks_mod.drain_destination_dlq, "apply_async"):
        tasks_mod.drain_destination_dlq(DEST, None, None, True)

    kinds = set(_kinds(db_estatico).values())
    assert kinds == {"breaker_open"}
    assert DestinationRepository.DLQ_KIND_EM_REPROCESSO not in kinds


# ── 4. nada é apagado sem confirmação ─────────────────────────────────

def test_rejeicao_parcial_preserva_SO_os_recusados_com_o_motivo_novo(db_estatico) -> None:
    """O caso que o desenho anterior errava.

    Os sinks não levantam exceção, devolvem ``DeliveryResult``. Então "não
    levantou" NÃO é sinal de entrega. Aqui metade do lote é recusada pelo
    destino; só essa metade pode sobreviver, e com o motivo verdadeiro.
    """
    ids = _semear(db_estatico, 10)
    recusados = set(ids[:4])

    async def _recusa_metade(dest_id, batch):
        # Simula o que o caminho real faz: regravar a linha do evento recusado.
        from backend.app.collectors.delivery import persist_batch_dlq

        maus = [e for e in batch if e["_centralops"]["event_id"] in recusados]
        if maus:
            persist_batch_dlq(
                maus,
                destination_id=dest_id,
                error_kind="schema_rejected",
                organization_id=7,
                error_detail="coluna desconhecida",
            )

    with patch.object(tasks_mod, "dispatch_batch_to_destination", _recusa_metade), \
         patch.object(tasks_mod.drain_destination_dlq, "apply_async"):
        r = tasks_mod.drain_destination_dlq(DEST, None, None, True)

    assert r["delivered"] == 6
    assert r["failed"] == 4

    kinds = _kinds(db_estatico)
    assert set(kinds) == recusados, "só os recusados podem restar"
    assert set(kinds.values()) == {"schema_rejected"}, (
        "o motivo tem que ser o NOVO e verdadeiro, não o antigo nem o sentinela"
    )


def test_task_morta_no_meio_nao_perde_evento(db_estatico) -> None:
    """A linha é carimbada, nunca apagada antes de confirmar.

    O desenho anterior apagava ANTES de despachar para liberar o unique. Isso
    funcionava, mas se a task morresse na janela (e o ``task_time_limit`` de 15
    min tornava isso alcançável) o evento sumia. Aqui a morte é simulada por uma
    exceção dura no meio do segundo lote.
    """
    _semear(db_estatico, 150)
    n = {"i": 0}

    async def _morre_no_segundo(dest_id, batch):
        n["i"] += 1
        if n["i"] == 2:
            raise KeyboardInterrupt("worker morto")

    with patch.object(tasks_mod, "dispatch_batch_to_destination", _morre_no_segundo), \
         patch.object(tasks_mod.drain_destination_dlq, "apply_async"):
        with pytest.raises(KeyboardInterrupt):
            tasks_mod.drain_destination_dlq(DEST, None, None, True)

    # 100 do primeiro lote saíram; os 50 do lote interrompido continuam na DLQ.
    assert _contar(db_estatico) == 50, "nenhum evento pode sumir na morte da task"
