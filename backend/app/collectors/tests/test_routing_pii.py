"""Redação de PII por rota integrada ao motor.

A MESMA origem chega ÍNTEGRA no lago e MASCARADA no
SIEM, numa única passada — sem corromper a cópia byte-idêntica do wazuh-default
(hazard de referência compartilhada no fan-out). Cobre também o gate
PII_REDACTION_ENABLED (default ON; byte-idêntico enquanto NENHUMA rota tem spec,
porque a spec por-rota é o sinal de habilitação) e o FAIL-CLOSED, que permanece
para o caso rota-com-spec + flag desligada explicitamente.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import json

import pytest

from backend.app.collectors.routing.engine import CompiledRoute, route_batch
from backend.app.collectors.routing.pii_redaction import compile_pii_redaction


def _ev(email: str = "alice@example.com") -> dict:
    return {
        "_centralops": {"event_id": "e1", "organization_id": 7},
        "raw": {"user": {"email": email}, "src": {"ip": "203.0.113.5"}},
    }


def _mask_email():
    return compile_pii_redaction([{"path": "raw.user.email", "action": "mask"}])


# ── full-to-lake + masked-to-SIEM (uma passada) ──────────


def test_same_event_full_to_lake_masked_to_siem():
    """Rota 'lake' (sem redação, is_final=False) clona íntegro; cai pra rota
    'siem' (redação, is_final=True) que mascara. Uma origem, dois tratamentos."""
    lake = CompiledRoute(
        id="r-lake", name="lake", priority=10, condition={}, action="route",
        destination_ids=("lake-s3",), is_final=False, redaction=(),
    )
    siem = CompiledRoute(
        id="r-siem", name="siem", priority=20, condition={}, action="route",
        destination_ids=("elastic-siem",), is_final=True, redaction=_mask_email(),
    )
    batch = [_ev()]
    result = route_batch(batch, [lake, siem])

    # lago recebe o evento ÍNTEGRO...
    assert result.sub_batches["lake-s3"][0]["raw"]["user"]["email"] == "alice@example.com"
    # ...e o SIEM recebe MASCARADO.
    assert result.sub_batches["elastic-siem"][0]["raw"]["user"]["email"] == "[REDACTED]"
    assert "_centralops_redacted" in result.sub_batches["elastic-siem"][0]


def test_fanout_does_not_corrupt_shared_lake_reference():
    """COPY ISOLATION: o ramo mascarado é um deepcopy; o ramo do lago é a MESMA
    referência do batch de entrada (sem custo, byte-idêntico)."""
    lake = CompiledRoute(
        id="r-lake", name="lake", priority=10, condition={}, action="route",
        destination_ids=("lake-s3",), is_final=False, redaction=(),
    )
    siem = CompiledRoute(
        id="r-siem", name="siem", priority=20, condition={}, action="route",
        destination_ids=("elastic-siem",), is_final=True, redaction=_mask_email(),
    )
    batch = [_ev()]
    result = route_batch(batch, [lake, siem])

    # input intocado
    assert batch[0]["raw"]["user"]["email"] == "alice@example.com"
    # lago = MESMA referência do input (sem deepcopy)
    assert result.sub_batches["lake-s3"][0] is batch[0]
    # SIEM = objeto NOVO (deepcopy mascarado)
    assert result.sub_batches["elastic-siem"][0] is not batch[0]


def test_wazuh_default_branch_stays_byte_identical_when_siblings_redact():
    """Uma rota → wazuh-default (sem redação) + outra → elastic (com redação):
    a cópia do wazuh é a referência original, intacta."""
    waz = CompiledRoute(
        id="r-waz", name="waz", priority=10, condition={}, action="route",
        destination_ids=("wazuh-default",), is_final=False, redaction=(),
    )
    siem = CompiledRoute(
        id="r-siem", name="siem", priority=20, condition={}, action="route",
        destination_ids=("elastic-siem",), is_final=True, redaction=_mask_email(),
    )
    batch = [_ev()]
    result = route_batch(batch, [waz, siem])
    assert result.sub_batches["wazuh-default"][0] is batch[0]
    assert result.sub_batches["wazuh-default"][0]["raw"]["user"]["email"] == "alice@example.com"


def test_no_redaction_route_is_zero_copy():
    """Rota sem spec → referência compartilhada (zero deepcopy)."""
    r = CompiledRoute(
        id="r", name="r", priority=10, condition={}, action="route",
        destination_ids=("d1",), is_final=True, redaction=(),
    )
    batch = [_ev()]
    result = route_batch(batch, [r])
    assert result.sub_batches["d1"][0] is batch[0]


def test_unrouted_is_never_redacted():
    """Evento sem match → unrouted full-fidelity (rede de segurança, NUNCA redigido).
    Vendor-neutro: vai à DLQ (unrouted_events), não a um sink hardcoded."""
    # rota que não casa
    r = CompiledRoute(
        id="r", name="r", priority=10, condition={"severity_id": {"gte": 99}},
        action="route", destination_ids=("d1",), is_final=True, redaction=_mask_email(),
    )
    batch = [_ev()]
    result = route_batch(batch, [r])
    assert "d1" not in result.sub_batches
    assert "wazuh-default" not in result.sub_batches
    assert result.unrouted_events[0] is batch[0]  # original, full-fidelity
    assert result.unrouted == 1


# ── _compile_route_row: gate PII_REDACTION_ENABLED + FAIL-CLOSED ────────


def _row(pii_redaction):
    return SimpleNamespace(
        id="r1", name="r1", priority=100, condition="{}", action="route",
        destination_ids='["d1"]', is_final=True, enabled=True, canary_percent=100,
        pii_redaction=pii_redaction,
    )


def test_flag_off_with_spec_fails_closed(monkeypatch):
    """PII_REDACTION_ENABLED OFF + rota COM spec → FAIL-CLOSED (levanta): não
    entrega cleartext ao destino externo. _load_routes_for_org cai p/ wazuh."""
    from backend.app.collectors import pipeline
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "PII_REDACTION_ENABLED", False)
    row = _row(json.dumps([{"path": "raw.user.email", "action": "mask"}]))
    with pytest.raises(Exception):
        pipeline._compile_route_row(row)


def test_flag_off_no_spec_is_byte_identical(monkeypatch):
    """Estado DEFAULT (rota sem spec) + flag OFF → redaction vazio, byte-idêntico."""
    from backend.app.collectors import pipeline
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "PII_REDACTION_ENABLED", False)
    row = _row(None)
    assert pipeline._compile_route_row(row).redaction == ()


def test_flag_defaults_to_on():
    """Governança de PII é feature CORE, não opt-in de ambiente. O portão que
    importa é o POR-ROTA — a spec na rota é o sinal de habilitação."""
    from backend.app.core.config import Settings

    assert Settings.model_fields["PII_REDACTION_ENABLED"].default is True


def test_default_on_without_spec_is_still_byte_identical(monkeypatch):
    """Ligar o default não muda um byte enquanto nenhuma rota tem spec —
    _compile_route_row só entra na lógica quando pii_redaction é truthy."""
    from backend.app.collectors import pipeline
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "PII_REDACTION_ENABLED", True)
    assert pipeline._compile_route_row(_row(None)).redaction == ()


def test_default_on_with_spec_compiles_instead_of_failing_closed(monkeypatch):
    """Com o default ON, a rota que TEM spec passa a redigir de verdade — antes
    ela derrubava o carregamento e o tráfego inteiro ia pro default interno."""
    from backend.app.collectors import pipeline
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "PII_REDACTION_ENABLED", True)
    row = _row(json.dumps([{"path": "raw.user.email", "action": "mask"}]))
    assert pipeline._compile_route_row(row).redaction != ()


def test_dedup_same_dest_two_routes_redacting_wins():
    """Review HIGH: dois roteiam o MESMO destino (uma redige, outra não) → 1 só
    cópia, e a versão REDIGIDA vence (nunca cleartext + mascarada juntas)."""
    full = CompiledRoute(
        id="r-full", name="full", priority=10, condition={}, action="route",
        destination_ids=("siem",), is_final=False, redaction=(),
    )
    masked = CompiledRoute(
        id="r-mask", name="mask", priority=20, condition={}, action="route",
        destination_ids=("siem",), is_final=True, redaction=_mask_email(),
    )
    batch = [_ev()]
    result = route_batch(batch, [full, masked])
    # UMA cópia em 'siem', e MASCARADA (a redação vence a irmã cleartext).
    assert len(result.sub_batches["siem"]) == 1
    assert result.sub_batches["siem"][0]["raw"]["user"]["email"] == "[REDACTED]"


def test_dedup_duplicate_destination_ids_single_copy():
    """destination_ids=['d1','d1'] → 1 cópia (sem double-send)."""
    r = CompiledRoute(
        id="r", name="r", priority=10, condition={}, action="route",
        destination_ids=("d1", "d1"), is_final=True, redaction=(),
    )
    result = route_batch([_ev()], [r])
    assert len(result.sub_batches["d1"]) == 1


def test_flag_on_compiles_spec(monkeypatch):
    from backend.app.collectors import pipeline
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "PII_REDACTION_ENABLED", True)
    row = _row(json.dumps([{"path": "raw.user.email", "action": "mask"}]))
    compiled = pipeline._compile_route_row(row)
    assert len(compiled.redaction) == 1
    assert compiled.redaction[0].action == "mask"


def test_fail_closed_corrupt_spec_propagates(monkeypatch):
    """FAIL-CLOSED: spec corrompida em runtime (flag ON) PROPAGA — NÃO degrada
    para () (fail-open = PII em claro). _load_routes_for_org captura → tudo cai
    no wazuh-default interno."""
    from backend.app.collectors import pipeline
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "PII_REDACTION_ENABLED", True)
    # path fora da allowlist → compile levanta PiiRedactionError
    row = _row(json.dumps([{"path": "_centralops.event_id", "action": "drop_field"}]))
    with pytest.raises(Exception):
        pipeline._compile_route_row(row)


def test_load_routes_fail_closed_falls_back(monkeypatch):
    """_load_routes_for_org engole a exceção do compile e retorna [] → o ciclo
    manda tudo p/ wazuh-default (zero perda, sem vazamento externo)."""
    from backend.app.collectors import pipeline
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "PII_REDACTION_ENABLED", True)

    class _BadSession:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    # Simula uma rota com spec inválida vindo do repо.
    bad_row = _row(json.dumps([{"path": "evil", "action": "mask"}]))

    class _Repo:
        def __init__(self, *_a, **_k): pass
        def list_enabled_for_org(self, *_a, **_k): return [bad_row]

    monkeypatch.setattr(pipeline.database, "SessionLocal", lambda: _BadSession())
    monkeypatch.setattr("backend.app.db.repository.RouteRepository", _Repo)

    routes = pipeline._load_routes_for_org(7)
    assert routes == []  # fail-closed → sem rotas → fallback p/ wazuh-default
