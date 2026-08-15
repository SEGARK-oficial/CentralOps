"""Edição de agendamento que já está ativo.

Antes não existia rota de update. Quem errasse o intervalo, a janela ou a lista
de integrações tinha que apagar e recriar, e o histórico ia junto: os resultados
guardam ``schedule_id``, que passava a apontar para uma linha inexistente.

Os testes aqui travam três decisões que não são óbvias:

* **Editar não dispara execução.** O create roda o agendamento na hora; o update
  não. Corrigir um horário chamaria a API do fornecedor a cada gravação.
* **Mudar a cadência reagenda a partir de agora.** Um agendamento que vai de 24h
  para 15min precisa honrar os 15min já, não daqui a um dia.
* **Mudar só o valor reaproveita a unidade guardada.** "De 6 horas para 12" não
  pode virar 12 do default.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import models
from backend.app.db.database import Base, get_session
from backend.app.main import app


@pytest.fixture()
def ambiente():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Sessao = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def _sessao():
        db = Sessao()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_session] = _sessao
    abertos: list[TestClient] = []

    def cliente() -> TestClient:
        c = TestClient(app)
        abertos.append(c)
        return c

    yield cliente, Sessao

    for c in abertos:
        c.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


def _admin(client: TestClient) -> None:
    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPassword123!"},
    )
    assert r.status_code == 200, r.text


def _org_e_integracao(sessao) -> tuple[int, int]:
    org = models.Organization(
        name=f"Org {uuid4().hex[:6]}", slug=f"org-{uuid4().hex[:6]}", is_active=True
    )
    sessao.add(org)
    sessao.flush()
    integ = models.Integration(
        organization_id=org.id,
        name="Sophos",
        platform="sophos",
        region="us01",
        tenant_id="tenant-abc",
        access_token="tok-xyz",
    )
    sessao.add(integ)
    sessao.commit()
    sessao.refresh(integ)
    return org.id, integ.id


def _query(sessao, org_id: int | None = None) -> int:
    q = models.PredefinedQuery(
        title="Query de teste",
        statement="SELECT * FROM xdr_data LIMIT 1",
        table="xdr_data",
        organization_id=org_id,
    )
    sessao.add(q)
    sessao.commit()
    sessao.refresh(q)
    return q.id


def _agendamento(sessao, *, query_id: int, org_id: int, integ_id: int) -> models.ScheduledQuery:
    """Grava direto no banco.

    Criar pela rota dispararia a execução, que tentaria falar com o fornecedor.
    O alvo aqui é o update, não o create.
    """
    sched = models.ScheduledQuery(
        query_id=query_id,
        organization_id=org_id,
        client_ids=str(integ_id),
        interval_minutes=360,
        interval_value=6,
        interval_unit="hours",
        days_back=1,
        lookback_value=1,
        lookback_unit="days",
        notify_on_results=False,
        next_run=datetime.utcnow() + timedelta(hours=6),
    )
    sessao.add(sched)
    sessao.commit()
    sessao.refresh(sched)
    return sched


# ── o que o operador pediu ────────────────────────────────────────────

def test_edita_intervalo_de_agendamento_ativo(ambiente) -> None:
    cliente, Sessao = ambiente
    c = cliente()
    _admin(c)
    with Sessao() as s:
        org_id, integ_id = _org_e_integracao(s)
        qid = _query(s, org_id)
        sched = _agendamento(s, query_id=qid, org_id=org_id, integ_id=integ_id)
        sched_id = sched.id

    r = c.put(
        f"/api/schedules/{sched_id}",
        json={"interval_value": 15, "interval_unit": "minutes"},
    )

    assert r.status_code == 200, r.text
    with Sessao() as s:
        depois = s.get(models.ScheduledQuery, sched_id)
        assert depois.interval_value == 15
        assert depois.interval_unit == "minutes"
        assert depois.interval_minutes == 15


def test_mudar_a_cadencia_reagenda_a_partir_de_agora(ambiente) -> None:
    """Senão o agendamento fica esperando o ciclo antigo terminar.

    Quem baixa de 24h para 15min espera ver a próxima execução em 15min. Manter
    o ``next_run`` antigo faria a mudança parecer que não pegou.
    """
    cliente, Sessao = ambiente
    c = cliente()
    _admin(c)
    with Sessao() as s:
        org_id, integ_id = _org_e_integracao(s)
        qid = _query(s, org_id)
        sched = _agendamento(s, query_id=qid, org_id=org_id, integ_id=integ_id)
        sched.next_run = datetime.utcnow() + timedelta(hours=23)
        s.commit()
        sched_id = sched.id

    c.put(f"/api/schedules/{sched_id}", json={"interval_value": 15, "interval_unit": "minutes"})

    with Sessao() as s:
        depois = s.get(models.ScheduledQuery, sched_id)
        faltam = depois.next_run - datetime.utcnow()
        assert faltam < timedelta(minutes=20), (
            f"next_run ficou {faltam} à frente; a cadência nova é de 15 minutos."
        )


def test_editar_so_o_valor_reaproveita_a_unidade_guardada(ambiente) -> None:
    cliente, Sessao = ambiente
    c = cliente()
    _admin(c)
    with Sessao() as s:
        org_id, integ_id = _org_e_integracao(s)
        qid = _query(s, org_id)
        sched_id = _agendamento(s, query_id=qid, org_id=org_id, integ_id=integ_id).id

    # Só o valor. A unidade guardada é "hours".
    r = c.put(f"/api/schedules/{sched_id}", json={"interval_value": 12})

    assert r.status_code == 200, r.text
    with Sessao() as s:
        depois = s.get(models.ScheduledQuery, sched_id)
        assert depois.interval_unit == "hours"
        assert depois.interval_minutes == 720, (
            "12 sem unidade virou outra coisa; o par valor+unidade se perdeu."
        )


def test_editar_nao_dispara_execucao(ambiente) -> None:
    """Corrigir um campo não pode gastar quota do fornecedor."""
    cliente, Sessao = ambiente
    c = cliente()
    _admin(c)
    with Sessao() as s:
        org_id, integ_id = _org_e_integracao(s)
        qid = _query(s, org_id)
        sched_id = _agendamento(s, query_id=qid, org_id=org_id, integ_id=integ_id).id
        antes = s.query(models.SearchResult).count()

    c.put(f"/api/schedules/{sched_id}", json={"notify_on_results": True})

    with Sessao() as s:
        assert s.query(models.SearchResult).count() == antes, (
            "A edição criou execução. Editar deve ser inerte."
        )


def test_editar_lookback_recalcula_days_back(ambiente) -> None:
    cliente, Sessao = ambiente
    c = cliente()
    _admin(c)
    with Sessao() as s:
        org_id, integ_id = _org_e_integracao(s)
        qid = _query(s, org_id)
        sched_id = _agendamento(s, query_id=qid, org_id=org_id, integ_id=integ_id).id

    r = c.put(f"/api/schedules/{sched_id}", json={"lookback_value": 72, "lookback_unit": "hours"})

    assert r.status_code == 200, r.text
    with Sessao() as s:
        depois = s.get(models.ScheduledQuery, sched_id)
        assert depois.days_back == 3


# ── o que precisa ser recusado ────────────────────────────────────────

def test_corpo_vazio_e_recusado(ambiente) -> None:
    cliente, Sessao = ambiente
    c = cliente()
    _admin(c)
    with Sessao() as s:
        org_id, integ_id = _org_e_integracao(s)
        qid = _query(s, org_id)
        sched_id = _agendamento(s, query_id=qid, org_id=org_id, integ_id=integ_id).id

    r = c.put(f"/api/schedules/{sched_id}", json={})

    assert r.status_code == 400


def test_campo_desconhecido_vira_422_em_vez_de_sumir(ambiente) -> None:
    """O descarte silencioso do Pydantic já custou investigação neste repo."""
    cliente, Sessao = ambiente
    c = cliente()
    _admin(c)
    with Sessao() as s:
        org_id, integ_id = _org_e_integracao(s)
        qid = _query(s, org_id)
        sched_id = _agendamento(s, query_id=qid, org_id=org_id, integ_id=integ_id).id

    r = c.put(f"/api/schedules/{sched_id}", json={"intervalo": 15})

    assert r.status_code == 422, (
        "Chave desconhecida foi aceita. O operador recebe 200 e o valor não muda."
    )


def test_lista_de_integracoes_vazia_e_recusada(ambiente) -> None:
    """Agendamento sem integração fica vivo e nunca roda."""
    cliente, Sessao = ambiente
    c = cliente()
    _admin(c)
    with Sessao() as s:
        org_id, integ_id = _org_e_integracao(s)
        qid = _query(s, org_id)
        sched_id = _agendamento(s, query_id=qid, org_id=org_id, integ_id=integ_id).id

    r = c.put(f"/api/schedules/{sched_id}", json={"client_ids": []})

    assert r.status_code == 422


def test_agendamento_inexistente_da_404(ambiente) -> None:
    cliente, _ = ambiente
    c = cliente()
    _admin(c)

    r = c.put("/api/schedules/999999", json={"interval_value": 5})

    assert r.status_code == 404
