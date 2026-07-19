"""Native observability store (Redis rollups, self-sufficient)."""

from __future__ import annotations

import os
from unittest.mock import patch

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import fakeredis
import pytest

from backend.app.collectors import observability_store as obs

NOW = 1_700_000_000.0  # fixed second; minute bucket = floor/60*60
MIN = int(NOW // 60) * 60
HOUR = 60 * 60


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


# ── bucket_seconds/ttl_seconds POR CHAMADA (janela de 24h) ────────────────


def test_hourly_bucket_round_trip_over_24h_window() -> None:
    """Buckets HORÁRIOS habilitam uma janela de 24h sem estourar o TTL default
    de 3h (por-minuto). Um disparo em cada uma das últimas 24 horas soma 24,
    e o hash tem 24 campos — não 1440 (o que per-minute exigiria)."""
    r = _fake()
    ttl = 25 * HOUR
    with patch.object(obs, "_redis", return_value=r):
        for hours_ago in range(24):
            obs.record_counter(
                "rule", "42", "matches", 1.0,
                now=NOW - hours_ago * HOUR,
                ttl_seconds=ttl,
                bucket_seconds=HOUR,
            )
        total = obs.read_window_total(
            "rule", "42", "matches", minutes=24 * 60, now=NOW,
            bucket_seconds=HOUR, ttl_seconds=ttl,
        )
        fields = r.hgetall(obs._key("rule", "42", "matches"))
    assert total == 24.0
    assert len(fields) == 24  # não 1440 (per-minute equivalente)


def test_hourly_bucket_ttl_expires_via_redis_expire() -> None:
    """``ttl_seconds`` custom chega mesmo até o EXPIRE do Redis, não só ao
    epoch do bucket."""
    r = _fake()
    with patch.object(obs, "_redis", return_value=r):
        obs.record_counter(
            "rule", "42", "matches", 1.0, now=NOW, ttl_seconds=25 * HOUR, bucket_seconds=HOUR,
        )
        ttl = r.ttl(obs._key("rule", "42", "matches"))
    assert ttl == 25 * HOUR


def test_record_counter_and_read_window_total_defaults_are_unchanged() -> None:
    """R8 / regressão: chamar sem ``ttl_seconds``/``bucket_seconds`` tem que
    produzir EXATAMENTE o comportamento anterior à extensão (TTL 3h,
    granularidade por minuto) — os ~15 chamadores existentes (pipeline.py,
    routers/*, reduction/metering.py) não passam esses kwargs e não podem
    mudar de comportamento."""
    r = _fake()
    with patch.object(obs, "_redis", return_value=r):
        obs.record_counter("dest", "d1", "sent", 5, now=NOW)
        key = obs._key("dest", "d1", "sent")
        ttl = r.ttl(key)
        fields = r.hgetall(key)
        total = obs.read_window_total("dest", "d1", "sent", minutes=60, now=NOW)
    assert ttl == obs._TTL_SECONDS == 3 * 60 * 60
    assert set(fields.keys()) == {str(MIN)}
    assert total == 5.0


def test_default_ttl_bucket_invariants() -> None:
    """R8: as constantes que governam retenção/granularidade default nunca
    podem regredir de volta para o estado "janela de 24h é impossível" (TTL de
    3h com buckets por minuto — o bug verificado que motivou esta extensão)."""
    assert obs._BUCKET_SECONDS == 60
    assert obs._TTL_SECONDS == 3 * 60 * 60
    assert obs._TTL_SECONDS % obs._BUCKET_SECONDS == 0
    assert obs._TTL_SECONDS // obs._BUCKET_SECONDS == 180  # 3h em buckets de 1 min
    # a invariante que torna a extensão NECESSÁRIA: os defaults NÃO sustentam
    # uma janela de 24h — quem precisar dela tem que passar bucket_seconds/
    # ttl_seconds explícitos (ver test_hourly_bucket_round_trip_over_24h_window).
    assert obs._TTL_SECONDS < 24 * 60 * 60


def test_window_exceeding_ttl_is_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    """Pedir uma janela maior que o TTL configurado não levanta nem finge que
    está tudo bem em silêncio — vira um log de diagnóstico (debug), porque a
    soma pode estar sub-contada (buckets do início da janela já expiraram)."""
    r = _fake()
    with patch.object(obs, "_redis", return_value=r):
        with caplog.at_level("DEBUG"):
            total = obs.read_window_total("rule", "1", "matches", minutes=24 * 60, now=NOW)
    assert total == 0.0
    assert any("janela" in rec.message for rec in caplog.records)


# ── read_window_total_strict: propaga falha em vez de virar 0.0 ───────────


def test_read_window_total_strict_matches_read_window_total_when_healthy() -> None:
    r = _fake()
    with patch.object(obs, "_redis", return_value=r):
        obs.record_counter("rule", "9", "matches", 3, now=NOW)
        obs.record_counter("rule", "9", "matches", 4, now=NOW)
        total = obs.read_window_total("rule", "9", "matches", minutes=60, now=NOW)
        strict_total = obs.read_window_total_strict("rule", "9", "matches", minutes=60, now=NOW)
    assert total == 7.0
    assert strict_total == 7.0


def test_read_window_total_strict_propagates_redis_failure() -> None:
    class _Boom:
        def hgetall(self, *_a: object) -> None:
            raise RuntimeError("redis down")

    with patch.object(obs, "_redis", return_value=_Boom()):
        with pytest.raises(RuntimeError, match="redis down"):
            obs.read_window_total_strict("rule", "42", "matches", minutes=60, now=NOW)


def test_read_window_total_swallows_the_same_failure_strict_propagates() -> None:
    """O par de funções lado a lado sobre a MESMA falha: ``read_window_total``
    continua devolvendo 0.0 (ambíguo com "sem dado"); ``_strict`` propaga —
    é exatamente essa distinção que o chamador (UI de disparos) precisa."""
    class _Boom:
        def hgetall(self, *_a: object) -> None:
            raise RuntimeError("redis down")

    with patch.object(obs, "_redis", return_value=_Boom()):
        assert obs.read_window_total("rule", "42", "matches", minutes=60, now=NOW) == 0.0
        with pytest.raises(RuntimeError):
            obs.read_window_total_strict("rule", "42", "matches", minutes=60, now=NOW)


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
