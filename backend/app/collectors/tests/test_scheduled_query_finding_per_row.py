"""O achado de uma scheduled query sobrevive a um destino que ACHATA JSON.

O 2004-resumo põe a tabela em ``normalized.evidences[]`` — e o analysisd do
Wazuh entrega um array de objetos como UMA string: nenhuma regra alcança
``evidences[0].device.hostname``, o IRIS não ganha asset, o painel mostra um
blob. Medido com o decoder real (``armory_trace_log``): 51 campos extraídos e
``normalized.evidences`` inteiro num campo só.

A forma ``per_row`` emite um 2004 POR LINHA com ``device``/``actor``/
``process`` no nível da classe (a 2004 admite os três), onde viram campos
planos. Junto vêm três coisas que o contrato antigo não tinha:

* **severidade única** — a da query, a mesma na Detection, no 1006 e no 2004
  (antes: Detection 4, eventos 5 fixo);
* **dedup por CONTEÚDO** — ``dedup_key`` por linha (fingerprint da entidade),
  então a linha repetida no run seguinte BUMPA a Detection e sai como Update,
  e a linha nova sai como Create (antes: uma Detection nova por run, ``count``
  sempre 1, oito iguais por dia);
* **teto declarado** — acima de ``QUERY_FINDING_MAX_ROWS_PER_RUN`` o excedente
  é dito no evento (``rows_over_cap``), nunca descartado em silêncio.

Os nomes de coluna são os de uma hunt de ``process_activity`` real; os VALORES
são sintéticos.
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.collectors import scheduler_tasks
from backend.app.collectors.normalize.ocsf import get_registry, structural_gate
from backend.app.collectors.normalize.ocsf.query_rows import (
    build_row_finding_normalized,
    cap_rows_by_bytes,
    evidence_fingerprint,
    row_to_evidence,
)
from backend.app.core.config import settings
from backend.app.db import models
from backend.app.db.models import Base

_ROW_A = {
    "Endpoint ID": "11111111-2222-3333-4444-555555555555",
    "Hostname": "HOST-0001",
    "Public IP": "203.0.113.10",
    "OS": "Windows 10 Pro 22H2",
    "OS Platform": "windows",
    "Username": "svc_test",
    "Process Name": "anydesk.exe",
    "Process Path": "C:\\Program Files\\Test\\anydesk.exe",
    "SHA256": "a" * 64,
    "Cmdline (Any)": "anydesk.exe --start-service",
    "Parent Name (Any)": "explorer.exe",
    "First Seen": "2026-08-19T20:31:23Z",
    "Last Seen": "2026-08-19T21:04:12Z",
    "Event Count": 7,
    "Match Reason": "PROC_NAME_IOC | C2_DOMAIN_IN_CMDLINE",
}
_ROW_B = {
    "Endpoint ID": "99999999-8888-7777-6666-555555555555",
    "Hostname": "SRV-0002",
    "Username": "svc_backup",
    "Process Name": "nltest.exe",
    "Cmdline (Any)": "nltest /dclist:lab.local",
    "Event Count": 2,
    "Match Reason": "DOMAIN_CONTROLLER_LIST_NLTEST",
}


def _fp(row):
    evidence, _ = row_to_evidence(row)
    return evidence_fingerprint(evidence, row)


# ── builder por linha ─────────────────────────────────────────────────


class TestRowFindingBuilder:
    def _build(self, row, **kw):
        params = dict(
            row=row,
            finding_uid_base="sched:2:integ:9:539",
            title="Hunt de teste",
            description="descricao",
            severity_id=4,
            query_id=1,
            occurred_ms=1787252507558,
            row_index=0,
            rows_total=1,
            rows_emitted=1,
        )
        params.update(kw)
        return build_row_finding_normalized(**params)

    def test_entity_objects_are_promoted_to_class_level(self) -> None:
        # O ponto do módulo: num destino que achata JSON, só campo de primeiro
        # nível vira campo indexável.
        normalized, _ = self._build(_ROW_A)
        assert normalized["device"]["hostname"] == "HOST-0001"
        assert normalized["device"]["uid"] == _ROW_A["Endpoint ID"]
        assert normalized["actor"]["user"]["name"] == "svc_test"
        assert normalized["process"]["name"] == "anydesk.exe"
        assert normalized["process"]["file"]["hashes"][0]["value"] == "a" * 64
        # ... e a evidência continua onde a spec manda, para o consumidor OCSF.
        assert normalized["evidences"][0]["device"] == normalized["device"]
        assert len(normalized["evidences"]) == 1
        assert normalized["count"] == 1

    def test_uid_carries_the_row_identity(self) -> None:
        normalized, fingerprint = self._build(_ROW_A)
        assert normalized["finding_info"]["uid"] == f"sched:2:integ:9:539:{fingerprint}"
        assert normalized["unmapped"]["row_fingerprint"] == fingerprint
        assert normalized["finding_info"]["types"] == ["PROC_NAME_IOC", "C2_DOMAIN_IN_CMDLINE"]
        assert normalized["unmapped"]["events_total"] == 7

    def test_cap_is_declared_in_every_row_event(self) -> None:
        normalized, _ = self._build(_ROW_A, rows_total=500, rows_emitted=200)
        assert normalized["unmapped"]["rows_total"] == 500
        assert normalized["unmapped"]["rows_emitted"] == 200
        assert normalized["unmapped"]["rows_over_cap"] == 300

    def test_activity_id_comes_from_the_caller(self) -> None:
        # Só o banco sabe se a linha é nova; o builder obedece.
        created, _ = self._build(_ROW_A, activity_id=1)
        updated, _ = self._build(_ROW_A, activity_id=2)
        assert created["type_uid"] == 200401
        assert updated["type_uid"] == 200402

    def test_passes_the_structural_gate(self) -> None:
        for row in (_ROW_A, _ROW_B):
            normalized, _ = self._build(row)
            verdict = structural_gate(normalized, get_registry("1.8.0"))
            assert verdict.valid, (verdict.reason, verdict.missing_required)
            assert not verdict.missing_required


# ── identidade da linha ───────────────────────────────────────────────


class TestFingerprint:
    def test_same_row_same_digest(self) -> None:
        assert _fp(_ROW_A) == _fp(dict(_ROW_A))

    def test_distinct_entities_distinct_digests(self) -> None:
        assert _fp(_ROW_A) != _fp(_ROW_B)
        assert _fp(_ROW_A) != _fp({**_ROW_A, "Hostname": "HOST-0002"})
        assert _fp(_ROW_A) != _fp({**_ROW_A, "Process Name": "rustdesk.exe"})

    def test_run_aggregates_do_not_change_identity(self) -> None:
        # O mesmo processo na mesma máquina é o MESMO achado no run seguinte,
        # mesmo que ``Last Seen`` tenha andado e ``ANY_VALUE(cmdline)`` tenha
        # devolvido outra amostra.
        moved = {
            **_ROW_A,
            "Last Seen": "2026-08-20T09:00:00Z",
            "Event Count": 30,
            "Cmdline (Any)": "anydesk.exe --tray",
            "Match Reason": "PROC_NAME_IOC",
        }
        assert _fp(_ROW_A) == _fp(moved)

    def test_unknown_schema_falls_back_to_the_whole_row_minus_aggregates(self) -> None:
        # Sem nenhuma coluna de entidade a identidade é a linha, e a linha
        # continua distinguível — e reconhecível quando só o run mudou.
        a = {"Alias X": "v1", "Alias Y": "w", "Event Count": 1, "Last Seen": "2026-08-19T00:00:00Z"}
        b = {"Alias X": "v2", "Alias Y": "w", "Event Count": 1, "Last Seen": "2026-08-19T00:00:00Z"}
        a_next_run = {**a, "Event Count": 9, "Last Seen": "2026-08-20T00:00:00Z"}
        assert _fp(a) != _fp(b)
        assert _fp(a) == _fp(a_next_run)

    def test_digest_is_short_and_hex(self) -> None:
        digest = _fp(_ROW_A)
        assert len(digest) == 16
        int(digest, 16)


# ── raw.items por bytes ───────────────────────────────────────────────


class TestCapRowsByBytes:
    def test_first_row_always_enters(self) -> None:
        kept, truncated = cap_rows_by_bytes([{"blob": "x" * 10_000}, {"i": 1}], max_bytes=64)
        assert len(kept) == 1
        assert truncated is True

    def test_small_rows_all_fit(self) -> None:
        rows = [{"i": i} for i in range(100)]
        kept, truncated = cap_rows_by_bytes(rows, max_bytes=48 * 1024)
        assert kept == rows
        assert truncated is False

    def test_cut_respects_the_budget(self) -> None:
        rows = [{"i": i, "blob": "x" * 1000} for i in range(100)]
        kept, truncated = cap_rows_by_bytes(rows, max_bytes=10_000)
        assert truncated is True
        assert len(json.dumps(kept, separators=(",", ":")).encode()) <= 10_000


# ── resolvers do produtor ─────────────────────────────────────────────


class TestResolvers:
    def test_severity_is_the_query_s_or_the_default(self) -> None:
        assert scheduler_tasks._resolve_query_severity(SimpleNamespace(severity_id=2)) == 2
        assert (
            scheduler_tasks._resolve_query_severity(SimpleNamespace(severity_id=None))
            == settings.QUERY_DETECTION_DEFAULT_SEVERITY_ID
        )
        assert (
            scheduler_tasks._resolve_query_severity(SimpleNamespace())
            == settings.QUERY_DETECTION_DEFAULT_SEVERITY_ID
        )

    def test_severity_out_of_enum_falls_back(self) -> None:
        # Um valor fora do enum derrubaria o evento no gate estrutural — o
        # alerta sumiria por causa de um número digitado numa tela.
        assert (
            scheduler_tasks._resolve_query_severity(SimpleNamespace(severity_id=42))
            == settings.QUERY_DETECTION_DEFAULT_SEVERITY_ID
        )
        assert (
            scheduler_tasks._resolve_query_severity(SimpleNamespace(severity_id=True))
            == settings.QUERY_DETECTION_DEFAULT_SEVERITY_ID
        )

    def test_shape_defaults_to_both(self) -> None:
        assert scheduler_tasks._resolve_finding_shape(SimpleNamespace()) == "both"
        assert scheduler_tasks._resolve_finding_shape(SimpleNamespace(finding_shape=None)) == "both"
        assert scheduler_tasks._resolve_finding_shape(SimpleNamespace(finding_shape="weird")) == "both"
        assert scheduler_tasks._resolve_finding_shape(SimpleNamespace(finding_shape="per_row")) == "per_row"

    def test_suppression_covers_two_cadences(self) -> None:
        # Schedule horário: 3600 s era MENOR que a deriva real entre runs (60 a
        # 61 min) e a linha repetida virava Detection nova por segundos.
        hourly = SimpleNamespace(interval_value=1, interval_unit="hours")
        assert scheduler_tasks._suppression_seconds(hourly) == 7200
        ten_min = SimpleNamespace(interval_value=10, interval_unit="minutes")
        assert scheduler_tasks._suppression_seconds(ten_min) == settings.QUERY_DETECTION_SUPPRESSION_SECONDS
        legacy = SimpleNamespace(interval_minutes=240)
        assert scheduler_tasks._suppression_seconds(legacy) == 2 * 240 * 60
        assert scheduler_tasks._suppression_seconds(SimpleNamespace()) == settings.QUERY_DETECTION_SUPPRESSION_SECONDS


# ── o lote que sai ────────────────────────────────────────────────────


def _dispatch(items, *, query=None, **kw):
    captured: list[dict] = []
    integration = SimpleNamespace(
        id=9, name="EDR", platform="sophos", organization_id=8, data_geography="US",
        organization=SimpleNamespace(id=8, name="Org", slug="org"),
    )
    with patch(
        "backend.app.collectors.pipeline._enqueue_dispatch",
        lambda batch, *a, **k: captured.extend(batch),
    ):
        returned = scheduler_tasks._dispatch_scheduled_query_alert(
            integration=integration,
            sched=SimpleNamespace(id=2),
            query_def=query
            or SimpleNamespace(id=1, title="Hunt", description="d", statement="SELECT 1", table="t"),
            items=items,
            from_ts="2026-08-19T19:01:40Z",
            to_ts="2026-08-20T19:01:40Z",
            record=SimpleNamespace(id=539, language="xdr_data_lake", ocsf_mapping_version="1"),
            **kw,
        )
    assert returned == captured
    return captured


def _classes(batch):
    return [e["normalized"]["class_uid"] for e in batch]


def _row_events(batch):
    return [e for e in batch if "row_fingerprint" in e["normalized"].get("unmapped", {})]


class TestDispatchShapes:
    def test_both_emits_job_summary_and_one_per_row(self) -> None:
        batch = _dispatch([_ROW_A, _ROW_B])
        assert _classes(batch) == [1006, 2004, 2004, 2004]
        assert len(_row_events(batch)) == 2

    def test_summary_shape_keeps_the_old_pair(self) -> None:
        batch = _dispatch([_ROW_A, _ROW_B], finding_shape="summary")
        assert _classes(batch) == [1006, 2004]
        assert _row_events(batch) == []

    def test_per_row_shape_skips_the_summary(self) -> None:
        batch = _dispatch([_ROW_A, _ROW_B], finding_shape="per_row")
        assert _classes(batch) == [1006, 2004, 2004]
        assert len(_row_events(batch)) == 2

    def test_row_events_are_flat_and_addressable(self) -> None:
        batch = _dispatch([_ROW_A])
        (row_event,) = _row_events(batch)
        n = row_event["normalized"]
        assert n["device"]["hostname"] == "HOST-0001"
        assert n["process"]["name"] == "anydesk.exe"
        assert row_event["_centralops"]["event_type"] == "centralops.scheduled_query.finding"
        fingerprint = n["unmapped"]["row_fingerprint"]
        assert row_event["_centralops"]["event_id"] == f"sched-2-539-row-{fingerprint}"
        assert n["unmapped"]["dedup_key"] == f"sched:2:integ:9:{fingerprint}"
        # uid = dedup_key: a identidade que o Update do run seguinte e o Close
        # da triagem vão repetir. O run fica em search_result_id.
        assert n["finding_info"]["uid"] == n["unmapped"]["dedup_key"]
        assert n["unmapped"]["search_result_id"] == 539
        # O evento por linha não repete o statement nem depende do raw.
        assert "statement" not in n["unmapped"]
        assert row_event["raw"] == {}

    def test_every_event_carries_the_query_severity(self) -> None:
        query = SimpleNamespace(
            id=1, title="Hunt", description="d", statement="SELECT 1", table="t", severity_id=2,
        )
        batch = _dispatch([_ROW_A, _ROW_B], query=query)
        assert {e["normalized"]["severity_id"] for e in batch} == {2}
        assert {e["_centralops"]["severity_id"] for e in batch} == {2}

    def test_duplicate_identity_in_the_same_run_is_one_event(self) -> None:
        # A hunt agrupou por cmdline: a mesma máquina + processo aparece em
        # duas linhas. É UM achado.
        twin = {**_ROW_A, "Cmdline (Any)": "anydesk.exe --tray"}
        batch = _dispatch([_ROW_A, twin, _ROW_B])
        assert len(_row_events(batch)) == 2

    def test_cap_is_honored_and_declared(self) -> None:
        rows = [{**_ROW_B, "Hostname": f"SRV-{i:04d}", "Endpoint ID": f"id-{i}"} for i in range(7)]
        batch = _dispatch(rows, rows_cap=3)
        row_events = _row_events(batch)
        assert len(row_events) == 3
        summary = batch[1]["normalized"]["unmapped"]
        assert summary["rows_total"] == 7
        assert summary["rows_emitted"] == 3
        assert summary["rows_over_cap"] == 4
        assert all(e["normalized"]["unmapped"]["rows_over_cap"] == 4 for e in row_events)

    def test_activity_follows_the_detection(self) -> None:
        batch = _dispatch([_ROW_A, _ROW_B], row_activities={_fp(_ROW_A): 2})
        by_fp = {e["normalized"]["unmapped"]["row_fingerprint"]: e for e in _row_events(batch)}
        assert by_fp[_fp(_ROW_A)]["normalized"]["activity_id"] == 2  # Update
        assert by_fp[_fp(_ROW_B)]["normalized"]["activity_id"] == 1  # Create

    def test_all_events_pass_the_structural_gate(self) -> None:
        registry = get_registry("1.8.0")
        for envelope in _dispatch([_ROW_A, _ROW_B]):
            verdict = structural_gate(envelope["normalized"], registry)
            assert verdict.valid, (verdict.reason, verdict.missing_required)

    def test_summary_count_is_rows_not_events(self) -> None:
        batch = _dispatch([_ROW_A, _ROW_B])
        summary = batch[1]["normalized"]
        assert summary["count"] == 2
        assert summary["unmapped"]["events_total"] == 9  # 7 + 2
        assert summary["unmapped"]["finding_shape"] == "both"

    def test_job_event_declares_its_raw_budget(self) -> None:
        batch = _dispatch([_ROW_A, _ROW_B])
        raw = batch[0]["raw"]
        assert raw["items_total"] == 2
        assert raw["items_included"] == 2
        assert raw["items_truncated"] is False


# ── Detections por linha, num banco de verdade ────────────────────────


@pytest.fixture()
def db_session():
    """SQLite em memória com o schema real. Sem ``PRAGMA foreign_keys``: o que
    se testa aqui é a DEDUP por conteúdo, não a integridade referencial (a
    org/integração de fixture não existem como linhas)."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    maker = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    session = maker()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _record(db, items, *, shape="both", record_id=539, rows_cap=200, sched=None):
    return scheduler_tasks._record_query_detections(
        db=db,
        integration=SimpleNamespace(id=9, organization_id=8),
        sched=sched or SimpleNamespace(id=2, interval_value=1, interval_unit="hours"),
        query_def=SimpleNamespace(id=1, title="Hunt"),
        items=items,
        record=SimpleNamespace(id=record_id),
        dialect="xdr_data_lake",
        shape=shape,
        severity_id=3,
        rows_cap=rows_cap,
        base_key="sched:2:integ:9",
    )


class TestRowDetections:
    def test_first_run_creates_one_detection_per_row_with_ocsf_ref(self, db_session) -> None:
        activities = _record(db_session, [_ROW_A, _ROW_B])
        assert activities == {_fp(_ROW_A): 1, _fp(_ROW_B): 1}

        rows = db_session.query(models.Detection).order_by(models.Detection.id).all()
        assert len(rows) == 2
        keys = {d.dedup_key for d in rows}
        assert keys == {f"sched:2:integ:9:{_fp(_ROW_A)}", f"sched:2:integ:9:{_fp(_ROW_B)}"}
        for det in rows:
            fingerprint = det.dedup_key.rsplit(":", 1)[1]
            assert det.ocsf_ref == f"sched-2-539-row-{fingerprint}"
            assert det.severity_id == 3
            assert det.search_result_id == 539
            assert det.suppression_window_seconds == 7200
            assert det.count == 1

    def test_second_run_bumps_the_repeated_row_and_creates_the_new_one(self, db_session) -> None:
        _record(db_session, [_ROW_A], record_id=539)
        moved = {**_ROW_A, "Last Seen": "2026-08-20T09:00:00Z", "Event Count": 30}
        activities = _record(db_session, [moved, _ROW_B], record_id=540)

        assert activities[_fp(_ROW_A)] == 2  # Update: a linha já existia
        assert activities[_fp(_ROW_B)] == 1  # Create: entidade nova

        rows = {d.dedup_key: d for d in db_session.query(models.Detection).all()}
        assert len(rows) == 2
        existing = rows[f"sched:2:integ:9:{_fp(_ROW_A)}"]
        assert existing.count == 2
        assert existing.search_result_id == 540

    def test_beyond_the_window_the_row_is_a_new_detection(self, db_session) -> None:
        _record(db_session, [_ROW_A])
        stale = db_session.query(models.Detection).one()
        stale.last_seen = datetime.utcnow() - timedelta(seconds=7201)
        db_session.commit()

        activities = _record(db_session, [_ROW_A], record_id=541)
        assert activities[_fp(_ROW_A)] == 1
        assert db_session.query(models.Detection).count() == 2

    def test_cap_limits_detections_too(self, db_session) -> None:
        rows = [{**_ROW_B, "Hostname": f"SRV-{i:04d}", "Endpoint ID": f"id-{i}"} for i in range(7)]
        activities = _record(db_session, rows, rows_cap=3)
        assert len(activities) == 3
        assert db_session.query(models.Detection).count() == 3

    def test_summary_shape_keeps_the_run_level_detection(self, db_session) -> None:
        activities = _record(db_session, [_ROW_A, _ROW_B], shape="summary")
        assert activities == {}
        det = db_session.query(models.Detection).one()
        assert det.dedup_key == "sched:2:integ:9"
        assert det.ocsf_ref == "sched-2-539-finding"
