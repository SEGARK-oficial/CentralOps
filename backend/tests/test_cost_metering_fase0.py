"""Cost/volume metering (medição pura, sem alavanca de redução).

Cobre as invariantes que tornam esta fase segura de mergear:
  * **flag-off = no-op** (zero serialização, hot path byte-idêntico) — a garantia de
    rollback;
  * **bijeção** OTel ``_SPEC`` ↔ fachadas ``metrics`` (os 2 contadores IN);
  * **record_in/record_out** corretos (bytes lógicos, séries source/org/dest) e
    **fail-closed** sem org (anti cross-tenant);
  * o **seam EE** ``ee_hooks.cost_pricer`` (register/get/reset + conflito → RuntimeError);
  * o endpoint **GET /collectors/cost-summary** (volume + razão; US$ só com pricer EE).
"""
from __future__ import annotations

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import pytest

from backend.app.collectors import metrics, otel_metrics
from backend.app.collectors import observability_store as obs
from backend.app.collectors.reduction import metering
from backend.app.core import ee_hooks
from backend.app.core.config import settings


class _RecordingCounter:
    """Spy para o store: grava (kind, oid, metric, value)."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __call__(self, kind, oid, metric, value=1.0, *, now=None) -> None:
        self.calls.append((kind, oid, metric, float(value)))


class _SpyInstrument:
    """Spy para uma fachada OTel: grava labels + inc."""

    def __init__(self) -> None:
        self.incs: list[tuple] = []
        self._last: dict = {}

    def labels(self, **kw):
        self._last = kw
        return self

    def inc(self, amount: float = 1) -> None:
        self.incs.append((dict(self._last), float(amount)))


@pytest.fixture()
def spies(monkeypatch):
    rec = _RecordingCounter()
    ev_in, by_in = _SpyInstrument(), _SpyInstrument()
    monkeypatch.setattr(obs, "record_counter", rec)
    monkeypatch.setattr(metrics, "EVENTS_IN", ev_in)
    monkeypatch.setattr(metrics, "BYTES_IN", by_in)
    return rec, ev_in, by_in


@pytest.fixture(autouse=True)
def _reset_pricer():
    ee_hooks.reset_cost_pricer()
    yield
    ee_hooks.reset_cost_pricer()


# ── flag-off no-op (rollback / byte-idêntico) ────────────────────────────────────

def test_metering_disabled_by_default():
    assert settings.COST_METERING_ENABLED is False
    assert metering.enabled() is False


def test_record_in_is_noop_when_flag_off(monkeypatch, spies):
    rec, ev_in, by_in = spies
    monkeypatch.setattr(settings, "COST_METERING_ENABLED", False)
    metering.record_in(1, 10, {"a": "b" * 100})
    assert rec.calls == [] and ev_in.incs == [] and by_in.incs == []


def test_record_out_is_noop_when_flag_off(monkeypatch, spies):
    rec, _, _ = spies
    monkeypatch.setattr(settings, "COST_METERING_ENABLED", False)
    metering.record_out(1, 5, 1234.0)
    assert rec.calls == []


def test_record_in_does_not_serialize_when_flag_off(monkeypatch):
    """Flag-off não pode nem chamar dumps_bytes (custo zero)."""
    monkeypatch.setattr(settings, "COST_METERING_ENABLED", False)
    called = {"n": 0}
    import backend.app.collectors.output._fastjson as fj

    orig = fj.dumps_bytes
    monkeypatch.setattr(fj, "dumps_bytes", lambda o: (called.__setitem__("n", called["n"] + 1) or orig(o)))
    metering.record_in(1, 10, {"x": 1})
    assert called["n"] == 0


# ── bijeção _SPEC ↔ fachadas ─────────────────────────────────────────────────────

def test_in_counters_are_in_spec_and_have_facades():
    for name in ("collector_events_in_total", "collector_bytes_in_total"):
        assert name in otel_metrics._SPEC
        assert otel_metrics._SPEC[name]["kind"] == "counter"
    # importar metrics (no topo) já teria levantado RuntimeError se dessincronizado.
    assert metrics.EVENTS_IN._name == "collector_events_in_total"
    assert metrics.BYTES_IN._name == "collector_bytes_in_total"


def test_reduction_ratio_not_a_synchronous_instrument():
    """A razão é derivada no read-time, NÃO um instrumento OTLP."""
    assert "collector_reduction_ratio" not in otel_metrics._SPEC


# ── record_in / record_out corretos ──────────────────────────────────────────────

def test_record_in_records_volume_under_source_and_org(monkeypatch, spies):
    rec, ev_in, by_in = spies
    monkeypatch.setattr(settings, "COST_METERING_ENABLED", True)
    raw = {"event": "x", "payload": "y" * 50}
    from backend.app.collectors.output._fastjson import dumps_bytes

    expected = len(dumps_bytes(raw))
    metering.record_in(7, 42, raw)
    # OTel: 1 evento + expected bytes, labels org+integration.
    assert ev_in.incs == [({"org_id": "7", "integration_id": "42"}, 1.0)]
    assert by_in.incs == [({"org_id": "7", "integration_id": "42"}, float(expected))]
    # store: source/{42} e org/{7}, events_in + bytes_in.
    assert ("source", "42", "events_in", 1.0) in rec.calls
    assert ("source", "42", "bytes_in", float(expected)) in rec.calls
    assert ("org", "7", "events_in", 1.0) in rec.calls
    assert ("org", "7", "bytes_in", float(expected)) in rec.calls


def test_record_in_fail_closed_without_org(monkeypatch, spies):
    """Sem org → não grava série org nem OTel (anti cross-tenant); source ainda conta."""
    rec, ev_in, by_in = spies
    monkeypatch.setattr(settings, "COST_METERING_ENABLED", True)
    metering.record_in(None, 42, {"x": 1})
    assert ev_in.incs == [] and by_in.incs == []  # OTel exige org+integration
    assert not any(c[0] == "org" for c in rec.calls)  # nenhuma série org
    assert any(c == ("source", "42", "events_in", 1.0) for c in rec.calls)


def test_record_out_records_org_rollup(monkeypatch, spies):
    rec, _, _ = spies
    monkeypatch.setattr(settings, "COST_METERING_ENABLED", True)
    metering.record_out(7, 5, 9000.0)
    assert ("org", "7", "events_out", 5.0) in rec.calls
    assert ("org", "7", "bytes_out", 9000.0) in rec.calls


def test_record_out_noop_without_org_or_events(monkeypatch, spies):
    rec, _, _ = spies
    monkeypatch.setattr(settings, "COST_METERING_ENABLED", True)
    metering.record_out(None, 5, 9000.0)
    metering.record_out(7, 0, 0.0)
    assert rec.calls == []


def test_dispatch_metering_hook_never_escapes_on_malformed_batch(monkeypatch, spies):
    """Garantia da fronteira: o hook OUT em _record_dest_observability, com metering ON
    e um lote com elementos NÃO-dict (envelope corrompido), NUNCA pode levantar — senão
    falharia a task de dispatch DEPOIS de já ter entregue (retry → entrega duplicada)."""
    monkeypatch.setattr(settings, "COST_METERING_ENABLED", True)
    from backend.app.collectors.pipeline import _record_dest_observability

    batch = [{"_centralops": {"organization_id": 5}}, "lixo-não-dict", None]
    # Não deve levantar (o guard isinstance + try/except cobrem o pré-âmbulo + record_out).
    _record_dest_observability("dest-1", accepted=3, rejected_count=0, latency_s=0.01, batch=batch)


# ── seam EE cost_pricer ──────────────────────────────────────────────────────────

def test_cost_pricer_seam_register_get_reset():
    assert ee_hooks.get_cost_pricer() is None  # Community default
    pricer = lambda org, dest, gb: {"usd": gb * 2.5, "currency": "USD"}
    ee_hooks.register_cost_pricer(pricer)
    assert ee_hooks.get_cost_pricer() is pricer
    ee_hooks.register_cost_pricer(pricer)  # idempotente no MESMO callable
    ee_hooks.reset_cost_pricer()
    assert ee_hooks.get_cost_pricer() is None


def test_cost_pricer_conflicting_reregister_raises():
    ee_hooks.register_cost_pricer(lambda o, d, gb: {"usd": 1.0, "currency": "USD"})
    with pytest.raises(RuntimeError):
        ee_hooks.register_cost_pricer(lambda o, d, gb: {"usd": 2.0, "currency": "USD"})


# ── endpoint GET /collectors/cost-summary ────────────────────────────────────────

def _seed(monkeypatch, totals: dict):
    """Monkeypatch obs.read_window_total para devolver ``totals[(kind,oid,metric)]``."""
    from backend.app.collectors import observability_store as _obs

    def fake(kind, oid, metric, *, minutes, now=None):
        return float(totals.get((kind, oid, metric), 0.0))

    monkeypatch.setattr(_obs, "read_window_total", fake)


def _call_endpoint(monkeypatch, org_ids):
    """Chama a função do endpoint direto, com accessible_org_ids fixo (evita TestClient)."""
    from backend.app.core import tenant
    from backend.app.routers import collectors as router

    monkeypatch.setattr(tenant, "accessible_org_ids", lambda user, db: set(org_ids))
    return router.get_cost_summary(db=None, current_user=object())


def test_cost_summary_community_volume_and_ratio_no_usd(monkeypatch):
    _seed(monkeypatch, {
        ("org", "1", "bytes_in"): 1000, ("org", "1", "bytes_out"): 700,
        ("org", "1", "events_in"): 10, ("org", "1", "events_out"): 7,
    })
    out = _call_endpoint(monkeypatch, [1])
    assert out.pricing_available is False
    assert len(out.rows) == 1
    row = out.rows[0]
    assert row.organization_id == 1 and row.bytes_in == 1000 and row.bytes_out == 700
    assert row.out_in_byte_ratio == pytest.approx(0.7)  # bytes_out/bytes_in (informativo)
    assert row.reduction_active is False  # sem alavanca
    assert row.cost is None  # Community: sem US$


def test_cost_summary_omits_orgs_without_data(monkeypatch):
    _seed(monkeypatch, {("org", "1", "bytes_in"): 500})
    out = _call_endpoint(monkeypatch, [1, 2, 3])
    assert {r.organization_id for r in out.rows} == {1}  # 2 e 3 sem dado → omitidas


def test_cost_summary_enriches_usd_when_ee_pricer_registered(monkeypatch):
    _seed(monkeypatch, {
        ("org", "1", "bytes_in"): 2_000_000_000, ("org", "1", "bytes_out"): 1_000_000_000,
        ("org", "1", "events_in"): 100, ("org", "1", "events_out"): 50,
    })
    ee_hooks.register_cost_pricer(lambda org, dest, gb: {"usd": round(gb * 3.0, 2), "currency": "USD"})
    out = _call_endpoint(monkeypatch, [1])
    assert out.pricing_available is True
    row = out.rows[0]
    assert row.cost is not None
    assert row.cost.currency == "USD"
    assert row.cost.usd == pytest.approx(3.0)  # 1 GB out * 3.0


def test_cost_summary_surfaces_savings_when_a_lever_reduced(monkeypatch):
    """Com bytes_saved na janela: reduction_active=True, reduction_pct
    e savings_usd_per_day (via pricer EE) aparecem."""
    from backend.app.routers import collectors as router

    # 1 GB entregue, 1 GB evitado (metade do que SERIA entregue) na janela.
    _seed(monkeypatch, {
        ("org", "1", "bytes_out"): 1_000_000_000,
        ("org", "1", "bytes_saved"): 1_000_000_000,
        ("org", "1", "events_out"): 50,
    })
    ee_hooks.register_cost_pricer(lambda org, dest, gb: {"usd": round(gb * 3.0, 4), "currency": "USD"})
    out = _call_endpoint(monkeypatch, [1])
    row = out.rows[0]
    assert row.reduction_active is True
    assert row.bytes_saved == 1_000_000_000
    assert row.reduction_pct == pytest.approx(0.5)  # saved / (out + saved)
    # savings da janela (1 GB × 3.0 = 3.0) extrapolado p/ dia.
    expected_day = 3.0 * (1440.0 / router._COST_WINDOW_MINUTES)
    assert row.savings_usd_per_day == pytest.approx(expected_day)
