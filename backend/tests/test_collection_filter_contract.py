"""Contrato dos filtros de COLETA — a camada que decide o que nem chega a ser puxado.

Complementa (sem repetir) ``test_integration_collection_filters_api.py``, que
exercita os endpoints. Aqui o alvo é o núcleo: a declaração
(``CollectionFilterField``), a validação (``coerce`` / ``coerce_filters``), o
carregamento no ciclo (``pipeline._load_collection_filters``) e — o mais
importante — o **guard estrutural** que impede um filtro declarado de nunca ser
aplicado.

Por que esse guard é o teste mais importante do arquivo: um filtro que a
``CollectorRegistration`` anuncia mas o coletor não lê passa por TODAS as outras
verificações. O catálogo mostra o campo, o formulário salva, o GET devolve o
valor, a auditoria registra a mudança — e o volume coletado não muda um byte. O
operador liga o filtro achando que cortou o ruído, o custo continua igual e a
coleta continua atrasada. É uma falha silenciosa de ponta a ponta.

Contexto do incidente (produção, jul/2026): o coletor Wazuh puxava TODAS as
severidades e a regra de roteamento descartava ``severity_id <= 2`` só depois.
Backlog de 2.906.255 eventos com 97,6% descartáveis; o teto por ciclo estava do
lado errado do funil.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import inspect
import json
import types
from datetime import timedelta
from typing import Any, Dict, Optional

import pytest

from backend.app.collectors import registry as registry_module
from backend.app.collectors.base import BaseCollector, CollectorContext
from backend.app.collectors.registry import (
    CollectionFilterField,
    CollectorRegistration,
)


# ── Helpers ──────────────────────────────────────────────────────────────


class _DummyCollector(BaseCollector):
    """Concreta só o bastante para caber numa ``CollectorRegistration``."""

    platform = "dummy"
    stream = "things"
    event_type = "dummy.thing"

    @property
    def domain(self) -> str:  # pragma: no cover — nunca chamado nestes testes
        return "dummy.example"

    async def collect(self):  # pragma: no cover — idem
        if False:
            yield {}

    def extract_message_id(self, event: Dict[str, Any]) -> str:  # pragma: no cover
        return str(event.get("id") or "")


async def _noop_refresh(integration_id: int) -> Dict[str, object]:  # pragma: no cover
    return {}


_LEVEL = CollectionFilterField(
    key="min_rule_level", label="Nível mínimo", type="int_range", default=0, min=0, max=16
)
_MODE = CollectionFilterField(
    key="mode", label="Modo", type="enum", default="all", options=("all", "high")
)
_FLAG = CollectionFilterField(key="flag", label="Bandeira", type="bool", default=False)


def _registration(*filters: CollectionFilterField) -> CollectorRegistration:
    return CollectorRegistration(
        platform="dummy",
        stream="things",
        collector_cls=_DummyCollector,
        refresh_fn=_noop_refresh,
        schedule=timedelta(minutes=5),
        queue="collect.bulk",
        task_name="collectors.collect_vendor_logs_bulk",
        filters=filters,
    )


# ── Declaração: o que o plugin NÃO pode declarar ─────────────────────────


def test_rejects_unknown_type() -> None:
    """Tipo fora do vocabulário quebra no import do vendor, não em produção.

    A UI renderiza o campo a partir do ``type``; um tipo que ela não conhece
    viraria um campo invisível ou um input errado — descoberto pelo operador.
    """
    with pytest.raises(ValueError, match="type inválido"):
        CollectionFilterField(key="x", label="X", type="slider", default=0)


def test_int_range_requires_min_and_max() -> None:
    """Sem limites não há como validar: qualquer inteiro viraria consulta válida."""
    with pytest.raises(ValueError, match="int_range exige min e max"):
        CollectionFilterField(key="x", label="X", type="int_range", default=0, min=0)
    with pytest.raises(ValueError, match="int_range exige min e max"):
        CollectionFilterField(key="x", label="X", type="int_range", default=0, max=16)


def test_int_range_default_must_be_inside_the_range() -> None:
    with pytest.raises(ValueError, match="fora de"):
        CollectionFilterField(key="x", label="X", type="int_range", default=99, min=0, max=16)


def test_int_range_default_must_be_an_int() -> None:
    """``default=None`` num int_range é o erro que reintroduz o bug original.

    O contrato diz que o default é o valor que NÃO filtra nada. ``None`` seria
    lido como "sem filtro" no caminho quente, mas ``is_noop`` deixaria de casar
    com o extremo real do range e um operador que "voltasse ao mínimo" gravaria
    um filtro ativo achando que tinha desligado.
    """
    with pytest.raises(ValueError, match="fora de"):
        CollectionFilterField(key="x", label="X", type="int_range", min=0, max=16)


def test_enum_requires_options() -> None:
    with pytest.raises(ValueError, match="enum exige options"):
        CollectionFilterField(key="x", label="X", type="enum", default="all")


def test_enum_default_must_be_one_of_the_options() -> None:
    with pytest.raises(ValueError, match="fora de options"):
        CollectionFilterField(
            key="x", label="X", type="enum", default="nope", options=("all", "high")
        )


def test_bool_requires_boolean_default() -> None:
    with pytest.raises(ValueError, match="bool exige default booleano"):
        CollectionFilterField(key="x", label="X", type="bool", default=0)


def test_valid_declarations_are_accepted() -> None:
    assert _LEVEL.default == 0 and _MODE.default == "all" and _FLAG.default is False


# ── coerce: um valor por vez ─────────────────────────────────────────────


def test_coerce_accepts_valid_values() -> None:
    assert _LEVEL.coerce(7) == 7
    assert _MODE.coerce("high") == "high"
    assert _FLAG.coerce(True) is True


def test_coerce_none_falls_back_to_the_no_op_default() -> None:
    """``None`` é "campo não enviado", não "filtro zerado" — devolve o default."""
    assert _LEVEL.coerce(None) == 0
    assert _MODE.coerce(None) == "all"
    assert _FLAG.coerce(None) is False


@pytest.mark.parametrize("bad", ["7", 7.5, [7], {"gte": 7}])
def test_coerce_int_range_rejects_wrong_type(bad: Any) -> None:
    with pytest.raises(ValueError, match="esperado inteiro"):
        _LEVEL.coerce(bad)


@pytest.mark.parametrize("bad", [True, False])
def test_coerce_int_range_rejects_bool_even_though_python_says_it_is_an_int(bad: bool) -> None:
    """``isinstance(True, int)`` é ``True`` em Python — o guard tem de pegar isso.

    Sem o ``isinstance(value, bool)`` explícito, um ``{"min_rule_level": true}``
    vindo de um JSON mal montado viraria ``rule.level >= 1`` em silêncio: um
    filtro que o operador nunca pediu, cortando os informativos e ninguém
    sabendo por quê. ``False`` seria pior — viraria ``>= 0``, indistinguível de
    "sem filtro" no cursor mas persistido como configuração ativa.
    """
    with pytest.raises(ValueError, match="esperado inteiro"):
        _LEVEL.coerce(bad)


@pytest.mark.parametrize("bad", [-1, 17, 100])
def test_coerce_int_range_rejects_out_of_range(bad: int) -> None:
    with pytest.raises(ValueError, match="fora de"):
        _LEVEL.coerce(bad)


def test_coerce_int_range_accepts_the_inclusive_bounds() -> None:
    assert _LEVEL.coerce(0) == 0
    assert _LEVEL.coerce(16) == 16


def test_coerce_enum_rejects_value_outside_options() -> None:
    with pytest.raises(ValueError, match="não está em"):
        _MODE.coerce("critical")


def test_coerce_bool_rejects_non_boolean() -> None:
    with pytest.raises(ValueError, match="esperado booleano"):
        _FLAG.coerce("true")
    with pytest.raises(ValueError, match="esperado booleano"):
        _FLAG.coerce(1)


# ── coerce_filters: o dict inteiro ───────────────────────────────────────


def test_coerce_filters_rejects_unknown_key() -> None:
    """Chave desconhecida é ERRO, nunca campo ignorado.

    Ignorar silenciosamente é o pior desfecho possível: o operador digita
    ``min_rule_lvl``, a API responde 200, a tela mostra o filtro salvo e o
    coletor continua puxando tudo.
    """
    reg = _registration(_LEVEL)
    with pytest.raises(ValueError, match="filtro desconhecido"):
        reg.coerce_filters({"min_rule_lvl": 7})


def test_coerce_filters_error_lists_the_supported_keys() -> None:
    reg = _registration(_LEVEL, _MODE)
    with pytest.raises(ValueError) as exc:
        reg.coerce_filters({"nope": 1})
    assert "min_rule_level" in str(exc.value) and "mode" in str(exc.value)


def test_coerce_filters_on_a_stream_without_filters_says_so() -> None:
    reg = _registration()
    with pytest.raises(ValueError, match="nenhum"):
        reg.coerce_filters({"min_rule_level": 7})


def test_coerce_filters_omits_values_equal_to_the_default() -> None:
    """Gravar o default deixaria lixo com cara de configuração ativa.

    Voltar ao valor que não filtra tem de LIMPAR a linha — assim a tela distingue
    "nunca configurado" de "configurado para não filtrar", e o caminho quente do
    coletor continua vendo ``filters == {}``.
    """
    reg = _registration(_LEVEL, _MODE, _FLAG)
    assert reg.coerce_filters({"min_rule_level": 0, "mode": "all", "flag": False}) == {}


def test_coerce_filters_keeps_only_what_actually_filters() -> None:
    reg = _registration(_LEVEL, _MODE, _FLAG)
    out = reg.coerce_filters({"min_rule_level": 7, "mode": "all", "flag": True})
    assert out == {"min_rule_level": 7, "flag": True}


def test_coerce_filters_treats_none_as_default_and_omits_it() -> None:
    reg = _registration(_LEVEL)
    assert reg.coerce_filters({"min_rule_level": None}) == {}


def test_coerce_filters_of_empty_input_is_empty() -> None:
    reg = _registration(_LEVEL)
    assert reg.coerce_filters(None) == {}
    assert reg.coerce_filters({}) == {}


def test_is_noop_matches_default_and_none() -> None:
    assert _LEVEL.is_noop(0) and _LEVEL.is_noop(None)
    assert not _LEVEL.is_noop(7)


# ── GUARD ESTRUTURAL: todo filtro declarado é de fato aplicado ───────────


def _declared_filters() -> list:
    """Todo par (registration, key) que algum plugin anuncia — descoberto, não listado.

    Um vendor novo que declare filtros entra no guard sozinho; ninguém precisa
    lembrar de vir aqui adicioná-lo.
    """
    return [
        (reg, spec.key)
        for reg in registry_module.all_registrations()
        for spec in reg.filters
    ]


_DECLARED = _declared_filters()
_DECLARED_IDS = [f"{reg.platform}/{reg.stream}:{key}" for reg, key in _DECLARED]


def test_at_least_one_filter_is_declared_in_the_registry() -> None:
    """Âncora do guard abaixo: se ninguém declara filtro, ele não verifica nada.

    Este teste existe para que apagar o filtro do Wazuh por acidente apareça
    como falha, em vez de esvaziar silenciosamente a parametrização.
    """
    assert _DECLARED, "nenhuma CollectorRegistration declara filtros de coleta"


@pytest.mark.source_only  # lê o .py; na imagem Cython o fonte não existe
@pytest.mark.parametrize("reg,key", _DECLARED, ids=_DECLARED_IDS)
def test_every_declared_filter_is_read_by_its_collector(reg, key: str) -> None:
    """Filtro declarado e não aplicado é a pior falha deste subsistema.

    Toda a cadeia de validação continua verde — catálogo, PUT, GET, auditoria —
    e o único efeito observável é o que NÃO acontece: o volume não cai. O
    operador conclui que reduziu custo, o backlog continua crescendo e nada
    aponta para a causa.

    O acesso tem de passar por ``BaseCollector.filter_value("<key>")``: é o
    único ponto que traduz "não configurado" em ``None`` e mantém a consulta
    idêntica à de sempre no caminho quente.
    """
    patterns = (f'filter_value("{key}")', f"filter_value('{key}')")
    class_src = inspect.getsource(reg.collector_cls)
    module = inspect.getmodule(reg.collector_cls)
    module_src = inspect.getsource(module) if module else ""
    assert any(p in class_src for p in patterns) or any(p in module_src for p in patterns), (
        f"{reg.platform}/{reg.stream} declara o filtro {key!r} mas "
        f"{reg.collector_cls.__name__} nunca chama filter_value({key!r}) — a UI "
        "confirmaria o filtro e o volume coletado não mudaria"
    )


@pytest.mark.source_only  # lê o .py; na imagem Cython o fonte não existe
def test_wazuh_detections_declares_min_rule_level_with_a_no_op_default() -> None:
    """O default TEM de coletar tudo: atualizar sem abrir a tela não muda nada.

    ``rule.level`` vai de 0 a 16 no Wazuh; ``0`` é o extremo que não corta nada.
    Qualquer outro default faria um upgrade cortar eventos silenciosamente.
    """
    reg = registry_module.get("wazuh", "detections")
    spec = reg.filter_by_key("min_rule_level")
    assert spec is not None, "wazuh/detections perdeu o filtro min_rule_level"
    assert spec.type == "int_range" and (spec.min, spec.max) == (0, 16)
    assert spec.default == 0, "default != 0 faria o upgrade cortar eventos sozinho"
    assert spec.warning_text, (
        "sem warning_text a UI liga o filtro sem avisar que o evento filtrado "
        "NUNCA entra na plataforma (nem drift, nem captura, nem rota futura)"
    )


# ── Carregamento no ciclo: pipeline._load_collection_filters ─────────────


def _integration(collection_filters: Optional[str]) -> Any:
    return types.SimpleNamespace(id=42, collection_filters=collection_filters)


def test_load_collection_filters_reads_the_stream_slice() -> None:
    from backend.app.collectors import pipeline

    reg = _registration(_LEVEL)
    raw = json.dumps({"things": {"min_rule_level": 7}, "other": {"min_rule_level": 12}})
    assert pipeline._load_collection_filters(_integration(raw), reg, "things") == {
        "min_rule_level": 7
    }


def test_load_collection_filters_is_empty_when_column_is_null() -> None:
    """O caminho de toda instalação que nunca abriu a tela."""
    from backend.app.collectors import pipeline

    reg = _registration(_LEVEL)
    assert pipeline._load_collection_filters(_integration(None), reg, "things") == {}


def test_load_collection_filters_is_empty_for_a_stream_without_configuration() -> None:
    from backend.app.collectors import pipeline

    reg = _registration(_LEVEL)
    raw = json.dumps({"other": {"min_rule_level": 7}})
    assert pipeline._load_collection_filters(_integration(raw), reg, "things") == {}


def test_load_collection_filters_fails_open_on_corrupt_json(caplog) -> None:
    """Config ruim vira SEM filtro + WARNING — nunca coleta abortada.

    Filtrar é otimização de custo. Parar de coletar por causa de uma linha
    inválida no banco trocaria custo por PERDA DE DADO, que é o oposto do
    objetivo. (Do lado da API a validação é fail-closed: o valor inválido nunca
    chega a ser gravado.)
    """
    from backend.app.collectors import pipeline

    reg = _registration(_LEVEL)
    with caplog.at_level("WARNING"):
        assert pipeline._load_collection_filters(_integration("{nao-e-json"), reg, "things") == {}
    assert "SEM filtro" in caplog.text


def test_load_collection_filters_fails_open_on_value_that_no_longer_validates(caplog) -> None:
    """Valor gravado sob um contrato antigo (max=16) e lido sob um novo (max=10).

    Mandar ``gte 16`` para um vendor que agora só aceita até 10 devolveria zero
    resultados e o stream inteiro pareceria vazio — falha muda. Revalidar e
    ignorar é o comportamento seguro.
    """
    from backend.app.collectors import pipeline

    narrower = CollectionFilterField(
        key="min_rule_level", label="Nível", type="int_range", default=0, min=0, max=10
    )
    reg = _registration(narrower)
    raw = json.dumps({"things": {"min_rule_level": 16}})
    with caplog.at_level("WARNING"):
        assert pipeline._load_collection_filters(_integration(raw), reg, "things") == {}
    assert "SEM filtro" in caplog.text


def test_load_collection_filters_accepts_an_already_parsed_dict() -> None:
    """A coluna é Text hoje; um driver que devolva JSON nativo não pode quebrar."""
    from backend.app.collectors import pipeline

    reg = _registration(_LEVEL)
    integration = _integration(None)
    integration.collection_filters = {"things": {"min_rule_level": 12}}
    assert pipeline._load_collection_filters(integration, reg, "things") == {
        "min_rule_level": 12
    }


# ── BaseCollector: acesso e sinalização ─────────────────────────────────


def _ctx(**over: Any) -> CollectorContext:
    base = dict(
        integration_id=42,
        organization_id=7,
        platform="dummy",
        headers={},
        session=None,
        cursor=None,
        domain_limiter=None,
        rate_limiter=None,
        redis=None,
    )
    base.update(over)
    return CollectorContext(**base)  # type: ignore[arg-type]


def test_filter_value_is_none_when_nothing_is_configured() -> None:
    """O caminho quente: sem filtro o coletor monta a consulta de sempre."""
    assert _DummyCollector(_ctx()).filter_value("min_rule_level") is None


def test_filter_value_returns_the_configured_value() -> None:
    c = _DummyCollector(_ctx(filters={"min_rule_level": 7}))
    assert c.filter_value("min_rule_level") == 7


def test_context_defaults_are_no_filter_and_no_cap() -> None:
    ctx = _ctx()
    assert ctx.filters == {} and ctx.hit_cycle_cap is False


def test_mark_cycle_capped_sets_the_flag_on_the_context() -> None:
    """É o sinal que o pipeline persiste como ``last_run_capped``."""
    ctx = _ctx()
    _DummyCollector(ctx).mark_cycle_capped()
    assert ctx.hit_cycle_cap is True


# ── Ponta da cadeia: o filtro chega na consulta do fornecedor ───────────


def test_wazuh_query_without_filter_is_byte_identical_to_the_previous_format() -> None:
    """Sem filtro, a consulta é a MESMA de antes — inclusive na ordem das chaves.

    É a garantia de upgrade: quem atualiza e não abre a tela manda para o Indexer
    exatamente os mesmos bytes que mandava. Comparar o JSON serializado (e não os
    dicts) é de propósito — um ``bool`` envolvendo o ``range`` seria equivalente
    para o OpenSearch, mas mudaria o plano de execução e o perfil de cache num
    cluster que hoje está no limite.
    """
    from backend.app.collectors.vendors.wazuh_detections import (
        _PAGE_SIZE,
        WazuhDetectionsCollector,
    )

    body = WazuhDetectionsCollector._search_body("2026-07-24T10:00:00Z", 400)
    esperado = {
        "size": _PAGE_SIZE,
        "from": 400,
        "track_total_hits": False,
        "sort": [{"timestamp": {"order": "asc"}}],
        "query": {"range": {"timestamp": {"gte": "2026-07-24T10:00:00Z"}}},
    }
    assert json.dumps(body) == json.dumps(esperado)


def test_wazuh_query_default_argument_means_no_filter() -> None:
    """``min_rule_level=None`` é o mesmo que não passar o argumento."""
    from backend.app.collectors.vendors.wazuh_detections import WazuhDetectionsCollector

    a = WazuhDetectionsCollector._search_body("2026-07-24T10:00:00Z", 0)
    b = WazuhDetectionsCollector._search_body("2026-07-24T10:00:00Z", 0, None)
    assert json.dumps(a) == json.dumps(b)


def test_wazuh_query_with_min_rule_level_pushes_the_cut_to_the_indexer() -> None:
    """``min_rule_level=7`` = ``severity_id <= 2`` descartado na ORIGEM.

    Mapeamento do Wazuh: 0-3 Informativo, 4-6 Baixo, 7-11 Médio, 12-14 Alto,
    15-16 Crítico. A regra de roteamento que descarta severidade <= 2 equivale a
    ``rule.level >= 7`` aqui — a diferença é que agora o evento nem é
    transportado, e o teto por ciclo passa a ser gasto no que será entregue.
    """
    from backend.app.collectors.vendors.wazuh_detections import WazuhDetectionsCollector

    body = WazuhDetectionsCollector._search_body("2026-07-24T10:00:00Z", 0, 7)
    clauses = body["query"]["bool"]["filter"]
    assert {"range": {"timestamp": {"gte": "2026-07-24T10:00:00Z"}}} in clauses
    assert {"range": {"rule.level": {"gte": 7}}} in clauses
    # Paginação e ordenação não podem mudar por causa do filtro: o cursor
    # {from_ts} depende de ``sort`` ascendente por timestamp.
    assert body["sort"] == [{"timestamp": {"order": "asc"}}]
    assert body["track_total_hits"] is False


def test_wazuh_query_keeps_the_time_window_when_filtering() -> None:
    """Perder a janela temporal transformaria cada ciclo numa varredura do índice."""
    from backend.app.collectors.vendors.wazuh_detections import WazuhDetectionsCollector

    body = WazuhDetectionsCollector._search_body("2026-07-24T10:00:00Z", 200, 12)
    serialized = json.dumps(body)
    assert '"gte": "2026-07-24T10:00:00Z"' in serialized
    assert body["from"] == 200


def test_wazuh_query_filter_at_level_zero_is_not_reachable_through_the_contract() -> None:
    """``0`` nunca chega ao coletor: ``coerce_filters`` omite o default.

    O guard é aqui e não no ``_search_body`` de propósito — ``rule.level >= 0``
    seria inofensivo, mas adicionaria um ``bool`` inútil à consulta de TODA
    instalação que tivesse gravado o valor por engano.
    """
    reg = registry_module.get("wazuh", "detections")
    assert reg.coerce_filters({"min_rule_level": 0}) == {}


# ── Backfill honra o mesmo filtro ───────────────────────────────────────


@pytest.mark.source_only  # lê o .py; na imagem Cython o fonte não existe
def test_backfill_builds_the_context_with_the_integration_filters() -> None:
    """Backfill que ignorasse o filtro recriaria o problema por outra porta.

    A janela voltaria cheia dos eventos que a regra de roteamento descarta em
    seguida — o job inteiro gasto com ruído, exatamente o que o filtro existe
    para evitar. Recuperar de propósito o que foi filtrado é um ato explícito:
    desligar o filtro, rodar o backfill, religar.
    """
    from backend.app.collectors import backfill_tasks

    src = inspect.getsource(backfill_tasks.run_backfill_collection_once)
    assert "filters=_load_collection_filters(integration, registration, stream)" in src, (
        "run_backfill_collection_once não passa os filtros de coleta para o "
        "CollectorContext — o backfill puxaria o que o polling filtra"
    )
    assert "bounded_per_cycle=False" in src, (
        "o backfill deixou de ser unbounded; o teto por ciclo truncaria o job"
    )
