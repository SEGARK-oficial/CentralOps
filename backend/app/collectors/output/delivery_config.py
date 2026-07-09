"""Validated per-destination **delivery policy**.

The ``destinations.delivery`` column is free-form JSON in the DB. Until now it
was read with ad-hoc ``dict.get`` defaults scattered across the breaker and the
dispatcher. This module makes it a single, validated contract:

  - ``breaker``       — circuit breaker thresholds.
  - ``concurrency``   — per-destination concurrency cap (bulkhead).
  - ``backpressure``  — load-shedding policy: block | drop_newest | persistent_queue.
  - ``queue_ceiling`` — per-destination queue-depth ceiling (0 = unlimited).
  - ``shadow``        — format+validate+measure, do NOT deliver.
  - ``batch``         — batch sizing: max_items (ENFORCED by dispatcher), flush_ms
    (documented; no temporal aggregation yet).
  - ``retry``         — exponential backoff retry (ENFORCED by dispatcher): initial_ms,
    max_ms, multiplier, max_retries.  Old ``backoff_max_s`` field auto-migrated.
  - ``timeout_ms``    — per-send timeout (ENFORCED by dispatcher via asyncio.wait_for).

Two parse modes:
  - ``parse_delivery``         — STRICT (create/update validation → 422 on typo).
  - ``parse_delivery_lenient`` — NEVER raises (hot path; bad row → defaults).

Per-kind defaults: a ``DestinationRegistration`` may declare ``delivery_defaults``
(e.g. jsonl → concurrency=1, splunk_hec → concurrency=8). They are **deep-merged**
UNDER the user's delivery (user wins), then validated. An empty ``{}`` (every
existing row, incl. ``wazuh-default``) yields the model defaults — byte-identical.
"""

from __future__ import annotations

from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator


# ── Sub-schemas ────────────────────────────────────────────────────────


class BreakerConfig(BaseModel):
    """Circuit-breaker thresholds. Single source of truth for the breaker
    defaults — ``circuit_breaker._breaker_cfg`` reads these."""

    model_config = ConfigDict(extra="forbid")

    failure_threshold: int = Field(default=5, ge=1, le=1000)
    cooldown_s: int = Field(default=30, ge=1, le=3600)
    window_s: int = Field(default=60, ge=1, le=86400)


class BatchConfig(BaseModel):
    """Batch sizing — max_items is ENFORCED by the dispatcher.

    ``flush_ms`` is documented but the dispatcher has no temporal aggregation
    today (it flushes when Celery delivers a task); kept for future wiring.
    """

    model_config = ConfigDict(extra="forbid")

    max_items: int = Field(default=500, ge=1, le=100000)
    #: fecha o chunk também por TAMANHO (bytes do payload serializado):
    #: um chunk fecha ao atingir ``max_items`` OU ``max_bytes`` (o que vier
    #: primeiro), evitando lotes gigantes que estouram o limite de payload do
    #: sink (ex.: HEC, OS_MAXSTR do Wazuh). ENFORCED pelo dispatcher.
    #: 0 = sem teto de bytes (só ``max_items``). Default 1 MiB.
    max_bytes: int = Field(default=1_048_576, ge=0, le=104_857_600)
    flush_ms: int = Field(default=1000, ge=10, le=600000)


class RetryConfig(BaseModel):
    """Exponential-backoff retry policy — ENFORCED by the dispatcher.

    Wait between attempt *n* (0-indexed) = min(max_ms, initial_ms * multiplier**n).

    Back-compat: legacy ``backoff_max_s`` (int, seconds) is accepted and migrated
    to ``max_ms`` automatically.  If both are present, ``max_ms`` wins.
    """

    model_config = ConfigDict(extra="forbid")

    max_retries: int = Field(default=3, ge=0, le=100)
    #: kill-switch do retry. ``False`` → entrega uma única vez; uma
    #: falha transitória vai direto à DLQ / propaga (sem backoff). ENFORCED.
    enabled: bool = True
    initial_ms: int = Field(default=200, ge=10, le=60000)
    max_ms: int = Field(default=5000, ge=10, le=600000)
    multiplier: float = Field(default=2.0, ge=1.0, le=10.0)
    #: teto de tempo TOTAL gasto re-tentando UM chunk (ms). Ao exceder,
    #: para mesmo que ``max_retries`` não tenha esgotado (evita martelar um sink
    #: degradado indefinidamente). ENFORCED pelo dispatcher. 0 = sem teto.
    max_elapsed_ms: int = Field(default=300_000, ge=0, le=3_600_000)

    @model_validator(mode="before")
    @classmethod
    def _migrate_backoff_max_s(cls, data: Any) -> Any:
        """Accept old ``backoff_max_s`` (seconds) and translate to ``max_ms``
        when ``max_ms`` is not explicitly set.  Drops the legacy key so
        ``extra='forbid'`` does not reject it.
        """
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if "backoff_max_s" in data:
            legacy_s = data.pop("backoff_max_s")
            # Only apply when the caller did NOT also set max_ms explicitly.
            if "max_ms" not in data and isinstance(legacy_s, (int, float)):
                data["max_ms"] = int(legacy_s * 1000)
        return data


def backoff_delay_s(retry: RetryConfig, attempt: int) -> float:
    """Segundos a esperar antes do retry ``attempt`` (0-indexed).

    Função de MÓDULO (não método do modelo) de propósito: sob compilação Cython
    untyped, um método em ``BaseModel`` vira ``cyfunction``, que o pydantic v2
    confunde com um campo não-anotado (PydanticUserError em tempo de import).
    Mantê-la fora da classe é Cython-safe. ``attempt=0`` → primeiro retry.
    Retorna float para alimentar ``asyncio.sleep`` direto.
    """
    ms = min(retry.max_ms, retry.initial_ms * (retry.multiplier ** attempt))
    return ms / 1000.0


#: load-shedding policy. ``persistent_queue`` = current behaviour (broker
#: durability, no shedding). ``drop_newest`` = shed at the ceiling. ``block`` =
#: reserved (pause-collection semantics; treated as persistent_queue until wired).
BackpressurePolicy = Literal["block", "drop_newest", "persistent_queue"]


class CostConfig(BaseModel):
    """FinOps por destino (open-core split). O ``cost_per_gb`` é uma config
    COMMUNITY (o operador/partner edita por-destino/por-org, é só um número), MAS a
    TRADUÇÃO de bytes→US$ é Enterprise (seam ``ee_hooks.cost_pricer``): o core Community
    valida e guarda o preço, o pacote EE é quem calcula savings-em-US$/ROI. Default 0.0 ⇒
    o volume/razão continua computando; só não há tradução em $ (degradação graciosa)."""

    model_config = ConfigDict(extra="forbid")

    #: US$/GB LÓGICO ingerido (pré-compressão) — casa com o faturamento do SIEM
    #: (Sentinel/Elastic faturam volume lógico). 0 = sem preço (só volume, sem $).
    cost_per_gb: float = Field(default=0.0, ge=0.0, le=100000.0)
    currency: str = Field(default="USD", min_length=3, max_length=3)
    #: rótulo livre p/ a UI (ex.: "Sentinel Analytics", "S3 barato"). Só apresentação.
    tier_label: str = Field(default="", max_length=64)


class AggregateConfig(BaseModel):
    """Agregação/rollup log→métrica POR DESTINO (opt-in).

    ``group_by`` VAZIO (default) = desligado (byte-idêntico). Não-vazio = os eventos
    entregues a ESTE destino são colapsados por esses labels do ``_centralops`` em 1
    metric-event por grupo. Ligue só onde a granularidade por-evento não importa — a
    cópia full-fidelity vai a OUTRO destino (lago) por rota separada, então detecção
    nunca é agregada. ``max_groups`` é o teto anti-OOM: cardinalidade acima
    dele faz o lote passar INTACTO (fail-open)."""

    model_config = ConfigDict(extra="forbid")

    group_by: list[str] = Field(default_factory=list)
    max_groups: int = Field(default=1000, ge=1, le=1_000_000)


class DeliveryConfig(BaseModel):
    """The full validated delivery policy for one destination.

    ``extra="forbid"`` → a typo'd key (e.g. ``concurency``) is a 422 at
    create-time, not a silently-ignored field at 3 a.m.
    """

    model_config = ConfigDict(extra="forbid")

    batch: BatchConfig = Field(default_factory=BatchConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    breaker: BreakerConfig = Field(default_factory=BreakerConfig)

    #: per-send timeout, ms — ENFORCED by the dispatcher via asyncio.wait_for.
    #: Timeout → retryable (counted against max_retries).
    timeout_ms: int = Field(default=30000, ge=100, le=600000)

    #: max concurrent ``send_batch`` for THIS destination, PER worker
    #: process (prefork). NOT a global cap (see ``destination_limiter``).
    concurrency: int = Field(default=4, ge=1, le=256)

    #: load-shedding policy.
    backpressure: BackpressurePolicy = "persistent_queue"

    #: per-destination broker queue-depth ceiling (0 = unlimited / disabled).
    queue_ceiling: int = Field(default=0, ge=0, le=10_000_000)

    #: shadow mode: run format+validate+metrics but skip the actual send.
    shadow: bool = False

    #: tiering hot/cold por destino. **METADADO apenas**:
    #: descreve a intenção (SIEM quente vs. lago frio) e auto-aparece no catálogo
    #: (describe()['delivery_schema']) p/ a UI, mas NÃO move dado nem aplica
    #: retenção ainda (a execução de tiered storage não está implementada). Default
    #: 'hot' = comportamento atual.
    tier: Literal["hot", "cold"] = "hot"

    #: retenção desejada no destino, em dias (0 = ilimitado/não
    #: especificado). METADADO apenas: NÃO apaga nada ainda — a enforcement de
    #: retenção por destino não está implementada. Surge no catálogo p/ a UI.
    retention_days: int = Field(default=0, ge=0, le=36500)

    #: FinOps por destino: US$/GB p/ traduzir volume→$ (o cálculo em $ é
    #: EE, seam ee_hooks.cost_pricer). Community valida/guarda; default 0 = sem preço.
    cost: CostConfig = Field(default_factory=CostConfig)

    #: agregação log→métrica por destino. group_by vazio (default) =
    #: desligado (byte-idêntico). Ligue só em destinos onde detecção NÃO se apoia.
    aggregate: AggregateConfig = Field(default_factory=AggregateConfig)


# ── Merge + parse helpers ──────────────────────────────────────────────


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict:
    """Recursively merge ``override`` over ``base`` (override wins). Nested
    dicts merge; scalars/lists replace. Neither input is mutated."""
    out: dict = dict(base or {})
    for key, val in (override or {}).items():
        if isinstance(val, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def _kind_defaults(kind: str) -> dict:
    """``delivery_defaults`` declared by the kind's registration (or {})."""
    try:
        from .destinations import registry as _registry

        reg = _registry.get(kind)
        return dict(getattr(reg, "delivery_defaults", {}) or {})
    except Exception:
        return {}


def parse_delivery(kind: str, raw_delivery: Mapping[str, Any] | None) -> DeliveryConfig:
    """STRICT parse for create/update. Deep-merges the kind's delivery_defaults
    under the user's delivery, then validates. Raises ``ValidationError`` on
    invalid/unknown keys so the API returns 422 with detail."""
    merged = deep_merge(_kind_defaults(kind), dict(raw_delivery or {}))
    return DeliveryConfig(**merged)


def parse_delivery_lenient(
    kind: str, raw_delivery: Mapping[str, Any] | None
) -> DeliveryConfig:
    """HOT-PATH parse — NEVER raises. A malformed row (which create-validation
    should have prevented) falls back to kind/model defaults so dispatch never
    crashes on a bad delivery blob."""
    try:
        return parse_delivery(kind, raw_delivery)
    except Exception:
        try:
            return DeliveryConfig(**_kind_defaults(kind))
        except Exception:
            return DeliveryConfig()
