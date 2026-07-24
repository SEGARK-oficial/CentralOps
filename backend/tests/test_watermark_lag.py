"""Watermark: até onde a coleta chegou na linha do tempo do FORNECEDOR.

O ponto cego que originou isto (produção, jul/2026): ``lag_seconds`` mede
``agora − last_success_at``, e ``last_success_at`` é reescrito a cada ciclo que
termina sem erro — inclusive quando o ciclo processou o dia ANTERIOR. Um coletor
15 horas atrasado reportava ``lag_seconds: 0`` e status ``healthy``, por semanas.

``watermark_at`` responde a outra pergunta: *até que instante do fornecedor nós
consumimos?* E ``last_run_capped`` responde *sobrou trabalho?*. Os dois só valem
juntos — watermark parado sem teto atingido é um stream sem eventos, o que é
normal e não pode acender alarme.

O router da Saúde do Pipeline (agregação, limiar, escalonamento para
``degraded``) é coberto em ``test_pipeline_health_router.py``; aqui ficam as
camadas abaixo: tradução do cursor, persistência e o ponto do ciclo que grava.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import inspect
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.collectors import registry as registry_module
from backend.app.collectors.base import BaseCollector
from backend.app.collectors.vendors.wazuh_detections import WazuhDetectionsCollector
from backend.app.db import models
from backend.app.db.database import Base
from backend.app.db.repository import CollectionStateRepository


# ── Tradução do cursor → instante do fornecedor ──────────────────────────


def test_watermark_parses_the_wazuh_offset_format() -> None:
    """``+0000`` (sem dois-pontos) é o que o Wazuh grava no campo ``timestamp``."""
    at = WazuhDetectionsCollector.watermark_at({"from_ts": "2026-07-24T10:30:00.000+0000"})
    assert at == datetime(2026, 7, 24, 10, 30, 0)


def test_watermark_parses_the_z_suffix_format() -> None:
    """``Z`` é o formato do lookback default do próprio módulo.

    Os dois formatos convivem no MESMO campo: o cursor nasce com ``Z`` no
    primeiro ciclo e passa a ``+0000`` assim que um alerta real é lido. Um parser
    que só entendesse um deles perderia o indicador exatamente quando ele começa
    a valer.
    """
    at = WazuhDetectionsCollector.watermark_at({"from_ts": "2026-07-24T10:30:00Z"})
    assert at == datetime(2026, 7, 24, 10, 30, 0)


def test_watermark_parses_the_iso_offset_with_colon() -> None:
    at = WazuhDetectionsCollector.watermark_at({"from_ts": "2026-07-24T10:30:00+00:00"})
    assert at == datetime(2026, 7, 24, 10, 30, 0)


def test_watermark_normalizes_a_non_utc_offset_to_utc() -> None:
    """Indexer em fuso local não pode virar 3h de atraso fantasma.

    A comparação a jusante é contra ``datetime.utcnow()`` (naive); devolver o
    horário local marcaria backlog onde não há.
    """
    at = WazuhDetectionsCollector.watermark_at({"from_ts": "2026-07-24T07:30:00-0300"})
    assert at == datetime(2026, 7, 24, 10, 30, 0)


def test_watermark_is_naive_utc_for_comparison_with_utcnow() -> None:
    at = WazuhDetectionsCollector.watermark_at({"from_ts": "2026-07-24T10:30:00Z"})
    assert at is not None and at.tzinfo is None


@pytest.mark.parametrize(
    "cursor",
    [
        None,
        {},
        {"from_ts": None},
        {"from_ts": ""},
        {"from_ts": 1753351800},
        {"from_ts": "ontem de manhã"},
        {"from_ts": "2026-13-45T99:99:99Z"},
        {"offset": 400},
    ],
    ids=["none", "vazio", "null", "string-vazia", "epoch-int", "texto", "data-invalida", "sem-chave"],
)
def test_watermark_is_none_for_missing_or_garbage_cursor(cursor) -> None:
    """``None`` = "não medível". A Saúde omite o indicador em vez de inventar.

    O que NÃO pode acontecer é devolver 0/agora: seria afirmar "em dia" com base
    em um cursor que ninguém conseguiu ler.
    """
    assert WazuhDetectionsCollector.watermark_at(cursor) is None


def test_unparseable_cursor_logs_a_warning(caplog) -> None:
    """Sumir em silêncio é o modo de falha que este campo existe para evitar."""
    with caplog.at_level("WARNING"):
        assert WazuhDetectionsCollector.watermark_at({"from_ts": "nao-e-iso"}) is None
    assert "atraso de watermark" in caplog.text


# ── Coletor sem cursor temporal ──────────────────────────────────────────


class _OpaquePageCollector(BaseCollector):
    """Cursor de página opaca (``{"next": "abc123"}``) — não tem instante algum."""

    platform = "dummy"
    stream = "things"
    event_type = "dummy.thing"

    @property
    def domain(self) -> str:  # pragma: no cover
        return "dummy.example"

    async def collect(self):  # pragma: no cover
        if False:
            yield {}

    def extract_message_id(self, event: Dict[str, Any]) -> str:  # pragma: no cover
        return ""


def test_collector_without_override_reports_no_watermark() -> None:
    """Não implementar ``watermark_at`` é legítimo e devolve ``None``.

    Cursor de página opaca não tem tradução para o relógio. O contrato é
    devolver ``None`` — a Saúde omite o campo — e nunca fabricar um instante.
    """
    assert _OpaquePageCollector.watermark_at({"next": "abc123"}) is None
    assert _OpaquePageCollector.watermark_at(None) is None
    assert BaseCollector.watermark_at({"from_ts": "2026-07-24T10:30:00Z"}) is None


def test_watermark_at_is_callable_without_instantiating_the_collector() -> None:
    """O pipeline chama ``collector_cls.watermark_at(ctx.cursor)`` — é classmethod.

    Se virasse método de instância, o ponto de gravação (que roda depois do
    ``collect()``) quebraria em runtime, no ciclo, e o indicador sumiria.
    """
    assert isinstance(
        inspect.getattr_static(WazuhDetectionsCollector, "watermark_at"), classmethod
    )


@pytest.mark.parametrize(
    "reg",
    registry_module.all_registrations(),
    ids=[f"{r.platform}/{r.stream}" for r in registry_module.all_registrations()],
)
def test_every_registered_collector_survives_a_foreign_cursor(reg) -> None:
    """``watermark_at`` roda no fim de TODO ciclo, para todo vendor.

    Uma exceção aqui não derruba o ciclo (o pipeline a engole), mas apagaria o
    indicador de atraso do vendor inteiro sem nenhum sinal na UI. Cursores de
    outros vendores é o cenário real depois de um restore ou de troca de stream.
    """
    for cursor in (None, {}, {"from_ts": "lixo"}, {"next": "abc"}, {"from_ts": 123}):
        try:
            got = reg.collector_cls.watermark_at(cursor)
        except Exception as exc:  # noqa: BLE001 — é exatamente o que se proíbe aqui
            pytest.fail(
                f"{reg.platform}/{reg.stream}.watermark_at({cursor!r}) levantou {exc!r}; "
                "o contrato é devolver None quando não dá para traduzir o cursor"
            )
        assert got is None or isinstance(got, datetime), (
            f"{reg.platform}/{reg.stream}.watermark_at devolveu {got!r} — só "
            "datetime ou None são comparáveis com o relógio"
        )


# ── Persistência ─────────────────────────────────────────────────────────


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = Session()
    db.add(models.Organization(id=1, name="Acme", slug="acme"))
    db.add(models.Integration(id=1, organization_id=1, name="wazuh", platform="wazuh"))
    db.commit()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def _repo(db) -> CollectionStateRepository:
    return CollectionStateRepository(db)


def test_first_cycle_records_watermark_and_cap(db_session) -> None:
    row = _repo(db_session).upsert(
        integration_id=1,
        stream="detections",
        cursor='{"from_ts":"2026-07-24T10:00:00Z"}',
        events_collected=10_000,
        watermark_at=datetime(2026, 7, 24, 10, 0, 0),
        last_run_capped=True,
    )
    assert row.watermark_at == datetime(2026, 7, 24, 10, 0, 0)
    assert row.last_run_capped is True


def test_default_upsert_writes_no_watermark_and_no_cap(db_session) -> None:
    """Chamada antiga (sem os dois argumentos) continua válida e neutra."""
    row = _repo(db_session).upsert(
        integration_id=1, stream="detections", cursor="{}", events_collected=0
    )
    assert row.watermark_at is None
    assert row.last_run_capped is False


def test_failed_cycle_preserves_the_watermark(db_session) -> None:
    """Watermark é uma POSIÇÃO alcançada, não um heartbeat.

    Zerá-lo no ciclo que falhou faria o atraso saltar para "desde sempre" e
    depois voltar ao normal — ruído justamente no sinal que existe para detectar
    backlog. E, pior, um erro transitório apagaria a única evidência de que a
    coleta estava atrasada ANTES da falha.
    """
    repo = _repo(db_session)
    repo.upsert(
        integration_id=1,
        stream="detections",
        cursor='{"from_ts":"2026-07-24T10:00:00Z"}',
        events_collected=100,
        watermark_at=datetime(2026, 7, 24, 10, 0, 0),
    )
    row = repo.upsert(
        integration_id=1,
        stream="detections",
        cursor='{"from_ts":"2026-07-24T10:00:00Z"}',
        events_collected=0,
        error="503 do Indexer",
    )
    assert row.watermark_at == datetime(2026, 7, 24, 10, 0, 0), (
        "o ciclo com erro apagou o watermark — o atraso acumulado sumiu do radar"
    )
    assert row.last_error == "503 do Indexer"
    assert row.consecutive_failures == 1


def test_failed_cycle_preserves_the_backlog_flag(db_session) -> None:
    """Um ciclo que morreu não drenou nada — logo "sobrou trabalho" continua valendo.

    Zerar a flag no erro apagaria a etiqueta de backlog a cada falha transitória,
    ou seja, exatamente quando o coletor está PIOR. O par com o watermark é o que
    sustenta o diagnóstico: sem a flag, atraso alto vira indistinguível de stream
    silencioso, que é o falso positivo que este desenho existe para evitar.
    """
    repo = _repo(db_session)
    repo.upsert(
        integration_id=1,
        stream="detections",
        cursor='{"from_ts":"2026-07-24T10:00:00Z"}',
        events_collected=10_000,
        watermark_at=datetime(2026, 7, 24, 10, 0, 0),
        last_run_capped=True,
    )
    row = repo.upsert(
        integration_id=1,
        stream="detections",
        cursor='{"from_ts":"2026-07-24T10:00:00Z"}',
        events_collected=0,
        error="503 do Indexer",
    )
    assert row.last_run_capped is True, (
        "o ciclo com erro baixou a flag de backlog — o sinal some justo na falha"
    )


def test_watermark_advances_when_the_cycle_progresses(db_session) -> None:
    repo = _repo(db_session)
    repo.upsert(
        integration_id=1,
        stream="detections",
        cursor="{}",
        events_collected=1,
        watermark_at=datetime(2026, 7, 24, 10, 0, 0),
    )
    row = repo.upsert(
        integration_id=1,
        stream="detections",
        cursor="{}",
        events_collected=1,
        watermark_at=datetime(2026, 7, 24, 11, 30, 0),
    )
    assert row.watermark_at == datetime(2026, 7, 24, 11, 30, 0)


def test_last_run_capped_reflects_the_LAST_cycle_not_a_sticky_flag(db_session) -> None:
    """Ao contrário do watermark, o teto é estado do ÚLTIMO run e tem de baixar.

    Se ficasse grudado em ``True``, a integração continuaria marcada como
    ``degraded`` para sempre depois de um único pico — e o operador aprenderia a
    ignorar o indicador, que é como se perde um alarme.
    """
    repo = _repo(db_session)
    repo.upsert(
        integration_id=1,
        stream="detections",
        cursor="{}",
        events_collected=10_000,
        watermark_at=datetime(2026, 7, 24, 10, 0, 0),
        last_run_capped=True,
    )
    row = repo.upsert(
        integration_id=1,
        stream="detections",
        cursor="{}",
        events_collected=42,
        watermark_at=datetime(2026, 7, 24, 12, 0, 0),
        last_run_capped=False,
    )
    assert row.last_run_capped is False, "o teto ficou grudado depois que o backlog drenou"


def test_failed_cycle_keeps_the_cap_flag(db_session) -> None:
    """Ciclo que falhou não DESMENTE o backlog — ele não traz evidência nenhuma.

    A leitura literal do nome do campo ("o último run bateu o teto?") sugeriria
    zerar aqui, já que o run morreu antes de paginar. Mas o que o campo alimenta
    é a pergunta do operador: *sobrou trabalho?*. Um timeout não drenou nada, logo
    o que sobrava continua sobrando.

    Zerar teria o efeito perverso de apagar a etiqueta de backlog a cada erro
    transitório — ou seja, exatamente quando o coletor está pior e o operador mais
    precisa do sinal. Não há risco de mascarar: um ciclo com erro já leva o status
    a ``degraded``/``unhealthy`` por conta própria, então a flag só ACRESCENTA o
    contexto "e ainda por cima está atrasado".

    O par sem-erro continua não-grudento —
    ``test_last_run_capped_reflects_the_LAST_cycle_not_a_sticky_flag`` garante que
    um ciclo bem-sucedido dentro do teto baixa a flag.
    """
    repo = _repo(db_session)
    repo.upsert(
        integration_id=1,
        stream="detections",
        cursor="{}",
        events_collected=10_000,
        last_run_capped=True,
    )
    row = repo.upsert(
        integration_id=1, stream="detections", cursor="{}", events_collected=0, error="timeout"
    )
    assert row.last_run_capped is True


def test_streams_of_the_same_integration_keep_independent_watermarks(db_session) -> None:
    """A agregação da Saúde é pelo PIOR stream — os valores precisam ser por linha."""
    repo = _repo(db_session)
    repo.upsert(
        integration_id=1,
        stream="detections",
        cursor="{}",
        events_collected=1,
        watermark_at=datetime(2026, 7, 23, 20, 0, 0),
        last_run_capped=True,
    )
    repo.upsert(
        integration_id=1,
        stream="alerts",
        cursor="{}",
        events_collected=1,
        watermark_at=datetime(2026, 7, 24, 12, 0, 0),
    )
    por_stream = {r.stream: r for r in repo.list_for_integration(integration_id=1)}
    assert por_stream["detections"].watermark_at == datetime(2026, 7, 23, 20, 0, 0)
    assert por_stream["detections"].last_run_capped is True
    assert por_stream["alerts"].watermark_at == datetime(2026, 7, 24, 12, 0, 0)
    assert por_stream["alerts"].last_run_capped is False


# ── CursorStore repassa o que o ciclo mediu ──────────────────────────────


class _FakeRedis:
    def __init__(self) -> None:
        self.store: Dict[str, str] = {}

    async def get(self, key: str) -> Optional[str]:
        return self.store.get(key)

    async def set(self, key: str, value: str) -> None:
        self.store[key] = value


async def test_cursor_store_forwards_watermark_and_cap_to_the_repository(monkeypatch) -> None:
    """O elo entre o ciclo e a tabela — silenciá-lo apagaria os dois sinais."""
    from backend.app.collectors.state import cursor as cursor_module

    capturado: Dict[str, Any] = {}

    class _SpyRepo:
        def __init__(self, db) -> None:
            pass

        def upsert(self, **kw):
            capturado.update(kw)

    class _SpySession:
        def __enter__(self):
            return None

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(cursor_module, "CollectionStateRepository", _SpyRepo)
    monkeypatch.setattr(cursor_module.database, "SessionLocal", lambda: _SpySession())

    store = cursor_module.CursorStore(_FakeRedis())
    await store.save(
        1,
        "detections",
        {"from_ts": "2026-07-24T10:00:00Z"},
        events_collected=10_000,
        watermark_at=datetime(2026, 7, 24, 10, 0, 0),
        last_run_capped=True,
    )
    assert capturado["watermark_at"] == datetime(2026, 7, 24, 10, 0, 0)
    assert capturado["last_run_capped"] is True


# ── O ponto do ciclo que mede e grava ───────────────────────────────────


@pytest.mark.source_only  # lê o .py; na imagem Cython o fonte não existe
def test_cycle_measures_the_watermark_from_the_collector_and_persists_it() -> None:
    """O ciclo tem de perguntar ao COLETOR — o cursor é opaco ao core.

    E tem de gravar junto o ``hit_cycle_cap``: sem o par, watermark parado num
    stream silencioso viraria falso positivo de backlog.
    """
    from backend.app.collectors import pipeline

    src = inspect.getsource(pipeline._run_collection_once)
    assert "collector_cls.watermark_at(ctx.cursor)" in src, (
        "o ciclo não pergunta o watermark ao coletor — o indicador de atraso "
        "nunca sai de None"
    )
    assert "watermark_at=_watermark" in src and "last_run_capped=bool(ctx.hit_cycle_cap)" in src, (
        "watermark/teto medidos mas não persistidos: a Saúde continua cega"
    )


@pytest.mark.source_only  # lê o .py; na imagem Cython o fonte não existe
def test_a_broken_watermark_never_takes_the_cycle_down() -> None:
    """Métrica não pode derrubar coleta.

    ``watermark_at`` é código de vendor rodando no fim de todo ciclo. Uma
    exceção ali sem proteção transformaria um problema de observabilidade em
    parada de ingestão.
    """
    from backend.app.collectors import pipeline

    src = inspect.getsource(pipeline._run_collection_once)
    idx = src.index("collector_cls.watermark_at")
    assert "try:" in src[max(idx - 120, 0) : idx], (
        "a chamada a watermark_at não está dentro de um try"
    )
    assert "except Exception" in src[idx : idx + 200], (
        "watermark_at sem guarda de exceção — vendor quebrado pararia a coleta"
    )


@pytest.mark.source_only  # lê o .py; na imagem Cython o fonte não existe
def test_error_path_does_not_overwrite_the_watermark() -> None:
    """O ``save`` do caminho de erro não passa watermark — a coluna é preservada.

    Espelho, no ciclo, do que ``test_failed_cycle_preserves_the_watermark``
    garante no repositório.
    """
    from backend.app.collectors import pipeline

    src = inspect.getsource(pipeline._run_collection_once)
    bloco_erro = src[src.index("cursor_before or {}") - 300 : src.index("cursor_before or {}") + 400]
    assert "watermark_at=" not in bloco_erro, (
        "o caminho de erro grava watermark — um 503 transitório zeraria o atraso "
        "acumulado e o backlog sumiria do radar"
    )


def test_watermark_lag_would_have_caught_the_incident() -> None:
    """Regressão do incidente, em números: 15h de atraso não podem virar 0.

    ``last_success_at`` era reescrito a cada ciclo bem-sucedido, então
    ``agora − last_success_at`` dava ~0 enquanto o cursor estava 15 horas atrás.
    O watermark mede a distância certa.
    """
    agora = datetime(2026, 7, 24, 12, 0, 0)
    cursor = {"from_ts": (agora - timedelta(hours=15)).replace(tzinfo=timezone.utc).isoformat()}
    watermark = WazuhDetectionsCollector.watermark_at(cursor)
    assert watermark is not None
    assert (agora - watermark).total_seconds() == pytest.approx(15 * 3600)
