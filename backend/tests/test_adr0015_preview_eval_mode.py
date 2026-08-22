"""O preview precisa cobrar o vocabulário do modo PEDIDO (ADR-0015, Fase 3).

O BUG QUE ESTE ARQUIVO FECHA, medido e não hipotético: ``evaluate_preview``
compilava SEMPRE com o compilador em voo, sem olhar o ``eval_mode`` da regra.
Uma regra ``eval_mode='batch'`` usando ``in``, ``nin`` ou ``exists`` — os três
operadores que existem só no inflight — voltava do preview com "casou 25 de 25".
Em produção, ``correlation_engine.matches_where`` acha o operador fora de
``_OPS``, devolve False no PRIMEIRO filtro e DESCARTA o item inteiro. Regra
verde na UI que nunca dispara: a falha silenciosa que a ADR existe para matar,
produzida justamente pela ferramenta de diagnóstico.

E A HONESTIDADE, que é o ponto: ramificar o operador conserta o sintoma MENOR.
O preview avalia o ENVELOPE do pipeline (``_centralops``/``normalized``/``raw``)
enquanto o motor batch avalia LINHAS CRUAS de query federada. Uma cláusula
``raw.user`` casa 25/25 aqui e resolve ``None`` lá, com operadores 100%
compartilhados. Os dois últimos blocos deste arquivo provam essa divergência com
teste, para que ela não fique só num comentário que ninguém lê — e para que
ninguém confunda "preview batch verde" com "vai disparar".
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import pytest

from backend.app.collectors.inflight.preview import (
    BATCH_PREVIEW_CAVEATS,
    build_preview_envelope,
    evaluate_preview,
)
from backend.app.collectors.inflight.runtime import (
    INFLIGHT_ALLOWED_OPS,
    REJECT_REASONS,
)
from backend.app.services.correlation_engine import _OPS as BATCH_OPS
from backend.app.services.correlation_engine import matches_where

_RULES = [
    {"target": "class_uid", "source": "eventType", "default": 1001},
    {"target": "severity_id", "source": "sev", "default": 1},
    {"target": "message", "source": "msg", "default": ""},
]

_RAW = {
    "id": "evt-1",
    "eventType": 2001,
    "sev": 4,
    "msg": "Malware detected",
    "user": "alice",
    "src_ip": "10.0.0.5",
}

#: Uma cláusula por operador do vocabulário em voo, TODAS escolhidas para CASAR
#: o envelope de ``_RAW``. Casar é obrigatório: um preview que devolvesse
#: ``matched=0`` por acidente do fixture tornaria o lado positivo dos pares
#: abaixo vacuamente verdadeiro, e o negativo deixaria de provar qualquer coisa.
_CLAUSE_BY_OP: dict[str, dict] = {
    "eq": {"field": "raw.user", "op": "eq", "value": "alice"},
    "ne": {"field": "raw.user", "op": "ne", "value": "bob"},
    "contains": {"field": "raw.msg", "op": "contains", "value": "Malware"},
    "gt": {"field": "raw.sev", "op": "gt", "value": 3},
    "gte": {"field": "raw.sev", "op": "gte", "value": 4},
    "lt": {"field": "raw.sev", "op": "lt", "value": 5},
    "lte": {"field": "raw.sev", "op": "lte", "value": 4},
    "in": {"field": "raw.user", "op": "in", "value": ["alice", "bob"]},
    "nin": {"field": "raw.user", "op": "nin", "value": ["carol"]},
    "exists": {"field": "raw.user", "op": "exists", "value": True},
}


def _envelope(raw=None):
    env = build_preview_envelope(
        raw if raw is not None else dict(_RAW),
        vendor="sophos",
        integration_id=7,
        organization_id=1,
        organization_name="ACME",
        customer_id="c-1",
        stream="alerts",
        event_type="alert",
        mapping_version_id=3,
        rules=_RULES,
        dsl_version=1,
    )
    assert env is not None, "a normalização falhou — o fixture está inválido"
    return env


def _where(*clauses: dict) -> str:
    return json.dumps(list(clauses))


# ── O gate de vocabulário, sempre em PAR ─────────────────────────────────────

@pytest.mark.parametrize("op", ["exists", "in", "nin"])
def test_batch_rejects_inflight_only_operator_that_inflight_accepts(op):
    """O par completo, no mesmo teste, de propósito.

    Sozinho, o lado ``batch`` passaria por VACUIDADE: bastaria o fixture estar
    quebrado, o envelope vir vazio ou a cláusula ser inválida por outro motivo
    para "invalid/unknown_op" sair de graça. O lado ``inflight`` prova que a
    MESMA regra, com o MESMO where, sobre o MESMO envelope, é válida e CASA —
    logo a reprovação do batch só pode vir do vocabulário.
    """
    env = _envelope()
    where = _where(_CLAUSE_BY_OP[op])

    batch = evaluate_preview([env], where, eval_mode="batch")
    assert batch.state == "invalid", (
        f"{op!r} não existe em correlation_engine._OPS: aprovar aqui é dizer "
        "'casou 25 de 25' para uma regra que o motor batch descarta inteira"
    )
    assert batch.reason == "unknown_op"
    assert batch.reason in REJECT_REASONS, (
        "a razão precisa ser do enum FECHADO de runtime.py — ela vira label de "
        "métrica, e um reason inventado aqui multiplicaria série em silêncio"
    )

    inflight = evaluate_preview([env], where, eval_mode="inflight")
    assert inflight.state == "ok"
    assert inflight.matched > 0, (
        f"{op!r} precisa CASAR no modo em voo, senão o lado negativo acima "
        "passa por vacuidade e este teste não prova nada"
    )


@pytest.mark.parametrize("op", sorted(INFLIGHT_ALLOWED_OPS))
def test_every_operator_is_judged_by_the_vocabulary_of_the_requested_mode(op):
    """Cobertura dos dois sentidos, operador por operador.

    Compartilhado ⇒ os DOIS modos avaliam e casam. Exclusivo do inflight ⇒ o
    batch reprova e o inflight casa. Um operador novo em qualquer um dos dois
    vocabulários cai aqui sem precisar de teste novo.
    """
    env = _envelope()
    where = _where(_CLAUSE_BY_OP[op])

    inflight = evaluate_preview([env], where, eval_mode="inflight")
    assert inflight.state == "ok" and inflight.matched == 1

    batch = evaluate_preview([env], where, eval_mode="batch")
    if op in BATCH_OPS:
        assert batch.state == "ok" and batch.matched == 1, (
            f"{op!r} é do vocabulário batch e foi reprovado — o gate ficou "
            "estrito demais e agora reprova regra legítima"
        )
    else:
        assert batch.state == "invalid" and batch.reason == "unknown_op"


def test_the_gap_between_the_two_vocabularies_is_exactly_the_three_known_ops():
    """Documenta a diferença que o gate existe para cobrar.

    Se alguém mexer em qualquer um dos dois vocabulários, este teste falha e
    obriga a revisitar o preview — que é o comportamento desejado: hoje a
    divergência foi descoberta em auditoria, não por teste.
    """
    assert len(BATCH_OPS) == 7 and len(INFLIGHT_ALLOWED_OPS) == 10
    assert sorted(INFLIGHT_ALLOWED_OPS - set(BATCH_OPS)) == ["exists", "in", "nin"]
    assert not set(BATCH_OPS) - INFLIGHT_ALLOWED_OPS, (
        "o batch ganhou um operador que o inflight não tem: o preview em voo "
        "passaria a reprovar regra que o batch aceita"
    )


def test_malformed_where_keeps_its_own_reason_in_batch_mode():
    """O gate de vocabulário não pode sequestrar as razões que já existiam.

    ``bad_json`` e ``empty_where`` continuam sendo a resposta certa: dizer
    ``unknown_op`` para um JSON quebrado mandaria o autor caçar um operador que
    ele nem chegou a escrever.
    """
    env = _envelope()
    assert evaluate_preview([env], "nao-e-json", eval_mode="batch").reason == "bad_json"
    assert evaluate_preview([env], "[]", eval_mode="batch").reason == "empty_where"


# ── O eco do modo ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("mode", ["inflight", "batch"])
def test_eval_mode_is_echoed_in_the_three_states(mode):
    """``ok``, ``invalid`` e ``empty`` — inclusive nos retornos PRECOCES.

    Sem o eco no ``invalid``, a UI diz "regra inválida" sem poder dizer
    "inválida para QUAL vocabulário", que é exatamente a pergunta seguinte do
    operador: ``exists`` é legítimo em voo e inexistente no batch, e a mesma
    regra muda de veredito conforme o radio button.
    """
    env = _envelope()

    ok = evaluate_preview([env], _where(_CLAUSE_BY_OP["eq"]), eval_mode=mode)
    assert ok.state == "ok" and ok.eval_mode == mode

    invalid = evaluate_preview([env], "nao-e-json", eval_mode=mode)
    assert invalid.state == "invalid" and invalid.eval_mode == mode

    empty = evaluate_preview([], _where(_CLAUSE_BY_OP["eq"]), eval_mode=mode)
    assert empty.state == "empty" and empty.eval_mode == mode


def test_positional_callers_keep_the_inflight_vocabulary():
    """O parâmetro é KWARG com default por contrato.

    Há chamadores que passam ``(envelopes, where_json)`` posicionalmente; um
    terceiro parâmetro posicional os quebraria — ou, pior, engoliria o
    ``eval_mode`` no lugar errado sem erro.
    """
    env = _envelope()
    r = evaluate_preview([env], _where(_CLAUSE_BY_OP["exists"]))
    assert r.eval_mode == "inflight"
    assert r.state == "ok" and r.matched == 1


def test_unknown_mode_raises_instead_of_falling_back_silently():
    """Cair no default calado devolveria o veredito de um vocabulário com o
    rótulo de outro — a mesma mentira que este parâmetro veio matar, só que
    mais difícil de enxergar."""
    env = _envelope()
    where = _where(_CLAUSE_BY_OP["eq"])
    with pytest.raises(ValueError):
        evaluate_preview([env], where, eval_mode="streaming")
    # Positivo ao lado: os dois modos do domínio fechado NÃO levantam.
    assert evaluate_preview([env], where, eval_mode="batch").state == "ok"
    assert evaluate_preview([env], where, eval_mode="inflight").state == "ok"


# ── A honestidade: o que o preview batch NÃO garante ─────────────────────────

def test_batch_result_confesses_that_it_approximates_vocabulary_not_source():
    """O resultado carrega as ressalvas; o modo em voo não carrega nenhuma.

    Não é enfeite: um operador que lê "casou 25 de 25" num preview batch precisa
    ler, no MESMO lugar, que o motor batch avalia outra fonte.
    """
    env = _envelope()
    where = _where(_CLAUSE_BY_OP["eq"])

    batch = evaluate_preview([env], where, eval_mode="batch")
    assert batch.caveats == BATCH_PREVIEW_CAVEATS and batch.caveats
    assert any("VOCABULÁRIO" in c and "FONTE" in c for c in batch.caveats)

    inflight = evaluate_preview([env], where, eval_mode="inflight")
    assert inflight.caveats == (), (
        "no modo em voo o preview avalia a MESMA estrutura com o MESMO "
        "comparador que o pipeline — não há aproximação a confessar"
    )


def test_the_batch_engine_sees_a_raw_row_and_not_the_envelope():
    """A divergência ESTRUTURAL que o gate de operador NÃO conserta.

    Operadores 100% compartilhados, ``eq`` dos dois lados, nenhum erro em lugar
    nenhum — e mesmo assim o preview diz "casou" e o motor batch não casaria,
    porque ``raw.user`` só existe no envelope que o pipeline monta. Corrigir
    isto exigiria rodar a query federada e está fora de escopo; deixar de
    DIZER isto seria repetir o silêncio da feature inteira.
    """
    env = _envelope()
    clause = _CLAUSE_BY_OP["eq"]  # raw.user == alice

    preview = evaluate_preview([env], _where(clause), eval_mode="batch")
    assert preview.state == "ok" and preview.matched == 1

    # O motor batch sobre a LINHA CRUA (o que a query federada devolve): o path
    # ``raw.user`` não existe lá.
    assert matches_where(dict(_RAW), [clause]) is False
    # Positivo ao lado, para provar que o False acima é do PATH e não de um
    # motor quebrado: o mesmo evento, no path da linha crua, casa.
    assert matches_where(
        dict(_RAW), [{"field": "user", "op": "eq", "value": "alice"}]
    ) is True


def test_negative_operators_are_stricter_here_than_in_the_batch_engine():
    """A 2ª divergência, na direção contrária — e por isso mais traiçoeira.

    O compilador em voo auto-injeta ``exists`` para todo path de ``ne``/``nin``
    (fecha o fail-open de allowlist); o motor batch mantém a VACUIDADE, onde
    campo ausente CASA. Logo o preview de uma regra batch com ``ne`` reporta
    MENOS casamentos do que o batch produziria — falso NEGATIVO, que faz o
    autor "consertar" uma regra que já funcionava.
    """
    sem_user = {k: v for k, v in _RAW.items() if k != "user"}
    clause = {"field": "raw.user", "op": "ne", "value": "bob"}

    preview = evaluate_preview([_envelope(sem_user)], _where(clause), eval_mode="batch")
    assert preview.state == "ok"
    assert preview.matched == 0, (
        "o exists auto-injetado tornou a cláusula fail-CLOSED no preview"
    )
    # O motor batch, sobre um item sem o campo, CASA por vacuidade.
    assert matches_where({}, [clause]) is True

    # Positivo ao lado: com o campo PRESENTE o preview casa, provando que o
    # zero acima vem da ausência do campo e não de um preview sempre-zero.
    presente = evaluate_preview([_envelope()], _where(clause), eval_mode="batch")
    assert presente.matched == 1
