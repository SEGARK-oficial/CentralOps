"""Sweep de reconciliação de entries órfãs do RedBeat.

Cobre a lógica PURA de detecção de órfãs (``scheduler._orphan_entry_keys``), que
fecha o único leak estrutural do RedBeat (entries sem TTL + deregister
best-effort). A I/O contra o Redis é uma casca fina sobre esta função.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

from backend.app.collectors.scheduler import _orphan_entry_keys

PREFIX = "redbeat:"
BEAT_KEYS = {"sophos-alerts", "sophos-cases", "wazuh", "wazuh-detections"}


def _key(beat_key: str, int_id) -> str:
    return f"{PREFIX}{beat_key}-{int_id}"


def test_orphan_detected_for_inactive_integration():
    existing = [_key("sophos-alerts", 1), _key("sophos-alerts", 2)]
    assert _orphan_entry_keys(existing, BEAT_KEYS, PREFIX, active_ids={1}) == [
        _key("sophos-alerts", 2)
    ]


def test_active_integration_is_kept():
    existing = [_key("wazuh-detections", 7)]
    assert _orphan_entry_keys(existing, BEAT_KEYS, PREFIX, active_ids={7}) == []


def test_static_and_malformed_entries_are_never_swept():
    # Nenhuma destas casa um beat_key + sufixo inteiro → jamais deletadas.
    existing = [
        f"{PREFIX}sophos-partner-sync",   # tarefa estática (beat_schedule.py)
        f"{PREFIX}retention-daily",       # tarefa estática
        f"{PREFIX}::schedule",            # zset interno do RedBeat
        _key("sophos-alerts", "abc"),     # sufixo não-inteiro
        f"{PREFIX}unknown-vendor-5",      # beat_key fora do registry
    ]
    assert _orphan_entry_keys(existing, BEAT_KEYS, PREFIX, active_ids=set()) == []


def test_overlapping_beat_key_prefixes_are_disambiguated():
    # wazuh vs wazuh-detections compartilham prefixo; o guarda isdigit atribui
    # cada entry ao beat_key correto independentemente da ordem do set.
    existing = [_key("wazuh", 3), _key("wazuh-detections", 3)]
    assert set(_orphan_entry_keys(existing, BEAT_KEYS, PREFIX, active_ids=set())) == {
        _key("wazuh", 3),
        _key("wazuh-detections", 3),
    }
    assert _orphan_entry_keys(existing, BEAT_KEYS, PREFIX, active_ids={3}) == []


def test_mixed_active_orphan_and_static():
    existing = [
        _key("sophos-alerts", 1),
        _key("sophos-cases", 1),       # 1 ativa → ambas mantidas
        _key("sophos-alerts", 9),      # 9 órfã
        f"{PREFIX}retention-daily",    # estática → ignorada
    ]
    assert _orphan_entry_keys(existing, BEAT_KEYS, PREFIX, active_ids={1}) == [
        _key("sophos-alerts", 9)
    ]
