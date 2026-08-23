"""Superfície B (ops): MÉTRICAS OTel-native via OTLP-PUSH.

**Fonte ÚNICA dos instrumentos de métrica do subsistema de collectors.** O
módulo ``metrics.py`` expõe fachadas com a API ``.labels(**kw).inc/observe/set``
sobre os instrumentos criados aqui — não há mais ``prometheus_client`` (a
instrumentação é 100% OTel; um endpoint Prometheus de compat, se desejado, é
servido pelo OTel Collector, não por processo).

Monta ``MeterProvider`` + ``PeriodicExportingMetricReader`` + ``OTLPMetricExporter``
(HTTP), inicializado por filho prefork no ``worker_process_init`` (como o
tracing). Gated por ``OTEL_ENABLED`` (default OFF ⇒ no-op total, zero overhead).
Degrada para no-op se os pacotes ``opentelemetry-*`` não estiverem instalados.

**PUSH (não pull):** cada processo filho EMPURRA suas métricas a cada
``OTEL_METRIC_EXPORT_INTERVAL_MS`` (default 60s) — evita a complexidade do
``PROMETHEUS_MULTIPROC_DIR`` (cada filho exporta o seu, distinto por
``service.instance.id``) e funciona através de NAT/egress-only, viabilizando o
híbrido SaaS/self-hosted. Vendor-neutro: aponte ``OTEL_EXPORTER_OTLP_ENDPOINT``
para um OTel Collector / Grafana / Datadog / Zabbix.

**Esta é a Superfície B (ops/SRE).** NÃO é a Superfície A (UI do cliente), que é
servida pelo ``observability_store`` (Redis), desacoplada.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from . import otel_common

logger = logging.getLogger(__name__)

# Estado de processo. Só vira ``True`` quando init_metrics monta o SDK com sucesso
# E a flag está ligada. Enquanto ``False``, todo emit é no-op.
_ENABLED: bool = False
_INITIALIZED: bool = False
_meter: Any = None
_instruments: Dict[str, Any] = {}

# ─── Catálogo autoritativo dos instrumentos (Superfície B) ──────────────────
#
# name → {kind, unit, labels, [buckets]}. ``metrics.py`` constrói uma fachada
# pública por entrada (a mesma série/nome que o SRE espera nos dashboards). Os
# nomes são mantidos IDÊNTICOS aos antigos do prometheus_client para que, na
# conversão OTLP→Prometheus feita pelo Collector, os dashboards/recording-rules/
# alertas herdados sigam válidos. ``event_id`` NUNCA é label (cardinalidade).
#
# unit: OTel UCUM — "1" (adimensional), "s" (segundos), "By" (bytes).
_SPEC: Dict[str, Dict[str, Any]] = {
    # coleta / normalização / vendor
    "collector_events_total": {"kind": "counter", "unit": "1", "labels": ("vendor", "tenant", "stream")},
    # volume/custo IN (lado da ingestão). Labels MÍNIMAS (org+
    # integration) p/ limitar cardinalidade no OTLP (que não tem TTL); o breakdown
    # rico por vendor/stream vive no observability_store (Redis, TTL 3h). No-op quando
    # COST_METERING_ENABLED=False. O lado OUT reusa collector_{events,bytes}_sent_total.
    "collector_events_in_total": {"kind": "counter", "unit": "1", "labels": ("org_id", "integration_id")},
    "collector_bytes_in_total": {"kind": "counter", "unit": "By", "labels": ("org_id", "integration_id")},
    "collector_api_latency_seconds": {"kind": "histogram", "unit": "s", "labels": ("vendor", "stream"), "buckets": (0.1, 0.25, 0.5, 1, 2, 5, 10, 30)},
    "collector_oauth_token_expires_in_seconds": {"kind": "gauge", "unit": "s", "labels": ("integration_id", "vendor")},
    "collector_cursor_lag_seconds": {"kind": "gauge", "unit": "s", "labels": ("integration_id", "stream")},
    # Atraso REAL: agora − watermark (até onde o cursor consumiu na linha do tempo
    # do FORNECEDOR). Não confundir com cursor_lag, que é derivado de last_success_at
    # e marca 0 mesmo num coletor horas atrasado.
    "collector_watermark_lag_seconds": {"kind": "gauge", "unit": "s", "labels": ("integration_id", "stream")},
    # Ciclo pulado porque o anterior do mesmo (integração, stream) ainda rodava.
    "collector_cycles_skipped_locked_total": {"kind": "counter", "unit": "1", "labels": ("integration_id", "stream")},
    "collector_task_duration_seconds": {"kind": "histogram", "unit": "s", "labels": ("stream", "queue"), "buckets": (1, 5, 15, 60, 300, 900)},
    "collector_rate_limit_backoffs_total": {"kind": "counter", "unit": "1", "labels": ("vendor",)},
    "collector_dedupe_drops_total": {"kind": "counter", "unit": "1", "labels": ("vendor", "stream")},
    # Saúde do Redis do dedupe — visibilidade de EVICÇÃO SILENCIOSA (chave
    # ``dedupe:*`` some antes do TTL lógico sob memory pressure ⇒ reentrega vira
    # "evento novo" sem erro nenhum). Amostrado periodicamente (fora do hot
    # path de claim(), ver ``state.dedupe.sample_redis_health``), não por
    # evento. ``evicted_keys`` é o contador CRU do Redis (INFO stats) — LastValue
    # por processo/instância; suba se subir (não deveria, se maxmemory/TTL
    # estiverem calibrados). ``memory_used_ratio`` = used_memory/maxmemory
    # (0 quando maxmemory=0, i.e. sem teto configurado) — alerta ANTES da
    # evicção começar.
    "collector_dedupe_redis_evicted_keys": {"kind": "gauge", "unit": "1", "labels": ()},
    "collector_dedupe_redis_memory_used_ratio": {"kind": "gauge", "unit": "1", "labels": ()},
    "collector_inflight_rules_loaded": {
        "kind": "gauge", "unit": "1", "labels": ("org_id",)},
    "collector_inflight_rules_rejected_total": {
        "kind": "counter", "unit": "1", "labels": ("reason",)},
    "collector_inflight_matches_total": {
        "kind": "counter", "unit": "1", "labels": ("rule_id",)},
    "collector_inflight_errors_total": {
        "kind": "counter", "unit": "1", "labels": ("reason",)},
    # Escrita do contador POR REGRA no observability_store que se PERDEU (Redis
    # fora, chave com tipo errado, ...). Existe porque essa perda é invisível de
    # todo outro ângulo: a chave simplesmente não nasce, e do lado da leitura
    # ausência de chave é indistinguível de "a regra não disparou" — nem o
    # ``read_window_total_strict`` (que só sabe distinguir falha DE LEITURA)
    # alcança. Sem esta série, o operador vê "0 disparos" para uma regra que
    # disparou, sem nenhum sinal de que o número não vale.
    #
    # ``metric`` (∈ {matches, overflow, err_<reason∈ERROR_REASONS>}) e NÃO
    # ``rule_id``: rule_id é id global, multiplicaria a cardinalidade e o OTLP
    # não tem TTL para envelhecer a série de uma regra apagada — a mesma recusa
    # já escrita para ``collector_inflight_errors_total``. ``metric`` é enum
    # FECHADO derivado de constantes do módulo (nunca valor de evento) e é o
    # eixo que responde a pergunta de triagem: todas as famílias subindo juntas
    # = o store inteiro caiu; UMA só subindo = uma chave específica quebrada.
    "collector_inflight_rule_metric_write_failures_total": {
        "kind": "counter", "unit": "1", "labels": ("metric",)},
    # Eventos OCSF 2004 (Detection Finding) que a classificação em voo entregou
    # ao roteamento. SEM labels: ``rule_id`` é id global (a mesma recusa já
    # escrita para ``collector_inflight_errors_total``) e ``org_id`` daria uma
    # série por tenant numa métrica que o operador lê como total do worker. O
    # breakdown por regra vive no observability_store, que tem TTL.
    "collector_inflight_events_emitted_total": {
        "kind": "counter", "unit": "1", "labels": ()},
    # A Detection foi gravada e o evento NÃO saiu — de propósito, não por falha
    # (falha é ``collector_inflight_errors_total{reason="emit_failed"}``). É a
    # série que responde "por que meu SIEM recebeu 1 alerta se a regra casou 40
    # vezes?": ``suppressed`` = a janela de supressão da regra agrupou o match
    # numa Detection que já existia; ``loop_guard`` = o evento que casou já era
    # um evento de detecção emitido por este produto; ``cycle_cap`` = teto de
    # emissão por ciclo. ``reason`` é enum FECHADO (``EMIT_SKIP_REASONS``) e
    # jamais interpola valor de evento.
    "collector_inflight_events_not_emitted_total": {
        "kind": "counter", "unit": "1", "labels": ("reason",)},
    # Duração do FLUSH das Detections (a escrita, não o ciclo inteiro). 1x por
    # ciclo, por org. Existe porque o teto global
    # (``INFLIGHT_MAX_DETECTIONS_PER_FLUSH``) só avisa DEPOIS de morder: sem
    # esta série o operador não tem como ver que está se aproximando do
    # penhasco — ``record`` commita por chave, então o custo cresce LINEAR com
    # a cardinalidade do group_by e some dentro da duração da task, que é
    # dominada pela coleta. SEM LABELS: ``org_id`` daria uma série por tenant
    # numa métrica que o operador lê como saúde do worker, e ``rule_id`` é id
    # global (a mesma recusa já escrita para
    # ``collector_inflight_errors_total``). Buckets na escala de round-trip de
    # Postgres em série: sub-10 ms é um flush de poucas chaves, >5 s já é o
    # regime em que o teto começa a fazer sentido.
    "collector_inflight_flush_seconds": {
        "kind": "histogram", "unit": "s", "labels": (),
        "buckets": (0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 30)},
    "collector_quarantine_total": {"kind": "counter", "unit": "1", "labels": ("vendor", "event_type", "error_kind")},
    "collector_normalize_latency_seconds": {"kind": "histogram", "unit": "s", "labels": ("vendor", "event_type"), "buckets": (0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1)},
    # conformidade OCSF (tag-and-pass). ``reason`` é enum FECHADO
    # (validator.OCSF_REASONS) — jamais interpola valor do evento (anti-PII + baixa
    # cardinalidade). valid + invalid (excluindo reason=out_of_scope) = taxa de conformidade.
    "collector_ocsf_valid_total": {"kind": "counter", "unit": "1", "labels": ("vendor", "event_type")},
    "collector_ocsf_invalid_total": {"kind": "counter", "unit": "1", "labels": ("vendor", "event_type", "reason")},
    "collector_ocsf_validate_latency_seconds": {"kind": "histogram", "unit": "s", "labels": ("vendor",), "buckets": (0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1)},
    # observabilidade IN-CODE por (vendor, capability). Toda
    # invocação de capability (run_query/collect/block_*) emite estas séries.
    "collector_capability_invocations_total": {"kind": "counter", "unit": "1", "labels": ("vendor", "capability", "outcome")},
    "collector_capability_latency_seconds": {"kind": "histogram", "unit": "s", "labels": ("vendor", "capability"), "buckets": (0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30)},
    # entrega / dispatch / roteamento (Superfície B core)
    "collector_dispatch_failures_total": {"kind": "counter", "unit": "1", "labels": ("target", "reason", "destination_id")},
    "collector_events_sent_total": {"kind": "counter", "unit": "1", "labels": ("destination_id", "kind")},
    "collector_events_rejected_total": {"kind": "counter", "unit": "1", "labels": ("destination_id", "kind", "error_kind")},
    "collector_bytes_sent_total": {"kind": "counter", "unit": "By", "labels": ("destination_id", "kind")},
    "collector_delivery_latency_seconds": {"kind": "histogram", "unit": "s", "labels": ("destination_id", "kind"), "buckets": (0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30)},
    "collector_retries_total": {"kind": "counter", "unit": "1", "labels": ("destination_id", "kind")},
    "collector_dlq_total": {"kind": "counter", "unit": "1", "labels": ("destination_id", "kind", "error_kind")},
    "collector_routing_decisions_total": {"kind": "counter", "unit": "1", "labels": ("outcome",)},
    "collector_route_events_total": {"kind": "counter", "unit": "1", "labels": ("route_id", "action")},
    # eventos reduzidos por uma alavanca (sampling/suppress/drop). ``reason``
    # ∈ {sample, suppress, drop}. Distinto de collector_dedupe_drops (idempotência) e de
    # collector_route_events{action=drop} (rota action=drop explícita do operador).
    "collector_events_dropped_total": {"kind": "counter", "unit": "1", "labels": ("route_id", "reason")},
    # bytes LÓGICOS evitados por uma alavanca de redução. ``reason`` ∈
    # {trim, sample, suppress, drop, aggregate, redaction}. Community mede o volume;
    # a tradução em US$ é EE (seam ee_hooks.cost_pricer). ``destination_id`` = "-"
    # quando pré-fan-out (trim no normalize, ainda sem destino).
    "collector_bytes_saved_total": {"kind": "counter", "unit": "By", "labels": ("destination_id", "reason")},
    # ── Enriquecimento (ADR-LOCAL-0002) ────────────────────────────────────
    # Contraparte de bytes_saved: sem ela, um estágio que ACRESCENTE volume
    # derruba a % de redução sem nenhum termo que explique de onde veio.
    "collector_bytes_added_total": {"kind": "counter", "unit": "By", "labels": ("destination_id", "reason")},
    # Eventos que passaram pelo aplicador e receberam ao menos um campo.
    "collector_enrich_events_total": {"kind": "counter", "unit": "1", "labels": ("org_id", "rule_id")},
    # Desfecho por regra. ``outcome`` ∈ {hit, miss, skipped, error, degraded}.
    # É a série que responde "meu enriquecimento está funcionando?" — um
    # miss_rate de 100% é indistinguível de "regra nunca avaliada" sem ela.
    "collector_enrich_lookups_total": {"kind": "counter", "unit": "1", "labels": ("enricher", "outcome")},
    # Cache: ``layer`` ∈ {l1, l2}. hit/miss separados por outcome acima seria
    # ambíguo (miss de cache ≠ miss de lookup), por isso série própria.
    "collector_enrich_cache_total": {"kind": "counter", "unit": "1", "labels": ("enricher", "layer", "result")},
    # Latência da RESOLUÇÃO (I/O), não da aplicação. A aplicação é pura e
    # medida em benchmark, não em produção — instrumentá-la custaria mais que
    # ela própria.
    "collector_enrich_resolve_seconds": {
        "kind": "histogram", "unit": "s", "labels": ("enricher",),
        "buckets": (0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
    },
    # Erros com causa. ``reason`` ∈ {timeout, http, auth, rate_limit, budget,
    # circuit_open, parse}. ``budget`` é o desabilitador de runtime que protege
    # o laço de um enricher "local" que mentiu sobre não fazer I/O.
    "collector_enrich_errors_total": {"kind": "counter", "unit": "1", "labels": ("enricher", "reason")},
    # Bytes residentes das tabelas carregadas, por enricher. É a métrica que o
    # HPA NÃO enxerga (escala só por CPU) — logo é a única forma de ver a
    # pressão de memória antes do cgroup-OOM.
    "collector_enrich_table_bytes": {"kind": "gauge", "unit": "By", "labels": ("enricher", "org_id")},
    "collector_enrich_table_entries": {"kind": "gauge", "unit": "1", "labels": ("enricher", "org_id")},
    # Ciclos em que o orçamento de enriquecimento remoto não coube e o estágio
    # foi desligado para o ciclo INTEIRO (gate binário — nunca parcial).
    "collector_enrich_budget_exhausted_total": {"kind": "counter", "unit": "1", "labels": ("org_id",)},
    # eventos SUPRIMIDOS por assinatura (rate-limit por rota). A 1ª
    # ocorrência da janela passa (preservando detecção); as repetições são suprimidas.
    "collector_suppressed_total": {"kind": "counter", "unit": "1", "labels": ("route_id",)},
    "collector_shadow_events_total": {"kind": "counter", "unit": "1", "labels": ("destination_id", "kind")},
    "collector_shadow_format_latency_seconds": {"kind": "histogram", "unit": "s", "labels": ("destination_id", "kind"), "buckets": (0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 2)},
    # Captura ao vivo: o breaker do tap ABRIU (Redis degradado). SEM labels — o
    # tap é estado de PROCESSO, não por org/sessão, e rotular por org daria
    # cardinalidade ilimitada num contador que só interessa como "está cego?".
    "collector_capture_tap_disabled_total": {"kind": "counter", "unit": "1", "labels": ()},
    "collector_dispatch_shed_total": {"kind": "counter", "unit": "1", "labels": ("destination_id", "reason")},
    "collector_dispatch_queue_depth": {"kind": "gauge", "unit": "1", "labels": ("queue",)},
    "collector_destination_breaker_state": {"kind": "gauge", "unit": "1", "labels": ("destination_id", "kind")},
    # Ingestão push: eventos aceitos no buffer, descartados por
    # backpressure (drop-oldest) e profundidade atual do buffer Redis por
    # (integração, stream). ``dropped`` > 0 = perda silenciosa → alerta no Grafana.
    "collector_ingest_accepted_total": {"kind": "counter", "unit": "1", "labels": ("vendor", "stream")},
    "collector_ingest_dropped_total": {"kind": "counter", "unit": "1", "labels": ("vendor", "stream")},
    "collector_ingest_buffer_depth": {"kind": "gauge", "unit": "1", "labels": ("integration_id", "stream")},
    # Eventos rejeitados no PARSE (antes do buffer): NDJSON malformado, item não-objeto
    # ou evento acima do teto por-evento. ``reason`` = parse|type|oversize. Distinto de
    # ``dropped`` (backpressure) — aqui a borda mandou algo que o servidor não normaliza.
    "collector_ingest_malformed_total": {"kind": "counter", "unit": "1", "labels": ("vendor", "stream", "reason")},
    # data-plane Kafka (produce/consume/lag). ``outcome`` do
    # produce: ok|error; do consume: ok|transient|failed|invalid. ``lag`` (gauge por
    # partição) = highwater − position do consumer group (alerta no Grafana/KEDA).
    "collector_dataplane_produced_total": {"kind": "counter", "unit": "1", "labels": ("destination_id", "outcome")},
    "collector_dataplane_produce_latency_seconds": {"kind": "histogram", "unit": "s", "labels": ("destination_id",), "buckets": (0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5)},
    "collector_dataplane_consumed_total": {"kind": "counter", "unit": "1", "labels": ("outcome",)},
    "collector_dataplane_consume_latency_seconds": {"kind": "histogram", "unit": "s", "labels": (), "buckets": (0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30)},
    "collector_dataplane_consumer_lag": {"kind": "gauge", "unit": "1", "labels": ("partition",)},
    # liveness (Superfície B) — heartbeat OBSERVÁVEL. Vale 1 enquanto o filho
    # prefork está vivo; o ``PeriodicExportingMetricReader`` chama o callback a
    # cada ciclo e RE-EXPORTA 1. Quando o processo morre, a série para de ser
    # empurrada e "envelhece" no Prometheus (staleness ~5min) → dashboards/alertas
    # detectam a queda. Por que push/observável e não scrape `up`: no modelo
    # OTLP-push NÃO existe `up{job=...}` (não há scrape). Distinto por processo
    # via resource (``service.instance.id``); o label ``role`` permite SLO de
    # disponibilidade por papel (worker/beat/dispatcher). É OBSERVÁVEL (sem
    # fachada ``.set()`` em metrics.py) — por isso fica fora da bijeção fachada↔_SPEC.
    "collector_up": {
        "kind": "observable_gauge",
        "unit": "1",
        "labels": ("role",),
        "description": "1 enquanto o worker prefork está vivo (liveness push).",
    },
}


def labels_for(name: str) -> Tuple[str, ...]:
    """Nomes de label declarados para uma série (usado pela fachada)."""
    spec = _SPEC.get(name)
    return tuple(spec["labels"]) if spec else ()


def kind_for(name: str) -> str:
    spec = _SPEC.get(name)
    return spec["kind"] if spec else "counter"


def _build_views() -> list:
    """Views que pinam os buckets dos histogramas aos limites herdados do
    prometheus_client — sem isso o OTel usaria buckets default e os painéis de
    percentil (p50/p95/p99) divergiriam após a conversão OTLP→Prometheus."""
    from opentelemetry.sdk.metrics.view import (
        ExplicitBucketHistogramAggregation,
        View,
    )

    views = []
    for name, spec in _SPEC.items():
        if spec["kind"] == "histogram" and spec.get("buckets"):
            views.append(
                View(
                    instrument_name=name,
                    aggregation=ExplicitBucketHistogramAggregation(
                        list(spec["buckets"])
                    ),
                )
            )
    return views


def _liveness_observations(role: str):
    """Callback do heartbeat ``collector_up`` — fábrica testável isoladamente.
    Retorna um callable que o ``PeriodicExportingMetricReader`` chama a cada
    ciclo; ele observa ``1`` com o label ``role`` (1 série por papel × instância).
    O import de ``Observation`` é tardio (pacote OTel é extra opcional)."""
    from opentelemetry.metrics import Observation

    def _callback(_options):
        yield Observation(1, {"role": role})

    return _callback


def init_metrics() -> bool:
    """Monta o MeterProvider OTLP-push UMA vez por processo. Retorna ``True`` se
    as métricas OTel ficaram ativas, ``False`` caso contrário (flag off, pacotes
    ausentes ou erro). Idempotente: chamadas subsequentes não remontam."""
    global _ENABLED, _INITIALIZED, _meter, _instruments

    if _INITIALIZED:
        return _ENABLED
    _INITIALIZED = True

    if not otel_common.otel_flag():
        return False

    try:
        from opentelemetry import metrics as _metrics
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

        from ..core.config import settings

        interval_ms = int(
            getattr(settings, "OTEL_METRIC_EXPORT_INTERVAL_MS", 60000) or 60000
        )
        endpoint = otel_common.otlp_endpoint_for("metrics")
        # Fail-safe: OTEL_ENABLED=true porém NENHUM endpoint resolvível com scheme.
        # Delegar ao SDK aqui construiria a URL relativa '/v1/metrics' (No scheme
        # supplied) porque o compose/Helm seta OTEL_EXPORTER_OTLP_ENDPOINT vazio →
        # export falho a cada ciclo, poluindo o log. Desliga limpo com 1 warning.
        if not endpoint and not otel_common.sdk_env_endpoint_valid():
            logger.warning(
                "OTEL_ENABLED=true mas nenhum endpoint OTLP com scheme "
                "(OTEL_EXPORTER_OTLP_ENDPOINT vazio/sem http[s]://) — métricas OTel "
                "DESLIGADAS neste processo (evita spam '/v1/metrics: No scheme supplied')"
            )
            _ENABLED = False
            return False
        exporter = (
            OTLPMetricExporter(endpoint=endpoint) if endpoint else OTLPMetricExporter()
        )
        reader = PeriodicExportingMetricReader(
            exporter, export_interval_millis=interval_ms
        )
        provider = MeterProvider(
            resource=otel_common.build_resource(),
            metric_readers=[reader],
            views=_build_views(),
        )
        _metrics.set_meter_provider(provider)
        _meter = _metrics.get_meter("centralops.collectors")

        for name, spec in _SPEC.items():
            kind, unit = spec["kind"], spec["unit"]
            if kind == "counter":
                _instruments[name] = _meter.create_counter(name, unit=unit)
            elif kind == "histogram":
                _instruments[name] = _meter.create_histogram(name, unit=unit)
            elif kind == "gauge":
                # Gauge síncrono OTel = LastValue POR PROCESSO. Em prefork cada
                # filho exporta SUA série (distinta por service.instance.id) —
                # NÃO há agregação cross-worker no SDK (ao contrário do antigo
                # prometheus multiprocess_mode="max"). Para `collector_dispatch_
                # queue_depth` e `collector_destination_breaker_state` o BACKEND
                # deve agregar com MAX por dimensão (ex.: `max by (destination_id,
                # kind) (collector_destination_breaker_state)`) p/ o estado
                # crítico (breaker open=1) de um worker não ser mascarado por
                # outro (closed=0).
                _instruments[name] = _meter.create_gauge(name, unit=unit)
            elif kind == "observable_gauge":
                # Heartbeat de liveness: o callback re-exporta 1 a cada ciclo do
                # reader, segmentado pelo papel do processo. O objeto retornado
                # DEVE ser RETIDO (guardado em _instruments) ou o SDK para de
                # chamar o callback (a coleta usa weakref).
                _instruments[name] = _meter.create_observable_gauge(
                    name,
                    callbacks=[_liveness_observations(otel_common.service_role())],
                    unit=unit,
                    description=spec.get("description", ""),
                )

        _ENABLED = True
        logger.info(
            "OTel metrics ativo (OTLP-push, %d instrumentos, interval=%dms, endpoint=%s)",
            len(_instruments),
            interval_ms,
            endpoint or "<env padrão OTLP>",
        )
        return True
    except Exception:
        logger.warning(
            "OTEL_ENABLED ligado mas métricas OTel indisponíveis (pacotes "
            "opentelemetry-* ausentes?) — métricas de ops desligadas neste processo",
            exc_info=True,
        )
        _ENABLED = False
        return False


def is_enabled() -> bool:
    return _ENABLED


def count(name: str, value: float = 1, attrs: Optional[Dict[str, Any]] = None) -> None:
    """Incrementa um Counter OTel (no-op se inativo). Best-effort: nunca levanta."""
    if not _ENABLED:
        return
    inst = _instruments.get(name)
    if inst is not None:
        try:
            inst.add(value, attributes=attrs or {})
        except Exception:  # pragma: no cover — telemetria nunca afeta o pipeline
            pass


def record(name: str, value: float, attrs: Optional[Dict[str, Any]] = None) -> None:
    """Registra num Histogram OTel (no-op se inativo)."""
    if not _ENABLED:
        return
    inst = _instruments.get(name)
    if inst is not None:
        try:
            inst.record(value, attributes=attrs or {})
        except Exception:  # pragma: no cover
            pass


def set_gauge(name: str, value: float, attrs: Optional[Dict[str, Any]] = None) -> None:
    """Seta um Gauge OTel síncrono (no-op se inativo)."""
    if not _ENABLED:
        return
    inst = _instruments.get(name)
    if inst is not None:
        try:
            inst.set(value, attributes=attrs or {})
        except Exception:  # pragma: no cover
            pass


def reset_for_tests() -> None:
    """Apenas para testes — força re-init no próximo init_metrics."""
    global _ENABLED, _INITIALIZED, _meter, _instruments
    _ENABLED = False
    _INITIALIZED = False
    _meter = None
    _instruments = {}
