"""O dedup de ``Detection`` é serializado por ``(organization_id, dedup_key)``.

``DetectionRepository.record`` faz read-then-write: procura uma Detection aberta
com a mesma chave dentro da janela de supressão e, se não achar, insere. Duas
tasks que casam a MESMA chave no mesmo instante fazem ambas o SELECT, nenhuma
acha, e ambas inserem — duas Detections para o evento que a supressão existe
para agrupar.

O dano não é a linha extra: é a supressão anti-spam deixar de suprimir
exatamente quando há volume, que é quando ela importa.

**Por que advisory lock e não UniqueConstraint.** A unicidade de
``(organization_id, dedup_key)`` está ausente DE PROPÓSITO — o docstring de
``models.Detection`` diz, com todas as letras, *"Sem UniqueConstraint — após a
janela, um novo alerta é legítimo"*. Um índice único faria a regra disparar uma
vez e nunca mais. O que precisa ser serializado é a JANELA, não a chave; e é
isso que o lock transaction-scoped faz, sem tocar no schema.

Este arquivo cobre os dois eixos que podem regredir em silêncio: o lock ser
pedido no Postgres, e a semântica pós-janela continuar intacta apesar dele.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import models, repository
from backend.app.db.models import Base

JANELA = 300


@pytest.fixture()
def sessao():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(eng, "connect")
    def _fk(dbapi_conn, _record):  # noqa: ANN001
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(eng)
    with sessionmaker(bind=eng)() as db:
        db.add(models.Organization(id=1, name="org-de-teste", slug="org-de-teste"))
        db.commit()
        yield db


def _record(db, *, dedup_key: str = "inflight:1:7:host-a"):
    return repository.DetectionRepository(db).record(
        organization_id=1,
        source="inflight",
        dedup_key=dedup_key,
        rule_id="7",
        rule_name="regra-de-teste",
        suppression_window_seconds=JANELA,
    )


# ── o lock existe, e só onde faz sentido ────────────────────────────────────


def test_no_postgres_o_lock_e_pedido_antes_do_select(monkeypatch) -> None:
    """CONTA as execuções em vez de levantar sentinela dentro do dublê: o
    ``record`` não tem ``except`` aqui, mas contar é o que sobrevive a alguém
    acrescentar um — e é a forma que o resto da suíte usa."""
    executados: list[str] = []

    class _BindPostgres:
        class dialect:  # noqa: N801
            name = "postgresql"

    db = _EspiaoDeSessao(executados, bind=_BindPostgres())
    repo = repository.DetectionRepository(db)
    with pytest.raises(_ParouNoSelect):
        repo.record(
            organization_id=1, source="inflight", dedup_key="k", suppression_window_seconds=JANELA
        )

    assert executados, "nenhum SQL cru foi executado — o lock não foi pedido"
    sql = executados[0].lower()
    assert "pg_advisory_xact_lock" in sql, (
        f"o primeiro SQL executado não é o lock, é: {executados[0]!r}. O lock tem "
        "de vir ANTES do SELECT, senão a janela de corrida continua aberta."
    )
    assert "hashtext" in sql, "a chave do lock não está sendo derivada da dedup_key"


def test_no_sqlite_o_lock_nao_e_pedido(sessao) -> None:
    """PAR POSITIVO do teste acima, e não simetria decorativa: ``pg_advisory_xact_lock``
    não existe no SQLite e derrubaria toda a suíte. Se este teste ficar vermelho,
    o ``if`` de dialeto virou incondicional."""
    executados: list[str] = []

    original = sessao.execute

    def _espia(stmt, *a, **kw):
        executados.append(str(stmt))
        return original(stmt, *a, **kw)

    sessao.execute = _espia  # type: ignore[method-assign]
    det = _record(sessao)

    assert det.id is not None, "o record falhou — o teste não mediu nada"
    assert not [s for s in executados if "advisory" in s.lower()], (
        "o advisory lock foi pedido num SQLite, onde a função não existe"
    )


# ── a semântica que o UniqueConstraint teria quebrado ────────────────────────


def test_dentro_da_janela_bumpa_em_vez_de_criar(sessao) -> None:
    primeira = _record(sessao)
    segunda = _record(sessao)

    assert segunda.id == primeira.id, "criou linha nova dentro da janela"
    assert segunda.count == 2
    assert _quantas(sessao) == 1


def test_depois_da_janela_uma_nova_deteccao_e_legitima(sessao) -> None:
    """O caso que um ``UniqueConstraint(organization_id, dedup_key)`` tornaria
    IMPOSSÍVEL — e o motivo de ele não existir.

    Uma regra que dispara hoje e volta a disparar amanhã tem de gerar dois
    alertas. Com índice único, geraria um só, para sempre."""
    primeira = _record(sessao)

    # Envelhece a primeira para além da janela, de forma determinística — dormir
    # 300s não é opção e o relógio não precisa participar disto.
    primeira.last_seen = datetime.utcnow() - timedelta(seconds=JANELA + 60)
    sessao.commit()

    segunda = _record(sessao)

    assert segunda.id != primeira.id, (
        "a segunda detecção foi absorvida pela primeira. Passada a janela de "
        "supressão, um alerta novo é o comportamento CORRETO — é exatamente o "
        "que a ausência de UniqueConstraint protege."
    )
    assert segunda.count == 1
    assert _quantas(sessao) == 2


def test_chaves_diferentes_nunca_se_misturam(sessao) -> None:
    """Anti-vacuidade dos dois acima: se o ``record`` passasse a ignorar a
    ``dedup_key``, o teste da janela ainda passaria (uma linha só), e este
    reprova."""
    a = _record(sessao, dedup_key="inflight:1:7:host-a")
    b = _record(sessao, dedup_key="inflight:1:7:host-b")

    assert a.id != b.id
    assert _quantas(sessao) == 2


# ── auxiliares ───────────────────────────────────────────────────────────────


def _quantas(db) -> int:
    return db.query(models.Detection).count()


class _ParouNoSelect(RuntimeError):
    """Interrompe o ``record`` logo após o lock, para o teste não precisar de um
    banco real só para observar a ORDEM das operações."""


class _EspiaoDeSessao:
    """Sessão mínima que registra SQL cru e aborta na primeira query do ORM."""

    def __init__(self, registro: list[str], *, bind) -> None:
        self._registro = registro
        self._bind = bind

    def get_bind(self):  # noqa: ANN201
        return self._bind

    def execute(self, stmt, *_a, **_kw):  # noqa: ANN201
        self._registro.append(str(stmt))
        return None

    def query(self, *_a, **_kw):  # noqa: ANN201
        raise _ParouNoSelect
