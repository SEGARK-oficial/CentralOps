"""Native observability store (Redis rollups, self-sufficient)."""

from __future__ import annotations

import os
from unittest.mock import patch

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import fakeredis

from backend.app.collectors import observability_store as obs

NOW = 1_700_000_000.0  # fixed second; minute bucket = floor/60*60
MIN = int(NOW // 60) * 60


def _fake():
    return fakeredis.FakeStrictRedis(decode_responses=True)


def test_counter_accumulates_into_minute_bucket() -> None:
    r = _fake()
    with patch.object(obs, "_redis", return_value=r):
        obs.record_counter("dest", "d1", "sent", 5, now=NOW)
        obs.record_counter("dest", "d1", "sent", 3, now=NOW)
        s = obs.read_series("dest", "d1", ["sent"], minutes=60, now=NOW)
    assert s["sent"] == [[MIN, 8.0]]


def test_latency_avg_is_sum_over_count() -> None:
    r = _fake()
    with patch.object(obs, "_redis", return_value=r):
        obs.record_latency("dest", "d1", 0.2, now=NOW)
        obs.record_latency("dest", "d1", 0.4, now=NOW)
        s = obs.read_series("dest", "d1", ["latency_avg"], minutes=60, now=NOW)
    assert s["latency_avg"] == [[MIN, 0.30000000000000004]] or s["latency_avg"] == [[MIN, 0.3]]


def test_window_total_and_rate() -> None:
    """EPS rolling-window computado no app (AxoSyslog
    eps_last_*): read_window_total soma a janela; read_window_rate divide por s."""
    r = _fake()
    with patch.object(obs, "_redis", return_value=r):
        obs.record_counter("dest", "dwin", "sent", 120, now=NOW)
        total = obs.read_window_total("dest", "dwin", "sent", minutes=60, now=NOW)
        rate = obs.read_window_rate("dest", "dwin", "sent", minutes=60, now=NOW)
    assert total == 120.0
    assert rate == 120.0 / 3600.0  # 120 eventos / (60min * 60s)


def test_window_rate_zero_without_data() -> None:
    r = _fake()
    with patch.object(obs, "_redis", return_value=r):
        assert obs.read_window_rate("dest", "empty", "sent", minutes=60, now=NOW) == 0.0
        assert obs.read_window_total("route", "empty", "matched", minutes=60, now=NOW) == 0.0


def test_old_buckets_filtered_by_window() -> None:
    r = _fake()
    with patch.object(obs, "_redis", return_value=r):
        obs.record_counter("dest", "d1", "sent", 1, now=NOW - 3600)  # 60 min ago
        obs.record_counter("dest", "d1", "sent", 9, now=NOW)
        s = obs.read_series("dest", "d1", ["sent"], minutes=10, now=NOW)  # last 10 min
    assert s["sent"] == [[MIN, 9.0]]  # the hour-old bucket is excluded


def test_gauge_latest_value() -> None:
    r = _fake()
    with patch.object(obs, "_redis", return_value=r):
        obs.set_gauge("dest", "d1", "queue_depth", 42)
        obs.set_gauge("dest", "d1", "backpressure_state", "drop_newest")
        g = obs.read_gauges("dest", "d1", ["queue_depth", "backpressure_state", "missing"])
    assert g["queue_depth"] == "42"
    assert g["backpressure_state"] == "drop_newest"
    assert g["missing"] is None


def test_per_route_counters() -> None:
    r = _fake()
    with patch.object(obs, "_redis", return_value=r):
        obs.record_counter("route", "r1", "matched", 7, now=NOW)
        s = obs.read_series("route", "r1", ["matched"], minutes=60, now=NOW)
    assert s["matched"] == [[MIN, 7.0]]


def test_read_series_never_emits_internal_latency_keys() -> None:
    """latency_sum/latency_count são accounting interno —
    nunca aparecem na saída, mesmo pedidos explicitamente."""
    r = _fake()
    with patch.object(obs, "_redis", return_value=r):
        obs.record_latency("dest", "d1", 0.2, now=NOW)
        s = obs.read_series(
            "dest", "d1", ["latency_avg", "latency_sum", "latency_count"], now=NOW
        )
    assert "latency_sum" not in s
    assert "latency_count" not in s
    assert s["latency_avg"] == [[MIN, 0.2]]


def test_record_tap_redacts_secrets_and_caps_ring() -> None:
    """O tap mascara segredos (por nome) e o ring é cap-ado em _TAP_MAX."""
    r = _fake()
    with patch.object(obs, "_redis", return_value=r):
        # 60 > _TAP_MAX(50): o ring deve reter só os mais recentes.
        envs = [
            {"_centralops": {"event_id": f"e{i}"}, "token": "supersecret", "msg": f"m{i}"}
            for i in range(60)
        ]
        obs.record_tap("d1", envs)
        out = obs.read_tap("d1", limit=200)
    assert len(out) <= obs._TAP_MAX  # hard cap, mesmo pedindo 200
    assert all(e.get("token") == "[REDACTED]" for e in out)  # segredo mascarado
    assert all("msg" in e for e in out)  # campo não-sensível preservado


def test_best_effort_never_raises() -> None:
    class _Boom:
        def pipeline(self):
            raise RuntimeError("redis down")

        def hgetall(self, *_):
            raise RuntimeError("redis down")

        def hset(self, *_):
            raise RuntimeError("redis down")

    with patch.object(obs, "_redis", return_value=_Boom()):
        obs.record_counter("dest", "d1", "sent", 1)  # must not raise
        obs.set_gauge("dest", "d1", "queue_depth", 1)  # must not raise
        assert obs.read_series("dest", "d1", ["sent"], now=NOW) == {"sent": []}
        assert obs.read_gauges("dest", "d1", ["x"]) == {"x": None}
