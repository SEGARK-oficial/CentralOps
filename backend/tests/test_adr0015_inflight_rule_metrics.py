"""Contador de "disparos nas últimas 24h" por regra de correlação (ADR-0015).

``flush_inflight`` espelha ``acc.matches``/``acc.overflow`` no
``observability_store`` (Redis nativo, kind="rule") — a UI precisa disso
porque o instrumento OTel equivalente (``INFLIGHT_MATCHES``) é NO-OP quando
``OTEL_ENABLED=False`` (default da instalação padrão): sem este espelhamento
o contador ficaria permanentemente zerado fora de um deployment com OTel
Collector configurado.

DUAS métricas (``matches`` e ``overflow``), não uma: a razão entre elas é o
diagnóstico de cardinalidade de ``group_by`` estourando o teto por
regra/ciclo — ver o docstring de ``flush_inflight``.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import fakeredis
import pytest

from backend.app.collectors import observability_store as obs
from backend.app.collectors.inflight.runtime import (
    RULE_METRIC_BUCKET_SECONDS,
    RULE_METRIC_TTL_SECONDS,
    RULE_METRIC_WINDOW_MINUTES,
    InflightAccumulator,
    flush_inflight,
)


def _fake_redis() -> "fakeredis.FakeStrictRedis":
    return fakeredis.FakeStrictRedis(decode_responses=True)


def _read_rule_window(kind_oid: str, metric: str) -> float:
    return obs.read_window_total(
        "rule", kind_oid, metric,
        minutes=RULE_METRIC_WINDOW_MINUTES,
        bucket_seconds=RULE_METRIC_BUCKET_SECONDS,
        ttl_seconds=RULE_METRIC_TTL_SECONDS,
    )


# ── R8: invariantes das constantes novas ───────────────────────────────────


def test_rule_metric_bucket_is_hourly_not_per_minute() -> None:
    """A feature inteira depende de NÃO regredir para per-minute: 1440 campos
    por hash por regra (per-minute) vs 24 (horário) numa janela de 24h."""
    assert RULE_METRIC_BUCKET_SECONDS == 60 * 60


def test_rule_metric_window_is_24h() -> None:
    assert RULE_METRIC_WINDOW_MINUTES == 24 * 60


def test_rule_metric_ttl_covers_the_full_24h_read_window() -> None:
    """A MESMA invariante que este trabalho existe pra corrigir: o TTL default
    do observability_store (3h) é insuficiente por construção pra uma janela
    de 24h — os buckets do início da janela expirariam antes da leitura.
    ``RULE_METRIC_TTL_SECONDS`` tem que superar a janela, com folga."""
    assert RULE_METRIC_TTL_SECONDS >= RULE_METRIC_WINDOW_MINUTES * 60
    assert RULE_METRIC_TTL_SECONDS - RULE_METRIC_WINDOW_MINUTES * 60 == 60 * 60  # folga de 1h, explícita
    # trava o bug original verificado: TTL default (3h) NÃO cobre 24h.
    assert obs._TTL_SECONDS < RULE_METRIC_WINDOW_MINUTES * 60


def test_rule_metric_hash_field_count_stays_small() -> None:
    """Regressão mecânica do "1440 campos" citado no contrato: com bucket
    horário, o hash de uma regra nunca passa de TTL/bucket campos — ordens de
    magnitude menor que o equivalente per-minute pela MESMA retenção."""
    max_fields_hourly = RULE_METRIC_TTL_SECONDS // RULE_METRIC_BUCKET_SECONDS
    max_fields_per_minute_equivalent = RULE_METRIC_TTL_SECONDS // 60
    assert max_fields_hourly == 25
    assert max_fields_hourly < max_fields_per_minute_equivalent
    assert max_fields_hourly <= 30  # teto generoso — pega qualquer revert acidental


# ── flush_inflight: grava matches E overflow ────────────────────────────────


@pytest.mark.asyncio
async def test_flush_inflight_writes_matches_and_overflow(monkeypatch: pytest.MonkeyPatch) -> None:
    r = _fake_redis()
    monkeypatch.setattr(obs, "_redis", lambda: r)

    acc = InflightAccumulator()
    acc.matches[101] = 1240
    acc.matches[102] = 3
    acc.overflow[101] = 900  # cardinalidade do group_by estourando o teto

    await flush_inflight(acc, organization_id=7)

    assert _read_rule_window("101", "matches") == 1240.0
    assert _read_rule_window("101", "overflow") == 900.0
    assert _read_rule_window("102", "matches") == 3.0
    # regra 102 nunca estourou o teto — nada gravado, leitura é 0.0 (sem dado).
    assert _read_rule_window("102", "overflow") == 0.0


@pytest.mark.asyncio
async def test_flush_inflight_accumulates_across_cycles(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dois ciclos seguidos somam no MESMO bucket horário (dentro da mesma
    hora) — o contador de 24h é sobre o acumulado, não um snapshot do
    último ciclo."""
    r = _fake_redis()
    monkeypatch.setattr(obs, "_redis", lambda: r)

    acc1 = InflightAccumulator()
    acc1.matches[5] = 10
    await flush_inflight(acc1, organization_id=1)

    acc2 = InflightAccumulator()
    acc2.matches[5] = 7
    await flush_inflight(acc2, organization_id=1)

    assert _read_rule_window("5", "matches") == 17.0


@pytest.mark.asyncio
async def test_flush_inflight_writes_nothing_when_no_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    r = _fake_redis()
    monkeypatch.setattr(obs, "_redis", lambda: r)

    acc = InflightAccumulator()  # nenhum match, nenhum overflow
    await flush_inflight(acc, organization_id=1)

    assert r.keys("obs:rule:*") == []


# ── best-effort: observability_store falhando não pode derrubar o flush ───


@pytest.mark.asyncio
async def test_flush_inflight_never_raises_when_observability_store_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simula o observability_store inteiro quebrado (ex.: Redis fora do ar).
    ``flush_inflight`` roda no ``finally`` do ciclo de coleta — se levantasse
    aqui, mascararia qualquer exceção original que estivesse se propagando
    por esse ``finally`` (semântica de ``finally`` do Python: uma exceção nova
    substitui a antiga)."""
    def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("observability_store totalmente indisponível")

    monkeypatch.setattr(obs, "record_counter", _boom)

    acc = InflightAccumulator()
    acc.matches[1] = 5
    acc.matches[2] = 1
    acc.overflow[1] = 2

    # não deve levantar
    await flush_inflight(acc, organization_id=1)


@pytest.mark.asyncio
async def test_flush_inflight_still_persists_detections_when_observability_store_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A gravação de métrica é um EXTRA best-effort — não pode competir com a
    persistência das Detections (``acc.pending``), que segue seu próprio
    caminho (``_flush_sync``) independentemente do observability_store."""
    from backend.app.collectors.inflight import runtime as runtime_mod
    from backend.app.collectors.inflight.matcher import CompiledInflightRule

    written: list[str] = []

    def _fake_flush_sync(pending: dict, organization_id: int) -> int:
        written.extend(pending.keys())
        return len(pending)

    def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("observability_store indisponível")

    monkeypatch.setattr(runtime_mod, "_flush_sync", _fake_flush_sync)
    monkeypatch.setattr(obs, "record_counter", _boom)

    rule = CompiledInflightRule(
        rule_id=1, name="r1", severity_id=4,
        suppression_window_seconds=3600, group_by_path=None, clauses=(),
    )
    acc = InflightAccumulator()
    acc.pending["inflight:1:1:*"] = {"rule": rule, "integration_id": None}
    acc.matches[1] = 1

    await flush_inflight(acc, organization_id=1)

    assert written == ["inflight:1:1:*"]
