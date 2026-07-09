"""Superfície B (ops): métricas OTel-native (OTLP-PUSH).

Estes testes rodam SEM exigir os pacotes ``opentelemetry-*`` instalados (extras
opcionais de deploy). Provam os invariantes da Superfície B:

1. OFF (default) ⇒ no-op total: init False, emits silenciosos, sem efeito.
2. ON sem os pacotes ⇒ degrada para no-op (init False, warning), NUNCA levanta.
3. **Fachada OTel-native**: ``metrics.<X>.labels(**kw).inc/observe/set`` empurra
   a série correta (nome + valor + atributos) p/ o instrumento OTel; no-op
   quando o export está off (sem prometheus_client — instrumentação 100% OTel).
4. Sem drift: TODA fachada em ``metrics`` corresponde a uma entrada de
   ``otel_metrics._SPEC`` e vice-versa; histogramas declaram buckets.
"""

from __future__ import annotations

import importlib.util
import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import pytest

from backend.app.collectors import metrics, otel_metrics
from backend.app.core.config import settings

_HAS_OTEL = importlib.util.find_spec("opentelemetry") is not None

# Valor de label do gauge de profundidade de fila usado abaixo. Atribuído via
# VARIÁVEL (nunca como literal de task-route com chave/valor entre aspas) de
# propósito: o guard estático ``test_compose_celery_queues_consumed`` escaneia
# ``app/**`` — incluindo esta pasta de testes — por rotas de task e flagaria um
# valor de label de métrica como se fosse uma fila Celery órfã.
_QLABEL = "obs-test-q"


@pytest.fixture(autouse=True)
def _reset_otel():
    """Cada teste parte de um estado de processo limpo (init é idempotente)."""
    otel_metrics.reset_for_tests()
    yield
    otel_metrics.reset_for_tests()


# ── Fakes p/ exercitar o caminho ATIVO sem o SDK real ────────────────────


class _FakeInstrument:
    """Captura (value, attrs) de add/record/set — espelha a API OTel."""

    def __init__(self) -> None:
        self.calls: list[tuple[float, dict]] = []

    def add(self, value, attributes=None):
        self.calls.append((value, dict(attributes or {})))

    def record(self, value, attributes=None):
        self.calls.append((value, dict(attributes or {})))

    def set(self, value, attributes=None):
        self.calls.append((value, dict(attributes or {})))


def _activate_fake_otel(monkeypatch):
    """Liga ``otel_metrics`` com instrumentos fake p/ TODOS os _SPEC (sem SDK)."""
    fakes = {name: _FakeInstrument() for name in otel_metrics._SPEC}
    monkeypatch.setattr(otel_metrics, "_ENABLED", True)
    monkeypatch.setattr(otel_metrics, "_INITIALIZED", True)
    monkeypatch.setattr(otel_metrics, "_instruments", fakes)
    return fakes


# ── Invariante 1: OFF é no-op total ──────────────────────────────────────


def test_off_is_total_noop(monkeypatch):
    monkeypatch.setattr(settings, "OTEL_ENABLED", False)
    assert otel_metrics.init_metrics() is False
    assert otel_metrics.is_enabled() is False
    # Emits silenciosos mesmo sem instrumentos montados.
    otel_metrics.count("collector_events_sent_total", 5, {"destination_id": "d1"})
    otel_metrics.record("collector_delivery_latency_seconds", 0.1, {})
    otel_metrics.set_gauge("collector_dispatch_queue_depth", 9, {"queue": _QLABEL})
    # E pela fachada também (sem levantar).
    metrics.EVENTS_SENT.labels(destination_id="d1", kind="splunk_hec").inc(3)


def test_init_is_idempotent(monkeypatch):
    monkeypatch.setattr(settings, "OTEL_ENABLED", False)
    assert otel_metrics.init_metrics() is False
    assert otel_metrics.init_metrics() is False  # 2ª não remonta
    assert otel_metrics.is_enabled() is False


# ── Invariante 2: ON sem pacotes degrada (não levanta) ───────────────────


def test_on_without_packages_degrades_gracefully(monkeypatch):
    monkeypatch.setattr(settings, "OTEL_ENABLED", True)
    result = otel_metrics.init_metrics()
    if _HAS_OTEL:
        assert result is True
        assert otel_metrics.is_enabled() is True
    else:
        assert result is False
        assert otel_metrics.is_enabled() is False
        otel_metrics.count("collector_events_sent_total", 1, {})


# ── Invariante 3: fachada OTel-native empurra a série certa ──────────────


def test_facade_emits_counter(monkeypatch):
    fakes = _activate_fake_otel(monkeypatch)
    metrics.EVENTS_SENT.labels(destination_id="d9", kind="otlp").inc(7)
    assert fakes["collector_events_sent_total"].calls == [
        (7, {"destination_id": "d9", "kind": "otlp"})
    ]


def test_facade_emits_histogram(monkeypatch):
    fakes = _activate_fake_otel(monkeypatch)
    metrics.DELIVERY_LATENCY.labels(destination_id="d9", kind="otlp").observe(1.5)
    assert fakes["collector_delivery_latency_seconds"].calls == [
        (1.5, {"destination_id": "d9", "kind": "otlp"})
    ]


def test_facade_emits_gauge(monkeypatch):
    fakes = _activate_fake_otel(monkeypatch)
    metrics.QUEUE_DEPTH.labels(queue=_QLABEL).set(42)
    assert fakes["collector_dispatch_queue_depth"].calls == [(42, {"queue": _QLABEL})]
    metrics.BREAKER_STATE.labels(destination_id="d9", kind="otlp").set(1)
    assert fakes["collector_destination_breaker_state"].calls == [
        (1, {"destination_id": "d9", "kind": "otlp"})
    ]


def test_facade_emits_with_mixed_kwargs(monkeypatch):
    """``EVENTS_REJECTED`` é chamado com kwarg explícito + **labels — os atributos
    OTel devem conter todos os labels (espelha o call site de _send_chunk)."""
    fakes = _activate_fake_otel(monkeypatch)
    metrics.EVENTS_REJECTED.labels(
        error_kind="poison", destination_id="d9", kind="otlp"
    ).inc()
    assert fakes["collector_events_rejected_total"].calls == [
        (1, {"error_kind": "poison", "destination_id": "d9", "kind": "otlp"})
    ]


def test_facade_positional_labels_map_to_declared_names(monkeypatch):
    """``.labels("v")`` posicional mapeia p/ o nome de label declarado no _SPEC."""
    fakes = _activate_fake_otel(monkeypatch)
    metrics.ROUTING_DECISIONS.labels("routed").inc(4)
    assert fakes["collector_routing_decisions_total"].calls == [
        (4, {"outcome": "routed"})
    ]


def test_facade_noop_when_otel_disabled(monkeypatch):
    """OTel OFF: o fake NUNCA é tocado (instrumentação 100% OTel, sem prometheus)."""
    fakes = {name: _FakeInstrument() for name in otel_metrics._SPEC}
    monkeypatch.setattr(otel_metrics, "_ENABLED", False)
    monkeypatch.setattr(otel_metrics, "_instruments", fakes)
    metrics.ROUTE_EVENTS.labels(route_id="r1", action="route").inc(4)
    assert fakes["collector_route_events_total"].calls == []


def test_facade_time_observes_duration(monkeypatch):
    """``.labels(...).time()`` cronometra o bloco e ``observe()`` a duração no
    histograma (compat com Histogram.time do prometheus, usado em TASK_DURATION)."""
    fakes = _activate_fake_otel(monkeypatch)
    with metrics.TASK_DURATION.labels(stream="s", queue=_QLABEL).time():
        pass
    calls = fakes["collector_task_duration_seconds"].calls
    assert len(calls) == 1
    duration, attrs = calls[0]
    assert duration >= 0.0
    assert attrs == {"stream": "s", "queue": _QLABEL}


def test_facade_time_records_even_on_exception(monkeypatch):
    """A duração é registrada mesmo se o bloco levantar (finally semantics) e a
    exceção re-propaga (não é suprimida)."""
    fakes = _activate_fake_otel(monkeypatch)
    with pytest.raises(ValueError):
        with metrics.TASK_DURATION.labels(stream="s", queue=_QLABEL).time():
            raise ValueError("boom")
    assert len(fakes["collector_task_duration_seconds"].calls) == 1


def test_facade_labelnames_preserved():
    """A fachada expõe ``_labelnames`` (compat com código/diagnóstico)."""
    assert metrics.EVENTS_SENT._labelnames == ("destination_id", "kind")
    assert metrics.ROUTING_DECISIONS._labelnames == ("outcome",)


# ── Invariante 4: sem drift entre _SPEC e as fachadas ────────────────────


def test_every_facade_maps_to_spec_and_vice_versa():
    """Bijeção entre as fachadas públicas de ``metrics`` e os instrumentos
    SÍNCRONOS de ``otel_metrics._SPEC`` — guarda contra adicionar uma série sem
    registrar (ou vice-versa). Observáveis (collector_up) não têm fachada e ficam
    fora da bijeção, mas seguem no catálogo _SPEC."""
    facade_names = {
        v._name
        for v in vars(metrics).values()
        if isinstance(v, metrics._Instrument)
    }
    sync_spec = {
        n for n, s in otel_metrics._SPEC.items() if s["kind"] != "observable_gauge"
    }
    assert facade_names == sync_spec
    # +2: cost/volume IN — collector_{events,bytes}_in_total. +1:
    # collector_ingest_malformed_total (robustez de ingestão push). +1:
    # collector_events_dropped_total (sampling). +1:
    # collector_bytes_saved_total (trim savings). +1:
    # collector_suppressed_total (suppression). Antes: 33 (+5 data-
    # plane Kafka, +3 ingest, base 28). +3: conformidade
    # OCSF collector_ocsf_{valid,invalid}_total + collector_ocsf_validate_latency_seconds.
    # 39 → 42.
    assert len(facade_names) == 42
    # O catálogo tem as síncronas + ao menos o observável collector_up.
    assert "collector_up" in otel_metrics._SPEC
    assert "collector_up" not in facade_names


def test_histograms_declare_buckets():
    """Todo histograma do _SPEC declara buckets explícitos (pinados via View p/
    casar os percentis herdados do prometheus na conversão OTLP→Prometheus)."""
    for name, spec in otel_metrics._SPEC.items():
        if spec["kind"] == "histogram":
            assert spec.get("buckets"), f"{name} sem buckets"


def test_histogram_buckets_match_prometheus_legacy():
    """Os buckets OTel batem EXATAMENTE com os limites herdados do prometheus_client
    — sem isso os painéis de percentil (p50/p95/p99) divergiriam após a conversão
    OTLP→Prometheus feita pelo Collector. Trava regressão silenciosa."""
    legacy = {
        "collector_api_latency_seconds": (0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
        "collector_task_duration_seconds": (1, 5, 15, 60, 300, 900),
        "collector_normalize_latency_seconds": (0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1),
        "collector_delivery_latency_seconds": (0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
        "collector_shadow_format_latency_seconds": (0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 2),
        # Latência de invocação de capability (vendor/capability).
        "collector_capability_latency_seconds": (0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
        # Data-plane Kafka. Histogramas NOVOS (não migrados do
        # prometheus_client) — buckets escolhidos p/ a latência do produce/consume.
        "collector_dataplane_produce_latency_seconds": (0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
        "collector_dataplane_consume_latency_seconds": (0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
        # Custo do structural gate OCSF no hot path (caminho válido).
        # Buckets espelham collector_normalize_latency_seconds (mesma escala sub-ms→s).
        "collector_ocsf_validate_latency_seconds": (0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1),
    }
    hist = {n for n, s in otel_metrics._SPEC.items() if s["kind"] == "histogram"}
    assert hist == set(legacy), "conjunto de histogramas divergiu do legado"
    for name, buckets in legacy.items():
        assert tuple(otel_metrics._SPEC[name]["buckets"]) == buckets, name


def test_labels_rejects_wrong_positional_count():
    """Nº errado de labels posicionais levanta (igual ao prometheus_client) — não
    trunca silenciosamente deixando a série OTel sem dimensão."""
    # DISPATCH_FAILURES declara 3 labels (target, reason, destination_id).
    with pytest.raises(ValueError, match="3 labels"):
        metrics.DISPATCH_FAILURES.labels("wazuh", "exhausted")  # falta destination_id
    # A contagem correta funciona.
    metrics.DISPATCH_FAILURES.labels("wazuh", "exhausted", "d1")


def test_spec_kinds_are_valid():
    for name, spec in otel_metrics._SPEC.items():
        assert spec["kind"] in {"counter", "histogram", "gauge", "observable_gauge"}, name
        assert spec["unit"] in {"1", "s", "By"}, name


def test_collector_up_is_observable_liveness():
    """``collector_up`` é o heartbeat de liveness: observable_gauge, unit 1, label
    ``role``, e NÃO tem fachada síncrona (é coletado por callback)."""
    spec = otel_metrics._SPEC["collector_up"]
    assert spec["kind"] == "observable_gauge"
    assert spec["unit"] == "1"
    assert spec["labels"] == ("role",)
    # Não existe fachada com esse nome em metrics.py (não se faz .set() nele).
    facade_names = {
        v._name for v in vars(metrics).values() if isinstance(v, metrics._Instrument)
    }
    assert "collector_up" not in facade_names


def test_collector_up_callback_observes_one_with_role():
    """A fábrica do callback do heartbeat observa exatamente 1 com o label
    ``role`` — sem depender de internals do SDK."""
    if not _HAS_OTEL:
        pytest.skip("opentelemetry não instalado neste ambiente")
    cb = otel_metrics._liveness_observations("worker")
    obs = list(cb(None))
    assert len(obs) == 1
    assert obs[0].value == 1
    assert obs[0].attributes == {"role": "worker"}


def test_collector_up_created_and_retained_with_real_sdk(monkeypatch):
    """Com o SDK real, ``init_metrics`` cria ``collector_up`` e o RETÉM em
    _instruments (senão o SDK para de coletar o observável)."""
    if not _HAS_OTEL:
        pytest.skip("opentelemetry não instalado neste ambiente")
    monkeypatch.setenv("SERVICE_ROLE", "dispatcher")
    monkeypatch.setattr(settings, "OTEL_ENABLED", True)
    assert otel_metrics.init_metrics() is True
    assert "collector_up" in otel_metrics._instruments
    # Todos os instrumentos do _SPEC (síncronos + observável) foram criados.
    assert set(otel_metrics._instruments) == set(otel_metrics._SPEC)


# ── Caminho ATIVO com SDK real (só quando o extra está instalado) ────────


def test_init_active_with_real_sdk(monkeypatch):
    if not _HAS_OTEL:
        pytest.skip("opentelemetry não instalado neste ambiente")
    monkeypatch.setattr(settings, "OTEL_ENABLED", True)
    assert otel_metrics.init_metrics() is True
    assert otel_metrics.is_enabled() is True
    # Todos os instrumentos do _SPEC (24: 23 sincronos + collector_up observavel) foram criados.
    assert set(otel_metrics._instruments) == set(otel_metrics._SPEC)
    # Emits reais (via fachada) não levantam.
    metrics.EVENTS_SENT.labels(destination_id="d1", kind="otlp").inc(1)
    metrics.DELIVERY_LATENCY.labels(destination_id="d1", kind="otlp").observe(0.1)
    metrics.QUEUE_DEPTH.labels(queue=_QLABEL).set(5)


def test_resource_has_semantic_attrs():
    if not _HAS_OTEL:
        pytest.skip("opentelemetry não instalado neste ambiente")
    from backend.app.collectors import otel_common

    res = otel_common.build_resource()
    attrs = dict(res.attributes)
    assert attrs.get("service.name")
    assert "service.version" in attrs
    assert "service.instance.id" in attrs
    assert "deployment.environment" in attrs
