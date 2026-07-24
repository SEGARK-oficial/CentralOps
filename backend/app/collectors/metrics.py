"""Métricas do subsistema de collectors — OTel-native.

**Instrumentação 100% OpenTelemetry.** Cada nome público aqui é uma FACHADA com
a API ergonômica ``.labels(**kw).inc()/observe()/set()`` sobre um instrumento
OTel criado em :mod:`otel_metrics` (a fonte única do catálogo). Export via
**OTLP-PUSH** (Superfície B / ops) — sem ``prometheus_client``, sem
``start_http_server`` por processo, sem ``PROMETHEUS_MULTIPROC_DIR``.

Quando ``OTEL_ENABLED`` está off (default) os emits são no-op de custo
desprezível. A UI do cliente (Superfície A) NÃO depende destes instrumentos —
é servida pelo ``observability_store`` (Redis).

Um endpoint Prometheus ``/metrics`` de compatibilidade, quando desejado, é
exposto pelo **OTel Collector** (exporter ``prometheus``) a jusante — ver
``compose/otel-collector-config.yaml`` e ``docs/observability``. Os nomes de
série são mantidos idênticos aos antigos para que os dashboards/alertas
herdados sigam válidos após a conversão OTLP→Prometheus.
"""

from __future__ import annotations

import time as _time
from contextlib import contextmanager
from typing import Any, Iterator, Literal, Tuple

from . import otel_metrics


class _Timer:
    """Context manager devolvido por ``_Bound.time()`` — mede o bloco e registra
    a duração (segundos) no histograma. Espelha ``prometheus_client``'s
    ``Histogram.time()`` (usado com ``with TASK_DURATION.labels(...).time():``)."""

    __slots__ = ("_bound", "_start")

    def __init__(self, bound: "_Bound") -> None:
        self._bound = bound
        self._start = 0.0

    def __enter__(self) -> "_Timer":
        self._start = _time.perf_counter()
        return self

    def __exit__(self, *exc: Any) -> Literal[False]:
        self._bound.observe(_time.perf_counter() - self._start)
        return False  # nunca suprime exceção


class _Bound:
    """Instrumento já com labels resolvidos (saída de ``.labels(...)``).
    Empurra para o OTel; no-op quando o export está off."""

    __slots__ = ("_name", "_attrs")

    def __init__(self, name: str, attrs: dict) -> None:
        self._name = name
        self._attrs = attrs

    def inc(self, amount: float = 1) -> None:
        otel_metrics.count(self._name, amount, self._attrs)

    def observe(self, amount: float) -> None:
        otel_metrics.record(self._name, amount, self._attrs)

    def set(self, amount: float) -> None:
        otel_metrics.set_gauge(self._name, amount, self._attrs)

    def time(self) -> _Timer:
        """Cronometra o bloco e ``observe()`` a duração (compat Histogram.time)."""
        return _Timer(self)


class _Instrument:
    """Fachada de um instrumento OTel preservando a API ``.labels().inc/observe/set``
    (estilo prometheus_client) usada pelos call sites — assim a migração para
    OTel-native não tocou nenhum dos ~29 pontos de emissão."""

    __slots__ = ("_name", "_labelnames")

    def __init__(self, name: str) -> None:
        self._name = name
        self._labelnames: Tuple[str, ...] = otel_metrics.labels_for(name)

    def labels(self, *args: Any, **kwargs: Any) -> _Bound:
        if kwargs:
            attrs = {k: str(v) for k, v in kwargs.items()}
        else:
            # Fail-fast em nº errado de labels posicionais (igual ao
            # prometheus_client) — sem isso, ``zip`` truncaria silenciosamente e
            # a série OTel sairia com label faltando (ex.: destination_id ausente).
            if len(args) != len(self._labelnames):
                raise ValueError(
                    f"labels() de '{self._name}': esperados {len(self._labelnames)} "
                    f"labels {self._labelnames}, recebidos {len(args)} posicionais"
                )
            attrs = {k: str(v) for k, v in zip(self._labelnames, args)}
        return _Bound(self._name, attrs)

    # Instrumentos sem labels — mantém a API completa.
    def inc(self, amount: float = 1) -> None:
        otel_metrics.count(self._name, amount, {})

    def observe(self, amount: float) -> None:
        otel_metrics.record(self._name, amount, {})

    def set(self, amount: float) -> None:
        otel_metrics.set_gauge(self._name, amount, {})

    def time(self) -> _Timer:
        return _Timer(_Bound(self._name, {}))


def _instrument(name: str) -> _Instrument:
    return _Instrument(name)


# ─── Fachadas públicas (nome Python → série OTel) ────────────────────────────
# Catálogo autoritativo (kind/unit/labels/buckets) vive em otel_metrics._SPEC.

# coleta / normalização / vendor
EVENTS_TOTAL = _instrument("collector_events_total")
# metering de volume/custo IN (no-op quando COST_METERING_ENABLED off).
EVENTS_IN = _instrument("collector_events_in_total")
BYTES_IN = _instrument("collector_bytes_in_total")
API_LATENCY = _instrument("collector_api_latency_seconds")
OAUTH_EXPIRES = _instrument("collector_oauth_token_expires_in_seconds")
CURSOR_LAG = _instrument("collector_cursor_lag_seconds")
# Atraso REAL da coleta: ``agora − watermark``, onde watermark é até onde o cursor
# consumiu na linha do tempo do FORNECEDOR. Diferente de ``last_success_at``, que é
# reescrito a cada ciclo bem-sucedido mesmo processando o dia anterior — e por isso
# marcava 0 num coletor 15h atrasado (incidente jul/2026).
WATERMARK_LAG = _instrument("collector_watermark_lag_seconds")
# Ciclo pulado porque o anterior do MESMO (integração, stream) ainda rodava.
# Subir de forma sustentada = a cadência do stream está menor que a duração do
# ciclo, ou seja, há backlog e o coletor não está dando conta.
COLLECT_SKIPPED_LOCKED = _instrument("collector_cycles_skipped_locked_total")
TASK_DURATION = _instrument("collector_task_duration_seconds")
RATE_LIMIT_BACKOFFS = _instrument("collector_rate_limit_backoffs_total")
DEDUPE_DROPS = _instrument("collector_dedupe_drops_total")
# saúde do Redis do dedupe (evicção silenciosa / pressão de memória).
# Amostrado periodicamente por state.dedupe.sample_redis_health — NÃO no hot
# path de claim().
DEDUPE_REDIS_EVICTED_KEYS = _instrument("collector_dedupe_redis_evicted_keys")
DEDUPE_REDIS_MEMORY_USED_RATIO = _instrument("collector_dedupe_redis_memory_used_ratio")
QUARANTINE_TOTAL = _instrument("collector_quarantine_total")
# classificação em voo (ADR-0015 Fase 1). Emitidas 1x por CICLO
# (carga e flush), nunca por evento — R1.
INFLIGHT_RULES_LOADED = _instrument("collector_inflight_rules_loaded")
INFLIGHT_RULES_REJECTED = _instrument("collector_inflight_rules_rejected_total")
INFLIGHT_MATCHES = _instrument("collector_inflight_matches_total")
INFLIGHT_ERRORS = _instrument("collector_inflight_errors_total")
NORMALIZE_LATENCY = _instrument("collector_normalize_latency_seconds")
# conformidade OCSF (tag-and-pass). reason ∈ validator.OCSF_REASONS.
OCSF_VALID = _instrument("collector_ocsf_valid_total")
OCSF_INVALID = _instrument("collector_ocsf_invalid_total")
OCSF_VALIDATE_LATENCY = _instrument("collector_ocsf_validate_latency_seconds")

# capability invocations (observabilidade por vendor/capability)
CAPABILITY_INVOCATIONS = _instrument("collector_capability_invocations_total")
CAPABILITY_LATENCY = _instrument("collector_capability_latency_seconds")

# entrega / dispatch / roteamento (Superfície B core)
DISPATCH_FAILURES = _instrument("collector_dispatch_failures_total")
EVENTS_SENT = _instrument("collector_events_sent_total")
EVENTS_REJECTED = _instrument("collector_events_rejected_total")
BYTES_SENT = _instrument("collector_bytes_sent_total")
DELIVERY_LATENCY = _instrument("collector_delivery_latency_seconds")
RETRIES = _instrument("collector_retries_total")
DLQ_TOTAL = _instrument("collector_dlq_total")
ROUTING_DECISIONS = _instrument("collector_routing_decisions_total")
ROUTE_EVENTS = _instrument("collector_route_events_total")
# eventos reduzidos por uma alavanca (sample/suppress/drop) por rota+razão.
EVENTS_DROPPED = _instrument("collector_events_dropped_total")
# bytes lógicos evitados por uma alavanca, por destino+razão (o $ é EE).
BYTES_SAVED = _instrument("collector_bytes_saved_total")
# eventos suprimidos por assinatura (rate-limit por rota).
SUPPRESSED = _instrument("collector_suppressed_total")
SHADOW_EVENTS = _instrument("collector_shadow_events_total")
SHADOW_LATENCY = _instrument("collector_shadow_format_latency_seconds")
DISPATCH_SHED_TOTAL = _instrument("collector_dispatch_shed_total")
QUEUE_DEPTH = _instrument("collector_dispatch_queue_depth")
BREAKER_STATE = _instrument("collector_destination_breaker_state")
# Ingestão push.
INGEST_ACCEPTED = _instrument("collector_ingest_accepted_total")
INGEST_DROPPED = _instrument("collector_ingest_dropped_total")
INGEST_BUFFER_DEPTH = _instrument("collector_ingest_buffer_depth")
# Eventos rejeitados no parse (NDJSON malformado / não-objeto / acima do teto por-evento).
INGEST_MALFORMED = _instrument("collector_ingest_malformed_total")
# Data-plane Kafka.
DATAPLANE_PRODUCED = _instrument("collector_dataplane_produced_total")
DATAPLANE_PRODUCE_LATENCY = _instrument("collector_dataplane_produce_latency_seconds")
DATAPLANE_CONSUMED = _instrument("collector_dataplane_consumed_total")
DATAPLANE_CONSUME_LATENCY = _instrument("collector_dataplane_consume_latency_seconds")
DATAPLANE_CONSUMER_LAG = _instrument("collector_dataplane_consumer_lag")


@contextmanager
def observe_capability(vendor: str, capability: str) -> Iterator[None]:
    """Instrumenta uma invocação de capability.

    Emite ``collector_capability_invocations_total{vendor,capability,outcome}`` +
    ``collector_capability_latency_seconds{vendor,capability}``. ``outcome`` é
    ``"ok"`` ou ``"error"`` (a exceção é re-levantada). Uso::

        with observe_capability("sophos", "collect:alerts"):
            result = collector.collect()
    """
    started = _time.monotonic()
    outcome = "ok"
    try:
        yield
    except Exception:
        outcome = "error"
        raise
    finally:
        CAPABILITY_LATENCY.labels(vendor=vendor, capability=capability).observe(
            _time.monotonic() - started
        )
        CAPABILITY_INVOCATIONS.labels(
            vendor=vendor, capability=capability, outcome=outcome
        ).inc()


# ─── Validação import-time: bijeção fachadas ↔ otel_metrics._SPEC ────────────
# Pega typo em ``_instrument("collector_...")`` (que sairia silenciosamente como
# métrica perdida — nome fora do _SPEC ⇒ sem instrumento ⇒ emit no-op) JÁ NO
# IMPORT, não só no teste. App não sobe com fachada dessincronizada.
_facade_names = {
    _v._name for _v in list(globals().values()) if isinstance(_v, _Instrument)
}
# Instrumentos OBSERVÁVEIS (ex.: ``collector_up``) não têm fachada — são
# auto-coletados por callback, sem ``.labels().set()``. Ficam no catálogo _SPEC
# (fonte única) mas FORA da bijeção, que protege só os instrumentos síncronos.
_spec_facade_names = {
    _n for _n, _s in otel_metrics._SPEC.items() if _s["kind"] != "observable_gauge"
}
if _facade_names != _spec_facade_names:
    raise RuntimeError(
        "metrics.py dessincronizado de otel_metrics._SPEC — "
        f"fachada sem _SPEC (typo?): {sorted(_facade_names - _spec_facade_names)}; "
        f"_SPEC síncrono sem fachada: {sorted(_spec_facade_names - _facade_names)}"
    )
del _facade_names, _spec_facade_names
