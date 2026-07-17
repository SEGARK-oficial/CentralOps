"""Testes do ResilientRedBeatScheduler (recuperação in-process do lock + heartbeat).

Regressão do incidente jul/2026: o beat crashava em ``LockNotOwnedError`` (lock
perdido por evicção/restart do Redis/blip) e virava crash-loop, parando a coleta.
A subclasse re-adquire o MESMO lock (leadership-safe, via SET NX) em vez de morrer,
e grava heartbeat por tick para o healthcheck detectar beat travado.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from redbeat import RedBeatScheduler
from redis.exceptions import LockNotOwnedError

from backend.app.collectors import beat_scheduler_resilient as mod
from backend.app.collectors.beat_scheduler_resilient import ResilientRedBeatScheduler


def _make_scheduler(lock: object) -> ResilientRedBeatScheduler:
    """Instancia sem __init__ (que exigiria um Celery app real)."""
    sched = ResilientRedBeatScheduler.__new__(ResilientRedBeatScheduler)
    sched.lock = lock  # type: ignore[attr-defined]
    sched.max_interval = 30  # type: ignore[attr-defined]
    return sched


@pytest.fixture()
def hb_file(tmp_path, monkeypatch):
    p = tmp_path / "beat-heartbeat"
    monkeypatch.setattr(mod, "BEAT_HEARTBEAT_FILE", str(p))
    return p


def test_tick_success_passes_through_and_writes_heartbeat(hb_file) -> None:
    sched = _make_scheduler(MagicMock())
    with patch.object(RedBeatScheduler, "tick", return_value=12.3) as sup:
        result = sched.tick()
    assert result == 12.3
    sup.assert_called_once()
    # heartbeat gravado com um epoch inteiro plausível
    assert hb_file.exists()
    assert int(hb_file.read_text()) > 1_700_000_000


def test_lock_lost_then_reacquired_does_not_raise(hb_file) -> None:
    lock = MagicMock()
    lock.acquire.return_value = True  # lock estava livre → re-adquire
    sched = _make_scheduler(lock)
    with patch.object(RedBeatScheduler, "tick", side_effect=LockNotOwnedError("boom")):
        result = sched.tick()
    # não levanta; agenda um tick logo em seguida (min(max_interval, 5.0))
    assert result == 5.0
    # re-aquisição foi leadership-safe (blocking curto) e resetou o token local
    lock.acquire.assert_called_once()
    _, kwargs = lock.acquire.call_args
    assert kwargs.get("blocking") is True
    assert kwargs.get("blocking_timeout") == mod._REACQUIRE_BLOCKING_TIMEOUT
    assert lock.local.token is None


def test_lock_held_by_other_replica_reraises(hb_file) -> None:
    lock = MagicMock()
    lock.acquire.return_value = False  # outra réplica detém o lock → NÃO recupera
    sched = _make_scheduler(lock)
    with patch.object(RedBeatScheduler, "tick", side_effect=LockNotOwnedError("boom")):
        with pytest.raises(LockNotOwnedError):
            sched.tick()
    # propaga → crash+restart → hot-standby; exclusão mútua preservada
    lock.acquire.assert_called_once()


def test_lock_none_reraises(hb_file) -> None:
    sched = _make_scheduler(None)  # beat_init não adquiriu; nada a re-adquirir
    with patch.object(RedBeatScheduler, "tick", side_effect=LockNotOwnedError("boom")):
        with pytest.raises(LockNotOwnedError):
            sched.tick()


def test_reacquire_lock_error_reraises_original(hb_file) -> None:
    from redis.exceptions import LockError

    lock = MagicMock()
    lock.acquire.side_effect = LockError("redis down")
    sched = _make_scheduler(lock)
    with patch.object(RedBeatScheduler, "tick", side_effect=LockNotOwnedError("boom")):
        with pytest.raises(LockNotOwnedError):
            sched.tick()


def test_heartbeat_write_failure_never_breaks_tick(monkeypatch) -> None:
    # aponta o heartbeat p/ um caminho inescrevível — o tick NÃO pode falhar
    monkeypatch.setattr(mod, "BEAT_HEARTBEAT_FILE", "/nonexistent-dir-xyz/hb")
    sched = _make_scheduler(MagicMock())
    with patch.object(RedBeatScheduler, "tick", return_value=7.0):
        assert sched.tick() == 7.0


def test_heartbeat_is_written_before_super_tick(hb_file) -> None:
    """Mesmo que super().tick() trave/levante, o heartbeat do início do tick já
    foi gravado (prova de que o loop entrou no tick)."""
    sched = _make_scheduler(None)
    with patch.object(RedBeatScheduler, "tick", side_effect=RuntimeError("hang")):
        with pytest.raises(RuntimeError):
            sched.tick()
    assert hb_file.exists()
