"""Invariante RedBeat: lock_timeout > beat_max_loop_interval (com folga 5×).

Regressão do incidente jul/2026: lock de 60s com max_loop_interval default do
celery (300s) — o Beat dormia mais que o TTL do próprio lock, caía em
``LockNotOwnedError``/crash-loop a cada ~90s (pior com 2 réplicas, que roubavam
o lock uma da outra) e entries de intervalo maior que o ciclo de vida do Beat
(sophos cases 3min / detections 5min) NUNCA disparavam — só a entry de 1min.
"""

from __future__ import annotations

from backend.app.collectors.celery_app import celery_app


def test_beat_max_loop_interval_is_explicit_and_small() -> None:
    """Sem isso o celery usa 300s — maior que qualquer lock razoável."""
    assert celery_app.conf.beat_max_loop_interval is not None
    assert celery_app.conf.beat_max_loop_interval <= 60


def test_lock_timeout_exceeds_max_loop_interval_with_margin() -> None:
    """Convenção da lib: lock_timeout = 5 × max_loop_interval (mínimo aceito: 2×)."""
    lock = celery_app.conf.redbeat_lock_timeout
    loop = celery_app.conf.beat_max_loop_interval
    assert lock >= 2 * loop, (
        f"redbeat_lock_timeout={lock}s não cobre beat_max_loop_interval={loop}s "
        "— o Beat pode dormir mais que o TTL do lock (LockNotOwnedError/crash-loop)"
    )
