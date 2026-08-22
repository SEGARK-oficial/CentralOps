"""Chaves inertes da DSL de enriquecimento: reconhecidas, recusadas, explicadas.

``entry_ttl_s`` era o caso: aceito com 200 OK, gravado na política, e sem UM leitor
no repo inteiro. O operador configurava expiração por linha, via a política salva, e
não expirava nada — fail-open exatamente no compilador que o ADR-LOCAL-0002 vende
como fail-closed (``dsl.py:10-14``).

Os testes vêm em PARES. "Recusa entry_ttl_s" sozinho passaria por vacuidade se o
compilador começasse a recusar a política inteira por outro motivo; por isso cada
recusa tem ao lado a MESMA política sem o campo, compilando.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import dataclasses

import pytest

from backend.app.collectors.enrich.contract import EnrichmentConfigError
from backend.app.collectors.enrich.dsl import (
    _INERT_RULE_KEYS,
    _RULE_KEYS,
    CompiledEnrichRule,
    compile_policy,
)


@pytest.fixture(autouse=True)
def _ensure_enrichers():
    from backend.app.collectors.enrich import enrichers  # noqa: F401


def _rule(**over):
    """Regra mínima que COMPILA. Cada teste muda só o que está sob julgamento."""
    base = {
        "id": "r1",
        "enricher": "opencti",
        # opencti declara ``required_secrets``; sem ``source`` o 422 viria daí e
        # mascararia o que este arquivo mede.
        "source": "fonte-de-teste",
        "key": {"source": "normalized.src_endpoint.ip", "kind": "ip"},
        "outputs": [{"from": "score", "target": "_centralops.enrichment.src.ti.score"}],
    }
    base.update(over)
    return base


# ── O par: com o campo é 422, sem o campo é 200 ─────────────────────────────

def test_entry_ttl_s_is_rejected_with_an_actionable_message():
    with pytest.raises(EnrichmentConfigError) as exc:
        compile_policy([_rule(entry_ttl_s=60)])

    msg = str(exc.value)
    # A mensagem tem três obrigações, e cada uma responde a uma pergunta que o
    # operador faria em seguida: por que parou de aceitar, o que o campo fazia,
    # e o que escrever no lugar.
    assert "entry_ttl_s" in msg
    assert "não é implementado" in msg
    assert "ttl_s" in msg and "negative_ttl_s" in msg
    # Não pode virar o erro genérico de chave desconhecida: quem digitou
    # ``entry_ttl_s`` digitou CERTO, e "desconhecida" o mandaria caçar typo.
    assert "desconhecida" not in msg


def test_the_same_policy_without_entry_ttl_s_compiles():
    """PAR POSITIVO do teste acima: sem ele, o negativo passa por vacuidade.

    Se um dia a regra-base parar de compilar (enricher renomeado, ``source``
    virando obrigatório de outro jeito), este teste cai junto e denuncia que o
    422 do teste anterior deixou de ser sobre ``entry_ttl_s``.
    """
    policy = compile_policy([_rule()])
    assert len(policy.rules) == 1
    assert policy.rules[0].rule_id == "r1"


def test_unknown_key_still_gets_the_generic_message():
    """Contraparte de ``assert "desconhecida" not in msg``.

    Sem este teste, aquela asserção negativa passaria se a palavra sumisse de
    TODAS as mensagens do compilador.
    """
    with pytest.raises(EnrichmentConfigError, match="desconhecida"):
        compile_policy([_rule(nao_existe=1)])


def test_rejected_key_is_not_advertised_as_permitted():
    """O erro genérico lista "Permitidas: [...]"; anunciar ali um campo que
    sempre dá 422 mandaria o operador escrever justamente o que será recusado."""
    with pytest.raises(EnrichmentConfigError) as exc:
        compile_policy([_rule(nao_existe=1)])

    msg = str(exc.value)
    assert "entry_ttl_s" not in msg
    # Positivo ao lado: a lista existe e carrega os campos que de fato compilam.
    assert "ttl_s" in msg and "negative_ttl_s" in msg


# ── Não-regressão: os TTLs que FUNCIONAM continuam funcionando ──────────────

def test_ttl_s_and_negative_ttl_s_are_accepted_and_reach_the_compiled_rule():
    """São lidos em ``EnrichRuntime.resolve_remote`` e aplicados em
    ``EnrichCache.put_many``. Se parassem de chegar compilados, o runtime cairia
    calado no ``suggested_*`` do enricher — TTL errado, nenhum erro."""
    policy = compile_policy([_rule(ttl_s=60, negative_ttl_s=30)])
    rule = policy.rules[0]
    assert rule.ttl_s == 60
    assert rule.negative_ttl_s == 30


def test_absent_ttls_compile_as_none_so_runtime_falls_back_to_caps():
    """``None`` não é detalhe: é o sinal de "não declarado" que faz o runtime usar
    ``reg.caps.suggested_ttl_s``. Um ``0`` acidental desligaria o cache."""
    rule = compile_policy([_rule()]).rules[0]
    assert rule.ttl_s is None
    assert rule.negative_ttl_s is None


@pytest.mark.parametrize("field", ["ttl_s", "negative_ttl_s"])
@pytest.mark.parametrize("bad", [-1, "abacaxi"])
def test_working_ttls_keep_validating_their_input(field, bad):
    with pytest.raises(EnrichmentConfigError, match=field):
        compile_policy([_rule(**{field: bad})])


# ── Invariantes do vocabulário de chaves inertes ────────────────────────────

def test_inert_keys_are_never_also_in_the_allowlist():
    """Se uma chave inerte voltasse à allowlist, ``_reject_unknown`` passaria a
    anunciá-la como permitida — o vazamento que este arquivo existe para impedir."""
    assert _INERT_RULE_KEYS, "sem chave inerte, os testes acima passam por vacuidade"
    assert not (set(_INERT_RULE_KEYS) & _RULE_KEYS)


def test_every_inert_key_points_at_something_that_actually_works():
    """Mensagem sem saída é beco: o operador descobre que o campo morreu e não
    descobre o que usar. Vale para toda chave inerte FUTURA, não só a de hoje.

    A busca exige o nome ENTRE ASPAS, como o resto do arquivo escreve campo. Sem
    as aspas, ``"id" in "campo removido"`` é True — "removido" contém "id" — e o
    invariante aprovaria justamente a mensagem-beco que ele deveria barrar.
    """
    for key, reason in _INERT_RULE_KEYS.items():
        cited = {k for k in _RULE_KEYS if f"'{k}'" in reason}
        assert cited, f"{key}: a mensagem não cita nenhum campo que compila"


def test_inert_keys_left_no_field_behind_on_the_compiled_rule():
    """Campo órfão no dataclass é o convite para alguém "religar" o knob lendo um
    valor que o compilador nunca mais preenche."""
    names = {f.name for f in dataclasses.fields(CompiledEnrichRule)}
    assert not (set(_INERT_RULE_KEYS) & names)
    # Positivo ao lado: os TTLs vivos continuam no dataclass.
    assert {"ttl_s", "negative_ttl_s"} <= names


def test_every_inert_key_is_actually_rejected():
    """Meta-teste: injeta cada chave inerte na regra-base e exige o 422 dela.

    Sem isso, acrescentar uma entrada em ``_INERT_RULE_KEYS`` e esquecer de
    ligá-la ao compilador passaria despercebido.
    """
    for key in _INERT_RULE_KEYS:
        with pytest.raises(EnrichmentConfigError) as exc:
            compile_policy([_rule(**{key: 1})])
        assert key in str(exc.value)
