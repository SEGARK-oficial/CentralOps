"""Fail-safes da supressão por assinatura.

INCIDENTE DE PRODUÇÃO que motiva estes testes: uma rota recebeu
``suppress_key="src_ip"`` (o exemplo que a própria UI sugeria) e o pipeline passou
a descartar ~100% do tráfego. Causa: ``src_ip`` não existe em ``_centralops`` — o
único escopo de labels visível à supressão —, então ``labels.get(k, "")`` devolvia
"" para TODOS os eventos, colapsando tudo numa assinatura só. Passados
``suppress_allow`` eventos por janela, todo o resto era descartado em silêncio:
sem erro, sem DLQ, sem métrica de anomalia.

Agravante: a rota tinha ``protect_detection=True``, e a supressão era a única
alavanca de redução que ignorava essa proteção.
"""
from __future__ import annotations

import pytest

from backend.app.collectors.routing.engine import event_labels
from backend.app.collectors.state.dedupe import suppress_signature


def _env(**over):
    labels = {
        "event_id": "e1", "vendor": "sophos", "event_type": "sophos.detection",
        "organization_id": 7, "severity_id": 3,
    }
    labels.update(over)
    return {"_centralops": labels, "normalized": {"src_endpoint": {"ip": "10.0.0.1"}},
            "raw": {"srcip": "10.0.0.1"}}


# ── assinatura degenerada ────────────────────────────────────────────────────

def test_key_outside_the_label_scope_yields_no_signature():
    """`src_ip` vive em normalized/raw, NUNCA em _centralops. Antes isso gerava
    assinatura constante (= descarta tudo); agora não gera assinatura nenhuma."""
    assert suppress_signature(event_labels(_env()), "src_ip") is None


def test_the_exact_key_from_the_incident_yields_no_signature():
    """O placeholder que a UI sugeria: 'src_ip,event_type'. `src_ip` não resolve e
    `event_type` é constante por coletor — juntos identificavam TODO o tráfego da
    integração como um evento só."""
    assert suppress_signature(event_labels(_env()), "src_ip") is None


def test_partial_resolution_is_still_valid():
    """Resolução PARCIAL segue válida — é o caso legítimo 'agrupa os que não têm
    o campo'. Só o caso TOTALMENTE vazio é degenerado."""
    sig = suppress_signature(event_labels(_env()), "src_ip,vendor")
    assert sig is not None and len(sig) == 16


def test_valid_keys_produce_distinct_signatures():
    a = suppress_signature(event_labels(_env(severity_id=1)), "vendor,severity_id")
    b = suppress_signature(event_labels(_env(severity_id=5)), "vendor,severity_id")
    assert a is not None and b is not None and a != b


def test_same_labels_produce_the_same_signature():
    a = suppress_signature(event_labels(_env(event_id="x")), "vendor,severity_id")
    b = suppress_signature(event_labels(_env(event_id="y")), "vendor,severity_id")
    assert a == b  # event_id fora da chave → mesma assinatura (dedup funcionando)


def test_empty_key_yields_no_signature():
    assert suppress_signature(event_labels(_env()), "") is None
    assert suppress_signature(event_labels(_env()), " , ") is None


def test_label_present_but_empty_string_is_degenerate():
    env = _env(vendor="")
    assert suppress_signature(event_labels(env), "vendor") is None


# ── guard de protect_detection no pré-filtro ─────────────────────────────────

class _Route:
    def __init__(self, rid, key, allow, protect):
        self.id, self.suppress_key = rid, key
        self.suppress_allow, self.protect_detection = allow, protect


def _prefilter(routes):
    """Espelha o pré-filtro de pipeline.py (_suppress_routes)."""
    return [
        r for r in routes
        if getattr(r, "suppress_key", None)
        and int(getattr(r, "suppress_allow", 0) or 0) > 0
        and not getattr(r, "protect_detection", True)
    ]


def test_protected_route_is_excluded_from_suppression():
    """O bug relatado: 'Proteger detecção' ligado E suppress_key cadastrado, e a
    supressão continuava rodando."""
    protegida = _Route("r1", "vendor", 2, True)
    assert _prefilter([protegida]) == []


def test_unprotected_route_with_key_is_included():
    aberta = _Route("r2", "vendor", 2, False)
    assert _prefilter([aberta]) == [aberta]


def test_route_without_key_is_excluded_even_if_unprotected():
    """Caso real em produção: suppress_allow=2 ficou órfão com suppress_key=null.
    Sem chave não há supressão — mas o valor gravado é uma bomba armada."""
    orfa = _Route("r3", None, 2, False)
    assert _prefilter([orfa]) == []


def test_missing_attribute_defaults_to_protected():
    """Rota antiga sem a coluna → assume PROTEGIDA (fail-safe), nunca suprime."""
    class _Legacy:
        id, suppress_key, suppress_allow = "r4", "vendor", 2

    assert _prefilter([_Legacy()]) == []


# ── validação de suppress_key no CRUD ────────────────────────────────────────
#
# ASSIMETRIA CORRIGIDA: uma CONDIÇÃO com {"src_ip": ...} sempre devolveu 422
# (ALLOWED_FIELDS), mas o MESMO campo num suppress_key era aceito em silêncio —
# e virava descarte de 100% do tráfego. Agora os dois usam a mesma allowlist.

def test_validate_rejects_field_outside_the_allowlist():
    from backend.app.collectors.routing import validate_suppress_key

    with pytest.raises(ValueError, match="unknown suppress_key field"):
        validate_suppress_key("src_ip")


def test_validate_rejects_unique_per_event_labels():
    """O extremo oposto da assinatura degenerada: agrupar por event_id gera uma
    assinatura por evento e a supressão NUNCA dispara. Também é erro."""
    from backend.app.collectors.routing import validate_suppress_key

    for key in ("event_id", "collected_at", "vendor,event_id"):
        with pytest.raises(ValueError, match="unique per event"):
            validate_suppress_key(key)


def test_validate_accepts_grouping_labels():
    from backend.app.collectors.routing import validate_suppress_key

    validate_suppress_key("vendor,severity_id")
    validate_suppress_key("platform")
    validate_suppress_key("organization_id,event_type")


def test_validate_accepts_none_and_empty_as_suppression_off():
    from backend.app.collectors.routing import validate_suppress_key

    validate_suppress_key(None)
    validate_suppress_key("")
    validate_suppress_key(" , ")


def test_validate_accepts_the_org_id_alias():
    """`org_id` é alias canônico de organization_id nas condições — o mesmo vale aqui."""
    from backend.app.collectors.routing import validate_suppress_key

    validate_suppress_key("org_id")
