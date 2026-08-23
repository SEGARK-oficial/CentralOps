"""A LINHA ``Detection`` gravada pelo flush em voo (ADR-0015, Fase 1).

Este é o único passo da cadeia em voo que entrega produto — o alerta que o
operador vê — e era o único elo sem cobertura. A causa é mecânica: ``_flush_sync``
está monkeypatchado em TODOS os testes que o alcançam
(``test_adr0015_inflight_rule_metrics``, ``test_adr0015_inflight_label_parity`` e
o teste de rota do repo EE), e o literal ``source="inflight"`` não aparecia em
nenhum assert do repositório. Consequência: um erro de assinatura, de FK, de tipo
de coluna ou de sessão em ``DetectionRepository.record`` passava verde na suíte
inteira e só aparecia em produção, na forma "a regra dispara, o contador de 24h
sobe, e a lista de detecções continua vazia".

Aqui ``_flush_sync`` e ``record`` rodam DE VERDADE contra um banco de teste, com
``PRAGMA foreign_keys=ON`` — sem a pragma o SQLite ignora FK e o assert de
``organization_id`` aprovaria uma linha órfã, que é exatamente o defeito que o
arquivo existe para pegar.

Cobre também o DEDUP LÓGICO descrito no docstring de ``models.Detection``, nos
DOIS sentidos: match repetido DENTRO da janela bumpa ``count``/``last_seen``;
FORA da janela nasce linha nova. Sem o par, um ``record`` que sempre criasse
linha nova (ou que sempre bumpasse) passaria com metade dos asserts.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import logging
from datetime import datetime, timedelta
from uuid import uuid4

import fakeredis
import pytest
from sqlalchemy import create_engine, event as sa_event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.collectors import observability_store as obs
from backend.app.collectors.inflight import runtime as runtime_mod
from backend.app.collectors.inflight.matcher import CompiledInflightRule
from backend.app.collectors.inflight.runtime import (
    InflightAccumulator,
    InflightFlushInterrupted,
    flush_inflight,
)
from backend.app.db import database, models
from backend.app.db.database import Base


# ── Harness ────────────────────────────────────────────────────────────────


@pytest.fixture()
def db_session(monkeypatch: pytest.MonkeyPatch):
    """Engine próprio + ``database.SessionLocal`` apontado para ele.

    ``_flush_sync`` abre ``database.SessionLocal()`` (engine GLOBAL) e não
    aceita sessão injetada — o ponto de troca é o atributo do módulo, resolvido
    a cada chamada pelo ``from ...db import database`` interno.

    ``StaticPool`` + ``check_same_thread=False`` porque ``flush_inflight``
    despacha o flush por ``asyncio.to_thread``: com o pool default cada thread
    abriria uma conexão nova, e cada conexão a ``:memory:`` é um banco SEPARADO
    e VAZIO — o sintoma seria "no such table: detections" vindo da thread.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @sa_event.listens_for(engine, "connect")
    def _enforce_fk(dbapi_conn: object, _rec: object) -> None:
        # O SQLite ignora FOREIGN KEY por default. Sem esta pragma, uma
        # Detection apontando para org/integração inexistente seria gravada em
        # silêncio e o assert de ``organization_id`` aprovaria uma linha órfã —
        # o teste ficaria verde justamente na classe de erro que ele promete
        # pegar (o Postgres de produção recusaria).
        cur = dbapi_conn.cursor()  # type: ignore[attr-defined]
        try:
            cur.execute("PRAGMA foreign_keys=ON")
        finally:
            cur.close()

    maker = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", maker)

    yield maker

    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture(autouse=True)
def _fake_obs(monkeypatch: pytest.MonkeyPatch) -> None:
    """``flush_inflight`` também espelha contadores no observability_store.
    Sem isto cada teste daqui tentaria abrir um Redis REAL em localhost — o
    ``record_counter`` engole a falha, mas o gate passaria a depender da
    máquina. fakeredis mantém esse caminho REAL e hermético."""
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(obs, "_redis", lambda: r)


def _seed_org(db_session) -> int:
    slug = f"org-{uuid4().hex[:8]}"
    with db_session() as db:
        org = models.Organization(name=slug, slug=slug, is_active=True)
        db.add(org)
        db.commit()
        db.refresh(org)
        return int(org.id)


def _seed_integration(db_session, *, org_id: int) -> int:
    with db_session() as db:
        intg = models.Integration(
            organization_id=org_id,
            name=f"intg-{uuid4().hex[:6]}",
            platform="sophos",
        )
        db.add(intg)
        db.commit()
        db.refresh(intg)
        return int(intg.id)


def _rule(
    rule_id: int = 77,
    *,
    name: str = "ssh brute force",
    severity_id: int = 5,
    window: int = 3600,
    group_by: tuple[str, ...] | None = ("user",),
) -> CompiledInflightRule:
    return CompiledInflightRule(
        rule_id=rule_id,
        name=name,
        severity_id=severity_id,
        suppression_window_seconds=window,
        group_by_path=group_by,
        clauses=(),
    )


def _detections(db_session) -> list[models.Detection]:
    with db_session() as db:
        return db.query(models.Detection).order_by(models.Detection.id.asc()).all()


def _backdate(db_session, detection_id: int, *, seconds: int) -> datetime:
    """Empurra ``last_seen`` para trás. Determinístico de propósito: comparar
    dois ``utcnow()`` consecutivos para provar o bump dependeria da resolução
    do relógio da máquina."""
    alvo = datetime.utcnow() - timedelta(seconds=seconds)
    with db_session() as db:
        det = db.get(models.Detection, detection_id)
        assert det is not None
        det.last_seen = alvo
        db.commit()
    return alvo


# ── 1. A linha existe, com os campos que importam ──────────────────────────


@pytest.mark.asyncio
async def test_flush_writes_a_real_detection_row(db_session) -> None:
    """O elo que faltava: ``flush_inflight`` → ``_flush_sync`` → ``record`` →
    linha no banco. Sem este teste, ``source="inflight"`` não era afirmado em
    lugar nenhum do repositório."""
    org_id = _seed_org(db_session)
    intg_id = _seed_integration(db_session, org_id=org_id)

    acc = InflightAccumulator()
    acc.add(
        _rule(77, name="ssh brute force", severity_id=5, window=900),
        {"user": "alice"},
        organization_id=org_id,
        integration_id=intg_id,
    )
    # ANTI-VACUIDADE: se ``add`` deixasse de acumular, todo assert de banco
    # abaixo viraria "0 == 0" e o arquivo inteiro aprovaria sem gravar nada.
    assert len(acc.pending) == 1

    await flush_inflight(acc, organization_id=org_id)

    linhas = _detections(db_session)
    assert len(linhas) == 1
    det = linhas[0]
    assert det.source == "inflight"
    assert det.organization_id == org_id
    assert det.integration_id == intg_id
    assert det.dedup_key == f"inflight:{org_id}:77:alice"
    assert det.severity_id == 5
    assert det.rule_name == "ssh brute force"
    assert det.suppression_window_seconds == 900
    assert det.status == "open"
    assert det.count == 1
    assert det.first_seen is not None and det.last_seen is not None
    assert det.first_seen == det.last_seen  # 1ª ocorrência

    # ``Detection.rule_id`` é coluna STRING; o valor lido de volta é "77" e não
    # 77. Este assert NÃO prova o tipo enviado — a afinidade TEXT do SQLite
    # converteria um int em silêncio. Quem trava o tipo é o teste da fronteira,
    # logo abaixo.
    assert det.rule_id == "77"

    # Caminho feliz não escreve erro nenhum — par positivo do assert de perda
    # que vem lá embaixo.
    assert acc.errors == {}


@pytest.mark.asyncio
async def test_rule_id_reaches_the_repository_as_a_string(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``rule_id`` é ``int`` na regra compilada e ``String`` na coluna —
    ``_flush_sync`` faz a conversão, e ela é load-bearing.

    Verificado na FRONTEIRA (kwargs que chegam a ``record``) porque o banco de
    teste não consegue reprovar: a afinidade TEXT do SQLite converte um int em
    silêncio e a linha volta como ``"77"`` de qualquer jeito. O Postgres de
    produção RECUSA o INSERT (integer em varchar) — perder o ``str()`` seria
    verde na suíte inteira e 100% de falha de gravação depois do deploy.
    """
    from backend.app.db import repository

    org_id = _seed_org(db_session)
    original = repository.DetectionRepository.record
    vistos: list[dict] = []

    def _spy(self, **kwargs):
        vistos.append(dict(kwargs))
        return original(self, **kwargs)

    monkeypatch.setattr(repository.DetectionRepository, "record", _spy)

    acc = InflightAccumulator()
    acc.add(_rule(77), {"user": "alice"}, organization_id=org_id)
    await flush_inflight(acc, organization_id=org_id)

    # Par positivo: o espião DELEGOU de verdade. Sem isto, um espião que só
    # coletasse kwargs deixaria o assert de tipo verde sem nada ser gravado.
    assert len(_detections(db_session)) == 1
    assert len(vistos) == 1
    assert vistos[0]["rule_id"] == "77"
    assert isinstance(vistos[0]["rule_id"], str), (
        "int em coluna varchar: o Postgres recusa o INSERT, o SQLite converte "
        "calado — por isso o tipo é afirmado aqui e não no valor lido de volta"
    )
    assert isinstance(vistos[0]["organization_id"], int)
    assert isinstance(vistos[0]["severity_id"], int)
    assert isinstance(vistos[0]["suppression_window_seconds"], int)


@pytest.mark.asyncio
async def test_each_dedup_key_becomes_its_own_row(db_session) -> None:
    """``_flush_sync`` percorre ``pending`` inteiro. Um ``break`` acidental no
    laço gravaria só a primeira Detection do ciclo e nada acusaria."""
    org_id = _seed_org(db_session)
    rule = _rule(88, group_by=("user",))

    acc = InflightAccumulator()
    for user in ("alice", "bob", "carol"):
        acc.add(rule, {"user": user}, organization_id=org_id)
    assert len(acc.pending) == 3

    await flush_inflight(acc, organization_id=org_id)

    linhas = _detections(db_session)
    assert [d.dedup_key for d in linhas] == [
        f"inflight:{org_id}:88:alice",
        f"inflight:{org_id}:88:bob",
        f"inflight:{org_id}:88:carol",
    ]
    assert {d.count for d in linhas} == {1}


@pytest.mark.asyncio
async def test_detection_is_scoped_to_the_org_that_matched(db_session) -> None:
    """Duas orgs, a MESMA regra e o MESMO valor de group_by: a ``dedup_key``
    carrega o ``organization_id``, então uma org não pode suprimir o alerta da
    outra — vazamento cross-tenant com cara de anti-spam."""
    org_a = _seed_org(db_session)
    org_b = _seed_org(db_session)
    rule = _rule(99)

    for org_id in (org_a, org_b):
        acc = InflightAccumulator()
        acc.add(rule, {"user": "alice"}, organization_id=org_id)
        await flush_inflight(acc, organization_id=org_id)

    linhas = _detections(db_session)
    assert len(linhas) == 2
    assert {d.organization_id for d in linhas} == {org_a, org_b}
    assert {d.count for d in linhas} == {1}
    assert len({d.dedup_key for d in linhas}) == 2


# ── 2. Dedup lógico: os DOIS lados da janela de supressão ───────────────────


@pytest.mark.asyncio
async def test_repeated_match_inside_the_window_bumps_instead_of_creating_a_row(
    db_session,
) -> None:
    """Anti-spam: dentro da ``suppression_window_seconds`` o mesmo
    ``(organization_id, dedup_key)`` BUMPA ``count``/``last_seen``. Sem este
    lado, um ``record`` que sempre criasse linha nova passaria — e o operador
    receberia um alerta por ciclo de coleta, para sempre."""
    org_id = _seed_org(db_session)
    rule = _rule(101, window=3600)

    acc1 = InflightAccumulator()
    acc1.add(rule, {"user": "alice"}, organization_id=org_id)
    await flush_inflight(acc1, organization_id=org_id)

    (primeira,) = _detections(db_session)
    det_id, first_seen = int(primeira.id), primeira.first_seen
    # Ainda MUITO dentro da janela de 1h — e com folga determinística contra a
    # resolução do relógio, para o assert de ``last_seen`` ser sobre o bump.
    antigo_last_seen = _backdate(db_session, det_id, seconds=60)

    acc2 = InflightAccumulator()
    acc2.add(rule, {"user": "alice"}, organization_id=org_id)
    await flush_inflight(acc2, organization_id=org_id)

    linhas = _detections(db_session)
    assert len(linhas) == 1, "match dentro da janela não pode criar linha nova"
    det = linhas[0]
    assert int(det.id) == det_id
    assert det.count == 2
    assert det.last_seen > antigo_last_seen
    assert det.first_seen == first_seen, "o bump não reescreve a 1ª ocorrência"
    assert acc2.errors == {}


@pytest.mark.asyncio
async def test_match_after_the_window_creates_a_new_row_instead_of_bumping(
    db_session,
) -> None:
    """O lado oposto, e o que impede o alerta de morrer: passada a janela, uma
    nova ocorrência é um alerta NOVO. Sem este lado, um ``record`` que sempre
    bumpasse passaria — a linha viraria um contador eterno com ``first_seen`` de
    meses atrás, e o incidente de hoje ficaria escondido dentro do de março."""
    org_id = _seed_org(db_session)
    rule = _rule(102, window=60)

    acc1 = InflightAccumulator()
    acc1.add(rule, {"user": "alice"}, organization_id=org_id)
    await flush_inflight(acc1, organization_id=org_id)

    (primeira,) = _detections(db_session)
    det_id = int(primeira.id)
    _backdate(db_session, det_id, seconds=3600)  # FORA da janela de 60s

    acc2 = InflightAccumulator()
    acc2.add(rule, {"user": "alice"}, organization_id=org_id)
    await flush_inflight(acc2, organization_id=org_id)

    linhas = _detections(db_session)
    assert len(linhas) == 2, "fora da janela, a ocorrência nova é alerta NOVO"
    assert [int(d.id) for d in linhas][0] == det_id
    assert {d.count for d in linhas} == {1}, "linha nova nasce com count=1"
    assert len({d.dedup_key for d in linhas}) == 1, "mesma chave, alertas distintos"


# ── 3. ``flush_lost`` conta a perda REAL, não ``len(acc.pending)`` ──────────
#
# ``record`` commita POR CHAVE: numa falha no meio do laço, as Detections
# anteriores JÁ estão duráveis. Contá-las como perda inflava a única série que
# mede o dano ao cliente.


@pytest.mark.asyncio
async def test_partial_flush_counts_only_what_was_not_committed(db_session) -> None:
    """Falha REAL de FK no meio do flush, sem duplo nenhum: a 3ª Detection
    aponta para uma integração inexistente e o commit é recusado.

    As duas primeiras ficam no banco; a 3ª e a 4ª se perdem. ``flush_lost`` tem
    de dizer 2 — não 4.
    """
    org_id = _seed_org(db_session)
    intg_id = _seed_integration(db_session, org_id=org_id)
    INTEGRACAO_INEXISTENTE = 10_000_001

    acc = InflightAccumulator()
    # Uma regra POR posição: é o que torna a atribuição verificável. Com uma só
    # regra, "atribuiu certo" e "atribuiu tudo à única regra que existe" seriam
    # o mesmo assert.
    acc.add(_rule(1, group_by=("user",)), {"user": "a"}, organization_id=org_id,
            integration_id=intg_id)
    acc.add(_rule(2, group_by=("user",)), {"user": "b"}, organization_id=org_id,
            integration_id=intg_id)
    acc.add(_rule(3, group_by=("user",)), {"user": "c"}, organization_id=org_id,
            integration_id=INTEGRACAO_INEXISTENTE)
    acc.add(_rule(4, group_by=("user",)), {"user": "d"}, organization_id=org_id,
            integration_id=intg_id)
    assert len(acc.pending) == 4

    await flush_inflight(acc, organization_id=org_id)  # best-effort: não levanta

    gravadas = _detections(db_session)
    assert [d.rule_id for d in gravadas] == ["1", "2"], (
        "as anteriores à falha estão COMMITADAS — é isso que torna a "
        "sobrecontagem uma mentira, e não um arredondamento"
    )

    # A conta honesta: 2 perdidas de 4 pendentes, atribuídas às regras certas.
    assert acc.errors["flush_lost"] == {3: 1, 4: 1}
    assert sum(acc.errors["flush_lost"].values()) == 2
    # Espelho negativo COM o par positivo acima: quem foi gravado NÃO aparece.
    assert 1 not in acc.errors["flush_lost"]
    assert 2 not in acc.errors["flush_lost"]
    # E o número velho, explicitamente, para o revert ficar visível.
    assert sum(acc.errors["flush_lost"].values()) != len(acc.pending)


def test_flush_sync_reports_the_keys_it_managed_to_commit(db_session) -> None:
    """Guard direto da mecânica: ``_flush_sync`` levanta
    ``InflightFlushInterrupted`` carregando as chaves já commitadas.

    O ``written`` que a função devolve NÃO sobrevive ao ``raise`` — a exceção é
    a única coisa que atravessa o ``to_thread``. Se ela voltar a subir pelada,
    ``flush_inflight`` cai no ramo de teto e a sobrecontagem volta em silêncio.
    """
    org_id = _seed_org(db_session)
    intg_id = _seed_integration(db_session, org_id=org_id)

    pending = {
        f"inflight:{org_id}:1:a": {"rule": _rule(1), "integration_id": intg_id},
        f"inflight:{org_id}:2:b": {"rule": _rule(2), "integration_id": 10_000_002},
        f"inflight:{org_id}:3:c": {"rule": _rule(3), "integration_id": intg_id},
    }

    with pytest.raises(InflightFlushInterrupted) as exc_info:
        runtime_mod._flush_sync(pending, org_id)

    assert exc_info.value.written_keys == (f"inflight:{org_id}:1:a",)
    # A causa verdadeira continua encadeada — o ``logger.exception`` do call
    # site imprime o erro do banco, não só o invólucro.
    assert exc_info.value.__cause__ is not None
    assert len(_detections(db_session)) == 1


@pytest.mark.asyncio
async def test_a_flush_that_writes_everything_costs_no_flush_lost(db_session) -> None:
    """Par positivo do bloco: sem falha, ``flush_lost`` não existe. Sem ele, um
    ``count_error`` que nunca fosse chamado passaria pelos asserts acima."""
    org_id = _seed_org(db_session)

    acc = InflightAccumulator()
    acc.add(_rule(1), {"user": "a"}, organization_id=org_id)
    acc.add(_rule(2), {"user": "b"}, organization_id=org_id)

    await flush_inflight(acc, organization_id=org_id)

    assert len(_detections(db_session)) == 2
    assert "flush_lost" not in acc.errors


@pytest.mark.asyncio
async def test_unmeasurable_failure_counts_everything_and_says_it_is_a_ceiling(
    db_session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A DEGRADAÇÃO DECLARADA, e o contrato de que ela é declarada.

    Quando a exceção nasce FORA do laço de escrita (``_flush_sync`` substituído,
    falha ao despachar para a thread) não existe medida de quantas foram
    gravadas. O código conta tudo — pessimismo do lado certo, porque calar perda
    que houve é pior que reportar perda que não houve — e o log tem de dizer que
    o número é um TETO, para ninguém tratá-lo como medição.
    """
    org_id = _seed_org(db_session)

    def _boom(*_a: object, **_k: object) -> int:
        raise RuntimeError("Postgres indisponível antes do primeiro record")

    monkeypatch.setattr(runtime_mod, "_flush_sync", _boom)

    acc = InflightAccumulator()
    acc.add(_rule(1), {"user": "a"}, organization_id=org_id)
    acc.add(_rule(2), {"user": "b"}, organization_id=org_id)

    with caplog.at_level(logging.ERROR):
        await flush_inflight(acc, organization_id=org_id)

    assert len(_detections(db_session)) == 0
    assert acc.errors["flush_lost"] == {1: 1, 2: 1}

    # CONTAGEM, não ``not any(...)``: um assert negativo aqui aprovaria por
    # vacuidade se o log parasse de sair.
    tetos = [rec for rec in caplog.records if "TETO" in rec.getMessage()]
    assert len(tetos) == 1, (
        "o ramo sem medida tem de DIZER que o número é um teto; sem essa "
        "palavra no log, ele é indistinguível de uma perda medida"
    )
    assert "até 2 Detection" in tetos[0].getMessage()
