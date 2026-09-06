"""ADR-0012 — o que a própria CentralOps produz passa pelo gate estrutural.

O hook de validação OCSF vive no laço de coleta e só vê o que um coletor
normalizou. O 1006/2004 da scheduled query, o 2004 da detecção em voo,
backfill e reprocesso entram por ``_enqueue_dispatch`` e nunca eram validados:
um Detection Finding montado com ``severity_id`` fora do enum chegava ao SIEM
do cliente como se fosse dado.

O gate de produtor interno (``pipeline._gate_internal_producers``) é
FAIL-CLOSED para ``_centralops.vendor == "centralops"`` e não toca evento de
vendor. Cada teste negativo aqui tem o positivo ao lado — um gate que "não
deixou passar" por vacuidade não prova nada.
"""

from __future__ import annotations

import logging
import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from backend.app.collectors import pipeline, scheduler_tasks
from backend.app.collectors.normalize.envelope import EnvelopeContext, build_envelope
from backend.app.collectors.normalize.ocsf.query_rows import build_finding_normalized
from backend.app.core.config import settings

_ROW = {"Hostname": "HOST-0001", "Process Name": "anydesk.exe", "Event Count": 1}


def _internal_2004(*, severity_id: int = 4, class_uid: int | None = None, vendor: str = "centralops"):
    normalized = build_finding_normalized(
        rows=[_ROW],
        finding_uid="sched:1:integ:1:1",
        title="Hunt",
        description=None,
        severity_id=severity_id,
        query_id=1,
        occurred_ms=1787252507558,
    )
    if class_uid is not None:
        normalized["class_uid"] = class_uid
        normalized["category_uid"] = class_uid // 1000
        normalized["type_uid"] = class_uid * 100 + normalized["activity_id"]
    ctx = EnvelopeContext(
        vendor=vendor,
        integration_id=1,
        customer_id=1,
        stream="scheduled_query",
        event_type="centralops.scheduled_query.finding",
        mapping_version_id=None,
        organization_id=1,
    )
    return build_envelope({}, normalized, ctx, vendor_msg_id="evt-1")


@pytest.fixture()
def routed(monkeypatch):
    """Captura o que chega ao roteamento e zera as métricas do gate."""
    captured: list[dict] = []
    monkeypatch.setattr(pipeline, "_enqueue_routed", lambda batch, routes: captured.extend(batch))
    monkeypatch.setattr(pipeline, "OCSF_VALID", MagicMock())
    monkeypatch.setattr(pipeline, "OCSF_INVALID", MagicMock())
    monkeypatch.setattr(settings, "OCSF_VALIDATE_INTERNAL_PRODUCERS", True)
    return captured


def test_valid_internal_event_passes_and_is_tagged(routed) -> None:
    envelope = _internal_2004()
    pipeline._enqueue_dispatch([envelope], routes=[])

    assert routed == [envelope]
    assert envelope["_centralops"]["ocsf_valid"] is True
    assert pipeline.OCSF_VALID.labels.return_value.inc.call_count == 1
    assert pipeline.OCSF_INVALID.labels.call_count == 0


def test_invalid_internal_event_is_dropped_counted_and_logged(routed, caplog) -> None:
    envelope = _internal_2004(severity_id=42)
    with caplog.at_level(logging.ERROR, logger="backend.app.collectors.pipeline"):
        pipeline._enqueue_dispatch([envelope], routes=[])

    assert routed == []
    pipeline.OCSF_INVALID.labels.assert_called_once_with(
        vendor="centralops",
        event_type="centralops.scheduled_query.finding",
        reason="bad_severity_id",
    )
    assert pipeline.OCSF_INVALID.labels.return_value.inc.call_count == 1
    assert any("INTERNO inválido" in r.getMessage() for r in caplog.records)
    # O log leva o id, nunca o payload.
    assert any("evt-1" in r.getMessage() for r in caplog.records)
    assert not any("HOST-0001" in r.getMessage() for r in caplog.records)


def test_only_the_invalid_one_is_dropped_from_a_mixed_batch(routed) -> None:
    good = _internal_2004()
    bad = _internal_2004(severity_id=42)
    pipeline._enqueue_dispatch([good, bad, good], routes=[])
    assert routed == [good, good]


def test_vendor_events_are_not_gated_here(routed) -> None:
    # O laço de coleta é quem valida evento de vendor (com a política da org).
    # Este gate não pode nem contar, nem descartar, nem etiquetar o que não é
    # produção interna.
    vendor_bad = _internal_2004(severity_id=42, vendor="sophos")
    pipeline._enqueue_dispatch([vendor_bad], routes=[])

    assert routed == [vendor_bad]
    assert "ocsf_valid" not in vendor_bad["_centralops"]
    assert pipeline.OCSF_INVALID.labels.call_count == 0
    assert pipeline.OCSF_VALID.labels.call_count == 0


def test_out_of_scope_class_passes_but_is_counted(routed) -> None:
    # Classe OCSF válida que o manifesto não vendoriza: o gate não tem como
    # julgar, então passa etiquetado — e conta, para a taxa de conformidade.
    envelope = _internal_2004(class_uid=5001)
    pipeline._enqueue_dispatch([envelope], routes=[])

    assert routed == [envelope]
    assert envelope["_centralops"]["ocsf_valid"] is False
    assert pipeline.OCSF_INVALID.labels.return_value.inc.call_count == 1


def test_flag_off_lets_the_invalid_event_through(routed, monkeypatch) -> None:
    monkeypatch.setattr(settings, "OCSF_VALIDATE_INTERNAL_PRODUCERS", False)
    bad = _internal_2004(severity_id=42)
    pipeline._enqueue_dispatch([bad], routes=[])
    assert routed == [bad]
    assert pipeline.OCSF_INVALID.labels.call_count == 0


def test_a_crashing_validator_does_not_silence_the_detection(routed, caplog) -> None:
    good = _internal_2004()
    with patch.object(pipeline.ocsf_validator, "structural_gate", side_effect=RuntimeError("boom")):
        with caplog.at_level(logging.ERROR, logger="backend.app.collectors.pipeline"):
            pipeline._enqueue_dispatch([good], routes=[])
    assert routed == [good]
    assert any("passa sem veredito" in r.getMessage() for r in caplog.records)


def test_the_real_scheduled_query_producers_pass_the_gate(routed) -> None:
    """Prova positiva: tudo que ``scheduler_tasks`` monta atravessa o gate."""
    integration = SimpleNamespace(
        id=9, name="EDR", platform="sophos", organization_id=8, data_geography="US",
        organization=SimpleNamespace(id=8, name="Org", slug="org"),
    )
    with patch("backend.app.collectors.pipeline._enqueue_dispatch") as enqueue:
        batch = scheduler_tasks._dispatch_scheduled_query_alert(
            integration=integration,
            sched=SimpleNamespace(id=2),
            query_def=SimpleNamespace(id=1, title="Hunt", description="d", statement="SELECT 1", table="t"),
            items=[_ROW, {**_ROW, "Hostname": "HOST-0002"}],
            from_ts="2026-08-19T19:01:40Z",
            to_ts="2026-08-20T19:01:40Z",
            record=SimpleNamespace(id=539, language="xdr_data_lake", ocsf_mapping_version="1"),
        )
        enqueue.assert_called_once()

    pipeline._enqueue_dispatch(batch, routes=[])
    assert len(batch) == 4  # 1006 + 2004-resumo + 2 × 2004 por linha
    assert routed == batch
    assert pipeline.OCSF_INVALID.labels.call_count == 0
    assert pipeline.OCSF_VALID.labels.return_value.inc.call_count == 4
