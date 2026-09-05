"""A detecção em voo SAINDO como evento OCSF 2004, roteada (ADR-0015).

Por que este arquivo existe: enquanto a detecção só era uma linha em
``detections``, a resposta para "onde chega o alerta? meu SOC vive no
Splunk/Sentinel" era "numa tabela que só a UI do EE lê". O caminho de saída
existe agora, e ele é a superfície de maior risco da feature — despacha evento
NOVO para o SIEM do cliente, a partir do ``finally`` do ciclo de coleta.

Os invariantes cobertos aqui, todos por COMPORTAMENTO (nada lê fonte, então este
arquivo roda também na imagem Cython, onde ``runtime.py`` é ``.so``):

* identidade OCSF completa e aritmeticamente correta, ``time`` em
  MILISSEGUNDOS e ``organization_id`` no envelope — sem ele o roteador casa só
  rota global e a rota do próprio tenant nunca recebe a detecção dele;
* PAR POSITIVO/NEGATIVO da flag: ON emite, OFF não emite NADA — e o negativo é
  provado por CONTAGEM de chamadas, nunca por exceção dentro do duplo (uma
  exceção ali seria engolida pelo best-effort e o teste passaria por vacuidade);
* R3: falha na emissão não derruba o flush, não impede a Detection de ser
  gravada, e é CONTADA e ATRIBUÍDA à regra;
* o que sai é PEQUENO e não carrega payload do cliente — com o par positivo
  (o ponteiro está lá) ao lado do negativo (o segredo não está);
* supressão, guard de laço e teto por ciclo: as três razões pelas quais uma
  Detection é gravada e o evento NÃO sai de propósito.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import json
import time as _time
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import fakeredis
import pytest
from sqlalchemy import create_engine, event as sa_event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.collectors import observability_store as obs
from backend.app.collectors import otel_metrics
from backend.app.collectors.inflight import runtime as runtime_mod
from backend.app.collectors.inflight.matcher import CompiledInflightRule
from backend.app.collectors.inflight.runtime import (
    DETECTION_EVENT_MAX_BYTES,
    DETECTION_EVENT_TEXT_MAXLEN,
    DETECTION_EVENT_TYPE,
    EMIT_SKIP_REASONS,
    ERROR_REASONS,
    InflightAccumulator,
    flush_inflight,
)
from backend.app.collectors.normalize.ocsf import validator as ocsf_validator
from backend.app.collectors.normalize.ocsf.classes import (
    ACTIVITY_ID_DETECTION_FINDING,
    CLASS_UID_DETECTION_FINDING,
)
from backend.app.core.config import settings
from backend.app.db import database, models
from backend.app.db.database import Base

#: ``OS_MAXSTR`` do Wazuh. Acima disto o ``analysisd`` TRUNCA em silêncio — o
#: alerta chega cortado ao meio e PARECE completo, que é pior que não chegar.
#: É o limite real de destino contra o qual o teto deste módulo é comparado.
OS_MAXSTR = 65536


# ── Harness ────────────────────────────────────────────────────────────────


@pytest.fixture()
def db_session(monkeypatch: pytest.MonkeyPatch):
    """Mesmo harness de ``test_adr0015_inflight_detection_row``: ``_flush_sync``
    abre ``database.SessionLocal()`` (engine GLOBAL) e roda em OUTRA thread
    (``asyncio.to_thread``) — daí ``StaticPool`` + ``check_same_thread=False``,
    senão cada thread abriria um ``:memory:`` novo e VAZIO e o sintoma seria
    "no such table: detections" vindo de dentro da thread."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @sa_event.listens_for(engine, "connect")
    def _enforce_fk(dbapi_conn: object, _rec: object) -> None:
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
    """O flush espelha contadores por regra no observability_store. Sem isto
    cada teste tentaria um Redis REAL em localhost: ``record_counter`` engole a
    falha, mas o gate passaria a depender da máquina."""
    r = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(obs, "_redis", lambda: r)


@pytest.fixture()
def despachados(monkeypatch: pytest.MonkeyPatch) -> list[list[dict]]:
    """Lotes entregues ao roteamento. Uma LISTA, não um contador booleano: o
    par negativo desta suíte é ``len(...) == 0``, e um espião que só marcasse
    "houve chamada" não distinguiria "não emitiu" de "emitiu lote vazio"."""
    lotes: list[list[dict]] = []
    monkeypatch.setattr(
        runtime_mod, "_dispatch_sync", lambda envelopes: lotes.append(list(envelopes))
    )
    return lotes


@pytest.fixture()
def metricas(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, float, dict]]:
    """``(nome, valor, attrs)`` do que os call sites empurram para o OTel.

    Espiona ``otel_metrics.count`` e não o instrumento porque com
    ``OTEL_ENABLED=False`` (o default) a fachada retorna ANTES de tocar nele —
    espiar o instrumento aprovaria tudo por vacuidade.
    """
    capturado: list[tuple[str, float, dict]] = []

    def _count(name: str, value: float = 1, attrs: dict | None = None) -> None:
        capturado.append((name, float(value), dict(attrs or {})))

    monkeypatch.setattr(otel_metrics, "count", _count)
    return capturado


def _total(metricas: list[tuple[str, float, dict]], nome: str, **attrs: str) -> float:
    return sum(
        v for n, v, a in metricas
        if n == nome and all(a.get(k) == val for k, val in attrs.items())
    )


@pytest.fixture()
def emissao_ligada(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "INFLIGHT_EMIT_OCSF_EVENT", True)


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
    group_by: tuple[str, ...] | None = ("raw", "user"),
) -> CompiledInflightRule:
    return CompiledInflightRule(
        rule_id=rule_id,
        name=name,
        severity_id=severity_id,
        suppression_window_seconds=window,
        group_by_path=group_by,
        clauses=(),
    )


#: Marcador plantado no payload do "cliente". Não pode aparecer em NADA que
#: saia — é assim que o teste de PII vira medida em vez de promessa.
SEGREDO = "TOKEN-SECRETO-QUE-NAO-PODE-SAIR-DO-PRODUTO"


def _envelope(
    *,
    org_id: int,
    user: str = "alice",
    self_emitted: bool = False,
) -> dict[str, Any]:
    """Envelope no formato que o pipeline entrega ao matcher."""
    return {
        "_centralops": {
            "vendor": "sophos",
            "platform": "sophos",
            "stream": "alerts",
            "event_type": (
                DETECTION_EVENT_TYPE if self_emitted else "sophos.alert"
            ),
            "event_id": "evt-abc-123",
            "customer_id": org_id,
            "customer_name": "Cliente Exemplo",
            "organization_id": org_id,
            "organization_slug": "cliente-exemplo",
            "data_geography": "eu",
        },
        "normalized": {
            "class_uid": 3002,
            "time": 1_750_000_000_000,
            "severity_id": 4,
        },
        "raw": {"user": user, "api_token": SEGREDO},
    }


def _detections(db_session) -> list[models.Detection]:
    with db_session() as db:
        return db.query(models.Detection).order_by(models.Detection.id.asc()).all()


# ── 1. Com a flag ON, sai um 2004 com identidade completa ──────────────────


@pytest.mark.asyncio
async def test_match_emits_a_2004_with_arithmetically_correct_identity(
    db_session, despachados, emissao_ligada
) -> None:
    """O contrato do evento: classe, aritmética do ``type_uid``, ``time`` em
    MILISSEGUNDOS e o tenant no envelope."""
    org_id = _seed_org(db_session)
    intg_id = _seed_integration(db_session, org_id=org_id)
    antes_ms = int(_time.time() * 1000)

    acc = InflightAccumulator()
    acc.add(_rule(), _envelope(org_id=org_id), organization_id=org_id,
            integration_id=intg_id)
    await flush_inflight(acc, organization_id=org_id)

    assert len(despachados) == 1, "o lote sai numa chamada só, 1x por ciclo"
    (envelope,) = despachados[0]
    norm = envelope["normalized"]

    assert norm["class_uid"] == CLASS_UID_DETECTION_FINDING == 2004
    assert norm["category_uid"] == 2 == norm["class_uid"] // 1000
    # ``create`` e não ``start``: a 2004 tem semântica de CICLO DE VIDA do
    # achado, e o que aconteceu foi a criação de um.
    assert norm["activity_id"] == ACTIVITY_ID_DETECTION_FINDING["create"] == 1
    # A fórmula E o literal: só a fórmula continuaria verdadeira se alguém
    # trocasse o ``activity_id`` — o evento mudaria de significado sem quebrar
    # nenhum assert.
    assert norm["type_uid"] == norm["class_uid"] * 100 + norm["activity_id"]
    assert norm["type_uid"] == 200401

    # MILISSEGUNDOS. Interpretar o campo como SEGUNDOS tem de dar um instante
    # absurdo — é essa a assinatura do erro de 1000x que este repo já pagou em
    # 16 mappings, e um ``> 0`` não a pegaria.
    depois_ms = int(_time.time() * 1000)
    assert antes_ms <= norm["time"] <= depois_ms
    assert norm["metadata"]["logged_time"] == norm["time"]
    assert datetime.utcfromtimestamp(norm["time"] / 1000).year == datetime.utcnow().year
    assert norm["time"] // 1000 > 1_600_000_000, (
        "o campo está em segundos: o valor cabe num epoch de segundos plausível"
    )

    # Sem isto o roteador casa SOMENTE rotas globais e a rota criada pelo
    # próprio tenant nunca recebe a detecção dele.
    meta = envelope["_centralops"]
    assert meta["organization_id"] == org_id
    assert meta["customer_id"] == org_id
    assert meta["organization_slug"] == "cliente-exemplo"
    # ``vendor`` = quem PRODUZIU o achado; ``platform`` = sobre quem ele é. É a
    # segunda que faz a rota da integração do cliente casar o alerta dela.
    assert meta["vendor"] == "centralops"
    assert meta["platform"] == "sophos"
    assert meta["event_type"] == DETECTION_EVENT_TYPE
    assert envelope["_centralops"]["integration_id"] == intg_id


@pytest.mark.asyncio
async def test_emitted_event_passes_the_structural_gate(
    db_session, despachados, emissao_ligada
) -> None:
    """O validador estrutural do próprio repo é o juiz — não os asserts acima.

    ``finding_info`` é OBRIGATÓRIO na 2004 pelo manifesto vendorado; um evento
    montado à mão que o esquecesse continuaria passando nos asserts de
    identidade e chegaria ao SIEM incompleto.
    """
    org_id = _seed_org(db_session)
    acc = InflightAccumulator()
    acc.add(_rule(), _envelope(org_id=org_id), organization_id=org_id)
    await flush_inflight(acc, organization_id=org_id)

    (envelope,) = despachados[0]
    reg = ocsf_validator.get_registry(settings.OCSF_VALIDATION_VERSION)
    res = ocsf_validator.structural_gate(envelope["normalized"], reg)

    assert res.valid is True and res.reason == ocsf_validator.REASON_OK
    assert res.missing_required == (), (
        f"a 2004 saiu sem campo obrigatório: {res.missing_required}"
    )


@pytest.mark.asyncio
async def test_the_real_wire_reaches_the_routing_helper(
    db_session, monkeypatch: pytest.MonkeyPatch, emissao_ligada
) -> None:
    """O ELO que os outros testes deste arquivo pulam: ``_dispatch_sync`` REAL.

    Todo teste acima espiona ``_dispatch_sync`` e, com isso, nenhum deles
    executa a única linha que liga esta feature ao produto — o import e a
    chamada de ``pipeline._enqueue_dispatch``. Um erro de nome, de assinatura ou
    um import circular passaria verde na suíte inteira e apareceria em produção
    como "a regra dispara, a Detection aparece na lista, e o SIEM não recebe
    nada". É exatamente a forma do buraco que ``test_adr0015_inflight_detection_
    row`` fechou para ``_flush_sync``.

    O espião desce um nível, para ``_enqueue_routed``: assim o caminho REAL
    inclui a resolução de rotas e a marcação de não-enriquecido, e só o envio
    às filas Celery fica de fora.
    """
    from backend.app.collectors import pipeline as pipeline_mod

    roteados: list[tuple[list, list]] = []
    monkeypatch.setattr(
        pipeline_mod, "_enqueue_routed",
        lambda batch, routes: roteados.append((list(batch), list(routes))),
    )

    org_id = _seed_org(db_session)
    acc = InflightAccumulator()
    acc.add(_rule(), _envelope(org_id=org_id), organization_id=org_id)
    await flush_inflight(acc, organization_id=org_id)

    assert len(roteados) == 1, "o evento não chegou ao roteamento"
    (lote, _rotas) = roteados[0]
    assert len(lote) == 1
    assert lote[0]["normalized"]["class_uid"] == CLASS_UID_DETECTION_FINDING
    # A marcação de não-enriquecido é OBRIGATÓRIA e sai do caminho real: sem
    # ela, um evento sem contexto no destino é indistinguível de um que passou
    # pelo enriquecimento e não casou regra nenhuma.
    assert lote[0]["_centralops"]["enrichment_skipped"] == "producer_unsupported"


# ── 2. PAR POSITIVO/NEGATIVO da flag ───────────────────────────────────────


@pytest.mark.asyncio
async def test_flag_off_emits_nothing_and_still_records_the_detection(
    db_session, despachados, metricas
) -> None:
    """O negativo é por CONTAGEM.

    Levantar dentro do duplo de dispatch NÃO provaria nada aqui: a emissão é
    best-effort e engole exceção por contrato (R3), então o teste passaria com
    a exceção sendo silenciosamente contada como ``emit_failed`` — verde pelo
    motivo errado. Contar chamadas é o único jeito honesto.

    E o par positivo mora no mesmo assert: a Detection CONTINUA sendo gravada
    com a flag desligada. Sem essa linha, um flush que não fizesse nada também
    passaria.
    """
    assert settings.INFLIGHT_EMIT_OCSF_EVENT is False, (
        "o default tem de ser OFF: ligar emissão de evento novo numa instalação "
        "existente muda volume e custo do cliente sem ele pedir (ADR-0015 §7)"
    )
    org_id = _seed_org(db_session)

    acc = InflightAccumulator()
    acc.add(_rule(), _envelope(org_id=org_id), organization_id=org_id)
    await flush_inflight(acc, organization_id=org_id)

    assert len(despachados) == 0
    assert len(_detections(db_session)) == 1
    # Nenhuma série NOVA nasce com a flag OFF — nem a de sucesso nem a de
    # "não emitido". Uma instalação que não pediu a feature não ganha painel.
    assert _total(metricas, "collector_inflight_events_emitted_total") == 0
    assert _total(metricas, "collector_inflight_events_not_emitted_total") == 0


@pytest.mark.asyncio
async def test_the_spy_does_see_an_emission_when_the_flag_is_on(
    db_session, despachados, metricas, emissao_ligada
) -> None:
    """META-TESTE do par acima: prova que o espião REGISTRA quando há emissão.

    Sem ele, um espião quebrado (patch no símbolo errado, fixture não aplicada)
    faria o teste da flag OFF passar por vacuidade — ele aprovaria um mundo em
    que nada é observável.
    """
    org_id = _seed_org(db_session)
    acc = InflightAccumulator()
    acc.add(_rule(), _envelope(org_id=org_id), organization_id=org_id)
    await flush_inflight(acc, organization_id=org_id)

    assert len(despachados) == 1 and len(despachados[0]) == 1
    assert _total(metricas, "collector_inflight_events_emitted_total") == 1


# ── 3. R3: a falha da emissão não derruba nada, e é CONTADA ────────────────


@pytest.mark.asyncio
async def test_dispatch_failure_keeps_the_detection_and_counts_the_loss(
    db_session, monkeypatch: pytest.MonkeyPatch, metricas, emissao_ligada
) -> None:
    """R3 é inviolável: o evento extra nunca pode custar o alerta durável.

    A falha é contada em ``emit_failed`` e ATRIBUÍDA à regra — "perdi 40
    eventos" não diz qual regra parou de chegar no SIEM.
    """
    org_id = _seed_org(db_session)

    def _boom(_envelopes: object) -> None:
        raise RuntimeError("broker de dispatch fora do ar")

    monkeypatch.setattr(runtime_mod, "_dispatch_sync", _boom)

    acc = InflightAccumulator()
    acc.add(_rule(rule_id=91), _envelope(org_id=org_id), organization_id=org_id)

    # Não levanta: a chamada abaixo é o que roda dentro do ``finally`` do ciclo
    # de coleta, onde uma exceção MASCARARIA o erro original em voo.
    await flush_inflight(acc, organization_id=org_id)

    assert len(_detections(db_session)) == 1, "a Detection é a fonte da verdade"
    assert acc.errors["emit_failed"] == {91: 1}
    assert "emit_failed" in ERROR_REASONS
    assert _total(
        metricas, "collector_inflight_errors_total", reason="emit_failed"
    ) == 1
    assert _total(metricas, "collector_inflight_events_emitted_total") == 0


@pytest.mark.asyncio
async def test_a_broken_event_builder_does_not_take_the_others_with_it(
    db_session, despachados, monkeypatch: pytest.MonkeyPatch, metricas, emissao_ligada
) -> None:
    """Falha ao MONTAR um evento é por-ticket, não por-lote.

    Sem este teste, um ``try`` no lugar errado faria o primeiro ticket ruim
    calar todos os alertas do ciclo — e o sintoma seria "às vezes o SIEM não
    recebe nada", que é indistinguível de "não houve detecção".
    """
    org_id = _seed_org(db_session)
    original = runtime_mod._build_detection_event

    def _falha_na_primeira(emit, org, now_ms):  # type: ignore[no-untyped-def]
        if emit.rule_id == 1:
            raise ValueError("regra 1 tem um ticket impossível")
        return original(emit, org, now_ms)

    monkeypatch.setattr(runtime_mod, "_build_detection_event", _falha_na_primeira)

    acc = InflightAccumulator()
    acc.add(_rule(rule_id=1), _envelope(org_id=org_id, user="a"),
            organization_id=org_id)
    acc.add(_rule(rule_id=2), _envelope(org_id=org_id, user="b"),
            organization_id=org_id)
    await flush_inflight(acc, organization_id=org_id)

    assert len(_detections(db_session)) == 2
    assert acc.errors["emit_failed"] == {1: 1}
    # PAR POSITIVO: o outro evento SAIU. Sem ele, um código que abortasse o
    # lote inteiro passaria no assert de cima.
    assert len(despachados) == 1 and len(despachados[0]) == 1
    assert despachados[0][0]["normalized"]["unmapped"]["rule_id"] == 2
    assert _total(metricas, "collector_inflight_events_emitted_total") == 1


# ── 4. O que SAI é pequeno, e não é o payload do cliente ───────────────────


@pytest.mark.asyncio
async def test_the_emitted_event_carries_the_pointer_and_not_the_payload(
    db_session, despachados, emissao_ligada
) -> None:
    """PAR completo: o ponteiro ESTÁ lá (positivo) e o payload NÃO (negativo).

    Só o negativo passaria por vacuidade num evento vazio; só o positivo não
    provaria nada sobre PII.
    """
    org_id = _seed_org(db_session)
    intg_id = _seed_integration(db_session, org_id=org_id)

    acc = InflightAccumulator()
    acc.add(_rule(), _envelope(org_id=org_id, user="alice"),
            organization_id=org_id, integration_id=intg_id)
    await flush_inflight(acc, organization_id=org_id)

    (envelope,) = despachados[0]
    serializado = json.dumps(envelope, ensure_ascii=False)
    unmapped = envelope["normalized"]["unmapped"]

    # POSITIVO — o que o analista precisa para pivotar até o evento de origem.
    assert unmapped["source_event_id"] == "evt-abc-123"
    assert unmapped["source_vendor"] == "sophos"
    assert unmapped["source_stream"] == "alerts"
    assert unmapped["source_event_time"] == 1_750_000_000_000
    assert unmapped["group_field"] == "raw.user"
    assert unmapped["group_value"] == "alice"
    assert unmapped["integration_id"] == intg_id
    assert unmapped["organization_id"] == org_id
    # A mesma chave da linha durável: é o que liga alerta no SIEM ↔ registro
    # interno, para o suporte responder "este alerta é qual linha do banco?".
    (det,) = _detections(db_session)
    assert envelope["normalized"]["finding_info"]["uid"] == det.dedup_key
    assert unmapped["detection_id"] == det.id

    # NEGATIVO — nada do payload do cliente viaja junto.
    assert SEGREDO not in serializado
    assert envelope["raw"] == {}, (
        "``raw`` vazio é a decisão, não um acaso: um destino em modo OCSF "
        "entrega SÓ o ``normalized``, então evidência que viva no raw é "
        "evidência que o analista não recebe — e evidência COMPLETA ali seria "
        "PII saindo por uma rota que o evento de origem talvez nem tenha."
    )


@pytest.mark.parametrize(
    "caractere, rotulo",
    [
        ("a", "1 byte"),
        ("Ã", "2 bytes"),
        # ASTRAL, 4 bytes em UTF-8. É o pior caso REAL e não um exagero de
        # laboratório: o truncamento conta CARACTERES e o teto de destino conta
        # BYTES, então um campo "dentro do limite" pode ocupar 4x o esperado.
        ("\U0001d518", "4 bytes"),
    ],
)
def test_the_worst_possible_event_still_fits_the_declared_ceiling(
    caractere: str, rotulo: str
) -> None:
    """INVARIANTE do teto: mede o PIOR caso, não um caso típico.

    Constrói o ponteiro pelo CAMINHO REAL (``_event_source_pointer``, que é
    quem trunca) a partir de um evento em que TODO campo textual é gigante, e
    mede. É o que transforma ``DETECTION_EVENT_MAX_BYTES`` de comentário em
    número verificado — e o que pegaria um campo novo adicionado ao evento sem
    passar por ``_evidence_text``.
    """
    gordo = caractere * 4000
    origem = {
        "_centralops": {
            k: gordo
            for k in (
                "vendor", "platform", "stream", "event_type", "event_id",
                "customer_name", "organization_slug", "data_geography",
            )
        },
        "normalized": {"time": 1_750_000_000_000, "class_uid": 3002},
        "raw": {"user": gordo},
    }
    source = runtime_mod._event_source_pointer(origem, ("raw", "user"), gordo)
    chave = (
        "inflight:999999:999999:"
        + caractere * int(settings.INFLIGHT_MAX_GROUP_VALUE_LEN)
        + "~0123456789abcdef"
    )
    emit = runtime_mod.DetectionEmit(
        dedup_key=chave,
        detection_id=2**63 - 1,
        rule_id=2**31 - 1,
        rule_name=gordo,
        severity_id=6,
        integration_id=2**31 - 1,
        source=source,
    )
    envelope = runtime_mod._build_detection_event(emit, 999999, 1_750_000_000_123)
    tamanho = len(json.dumps(envelope, ensure_ascii=False).encode("utf-8"))

    assert tamanho <= DETECTION_EVENT_MAX_BYTES, (
        f"o pior caso ({rotulo}/char) do evento emitido ocupa {tamanho} bytes, "
        f"acima do teto declarado de {DETECTION_EVENT_MAX_BYTES}"
    )
    # PAR POSITIVO do assert acima: o teto não passa por vacuidade num evento
    # que ficou vazio. Um evento de detecção com menos de 1 KiB nesta montagem
    # significa que os campos pararam de ser preenchidos.
    assert tamanho > 1024


def test_the_declared_ceiling_stays_far_below_the_real_destination_limit() -> None:
    """Um teto que encostasse no limite do destino não protegeria de nada.

    O ``analysisd`` do Wazuh TRUNCA em silêncio acima de ``OS_MAXSTR`` e o
    alerta chega cortado PARECENDO inteiro — o modo de falha pior que o alerta
    que não chega. A folga de 5x é o que dá espaço para o evento crescer (um
    campo novo, um destino que empacote metadados) sem chegar perto disso.
    """
    assert DETECTION_EVENT_MAX_BYTES < OS_MAXSTR // 5


def test_the_truncation_ceiling_matches_the_preview_discipline() -> None:
    """Os dois tetos de texto que sai do produto são o MESMO número.

    Divergir faria a disciplina de truncamento depender de POR ONDE o dado do
    cliente saiu — 120 chars na tela do preview e outra coisa no evento
    entregue ao SIEM. O preview é o precedente do repo para isso.
    """
    from backend.app.collectors.inflight import preview

    assert DETECTION_EVENT_TEXT_MAXLEN == preview._OBSERVED_MAXLEN == 120


def test_long_text_is_truncated_before_leaving_the_product() -> None:
    """O teto acima não é decorativo: o corte acontece."""
    assert runtime_mod._evidence_text(None) is None, (
        "ausente e vazio são estados diferentes no evento entregue"
    )
    assert runtime_mod._evidence_text("curto") == "curto"
    cortado = runtime_mod._evidence_text("y" * 500)
    assert cortado is not None
    assert len(cortado) == DETECTION_EVENT_TEXT_MAXLEN + 1  # +1 = a reticência
    assert cortado.endswith("…")


# ── 5. Gravada e NÃO emitida — as três razões deliberadas ──────────────────


@pytest.mark.asyncio
async def test_a_suppressed_bump_does_not_emit_a_second_event(
    db_session, despachados, metricas, emissao_ligada
) -> None:
    """A supressão é o contrato anti-spam da feature; a emissão não pode ser a
    porta dos fundos que a anula.

    Sem isto, uma regra que casa continuamente produziria UMA Detection e um
    evento a CADA ciclo de coleta (~30x/h com ciclo de 2min), no SIEM que o
    cliente paga por volume.
    """
    org_id = _seed_org(db_session)

    for _ in range(2):
        acc = InflightAccumulator()
        acc.add(_rule(window=3600), _envelope(org_id=org_id),
                organization_id=org_id)
        await flush_inflight(acc, organization_id=org_id)

    # UMA linha (bumpada), UM evento.
    (det,) = _detections(db_session)
    assert det.count == 2
    assert len(despachados) == 1, "o 2º ciclo bumpou a Detection e NÃO emitiu"
    assert _total(metricas, "collector_inflight_events_emitted_total") == 1
    assert _total(
        metricas, "collector_inflight_events_not_emitted_total", reason="suppressed"
    ) == 1


@pytest.mark.asyncio
async def test_outside_the_window_a_new_detection_emits_again(
    db_session, despachados, emissao_ligada
) -> None:
    """PAR POSITIVO do teste acima: passada a janela, nasce Detection nova e o
    evento SAI. Sem ele, um código que nunca emitisse depois da primeira vez
    passaria no assert anterior."""
    org_id = _seed_org(db_session)

    acc = InflightAccumulator()
    acc.add(_rule(window=60), _envelope(org_id=org_id), organization_id=org_id)
    await flush_inflight(acc, organization_id=org_id)

    # Determinístico: comparar dois ``utcnow()`` dependeria do relógio.
    with db_session() as db:
        det = db.query(models.Detection).one()
        det.last_seen = datetime.utcnow() - timedelta(seconds=600)
        db.commit()

    acc2 = InflightAccumulator()
    acc2.add(_rule(window=60), _envelope(org_id=org_id), organization_id=org_id)
    await flush_inflight(acc2, organization_id=org_id)

    assert len(_detections(db_session)) == 2
    assert len(despachados) == 2


@pytest.mark.asyncio
async def test_an_event_this_product_emitted_never_emits_another(
    db_session, despachados, metricas, emissao_ligada
) -> None:
    """GUARD DE LAÇO.

    Por dentro a cascata não é alcançável — ``_enqueue_dispatch`` é saída e não
    realimenta ``run_collection_once``, o único lugar onde o matcher roda. Mas a
    reentrada pela PORTA DA FRENTE é real neste repo: ``POST /api/ingest``
    empurra para um buffer que o ``PushBufferCollector`` drena DENTRO do ciclo
    normal. Um destino apontado para a própria instalação devolveria o evento à
    ingestão, e daí em diante ele é indistinguível de um evento de vendor.

    A Detection continua sendo GRAVADA (o detector é observador, nunca
    porteiro); só o evento não sai de novo.
    """
    org_id = _seed_org(db_session)

    acc = InflightAccumulator()
    acc.add(_rule(), _envelope(org_id=org_id, self_emitted=True),
            organization_id=org_id)
    await flush_inflight(acc, organization_id=org_id)

    assert len(_detections(db_session)) == 1
    assert len(despachados) == 0
    assert _total(
        metricas, "collector_inflight_events_not_emitted_total", reason="loop_guard"
    ) == 1


@pytest.mark.parametrize(
    "raw_reingerido, forma",
    [
        # (a) O destino entregou o ENVELOPE inteiro e ele voltou como raw.
        (
            {"user": "alice", "_centralops": {"event_type": DETECTION_EVENT_TYPE}},
            "envelope inteiro",
        ),
        # (b) O destino entregou SÓ o ``normalized`` (Chronicle, Security Lake,
        #     webhook em modo OCSF) — não há ``_centralops`` para achar, e o que
        #     sobra da marca é a chave dentro de ``unmapped``.
        (
            {"user": "alice", "unmapped": {"centralops_detection": True}},
            "só o normalized",
        ),
    ],
)
@pytest.mark.asyncio
async def test_the_loop_guard_also_sees_the_mark_one_level_down(
    db_session, despachados, emissao_ligada, raw_reingerido: dict, forma: str
) -> None:
    """A reentrada real NÃO preserva o envelope: o push-ingest re-normaliza, e o
    que voltou vira o ``raw`` do evento novo. Checar só o ``_centralops`` de
    topo deixaria a cascata aberta exatamente no caminho que o guard existe para
    cobrir — e em DUAS formas, porque o que o destino entrega depende do modo de
    payload da rota."""
    org_id = _seed_org(db_session)
    reingerido = _envelope(org_id=org_id)
    reingerido["raw"] = raw_reingerido

    acc = InflightAccumulator()
    acc.add(_rule(), reingerido, organization_id=org_id)
    await flush_inflight(acc, organization_id=org_id)

    assert len(_detections(db_session)) == 1, (
        f"a Detection tem de ser gravada mesmo assim ({forma})"
    )
    assert len(despachados) == 0


@pytest.mark.asyncio
async def test_the_per_cycle_ceiling_binds_and_the_excess_is_counted(
    db_session, despachados, monkeypatch: pytest.MonkeyPatch, metricas, emissao_ligada
) -> None:
    """INVARIANTE do teto por ciclo (R8).

    O teto estrutural que já existia (regras × chaves = 2500) é alto demais para
    servir de proteção: a 1 ciclo/2min seriam 75k eventos de alerta por hora, por
    integração, na fatura do cliente. Aqui se prova que o teto BINDA — e que o
    excedente é CONTADO, não some.
    """
    monkeypatch.setattr(settings, "INFLIGHT_EMIT_MAX_EVENTS_PER_CYCLE", 2)
    org_id = _seed_org(db_session)

    acc = InflightAccumulator()
    for i in range(5):
        acc.add(_rule(), _envelope(org_id=org_id, user=f"user{i}"),
                organization_id=org_id)
    await flush_inflight(acc, organization_id=org_id)

    # As Detections estão TODAS gravadas: o teto corta a entrega, nunca o
    # registro.
    assert len(_detections(db_session)) == 5
    assert len(despachados) == 1 and len(despachados[0]) == 2
    assert _total(metricas, "collector_inflight_events_emitted_total") == 2
    assert _total(
        metricas, "collector_inflight_events_not_emitted_total", reason="cycle_cap"
    ) == 3


@pytest.mark.asyncio
async def test_a_zero_ceiling_is_a_kill_switch_that_still_records(
    db_session, despachados, monkeypatch: pytest.MonkeyPatch, metricas, emissao_ligada
) -> None:
    """Teto 0 desliga a EMISSÃO sem desligar a detecção — o botão que um
    operador aperta às 3h da manhã quando o SIEM está afogando."""
    monkeypatch.setattr(settings, "INFLIGHT_EMIT_MAX_EVENTS_PER_CYCLE", 0)
    org_id = _seed_org(db_session)

    acc = InflightAccumulator()
    acc.add(_rule(), _envelope(org_id=org_id), organization_id=org_id)
    await flush_inflight(acc, organization_id=org_id)

    assert len(_detections(db_session)) == 1
    assert len(despachados) == 0
    assert _total(
        metricas, "collector_inflight_events_not_emitted_total", reason="cycle_cap"
    ) == 1


@pytest.mark.asyncio
async def test_emitted_skip_reasons_equal_the_closed_enum(
    db_session, monkeypatch: pytest.MonkeyPatch, metricas, emissao_ligada
) -> None:
    """IGUALDADE, não inclusão: ``⊆`` é widening-safe e deixaria passar tanto uma
    razão nova não declarada quanto uma razão declarada que nenhum call site
    emite mais — label morta no painel de ops.

    ``reason`` é label de métrica, logo nunca pode carregar valor de evento nem
    nome de regra; o enum fechado é o que garante isso.
    """
    monkeypatch.setattr(settings, "INFLIGHT_EMIT_MAX_EVENTS_PER_CYCLE", 1)
    org_id = _seed_org(db_session)

    # (a) suppressed — mesma chave duas vezes, dentro da janela.
    for _ in range(2):
        acc = InflightAccumulator()
        acc.add(_rule(rule_id=1), _envelope(org_id=org_id), organization_id=org_id)
        await flush_inflight(acc, organization_id=org_id)

    # (b) loop_guard + (c) cycle_cap, no mesmo ciclo.
    acc = InflightAccumulator()
    acc.add(_rule(rule_id=2), _envelope(org_id=org_id, user="b", self_emitted=True),
            organization_id=org_id)
    acc.add(_rule(rule_id=3), _envelope(org_id=org_id, user="c"),
            organization_id=org_id)
    acc.add(_rule(rule_id=4), _envelope(org_id=org_id, user="d"),
            organization_id=org_id)
    await flush_inflight(acc, organization_id=org_id)

    # (d) rule_opt_out (W1.4) — flag global OFF, uma regra pediu emissão e a
    # outra não. A que pediu sai (é o que faz o emissor rodar); a outra é
    # contada como opt-out, não como falha.
    monkeypatch.setattr(settings, "INFLIGHT_EMIT_OCSF_EVENT", False)
    acc = InflightAccumulator()
    acc.add(replace(_rule(rule_id=5), emit_event=True),
            _envelope(org_id=org_id, user="e"), organization_id=org_id)
    acc.add(_rule(rule_id=6), _envelope(org_id=org_id, user="f"),
            organization_id=org_id)
    await flush_inflight(acc, organization_id=org_id)

    razoes = {
        a["reason"]
        for n, _v, a in metricas
        if n == "collector_inflight_events_not_emitted_total"
    }
    assert razoes == set(EMIT_SKIP_REASONS)


# ── O guard de laço, amarrado ao que o produtor de fato emite ────────────────


def _emit_de_exemplo() -> "runtime_mod.DetectionEmit":
    return runtime_mod.DetectionEmit(
        dedup_key="inflight:1:7:host-a",
        detection_id=4242,
        rule_id=7,
        rule_name="regra-de-teste",
        severity_id=4,
        integration_id=11,
        source={"event_id": "ev-1", "group_value": "host-a"},
    )


@pytest.mark.parametrize(
    "forma",
    ["envelope_inteiro", "somente_normalized"],
    ids=["reingerido como envelope", "reingerido como normalized (destino OCSF)"],
)
def test_o_guard_de_laco_reconhece_o_evento_que_o_produtor_emitiu(forma: str) -> None:
    """IDA E VOLTA: o evento entra no guard vindo do PRODUTOR, não de um fixture.

    Este teste existe porque a versão anterior da suíte não amarrava as duas
    pontas. `_build_detection_event` **escreve** a marca; `_is_self_emitted` a
    **lê** — e o teste que cobria a leitura fabricava o envelope à mão, com o
    nome da chave digitado no próprio teste.

    Consequência medida por mutação: renomear a marca no produtor deixava a
    suíte inteira VERDE e o guard parava de cortar a cascata. O assert era
    negativo (`nada foi despachado`) e a pré-condição que o tornava verdadeiro
    vinha do teste, não da produção — vacuidade com outro disfarce.

    As duas formas são as duas reentradas reais pela porta da frente
    (`POST /api/ingest`): um destino que devolve o envelope inteiro, e um em
    modo OCSF que entrega só o `normalized` — esta segunda é justamente a que o
    docstring de `_is_self_emitted` diz ser a única sobrevivente, e era a que
    estava solta.
    """
    emitido = runtime_mod._build_detection_event(_emit_de_exemplo(), 42, 1_750_000_000_123)

    if forma == "envelope_inteiro":
        reingerido = {"raw": emitido, "_centralops": {}, "normalized": {}}
    else:
        reingerido = {"raw": emitido["normalized"], "_centralops": {}, "normalized": {}}

    assert runtime_mod._is_self_emitted(reingerido) is True, (
        f"o guard NÃO reconheceu, na forma {forma!r}, o evento que o próprio "
        "produtor acabou de montar. Produtor e guard divergiram — a cascata "
        "de auto-detecção volta a ser possível."
    )


def test_o_guard_de_laco_nao_corta_evento_de_terceiro() -> None:
    """PAR POSITIVO, e o que dá sentido ao teste acima.

    Um guard que devolvesse `True` para tudo satisfaria os dois casos anteriores
    e calaria a detecção inteira — trocaria cascata por cegueira. Este caso
    prova que ele discrimina."""
    alheio = {
        "raw": {"id": "ev-9", "user": "alguem"},
        "_centralops": {"event_type": "sophos.detection"},
        "normalized": {"class_uid": 2004},
    }

    assert runtime_mod._is_self_emitted(alheio) is False, (
        "o guard cortou um evento de vendor. Note que ele tem class_uid 2004: "
        "a marca do produto, não a classe OCSF, é o que identifica auto-emissão "
        "— um vendor que emita Detection Finding é entrada legítima."
    )


def test_um_group_by_absurdo_na_regra_nao_estoura_o_evento() -> None:
    """O teto tem de valer para o campo que vem da CONFIGURAÇÃO, não do evento.

    O caso de pior tamanho existente monta o ponteiro com ``("raw","user")`` —
    8 caracteres — e por isso não enxergava este eixo. ``group_by_field`` é
    ``Column(String)`` sem teto no modelo, e ``compile_rule`` não valida
    comprimento: um dot-path absurdo digitado na regra entra no evento três
    vezes (``unmapped.group_field``, ``message`` e ``finding_info.desc``).

    Medido antes da correção: 4.000 caracteres levavam o evento a ~13 KiB
    contra um teto de 12 KiB; 20.000 chegavam a ~61 KiB, roçando o
    ``OS_MAXSTR`` que trunca em silêncio do outro lado do fio.

    Um teto que depende de ninguém digitar demais não é teto — e o módulo
    declara, por escrito, que o tamanho é limitado POR CONSTRUÇÃO.
    """
    group_path = tuple("campo" + "x" * 200 for _ in range(40))  # ~8.200 chars

    ponteiro = runtime_mod._event_source_pointer(
        _envelope(org_id=42), group_path, "valor-do-grupo"
    )
    emit = runtime_mod.DetectionEmit(
        dedup_key="inflight:1:7:k",
        detection_id=1,
        rule_id=7,
        rule_name="r",
        severity_id=4,
        integration_id=1,
        source=ponteiro,
    )
    envelope = runtime_mod._build_detection_event(emit, 42, 1_750_000_000_123)
    tamanho = len(json.dumps(envelope, ensure_ascii=False).encode("utf-8"))

    assert tamanho <= DETECTION_EVENT_MAX_BYTES, (
        f"um group_by de {len('.'.join(group_path))} chars na REGRA levou o "
        f"evento a {tamanho} bytes, acima do teto de {DETECTION_EVENT_MAX_BYTES}. "
        "O campo vem da configuração, não do evento, e escapou do truncamento."
    )

    # PAR POSITIVO: o teto não passou por um evento que ficou vazio, e o campo
    # continua LÁ — truncado, não removido. Sumir com ele trocaria estouro por
    # perda de diagnóstico: é o dot-path que diz ao operador qual regra errou.
    campo = envelope["normalized"]["unmapped"]["group_field"]
    assert campo, "o group_field sumiu do evento em vez de ser truncado"
    assert len(campo) < len(".".join(group_path)), "o group_field não foi truncado"
