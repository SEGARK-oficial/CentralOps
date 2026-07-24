"""TTL de dedupe em SEGUNDOS — piso, teto e capacidade do keyspace.

MOTIVAÇÃO OPERACIONAL: com o TTL expresso só em DIAS, o mínimo configurável era
1 dia. A 100 ev/s isso são ~8,6 milhões de chaves `dedupe:*` (~1 GB) contra um
`maxmemory` default de 512mb — o Redis evicta as chaves ANTES do TTL e o
`claim()` seguinte trata o evento como novo, reentregando duplicata em silêncio
(incidente real com 310k chaves evictadas).

O keyspace não depende do volume acumulado: `chaves ≈ EPS × TTL_segundos`. Poder
escolher 4h em vez de 24h corta o keyspace por 6.
"""
from __future__ import annotations

from backend.app.collectors.state.dedupe import (
    DEFAULT_TTL_SECONDS,
    MAX_TTL_SECONDS,
    MIN_TTL_SECONDS,
    clamp_ttl_seconds,
    estimate_dedupe_keys,
)


def test_floor_is_four_hours():
    """4x o visibility_timeout (3600s) — a mesma margem que o teste de
    invariante ancora. Abaixo disso, claim órfã expira antes de o broker
    desistir de redeliverar."""
    assert MIN_TTL_SECONDS == 4 * 3600


def test_clamp_raises_value_below_the_floor():
    assert clamp_ttl_seconds(60) == MIN_TTL_SECONDS
    assert clamp_ttl_seconds(3600) == MIN_TTL_SECONDS


def test_clamp_caps_absurd_values():
    assert clamp_ttl_seconds(999 * 86400) == MAX_TTL_SECONDS


def test_clamp_passes_through_a_valid_value():
    quatro_horas = 4 * 3600
    assert clamp_ttl_seconds(quatro_horas) == quatro_horas
    assert clamp_ttl_seconds(86400) == 86400


def test_clamp_falls_back_on_garbage():
    for bad in (None, "", "abc", 0, -5):
        assert clamp_ttl_seconds(bad) == DEFAULT_TTL_SECONDS


def test_keyspace_estimate_is_rate_times_window():
    """A fórmula que faltava ao operador."""
    assert estimate_dedupe_keys(100, 86400) == 8_640_000   # 24h @ 100 ev/s
    assert estimate_dedupe_keys(100, 4 * 3600) == 1_440_000  # 4h — 6x menor


def test_shortening_the_ttl_is_the_lever_on_memory():
    """Regressão conceitual: o keyspace é proporcional AO TTL, não ao volume
    histórico — encurtar a janela é a alavanca direta sobre a memória."""
    a = estimate_dedupe_keys(100, 86400)
    b = estimate_dedupe_keys(100, 4 * 3600)
    assert a / b == 6.0
