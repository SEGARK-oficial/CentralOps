"""Detector de drift — flatten_paths e compute_unknown_paths.

Os tests de persistência (record_unknown_fields tocando DB) são
cobertos no test integration via seed/migrations.
"""

from __future__ import annotations

import pytest

import backend.app.collectors.normalize.drift as drift_mod
from backend.app.collectors.normalize.drift import (
    compute_unknown_paths,
    flatten_paths,
    should_capture,
)


def test_flatten_paths_simple_dict() -> None:
    paths = dict(flatten_paths({"a": 1, "b": "x"}))
    assert paths == {"a": 1, "b": "x"}


def test_flatten_paths_nested_dict() -> None:
    paths = dict(flatten_paths({"a": {"b": {"c": 42}}}))
    assert paths == {"a.b.c": 42}


def test_flatten_paths_with_list_samples() -> None:
    paths = dict(flatten_paths({"items": [{"id": "x"}, {"id": "y"}, {"id": "z"}, {"id": "w"}]}))
    # _MAX_LIST_SAMPLES = 3 → só primeiros 3 índices.
    assert "items[0].id" in paths
    assert "items[1].id" in paths
    assert "items[2].id" in paths
    assert "items[3].id" not in paths


def test_flatten_paths_empty_dict() -> None:
    paths = flatten_paths({})
    # Dict vazio aparece como folha (com placeholder ``{}``) — sample_value útil.
    assert len(paths) == 1
    assert paths[0][0] == "{}"


def test_flatten_paths_empty_list() -> None:
    paths = dict(flatten_paths({"tags": []}))
    assert "tags[]" in paths


def test_compute_unknown_paths_excludes_consumed_top_keys() -> None:
    raw = {
        "id": "a",
        "severity": "high",
        "createdAt": "2026-04-23T10:00:00Z",
        "extra_field_1": "yes",
        "nested": {"deep": "value"},
    }
    consumed = {
        "normalized.finding_info.uid",  # vem de "id"
        "normalized.severity_id",       # vem de "severity"
        "normalized.time",               # vem de "createdAt"
    }
    # NOTA: a heurística atual filtra pelo TOP-key do path. Como
    # consumed_paths são targets (com prefixo "normalized."), a função
    # remove o prefixo e usa o segmento seguinte como sinal de "consumido".
    # Aqui consumed_top_keys = {"finding_info", "severity_id", "time"} —
    # NENHUM bate com top-keys do raw, então TODOS os campos viram
    # "unknown". O cenário realista: a engine vai evoluir para anotar
    # source paths (e.g. ``severity``) em vez de targets.

    unknowns = compute_unknown_paths(raw, consumed)
    # Pelo menos os campos extras DEVEM estar presentes.
    paths = {p for p, _ in unknowns}
    assert "extra_field_1" in paths
    assert "nested.deep" in paths


def test_compute_unknown_paths_treats_lookalike_target_as_consumed() -> None:
    # Caso onde a target é ``normalized.severity_id`` mas o raw tem
    # diretamente uma chave ``severity_id`` no topo: a heurística
    # atualmente trata o segmento como consumido. Versões futuras podem
    # refinar — esse teste documenta o comportamento corrente.
    raw = {"severity_id": 4, "extra": "x"}
    consumed = {"normalized.severity_id"}
    unknowns = compute_unknown_paths(raw, consumed)
    paths = {p for p, _ in unknowns}
    # ``severity_id`` filtrado; ``extra`` segue como unknown.
    assert "extra" in paths
    assert "severity_id" not in paths


def test_compute_unknown_skips_underscore_namespace() -> None:
    """Campos do namespace ``_`` (preprocess virtual) nunca viram unknown.

    ``source:_processed.x`` e ``_processed.x`` são paths virtuais
    produzidos por ops preprocess. Não existem no raw — o
    detector de drift deve ignorá-los silenciosamente. Apenas
    ``other_field``, que está no raw mas não consumido, deve aparecer.
    """
    raw = {
        "processedData": "some-value",
        "other_field": "unrelated",
    }
    consumed = {
        "source:processedData",    # raw path legítimo — top-key consumida
        "source:_processed.x",     # virtual preprocess via source: prefix
        "_processed.x",            # virtual preprocess, literal
    }
    unknowns = compute_unknown_paths(raw, consumed)
    paths = {p for p, _ in unknowns}

    # ``other_field`` não foi consumido → deve aparecer como unknown.
    assert "other_field" in paths
    # ``processedData`` foi consumido via "source:processedData" → não é unknown.
    assert "processedData" not in paths
    # Nenhum path com prefixo ``_`` deve vazar como unknown.
    assert not any(p.startswith("_") for p in paths)


def test_compute_unknown_with_dsl_version_2() -> None:
    """Passar dsl_version=2 não altera o comportamento atual (compat forward).

    O parâmetro é reservado para lógica v2-específica. Hoje
    o resultado deve ser idêntico ao obtido com o default dsl_version=1.
    """
    raw = {
        "alert": {"severity": "high"},
        "unmapped_field": "value",
    }
    consumed = {"source:alert"}

    result_v1 = compute_unknown_paths(raw, consumed, dsl_version=1)
    result_v2 = compute_unknown_paths(raw, consumed, dsl_version=2)

    paths_v1 = {p for p, _ in result_v1}
    paths_v2 = {p for p, _ in result_v2}

    # Comportamento idêntico independente da versão da DSL por enquanto.
    assert paths_v1 == paths_v2
    # ``unmapped_field`` não consumido → unknown em ambas as versões.
    assert "unmapped_field" in paths_v1
    # ``alert.severity`` consumido via top-key "alert" → não é unknown.
    assert "alert.severity" not in paths_v1


# ── Janela de aprendizado (should_capture) ───────────────────────────────────


def test_learning_window_forces_capture_for_new_source() -> None:
    """Fonte NOVA: os 1ºs ``learning_events`` são capturados a 100% mesmo com
    sample_rate=0 (auto-discovery de syslog recém-apontado)."""
    drift_mod._seen_counts.clear()
    # sample_rate=0 → sem a janela, NUNCA capturaria. Com a janela, os 3 primeiros sim.
    caps = [
        should_capture("fortinet_fortigate", "traffic", 1, 0.0, learning_events=3)
        for _ in range(5)
    ]
    assert caps[:3] == [True, True, True]  # janela de aprendizado
    assert caps[3:] == [False, False]      # depois cai na amostragem (rate=0 → nunca)


def test_learning_window_is_per_org_vendor_event() -> None:
    """A janela é por combinação (org, vendor, event_type): orgs/vendors distintos
    têm janelas independentes — um não consome a cota do outro."""
    drift_mod._seen_counts.clear()
    assert should_capture("fortigate", "traffic", 1, 0.0, learning_events=1) is True
    assert should_capture("fortigate", "traffic", 1, 0.0, learning_events=1) is False
    # outra org → janela nova
    assert should_capture("fortigate", "traffic", 2, 0.0, learning_events=1) is True
    # outro vendor, mesma org → janela nova
    assert should_capture("paloalto", "traffic", 1, 0.0, learning_events=1) is True


def test_no_learning_window_falls_back_to_sampling() -> None:
    """learning_events=0 desliga o boost → puramente sample_rate."""
    drift_mod._seen_counts.clear()
    assert should_capture("v", "e", 1, 1.0, learning_events=0) is True   # rate=1 sempre
    assert should_capture("v", "e", 1, 0.0, learning_events=0) is False  # rate=0 nunca


def test_learning_window_respects_key_cap() -> None:
    """Mapa de rastreio cheio → chaves novas não ganham boost (degradação graciosa),
    caindo direto na amostragem sem estourar memória."""
    drift_mod._seen_counts.clear()
    original = drift_mod._MAX_TRACKED_KEYS
    try:
        drift_mod._MAX_TRACKED_KEYS = 1
        assert should_capture("v1", "e", 1, 0.0, learning_events=5) is True   # 1ª chave ok
        # 2ª chave: mapa cheio → sem boost, sample_rate=0 → False
        assert should_capture("v2", "e", 1, 0.0, learning_events=5) is False
    finally:
        drift_mod._MAX_TRACKED_KEYS = original
        drift_mod._seen_counts.clear()


# ── cobertura por PATH (substitui a comparação por chave de topo) ────────────
#
# Causa raiz relatada em produção: campos que existem no evento bruto do Wazuh
# nunca apareciam no Drift Explorer. O detector reduzia todo path consumido à
# sua CHAVE DE TOPO, então consumir `data.win.system.eventID` cegava todo
# `data.*`. Num alerta Wazuh típico isso suprimia 34 de 46 folhas.

def test_nested_sibling_under_a_mapped_key_is_reported() -> None:
    """O caso do usuário: o mapping lê UMA folha sob `data`, e as IRMÃS dela
    passam a ser reportadas em vez de sumirem junto com a top-key."""
    raw = {
        "data": {
            "win": {
                "system": {"eventID": "4624", "channel": "Security"},
                "eventdata": {"logonType": "3"},
            }
        }
    }
    consumed = {"source:data.win.system.eventID"}

    paths = {p for p, _ in compute_unknown_paths(raw, consumed)}

    assert "data.win.system.eventID" not in paths  # consumido
    assert "data.win.system.channel" in paths      # irmã — antes invisível
    assert "data.win.eventdata.logonType" in paths  # tia — antes invisível


def test_whole_subtree_source_still_covers_its_descendants() -> None:
    """`source: "data"` (passthrough da subárvore inteira, como no mapping
    default do Wazuh) continua cobrindo tudo abaixo — senão o passthrough
    inundaria a tela de falso positivo."""
    raw = {"data": {"a": {"b": 1}}, "outro": 2}

    paths = {p for p, _ in compute_unknown_paths(raw, {"source:data"})}

    assert not any(p.startswith("data") for p in paths)
    assert "outro" in paths


def test_prefix_match_does_not_leak_across_sibling_names() -> None:
    """`data.win` não pode cobrir `data.winlog` por prefixo de string."""
    raw = {"data": {"win": {"a": 1}, "winlog": {"b": 2}}}

    paths = {p for p, _ in compute_unknown_paths(raw, {"source:data.win"})}

    assert "data.win.a" not in paths
    assert "data.winlog.b" in paths


def test_quoted_jmespath_identifier_is_unquoted() -> None:
    """Regressão de falso positivo: `timestamp || "@timestamp"` é o source real
    do mapping default do Wazuh. O parser antigo fazia split('.') no texto cru,
    então a top-key virava '"@timestamp"' COM aspas e nunca casava — o campo
    aparecia como desconhecido embora fosse consumido."""
    raw = {"@timestamp": "2026-07-23T10:00:00Z", "outro": 1}

    paths = {p for p, _ in compute_unknown_paths(raw, {'source:timestamp || "@timestamp"'})}

    assert "@timestamp" not in paths
    assert "outro" in paths


def test_target_name_no_longer_masks_a_raw_key_when_sources_exist() -> None:
    """O amplificador: o TARGET OCSF também virava chave conhecida, então criar
    a regra `normalized.message` escondia o campo `message` do raw — e cada
    regra nova aumentava a cegueira. Com source disponível, o target não
    suprime mais nada."""
    raw = {"message": "texto do vendor", "rule": {"level": 5}}
    consumed = {"source:rule.level", "normalized.message", "normalized.severity_id"}

    paths = {p for p, _ in compute_unknown_paths(raw, consumed)}

    assert "message" in paths       # antes: suprimido pelo target homônimo
    assert "rule.level" not in paths


def test_legacy_mapping_without_sources_keeps_top_key_behaviour() -> None:
    """Mapping cujo engine não devolveu nenhum `source:` mantém a supressão
    conservadora — senão um mapping legado reportaria o raw inteiro como drift
    no primeiro deploy."""
    raw = {"severity_id": 4, "extra": "x"}

    paths = {p for p, _ in compute_unknown_paths(raw, {"normalized.severity_id"})}

    assert "severity_id" not in paths
    assert "extra" in paths


def test_array_indices_are_normalized_against_the_consumed_path() -> None:
    raw = {"rule": {"mitre": {"id": ["T1078", "T1110"], "tactic": ["Access"]}}}

    paths = {p for p, _ in compute_unknown_paths(raw, {"source:rule.mitre.id"})}

    assert not any(p.startswith("rule.mitre.id") for p in paths)
    assert "rule.mitre.tactic[0]" in paths


def test_function_call_source_extracts_its_arguments() -> None:
    """`join(', ', tags)` lê `tags`, não uma chave chamada `join`."""
    raw = {"tags": ["a", "b"], "outro": 1}

    paths = {p for p, _ in compute_unknown_paths(raw, {"source:join(', ', tags)"})}

    assert not any(p.startswith("tags") for p in paths)
    assert "outro" in paths


def test_multiselect_source_is_understood() -> None:
    raw = {"type": "alert", "outro": 1}

    paths = {p for p, _ in compute_unknown_paths(raw, {"source:[type]"})}

    assert "type" not in paths
    assert "outro" in paths


def test_pruning_frees_the_per_event_budget_for_unknown_leaves() -> None:
    """O teto _MAX_PATHS_PER_EVENT era gasto enumerando folhas JÁ MAPEADAS: num
    evento gordo (Sysmon via Wazuh) o orçamento acabava antes das chaves novas
    no fim do documento. Com a poda, o teto orça só o que é desconhecido."""
    raw = {
        "data": {f"campo_{i}": i for i in range(drift_mod._MAX_PATHS_PER_EVENT + 100)},
        "cluster": {"node": "worker-1"},  # chave nova, no FIM do documento
    }

    paths = {p for p, _ in compute_unknown_paths(raw, {"source:data"})}

    assert "cluster.node" in paths
    assert len(paths) == 1


def test_walk_has_a_depth_guard() -> None:
    """Sem teto de profundidade, payload cíclico levantava RecursionError num
    ponto FORA do try/except, abortando o ciclo de coleta inteiro."""
    cyclic: dict = {"a": {}}
    node = cyclic["a"]
    for _ in range(200):
        node["a"] = {}
        node = node["a"]
    node["leaf"] = 1

    paths = flatten_paths(cyclic)  # não pode levantar

    assert len(paths) <= drift_mod._MAX_PATHS_PER_EVENT


# ── sample_value: formato em vez do dado do cliente ──────────────────────────
#
# Campo NÃO MAPEADO é onde caem usuário, host, IP, caminho e linha de comando.
# A tabela unknown_fields é lida por perfil VIEWER (DRIFT_READ no /api/drift e
# MAPPING_READ no autocomplete do editor), tem retenção de 90 dias e last_seen
# reescrito a cada ocorrência — campo recorrente nunca expirava.

@pytest.mark.parametrize(
    "value,expected",
    [
        ("10.0.0.9", "<ipv4>"),
        ("svc_backup@corp.local", "<email>"),
        ("a3f1c2d4-1111-2222-3333-444455556666", "<uuid>"),
        ("2026-07-23T10:00:00Z", "<timestamp>"),
        ("C:/Windows/System32/x.dll", "<path_win>"),
        ("/etc/shadow", "<path_posix>"),
        ("00:1A:2B:3C:4D:5E", "<mac>"),
        ("d41d8cd98f00b204e9800998ecf8427e", "<md5>"),
        ("https://evil.example/?token=copsk_abc", "<url>"),
        (4624, "<number>"),
        (True, "<bool>"),
        (None, "<null>"),
        ("svc_backup", "<string len=10>"),
    ],
)
def test_masked_sample_describes_format_without_the_value(value, expected) -> None:
    assert drift_mod.build_sample_value(value, mode="masked") == expected


def test_masked_sample_never_echoes_the_input() -> None:
    for secret in ("svc_backup", "10.0.0.9", "copsk_deadbeef", "diretor@corp.com"):
        assert secret not in (drift_mod.build_sample_value(secret, mode="masked") or "")


def test_sample_mode_none_persists_nothing() -> None:
    assert drift_mod.build_sample_value("svc_backup", mode="none") is None


def test_sample_mode_raw_keeps_the_legacy_behaviour() -> None:
    assert drift_mod.build_sample_value("svc_backup", mode="raw") == "svc_backup"
