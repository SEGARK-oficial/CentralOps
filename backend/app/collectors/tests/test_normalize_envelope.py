"""Construção do envelope canônico {_centralops, normalized, raw}."""

from __future__ import annotations

import re

from backend.app.collectors.normalize import (
    ENVELOPE_SCHEMA_VERSION,
    OCSF_VERSION,
)
from backend.app.collectors.normalize.envelope import (
    EnvelopeContext,
    build_envelope,
    compute_event_id,
    has_customer_id,
)


def _ctx(
    integration_id: int = 42,
    customer_id: int = 7,
    customer_name: str = "test-org",
    organization_id: int = 99,
) -> EnvelopeContext:
    return EnvelopeContext(
        vendor="sophos",
        integration_id=integration_id,
        customer_id=customer_id,
        customer_name=customer_name,
        stream="alerts",
        event_type="sophos.alert",
        mapping_version_id="ver-1",
        organization_id=organization_id,
    )


def test_envelope_has_three_top_level_blocks() -> None:
    raw = {"id": "alert-1", "severity": "high"}
    normalized = {"normalized": {"class_uid": 2004, "severity_id": 4}}
    env = build_envelope(raw, normalized, _ctx())
    assert set(env.keys()) == {"_centralops", "normalized", "raw"}


def test_envelope_centralops_metadata_complete() -> None:
    env = build_envelope({"id": "x"}, {"normalized": {}}, _ctx())
    meta = env["_centralops"]
    assert meta["schema_version"] == ENVELOPE_SCHEMA_VERSION
    assert meta["ocsf_version"] == OCSF_VERSION
    assert meta["vendor"] == "sophos"
    assert meta["integration_id"] == 42
    assert meta["customer_id"] == 7
    assert meta["customer_name"] == "test-org"
    assert meta["stream"] == "alerts"
    assert meta["event_type"] == "sophos.alert"
    assert meta["mapping_version_id"] == "ver-1"
    assert meta["collector_host"]  # truthy
    assert meta["event_id"]
    assert meta["collected_at"].endswith("Z")
    # labels de roteamento/isolamento de 1ª classe.
    assert meta["organization_id"] == 99
    assert "severity_id" in meta


def test_envelope_emits_platform_label() -> None:
    """``platform`` is a first-class routing label."""
    ctx = EnvelopeContext(
        vendor="sophos", integration_id=1, customer_id=7,
        stream="alerts", event_type="sophos.alert", mapping_version_id="v1",
        platform="microsoft_defender",
    )
    env = build_envelope({"id": "x"}, {"normalized": {}}, ctx)
    assert env["_centralops"]["platform"] == "microsoft_defender"


def test_envelope_platform_defaults_to_vendor() -> None:
    """When ``platform`` is not set explicitly it mirrors ``vendor`` (the pipeline
    derives both from ``Integration.platform``) — adds the label without breaking
    any existing call-site."""
    env = build_envelope({"id": "x"}, {"normalized": {}}, _ctx())
    assert env["_centralops"]["platform"] == "sophos"
    assert env["_centralops"]["vendor"] == "sophos"


def test_envelope_schema_version_is_1_1_0() -> None:
    # Trava o bump (1.0.0 → 1.1.0). Mudança intencional de golden.
    assert ENVELOPE_SCHEMA_VERSION == "1.1.0"


def test_envelope_exposes_severity_id_mirroring_normalized() -> None:
    env = build_envelope(
        {"id": "x"}, {"normalized": {"class_uid": 2004, "severity_id": 5}}, _ctx()
    )
    # severity_id em _centralops espelha normalized.severity_id (mesma fonte).
    assert env["_centralops"]["severity_id"] == 5
    assert env["normalized"]["severity_id"] == 5


def test_envelope_severity_id_none_when_absent() -> None:
    env = build_envelope({"id": "x"}, {"normalized": {}}, _ctx())
    assert env["_centralops"]["severity_id"] is None


def test_envelope_organization_id_defaults_none_when_unset() -> None:
    ctx = EnvelopeContext(
        vendor="sophos", integration_id=1, customer_id=7,
        stream="alerts", event_type="sophos.alert", mapping_version_id="v1",
    )
    env = build_envelope({"id": "x"}, {"normalized": {}}, ctx)
    assert env["_centralops"]["organization_id"] is None


def test_envelope_uses_vendor_msg_id_when_provided() -> None:
    env = build_envelope({"id": "x"}, {"normalized": {}}, _ctx(), vendor_msg_id="vendor-evt-42")
    assert env["_centralops"]["event_id"] == "vendor-evt-42"


def test_envelope_falls_back_to_sha256_event_id() -> None:
    env = build_envelope({"id": "x"}, {"normalized": {}}, _ctx())
    assert re.match(r"^sha256:[0-9a-f]{64}$", env["_centralops"]["event_id"])


def test_envelope_raw_is_preserved_intact() -> None:
    raw = {"id": "x", "nested": {"foo": "bar"}, "list": [1, 2, 3]}
    env = build_envelope(raw, {"normalized": {}}, _ctx())
    assert env["raw"] == raw


def test_envelope_raw_does_not_alias_input() -> None:
    # ``raw`` no envelope deve ser independente — mutação no input
    # original não deve afetar o envelope.
    raw = {"id": "x"}
    env = build_envelope(raw, {"normalized": {}}, _ctx())
    raw["id"] = "MUTATED"
    assert env["raw"]["id"] == "x"


def test_envelope_accepts_normalized_block_without_wrapper_key() -> None:
    # A engine produz ``{"normalized": {...}}``; o builder também aceita
    # se quem chama já passar o conteúdo "achatado".
    env_a = build_envelope({}, {"normalized": {"class_uid": 2004}}, _ctx())
    env_b = build_envelope({}, {"class_uid": 2004}, _ctx())
    assert env_a["normalized"] == env_b["normalized"] == {"class_uid": 2004}


def test_compute_event_id_stable_for_same_payload() -> None:
    a = compute_event_id({"id": "x", "y": 1}, None)
    b = compute_event_id({"y": 1, "id": "x"}, None)  # ordem distinta
    assert a == b  # determinístico independente de ordem de keys


def test_has_customer_id_true_for_normal_envelope() -> None:
    env = build_envelope({}, {"normalized": {}}, _ctx())
    assert has_customer_id(env) is True


def test_has_customer_id_false_for_zero_or_missing() -> None:
    bad = {"_centralops": {"customer_id": None}}
    assert has_customer_id(bad) is False
    bad = {"_centralops": {}}
    assert has_customer_id(bad) is False
    bad = {"_centralops": {"customer_id": ""}}
    assert has_customer_id(bad) is False


def test_envelopes_for_different_tenants_are_isolated() -> None:
    e1 = build_envelope({"id": "x"}, {"normalized": {}}, _ctx(integration_id=10, customer_id=100))
    e2 = build_envelope({"id": "x"}, {"normalized": {}}, _ctx(integration_id=20, customer_id=200))
    assert e1["_centralops"]["customer_id"] == 100
    assert e2["_centralops"]["customer_id"] == 200
    assert e1["_centralops"]["integration_id"] == 10
    assert e2["_centralops"]["integration_id"] == 20


def test_envelope_customer_name_optional() -> None:
    """customer_name=None ainda produz envelope válido."""
    ctx = _ctx(customer_name=None)
    env = build_envelope({"foo": 1}, {"bar": 2}, ctx)
    assert env["_centralops"]["customer_name"] is None


# ── Testes de isolamento: copy-on-write sem deepcopy ────────────


def test_mutation_of_envelope_normalized_does_not_alias_source() -> None:
    """Adicionar chave em envelope['normalized'] NÃO afeta o source original.

    Garante que a troca de deepcopy → dict() (cópia rasa de 1º nível) mantém
    isolamento de chaves top-level.
    """
    source = {"class_uid": 2004, "severity_id": 4, "extra": "keep"}
    env = build_envelope({"id": "x"}, {"normalized": source}, _ctx())
    # Mutação: adição de chave top-level no normalized do envelope.
    env["normalized"]["injected"] = "hacked"
    assert "injected" not in source, "mutação no envelope não deve vazar para o source"


def test_mutation_of_envelope_raw_does_not_alias_source_top_level() -> None:
    """Adicionar/remover chave top-level em envelope['raw'] NÃO afeta raw original.

    O docstring de envelope.py já promete 'cópia rasa do dict original'.
    """
    raw = {"id": "x", "severity": "high"}
    env = build_envelope(raw, {"normalized": {}}, _ctx())
    # Mutação top-level no raw do envelope.
    env["raw"]["new_key"] = "injected"
    assert "new_key" not in raw, "mutação top-level no envelope.raw não deve afetar o raw original"


def test_two_envelopes_from_same_source_have_independent_normalized_blocks() -> None:
    """Dois envelopes construídos com o mesmo source têm blocos isolated.

    Caso de uso: dry-run/shadow onde o mesmo ApplyResult alimenta dois tenants.
    """
    shared_source = {"normalized": {"class_uid": 2004}}
    e1 = build_envelope({"id": "x"}, shared_source, _ctx(customer_id=100))
    e2 = build_envelope({"id": "x"}, shared_source, _ctx(customer_id=200))
    # Mutação em e1 não afeta e2.
    e1["normalized"]["class_uid"] = 9999
    assert e2["normalized"]["class_uid"] == 2004, "envelopes de tenants distintos devem ser isolados"
