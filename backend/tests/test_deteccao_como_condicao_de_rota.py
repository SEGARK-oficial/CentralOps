"""A DETECÇÃO COMO CONDIÇÃO DE ROTA, verificada por COMPORTAMENTO.

O pipeline já detectava ANTES de rotear e jogava o resultado fora na hora de
decidir para onde mandar. Este arquivo prende a ponte que faltava: o detector
marca ``_centralops.detection_matched`` no evento CASADO, e o motor de
roteamento passa a poder condicionar a entrega a essa marca — que é o que
permite detectar sobre o dado que vai ser descartado e mandar ao SIEM caro só o
que interessa.

Três invariantes, e cada teste existe por um modo de falha específico:

* **A marca acontece e ROTEIA.** Par obrigatório: um evento que casa é entregue
  pela rota condicionada E um que não casa NÃO é. Só a metade positiva deixaria
  passar uma marca escrita em todo evento (a rota entregaria os dois e o
  filtro não filtraria nada).
* **A promessa nova sobre o envelope.** O bloco de classificação prometia "sem
  mutação do envelope"; passa a prometer "só ACRESCENTA metadado do produto em
  ``_centralops``". A promessa vale o que vale o guard: ``raw`` e ``normalized``
  são comparados por igualdade PROFUNDA antes e depois.
* **R3 continua inviolável.** Falhar ao marcar não pode custar o evento — nem a
  Detection.

Nada é lido do ``.py``: sem marker ``source_only`` de propósito, porque na
imagem Cython ``pipeline`` e ``engine`` são ``.so`` e é lá que uma regressão
dessas custaria caro.

ANTI-VACUIDADE, o risco real: se o detector não RODAR (ruleset vazio ⇒
``_inflight_acc is None`` ⇒ o bloco inteiro é pulado), "o evento não foi
marcado" fica verde sem provar nada — e o teste do par negativo passaria pelo
motivo errado. Por isso o harness usa o matcher REAL contra uma regra REAL, e
:func:`test_o_harness_detecta_de_verdade` é a pré-condição de todos os outros.
"""

from __future__ import annotations

import copy
import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import asyncio
import contextlib
from typing import Any

import pytest

from backend.app.collectors import pipeline
from backend.app.collectors.inflight import runtime as runtime_mod
from backend.app.collectors.inflight.matcher import (
    CompiledClause,
    CompiledInflightRule,
    CompiledRuleSet,
)
from backend.app.collectors.routing import (
    ALLOWED_FIELDS,
    DETECTION_MATCHED_FIELD,
    CompiledRoute,
    route_batch,
    validate_condition,
)
from backend.app.collectors.state import dedupe


# ── A regra REAL: o matcher não é dublê ─────────────────────────────────────

#: Uma cláusula sobre ``raw.verdict``. Deliberadamente sobre o ``raw`` e não
#: sobre ``_centralops``: assim o evento casado e o não-casado diferem APENAS no
#: payload do vendor, e nenhuma diferença de label pode explicar sozinha a
#: decisão de rota que os testes abaixo atribuem à marca.
_REGRA = CompiledInflightRule(
    rule_id=91,
    name="verdict-malicioso",
    severity_id=4,
    suppression_window_seconds=3600,
    group_by_path=None,
    clauses=(CompiledClause(path=("raw", "verdict"), op="eq", value="malicious"),),
)
_RULESET = CompiledRuleSet(rules=(_REGRA,), share_paths=False)

#: Metade casa, metade não. A ordem alternada é de propósito: um bug que marcasse
#: "a partir do primeiro match" (marca pegajosa entre eventos do mesmo ciclo)
#: passaria despercebido se os casados viessem todos no fim.
_RAWS = [
    {"id": "m0", "verdict": "malicious", "user": "alice", "bytes": 10},
    {"id": "m1", "verdict": "clean", "user": "bob", "bytes": 20},
    {"id": "m2", "verdict": "malicious", "user": "carol", "bytes": 30},
    {"id": "m3", "verdict": "clean", "user": "dave", "bytes": 40},
]
_CASADOS = {"m0", "m2"}
_NAO_CASADOS = {"m1", "m3"}


# ── Dublês do ciclo (mesma forma do harness de R3) ───────────────────────────


class _FakeRedis:
    async def aclose(self) -> None:
        return None


class _FakeIntegration:
    id = 42
    is_active = True
    kind = "tenant"
    platform = "fakevendor"
    organization_id = 7
    organization = None
    data_geography = None


class _FakeDb:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def scalar(self, *a, **k):
        return _FakeIntegration()

    def expunge(self, *a, **k):
        return None


class _FakeConfig:
    collector_batch_size = 100  # um hand-off só: o lote inteiro chega junto
    collector_batch_flush_seconds = 1e9
    effective_dedupe_ttl_seconds = 60
    rate_limits_by_vendor: dict = {}
    domain_concurrency_limits: dict = {}


class _FakeApplied:
    """``output`` NÃO é vazio de propósito: ``normalized`` precisa ter conteúdo
    para a comparação por igualdade profunda significar alguma coisa."""

    output = {
        "class_uid": 2004,
        "severity_id": 3,
        "message": "evento de teste",
        "actor": {"user": {"name": "quem-quer-que-seja"}},
    }
    reduced_raw = None
    consumed_paths: tuple = ()
    ingest_fallback_targets: tuple = ()


class _FakeEngine:
    def apply(self, *a, **k):
        return _FakeApplied()


class _FakeCursorStore:
    def __init__(self, *a, **k):
        pass

    async def load(self, *a, **k):
        return {}

    async def save(self, *a, **k):
        return None


class _FakeRegistration:
    refresh_fn = None
    collector_cls: Any = None


def _acoro(value):
    async def _f(*a, **k):
        return value

    return _f


async def _noop_coro():
    return None


@contextlib.asynccontextmanager
async def _null_session():
    yield None


def _make_collector_cls(raw_events):
    class _Collector:
        event_type = "alert"

        def __init__(self, ctx):
            self.ctx = ctx

        async def collect(self):
            for ev in raw_events:
                yield ev

        def extract_message_id(self, raw):
            return raw["id"]

        @staticmethod
        def watermark_at(cursor):
            return None

    return _Collector


class _Sonda:
    def __init__(self) -> None:
        self.entregues: list[dict] = []
        self.add_calls = 0
        self.acumuladores: list[Any] = []


@pytest.fixture()
def harness(monkeypatch):
    """Ciclo REAL contra dublês, com o detector LIGADO e o matcher de verdade.

    Devolve ``(sonda, run)``. ``build_envelope`` NÃO é dublado: os testes de
    integridade precisam dos blocos ``raw``/``normalized`` reais que o pipeline
    entrega ao destino, e não de um envelope de mentira que só tem labels.
    """
    sonda = _Sonda()

    monkeypatch.setattr(
        "backend.app.collectors.celery_app.get_worker_redis", lambda: _FakeRedis()
    )
    monkeypatch.setattr(pipeline.database, "SessionLocal", lambda: _FakeDb())
    monkeypatch.setattr(pipeline, "_load_routes_for_org", lambda oid: [])
    monkeypatch.setattr(pipeline, "registry_has", lambda p, s: True)
    monkeypatch.setattr(pipeline, "_headers_for", lambda *a, **k: {})
    monkeypatch.setattr(pipeline, "_load_collection_filters", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "get_collector_config", _acoro(_FakeConfig()))
    monkeypatch.setattr(pipeline, "get_or_refresh_token", _acoro("tok"))
    monkeypatch.setattr(pipeline, "_load_current_mapping", lambda *a, **k: (1, [], "v2"))
    monkeypatch.setattr(pipeline, "CursorStore", _FakeCursorStore)
    monkeypatch.setattr(pipeline, "RedisRateLimiter", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "DomainLimiter", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_aiohttp_session", _null_session)
    monkeypatch.setattr(pipeline, "default_engine", _FakeEngine())
    monkeypatch.setattr(pipeline, "likely_no_session", lambda oid: True)
    monkeypatch.setattr(pipeline, "_record_source_ingested", lambda *a, **k: None)
    monkeypatch.setattr(pipeline.drift, "should_capture", lambda *a, **k: False)
    monkeypatch.setattr(pipeline.sample_reservoir, "push", lambda *a, **k: _noop_coro())

    async def _claim(redis, integration_id, msg_id, **kw):
        return True

    monkeypatch.setattr(pipeline, "claim", _claim)

    async def _release_many(redis, integration_id, ids):
        return len(list(ids))

    monkeypatch.setattr(dedupe, "release_many", _release_many)

    # O detector, LIGADO: ruleset não vazio ⇒ o bloco roda por evento.
    monkeypatch.setattr(
        runtime_mod, "load_inflight_rules_for_org", lambda oid: _RULESET
    )

    async def _flush(acc, organization_id):
        return None

    monkeypatch.setattr(runtime_mod, "flush_inflight", _flush)

    def run(raw_events=None, *, quebrar_marca: bool = False):
        class _AccSonda(runtime_mod.InflightAccumulator):
            def __init__(self) -> None:
                super().__init__()
                sonda.acumuladores.append(self)

            def add(self, *a, **k):
                sonda.add_calls += 1
                return super().add(*a, **k)

        monkeypatch.setattr(runtime_mod, "InflightAccumulator", _AccSonda)

        if quebrar_marca:
            # Injeção CIRÚRGICA da falha de marcação: o nome do rótulo vira um
            # objeto não-hasheável, então ``dict[chave] = True`` levanta
            # ``TypeError`` exatamente na linha da marca — sem tocar na estrutura
            # do bloco, sem dublar o matcher e sem mexer no ``acc.add``, que é
            # justamente o que este cenário precisa ver sobreviver.
            monkeypatch.setattr(pipeline, "_DETECTION_MATCHED_FIELD", [])

        reg = _FakeRegistration()
        reg.collector_cls = _make_collector_cls(
            _RAWS if raw_events is None else raw_events
        )
        monkeypatch.setattr(pipeline, "registry_get", lambda p, s: reg)

        def _dispatch(batch, routes=None, **kw):
            # Cópia PROFUNDA na fronteira do hand-off: o que o teste inspeciona
            # é o estado do envelope NO MOMENTO da entrega. Guardar a referência
            # deixaria uma mutação posterior reescrever a evidência.
            sonda.entregues.extend(copy.deepcopy(e) for e in batch)

        monkeypatch.setattr(pipeline, "_enqueue_dispatch", _dispatch)

        return asyncio.run(
            pipeline._run_collection_once(integration_id=42, stream="alerts")
        )

    return sonda, run


def _por_id(entregues: list[dict]) -> dict[str, dict]:
    return {e["_centralops"]["event_id"]: e for e in entregues}


# ── 0. Controle POSITIVO: sem ele todo o resto é vacuidade ──────────────────


def test_o_harness_detecta_de_verdade(harness):
    """Pré-condição de todos os outros: o matcher REAL roda, casa exatamente os
    dois eventos maliciosos e o acumulador recebe os dois matches. Sem isto,
    "o evento não foi marcado" ficaria verde num harness onde o detector nem
    executa."""
    sonda, run = harness
    run()

    assert sonda.add_calls == 2, (
        "o matcher real não casou os 2 eventos esperados — os testes de marca "
        f"abaixo estariam medindo um bloco que não roda (add_calls={sonda.add_calls})"
    )
    (acc,) = sonda.acumuladores
    assert acc.matches.get(_REGRA.rule_id) == 2
    assert not acc.errors, f"o caminho feliz não pode contar erro: {acc.errors}"
    assert sorted(_por_id(sonda.entregues)) == ["m0", "m1", "m2", "m3"]


# ── 1. A marca, e o PAR que impede a vacuidade ──────────────────────────────


def test_so_o_evento_casado_carrega_a_marca(harness):
    """Positivo e negativo no MESMO ciclo: os casados têm a marca, os não
    casados NÃO têm a chave (ausência é o "não casou" — ver
    ``DETECTION_MATCHED_FIELD``). Afirmar só o positivo deixaria passar uma
    marca escrita em todo evento, que é o bug que torna a condição de rota
    inútil sem nunca levantar."""
    sonda, run = harness
    run()

    por_id = _por_id(sonda.entregues)
    for eid in sorted(_CASADOS):
        assert por_id[eid]["_centralops"].get(DETECTION_MATCHED_FIELD) is True, (
            f"{eid} casou a regra e saiu SEM a marca"
        )
    for eid in sorted(_NAO_CASADOS):
        assert DETECTION_MATCHED_FIELD not in por_id[eid]["_centralops"], (
            f"{eid} não casou regra nenhuma e ganhou a marca mesmo assim — a "
            "condição de rota entregaria os dois lados"
        )


def test_a_rota_condicionada_entrega_o_casado_e_nao_o_nao_casado(harness):
    """A ponte inteira, ponta a ponta, sem dublê entre a detecção e a rota: os
    envelopes que o pipeline REALMENTE entregou alimentam o ``route_batch``
    REAL, com uma rota cuja única condição é a marca.

    O par é o teste: um ``route_batch`` que ignorasse a condição entregaria os
    4, e um que nunca casasse entregaria 0. Só a divisão 2/2 prova que a
    decisão veio da marca."""
    sonda, run = harness
    run()

    rota = CompiledRoute(
        id="siem-caro",
        name="só o que casou detecção",
        priority=10,
        condition={DETECTION_MATCHED_FIELD: True},
        action="route",
        destination_ids=("splunk",),
        is_final=True,
    )
    validate_condition(rota.condition)  # a condição é ACEITA pela API

    res = route_batch(sonda.entregues, [rota])
    entregues = {
        e["_centralops"]["event_id"] for e in res.sub_batches.get("splunk", [])
    }

    assert entregues == _CASADOS, (
        f"a rota condicionada à detecção entregou {sorted(entregues)}, "
        f"esperado {sorted(_CASADOS)}"
    )
    assert res.per_route.get("siem-caro") == 2
    # Par negativo explícito: os não-casados ficaram FORA (sem rota casada e sem
    # fallback ⇒ unrouted). Sem esta linha, "entregues == casados" ainda seria
    # compatível com um lote que só tivesse os 2 casados.
    assert res.unrouted == 2
    assert {
        e["_centralops"]["event_id"] for e in res.unrouted_events
    } == _NAO_CASADOS


def test_o_complemento_se_escreve_com_exists_false(harness):
    """A outra metade do vocabulário: "não casou" é AUSÊNCIA, e o operador que
    fala de ausência é ``exists``. Guard contra a regressão de quem tentaria
    ``{"eq": False}`` — valor que não existe no envelope e rota que nunca
    entrega."""
    sonda, run = harness
    run()

    rota = CompiledRoute(
        id="lago-barato",
        name="o que não casou",
        priority=10,
        condition={DETECTION_MATCHED_FIELD: {"exists": False}},
        action="route",
        destination_ids=("lake",),
        is_final=True,
    )
    validate_condition(rota.condition)

    res = route_batch(sonda.entregues, [rota])
    assert {
        e["_centralops"]["event_id"] for e in res.sub_batches.get("lake", [])
    } == _NAO_CASADOS


# ── 2. A promessa NOVA sobre o envelope ─────────────────────────────────────


def test_a_marca_nao_toca_raw_nem_normalized(harness):
    """O guard da promessa reescrita. O bloco não promete mais "não muta o
    envelope" — promete "só ACRESCENTA metadado do produto em ``_centralops``".

    Comparação por igualdade PROFUNDA contra o que o mapping produziu e contra o
    payload do vendor: uma escrita em ``normalized`` faria ``ocsf_valid``
    descrever um payload que não é o entregue, e uma escrita em ``raw``
    corromperia o bruto que o lago recebe. As duas passariam sem erro.

    ANTI-VACUIDADE: o teste exige que os blocos comparados NÃO sejam vazios e
    que a marca tenha de fato acontecido — senão ele aprova um pipeline que não
    entregou nada."""
    sonda, run = harness
    run()

    por_id = _por_id(sonda.entregues)
    assert set(por_id) == {"m0", "m1", "m2", "m3"}

    esperado_normalized = dict(_FakeApplied.output)
    for raw in _RAWS:
        eid = raw["id"]
        entregue = por_id[eid]
        assert entregue["raw"], "bloco raw vazio: a comparação abaixo é vácua"
        assert entregue["normalized"], "bloco normalized vazio: comparação vácua"
        assert entregue["raw"] == raw, (
            f"{eid}: o bloco raw mudou entre o vendor e a entrega"
        )
        assert entregue["normalized"] == esperado_normalized, (
            f"{eid}: o bloco normalized mudou depois do gate OCSF"
        )

    # Par POSITIVO obrigatório: os asserts de igualdade acima ficariam verdes num
    # ciclo em que a marca nunca fosse escrita — que é exatamente a regressão que
    # este arquivo existe para pegar.
    assert sum(
        1 for e in sonda.entregues if e["_centralops"].get(DETECTION_MATCHED_FIELD)
    ) == len(_CASADOS)


def test_a_marca_e_a_unica_chave_que_o_detector_acrescenta(harness):
    """Só ``_centralops`` muda, e dentro dele só UMA chave. Compara o conjunto de
    chaves do evento casado com o do não casado — dois eventos que passaram
    pelos MESMOS estágios e diferem apenas em ter casado ou não.

    Diferença ≠ {marca} significa que o detector escreveu (ou apagou) algo além
    do combinado — ``rule_id``, severidade, contadores — que é o que a decisão
    do dono proíbe."""
    sonda, run = harness
    run()

    por_id = _por_id(sonda.entregues)
    casado = set(por_id["m0"]["_centralops"])
    nao_casado = set(por_id["m1"]["_centralops"])

    assert casado - nao_casado == {DETECTION_MATCHED_FIELD}, (
        f"o detector acrescentou mais do que a marca: {sorted(casado - nao_casado)}"
    )
    assert nao_casado - casado == set(), (
        f"o detector REMOVEU labels do evento casado: {sorted(nao_casado - casado)}"
    )
    # E o resto do envelope tem exatamente os três blocos de sempre.
    assert set(por_id["m0"]) == {"_centralops", "normalized", "raw"}


# ── 3. R3: falhar ao marcar não custa nem o evento nem a Detection ──────────


def test_falha_ao_marcar_nao_impede_a_entrega_nem_a_deteccao(harness):
    """R3 continua inviolável no caminho novo. A marca explode em TODOS os
    eventos casados e:

      * os 4 eventos seguem entregues (o detector não virou porteiro);
      * o ``acc.add`` roda mesmo assim — a Detection não é vítima colateral de
        uma falha de marcação, e é por isso que a marca tem ``try`` PRÓPRIO;
      * a falha é CONTADA, e como ``mark_failed``, não como ``matcher``: somá-la
        no matcher mandaria o operador depurar a regra em vez do envelope.

    A contagem é a prova de não-vacuidade: sem ela, "os 4 foram entregues"
    ficaria verde num cenário em que a marca nunca foi tentada."""
    sonda, run = harness
    run(quebrar_marca=True)

    (acc,) = sonda.acumuladores
    assert acc.errors.get("mark_failed") == len(_CASADOS), (
        "a falha de marcação não foi contada uma vez por evento casado — sem "
        f"isso os asserts abaixo passam por vacuidade: {acc.errors}"
    )
    assert "matcher" not in acc.errors, (
        "a falha da MARCA foi contada como falha do MATCHER: o operador vai "
        f"depurar a regra errada ({acc.errors})"
    )
    assert sonda.add_calls == len(_CASADOS), (
        "a Detection morreu junto com a marca — o try da marca não protege o "
        f"acc.add (add_calls={sonda.add_calls})"
    )
    assert acc.matches.get(_REGRA.rule_id) == len(_CASADOS)
    assert sorted(_por_id(sonda.entregues)) == ["m0", "m1", "m2", "m3"], (
        "evento perdido com a marcação quebrada — o detector virou porteiro"
    )
    # E o envelope entregue simplesmente não tem a marca (nem meia marca).
    assert not any(
        DETECTION_MATCHED_FIELD in e["_centralops"] for e in sonda.entregues
    )


# ── 4. Não-regressão: a rota que existia antes não muda ─────────────────────


def test_rota_antiga_sem_a_condicao_nova_entrega_igual(harness):
    """O campo novo na allowlist não pode mudar rota nenhuma que já existia. Uma
    rota por ``vendor`` entrega os 4 eventos — casados e não casados — com o
    detector ligado e marcando.

    A regressão que este teste pega: qualquer tentativa de dar semântica
    IMPLÍCITA à marca (ex.: "rota sem condição de detecção só recebe o que não
    casou") partiria todo cliente instalado, em silêncio."""
    sonda, run = harness
    run()

    rota = CompiledRoute(
        id="catch-all-antigo",
        name="tudo do fakevendor",
        priority=100,
        condition={"vendor": "fakevendor"},
        action="route",
        destination_ids=("syslog",),
        is_final=True,
    )
    res = route_batch(sonda.entregues, [rota])

    assert {
        e["_centralops"]["event_id"] for e in res.sub_batches.get("syslog", [])
    } == {"m0", "m1", "m2", "m3"}
    assert res.unrouted == 0
    assert res.per_route.get("catch-all-antigo") == 4
    # Par POSITIVO: a marca ESTAVA lá e foi ignorada pela rota antiga — sem esta
    # linha o teste passaria num ciclo em que ninguém casou nada.
    assert any(
        e["_centralops"].get(DETECTION_MATCHED_FIELD) for e in sonda.entregues
    )


# ── 5. Invariantes do vocabulário (o campo é constante nova) ────────────────


def test_a_marca_e_um_campo_de_rota_de_primeira_classe():
    """A constante e a allowlist não podem divergir: quem escreve a marca e quem
    valida a condição têm de concordar no mesmo texto, senão a rota compila,
    fica verde na UI e nunca entrega."""
    assert DETECTION_MATCHED_FIELD in ALLOWED_FIELDS
    assert DETECTION_MATCHED_FIELD == "detection_matched"
    assert pipeline._DETECTION_MATCHED_FIELD == DETECTION_MATCHED_FIELD, (
        "o pipeline marcaria um rótulo diferente do que o roteador avalia"
    )


@pytest.mark.parametrize(
    "condicao",
    [
        {"detection_matched": True},
        {"detection_matched": {"eq": True}},
        {"detection_matched": {"ne": True}},
        {"detection_matched": {"exists": True}},
        {"detection_matched": {"exists": False}},
        {"detection_matched": {"in": [True]}},
        {"detection_matched": {"nin": [True]}},
    ],
)
def test_as_condicoes_que_a_marca_admite_sao_aceitas(condicao):
    """Par POSITIVO do guard de rejeição abaixo. Sem ele, um validador que
    recusasse TUDO para este campo passaria em todos os casos negativos."""
    validate_condition(condicao)


@pytest.mark.parametrize(
    "condicao",
    [
        # A forma que o ``<Input>`` do editor de condição emite hoje num ``eq``:
        # string onde se espera bool. Comparação nativa ⇒ nunca casa.
        {"detection_matched": "true"},
        {"detection_matched": {"eq": "true"}},
        {"detection_matched": 1},
        # ``False`` não existe no envelope: nunca casa.
        {"detection_matched": False},
        {"detection_matched": {"eq": False}},
        # ``ne: False`` casa TUDO (presente por desigualdade, ausente por
        # vacuidade de ``ne``): um filtro que não filtra.
        {"detection_matched": {"ne": False}},
        # Ordem sobre dois valores: legal em Python (bool é int), sem sentido aqui.
        {"detection_matched": {"gte": False}},
        {"detection_matched": {"lt": True}},
        {"detection_matched": {"in": [False]}},
        {"detection_matched": {"in": ["true"]}},
        {"detection_matched": {"in": []}},
        # O alias não é porta dos fundos.
        {"detection_matched": {"eq": None}},
    ],
)
def test_as_condicoes_que_nunca_casariam_sao_422_e_nao_silencio(condicao):
    """A armadilha que este guard fecha: TODAS estas condições compilam,
    salvam e ficam verdes na UI — e a rota simplesmente nunca entrega (ou
    entrega tudo). Contador zerado indistinguível de "não houve detecção". Mesmo
    remédio de ``validate_suppress_key``: o extremo que falhava calado vira erro
    de configuração explícito."""
    with pytest.raises(ValueError):
        validate_condition(condicao)


def test_a_marca_serve_de_assinatura_de_supressao():
    """Consequência de entrar em ``ALLOWED_FIELDS``: ``validate_suppress_key`` lê
    a MESMA allowlist. A marca é um label de AGRUPAMENTO (cardinalidade 2), não
    um único-por-evento, então aceitá-la é o comportamento correto — e afirmar
    isso aqui documenta que a interação foi considerada, não esquecida."""
    from backend.app.collectors.routing import validate_suppress_key

    validate_suppress_key(f"vendor,{DETECTION_MATCHED_FIELD}")
