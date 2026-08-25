"""ADR-0015 — um ``group_by_field`` que nunca resolve é REJEITADO, não ignorado.

A DECISÃO. Em voo, ``group_by_field`` é resolvido por ``_resolve`` a partir da
RAIZ do envelope, que tem exatamente três chaves. Um path cujo primeiro segmento
não seja uma delas resolve ``None`` em 100% dos eventos — não é um caso raro, é
impossível de acertar. ``compile_rule`` passa a recusar a regra com a razão
``group_by_root``.

O QUE ISSO CONSERTA, e por que a forma antiga era a pior possível. O motor conta
o match ANTES de resolver o group_by (``runtime.py``: ``self.matches[...] += 1``
e só depois o ``_resolve``). Com um path irresolvível, a regra ficava assim, para
sempre:

  * o painel de métricas mostrava a regra CASANDO, evento após evento;
  * Detection nenhuma nascia;
  * nada na tela ligava uma coisa à outra.

E o produto ENSINAVA o path quebrado: o placeholder do formulário dizia
``Ex: source.ip`` e a doc de operações repetia ``source.ip`` onze vezes. O
envelope não tem chave ``source`` — o operador seguia a documentação e recebia
uma regra que parecia funcionar e não produzia nada.

Rejeitar na COMPILAÇÃO troca esse silêncio por sinal: a regra entra em
``uncompilable_count`` e a linha ganha o selo "Não avaliada" na tela, que é
calculado justamente por ``compile_rule``.

POR QUE NÃO VALE PARA LOTE. O modo em lote roda sobre resultados de busca
federada (``correlation_engine.extract_path``), onde ``source.ip`` pode ser
perfeitamente válido. Este ``compile_rule`` é do caminho em voo e nunca recebe
regra em lote — os quatro chamadores são o ciclo do coletor, o preview e o
snapshot de limites, todos sobre ``list_inflight_for_org``, mais dois shims que
nem carregam ``group_by_field``.
"""

from __future__ import annotations

import inspect

import pytest

from backend.app.collectors.inflight.runtime import REJECT_REASONS, compile_rule
from backend.app.collectors.normalize.envelope import ENVELOPE_ROOTS, build_envelope


class _Regra:
    """Linha mínima que ``compile_rule`` sabe ler."""

    def __init__(self, group_by, where=None):
        self.id = 1
        self.name = "regra"
        self.severity_id = 4
        self.suppression_window_seconds = 3600
        self.group_by_field = group_by
        self.where_json = where or '[{"field": "_centralops.vendor", "op": "eq", "value": "x"}]'


# ── a rejeição ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "source.ip",  # o do placeholder e da doc — o caso real
        "user.name",
        "event.type",
        "severity_id",  # campo do OCSF, mas sem o prefixo `normalized.`
    ],
)
def test_path_fora_das_raizes_do_envelope_e_recusado(path: str) -> None:
    rule, reason = compile_rule(_Regra(path))

    assert rule is None, (
        f"{path!r} compilou. Ele resolve None em TODO evento: a regra contaria "
        "match para sempre e não produziria Detection nenhuma."
    )
    assert reason == "group_by_root"


@pytest.mark.parametrize("raiz", ENVELOPE_ROOTS)
def test_path_em_cada_raiz_valida_COMPILA(raiz: str) -> None:
    """PAR POSITIVO, e não simetria decorativa: sem ele, um ``return None`` posto
    por engano no lugar errado deixaria os testes acima verdes e a feature
    inteira morta. Cobre as TRÊS raízes, uma a uma — recusar ``raw`` por
    descuido seria uma perda de capacidade silenciosa."""
    rule, reason = compile_rule(_Regra(f"{raiz}.qualquer.coisa"))

    assert reason is None, f"raiz válida {raiz!r} foi recusada com {reason!r}"
    assert rule is not None
    assert rule.group_by_path == (raiz, "qualquer", "coisa")


def test_sem_group_by_continua_valido() -> None:
    """``group_by_field`` é opcional: ausente, a regra gera UMA Detection por
    janela de supressão. A checagem não pode transformar opcional em obrigatório."""
    rule, reason = compile_rule(_Regra(None))

    assert reason is None
    assert rule is not None and rule.group_by_path is None


# ── a constante não pode divergir do envelope real ──────────────────────────


def test_ENVELOPE_ROOTS_e_exatamente_o_que_build_envelope_produz() -> None:
    """Guard mecânico contra a divergência que tornaria esta checagem errada.

    Constrói um envelope de VERDADE e compara as chaves de topo. Se alguém
    acrescentar uma quarta chave ao envelope sem tocar na constante, toda regra
    que a usasse seria recusada — capacidade nova nascendo bloqueada, com uma
    mensagem de erro que aponta para o lugar errado."""
    from backend.app.collectors.normalize.envelope import EnvelopeContext

    ctx = EnvelopeContext(
        vendor="v",
        stream="s",
        event_type="e",
        integration_id=1,
        customer_id=1,
        mapping_version_id=1,
    )
    env = build_envelope({"a": 1}, {"b": 2}, ctx)

    assert set(env) == set(ENVELOPE_ROOTS), (
        f"build_envelope produz {sorted(env)} e ENVELOPE_ROOTS diz "
        f"{sorted(ENVELOPE_ROOTS)} — a checagem de group_by está julgando por "
        "um contrato que não é mais o real."
    )


def test_a_razao_esta_no_enum_fechado() -> None:
    """``REJECT_REASONS`` é label de métrica: uma razão fora do enum vira série
    órfã ou KeyError, dependendo de quem consome."""
    assert "group_by_root" in REJECT_REASONS


def test_a_checagem_nao_custa_por_evento() -> None:
    """R1 do ADR-0015 — zero trabalho novo no caminho do evento.

    A checagem tem de estar em ``compile_rule``, que roda uma vez por ciclo, e
    NÃO no avaliador por evento. Lê a fonte porque é a única forma de afirmar
    ONDE o custo mora."""
    fonte_compile = inspect.getsource(compile_rule)
    assert "group_by_root" in fonte_compile, (
        "a rejeição saiu de compile_rule — se foi para o caminho do evento, "
        "passou a custar por evento em vez de por ciclo."
    )


test_a_checagem_nao_custa_por_evento = pytest.mark.source_only(
    test_a_checagem_nao_custa_por_evento
)
