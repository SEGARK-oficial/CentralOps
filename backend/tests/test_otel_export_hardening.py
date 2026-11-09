"""Export OTel robusto para Collector OU backend direto, sem vazar segredo.

Cobre, de forma comportamental (roda igual na árvore de fontes e no gate compilado):

1. Redação de logs por HANDLER — log de logger FILHO não passa pelos filtros do
   raiz; o stdout e o export OTLP têm de sair redigidos mesmo assim (mensagem,
   ``extra`` e exceção).
2. Protocolo/endpoint OTLP por sinal (especificação) e a fábrica de exporters.
3. Flush no desligamento — o filho prefork sai por ``os._exit`` e pula o ``atexit``.
4. Span marca erro em falha TRATADA (lote desviado para a DLQ sem exceção) e grava
   a exceção redigida.
5. Traces da API: atributos de URL redigidos.
"""

from __future__ import annotations

import importlib.util
import io
import json
import logging
import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import pytest

from backend.app.collectors import otel_common, otel_logs, otel_metrics, tracing
from backend.app.core import api_tracing
from backend.app.core import logging_config as lc
from backend.app.core.config import settings

_HAS_SDK = importlib.util.find_spec("opentelemetry.sdk") is not None
needs_sdk = pytest.mark.skipif(not _HAS_SDK, reason="opentelemetry-sdk não instalado")

PAT = "copsk_" + "A" * 32
SECRETS = (PAT, "SEGREDO123", "SenhaRedis", "hunter2", "XYZ12345678")


def _assert_no_secret(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text, f"segredo {secret!r} vazou em: {text}"


@pytest.fixture
def isolated_root_logging():
    """Salva e restaura handlers/filtros/nível do raiz (configure_logging troca tudo)."""
    root = logging.getLogger()
    saved = (list(root.handlers), list(root.filters), root.level)
    yield root
    root.handlers, root.filters = saved[0], saved[1]
    root.setLevel(saved[2])


@pytest.fixture(autouse=True)
def _reset_otel_state():
    tracing.reset_for_tests()
    otel_metrics.reset_for_tests()
    otel_logs.reset_for_tests()
    otel_common.reset_for_tests()
    yield
    tracing.reset_for_tests()
    otel_metrics.reset_for_tests()
    otel_logs.reset_for_tests()
    otel_common.reset_for_tests()


# ── 1. Redação ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text, secret",
    [
        ("oauth resp access_token=eyJhbGciOi.SEGREDO123 expires_in=3600", "SEGREDO123"),
        ('{"client_secret": "SEGREDO123", "client_id": "app-1"}', "SEGREDO123"),
        ("{'Authorization': 'Bearer SEGREDO123abcdef'}", "SEGREDO123"),
        ("broker redis://:SenhaRedis@redis:6379/0", "SenhaRedis"),
        ("amqp://user:SenhaRedis@mq:5672//", "SenhaRedis"),
        ("GET /api/x?token=" + PAT + "&page=2", PAT),
        ("GET /api/x?api_key=XYZ12345678&page=2", "XYZ12345678"),
        ("password: hunter2", "hunter2"),
        ("PAT solto no meio " + PAT + " do texto", PAT),
    ],
)
def test_scrub_sensitive_text_masks_the_secret(text, secret):
    assert secret not in lc.scrub_sensitive_text(text)


@pytest.mark.parametrize(
    "text",
    [
        "invalid password for user admin",
        "missing Authorization header",
        "token_count=5 tokens_used=7",
        "Basic authentication enabled",
        "token expired, refreshing",
        "events=282 integration_id=1",
    ],
)
def test_scrub_sensitive_text_keeps_operational_text(text):
    """Redação cirúrgica: prosa sem valor sensível passa intacta."""
    assert lc.scrub_sensitive_text(text) == text


@pytest.mark.parametrize(
    "text",
    [
        '{"password": "secret123", "url": "redis://:SenhaRedis@r/0"}',
        "Authorization: Bearer abcdefghijkl token=" + PAT,
        "vault hvs.ABCDEFGHIJKLMNOP",
    ],
)
def test_scrub_sensitive_text_is_idempotent(text):
    """O formatter JSON redige e o envoltório do handler redige de novo: a
    segunda passada não pode estragar o que a primeira produziu."""
    once = lc.scrub_sensitive_text(text)
    assert lc.scrub_sensitive_text(once) == once


def test_child_logger_is_redacted_on_stdout(isolated_root_logging):
    """Log nascido em logger FILHO — o caso real da aplicação inteira."""
    lc.configure_logging()
    buf = io.StringIO()
    for handler in isolated_root_logging.handlers:
        if isinstance(handler, logging.StreamHandler):
            handler.setStream(buf)
    child = logging.getLogger("app.collectors.vendors.redaction_test")
    child.warning("oauth resp access_token=SEGREDO123 client_secret=hunter2")
    child.warning("broker redis://:SenhaRedis@redis:6379/0 token=%s", PAT)
    child.warning("com extra", extra={"url": "https://x/?api_key=XYZ12345678", "password": "hunter2"})

    lines = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    assert len(lines) == 3
    for line in lines:
        _assert_no_secret(json.dumps(line))
    assert "access_token=[REDACTED]" in lines[0]["message"]  # contexto preservado


def test_every_handler_gets_the_redaction_filter(isolated_root_logging):
    lc.configure_logging()
    assert isolated_root_logging.handlers
    for handler in isolated_root_logging.handlers:
        assert isinstance(handler.formatter, lc.RedactingFormatter)


def test_attach_redaction_is_idempotent():
    handler = logging.StreamHandler(io.StringIO())
    lc.attach_redaction(handler)
    lc.attach_redaction(handler)
    assert isinstance(handler.formatter, lc.RedactingFormatter)
    assert not isinstance(handler.formatter.inner, lc.RedactingFormatter)


def test_redaction_does_not_strip_args_from_the_shared_record(isolated_root_logging):
    """O registro é compartilhado pelos handlers: a redação de um handler não
    pode pré-formatar a mensagem nem zerar ``args`` para quem vem depois (um
    handler de captura, um agregador). Regressão do sweep completo."""
    lc.configure_logging()
    for handler in isolated_root_logging.handlers:
        if isinstance(handler, logging.StreamHandler):
            handler.setStream(io.StringIO())
    seen = []

    class _Capture(logging.Handler):
        def emit(self, record):
            seen.append((record.msg, record.args))

    isolated_root_logging.addHandler(_Capture())
    logging.getLogger("app.collectors.redaction_args_test").warning("cortou %d de %d", 3, 10)
    assert seen == [("cortou %d de %d", (3, 10))]


def test_plain_handler_without_json_formatter_is_redacted(isolated_root_logging):
    """O ``basicConfig`` do dispatcher Kafka cria handler de texto puro, sem o
    formatter JSON — só a redação de handler o protege."""
    buf = io.StringIO()
    plain = logging.StreamHandler(buf)
    plain.setFormatter(logging.Formatter("%(message)s"))
    isolated_root_logging.handlers = [plain]
    lc.attach_redaction_to_root_handlers()
    logging.getLogger("app.collectors.dataplane.redaction_test").warning(
        "token=%s url=redis://:SenhaRedis@r:6379", PAT
    )
    _assert_no_secret(buf.getvalue())
    assert "[REDACTED]" in buf.getvalue()


def test_celery_logging_signal_attaches_redaction():
    """O beat não dispara ``worker_init``: a redação entra pelo signal de setup do
    logging do Celery, nos handlers que o próprio Celery monta."""
    from backend.app.collectors import celery_app

    handler = logging.StreamHandler(io.StringIO())
    target = logging.Logger("celery-test-logger")
    target.addHandler(handler)
    celery_app._attach_log_redaction(logger=target)
    assert isinstance(handler.formatter, lc.RedactingFormatter)


def test_redacted_record_copy_leaves_the_original_untouched():
    record = logging.LogRecord("app.x", logging.WARNING, "", 0, "token=%s", (PAT,), None)
    copy = lc.redacted_record_copy(record)
    _assert_no_secret(copy.getMessage())
    assert record.args == (PAT,)  # os outros handlers recebem o registro original


@needs_sdk
def test_otel_log_export_is_redacted_message_extra_and_exception():
    """O ``LoggingHandler`` do SDK não usa o formatter e monta ``exception.*`` de
    ``exc_info`` — antes, tudo isso saía em claro para o backend OTLP."""
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs import export as log_export

    exporter_cls = getattr(log_export, "InMemoryLogRecordExporter", None) or getattr(
        log_export, "InMemoryLogExporter"
    )
    exporter = exporter_cls()
    provider = LoggerProvider()
    provider.add_log_record_processor(log_export.SimpleLogRecordProcessor(exporter))
    handler = otel_logs._redacting_handler_class(LoggingHandler)(
        level=logging.INFO, logger_provider=provider
    )
    child = logging.getLogger("app.collectors.vendors.otel_redaction_test")
    child.propagate = False
    child.addHandler(handler)
    try:
        child.warning("token=%s", PAT)
        child.warning("com extra", extra={"url": "https://x/?api_key=XYZ12345678"})
        try:
            raise ValueError("falhou em https://u:hunter2@host/?access_token=SEGREDO123")
        except ValueError:
            child.exception("erro de entrega")
        provider.force_flush()
    finally:
        child.removeHandler(handler)
        child.propagate = True

    exported = [getattr(item, "log_record", item) for item in exporter.get_finished_logs()]
    assert len(exported) == 3
    for rec in exported:
        _assert_no_secret(str(rec.body) + json.dumps(dict(rec.attributes or {}), default=str))
    exc_attrs = dict(exported[2].attributes or {})
    assert exc_attrs.get("exception.type") == "ValueError"  # a exceção continua lá
    assert "[REDACTED]" in exc_attrs.get("exception.message", "")


# ── 2. Protocolo / endpoint / fábrica ────────────────────────────────────


@pytest.fixture
def otlp_settings(monkeypatch):
    names = [
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_PROTOCOL",
        *(f"OTEL_EXPORTER_OTLP_{s.upper()}_{k}" for s in otel_common.SIGNALS for k in ("ENDPOINT", "PROTOCOL")),
    ]
    for name in names:
        monkeypatch.setattr(settings, name, "", raising=False)

    def _set(**values):
        for name, value in values.items():
            monkeypatch.setattr(settings, name, value, raising=False)

    return _set


def test_protocol_defaults_to_http_protobuf(otlp_settings):
    assert otel_common.otlp_protocol_for("traces") == "http/protobuf"


def test_signal_protocol_overrides_global(otlp_settings):
    otlp_settings(OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf", OTEL_EXPORTER_OTLP_TRACES_PROTOCOL="grpc")
    assert otel_common.otlp_protocol_for("traces") == "grpc"
    assert otel_common.otlp_protocol_for("metrics") == "http/protobuf"


def test_http_json_falls_back_to_protobuf_and_unknown_disables(otlp_settings):
    otlp_settings(OTEL_EXPORTER_OTLP_PROTOCOL="http/json")
    assert otel_common.otlp_protocol_for("logs") == "http/protobuf"
    otlp_settings(OTEL_EXPORTER_OTLP_PROTOCOL="thrift")
    assert otel_common.otlp_protocol_for("logs") is None
    assert otel_common.build_exporter("logs") is None


def test_signal_endpoint_wins_over_base(otlp_settings):
    """Precedência da especificação — antes o base sempre vencia."""
    otlp_settings(
        OTEL_EXPORTER_OTLP_ENDPOINT="http://collector:4318",
        OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="http://tracing-backend:4318/v1/traces",
    )
    assert otel_common.otlp_endpoint_for("traces") == "http://tracing-backend:4318/v1/traces"
    assert otel_common.otlp_endpoint_for("metrics") == "http://collector:4318/v1/metrics"


def test_grpc_base_endpoint_gets_no_path(otlp_settings):
    otlp_settings(OTEL_EXPORTER_OTLP_ENDPOINT="http://apm-proxy:4317", OTEL_EXPORTER_OTLP_PROTOCOL="grpc")
    assert otel_common.otlp_endpoint_for("traces") == "http://apm-proxy:4317"


def test_empty_env_endpoint_disables_the_signal(otlp_settings, monkeypatch):
    """Armadilha do compose: variável presente mas vazia ⇒ URL relativa no SDK."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
    assert otel_common.build_exporter("traces") is None


@needs_sdk
def test_build_exporter_honours_the_protocol(otlp_settings):
    pytest.importorskip("opentelemetry.exporter.otlp.proto.http")
    pytest.importorskip("opentelemetry.exporter.otlp.proto.grpc")
    otlp_settings(OTEL_EXPORTER_OTLP_ENDPOINT="http://collector:4318")
    http_exporter, _ = otel_common.build_exporter("traces")
    assert "proto.http" in type(http_exporter).__module__

    otlp_settings(OTEL_EXPORTER_OTLP_ENDPOINT="http://apm-proxy:4317", OTEL_EXPORTER_OTLP_PROTOCOL="grpc")
    grpc_exporter, desc = otel_common.build_exporter("traces")
    assert "proto.grpc" in type(grpc_exporter).__module__
    assert desc == "http://apm-proxy:4317"


def test_export_failures_are_counted():
    class _Result:
        def __init__(self, name):
            self.name = name

    exporter = MagicMock()
    exporter.export = MagicMock(side_effect=[_Result("SUCCESS"), _Result("FAILURE"), OSError("down")])
    calls = []
    with patch.object(otel_common, "_record_export_failure", side_effect=calls.append):
        otel_common._count_export_failures(exporter, "metrics")
        exporter.export("a")
        exporter.export("b")
        with pytest.raises(OSError):
            exporter.export("c")
    assert calls == ["metrics", "metrics"]


# ── 3. Flush no desligamento ─────────────────────────────────────────────


@pytest.fixture
def memory_traces(monkeypatch):
    """Tracing real (SDK) exportando para memória."""
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    real_build = otel_common.build_exporter

    def _build(signal):
        return (exporter, "memória") if signal == "traces" else real_build(signal)

    monkeypatch.setattr(otel_common, "build_exporter", _build)
    monkeypatch.setattr(settings, "OTEL_ENABLED", True)
    assert tracing.init_tracing() is True
    return exporter


@needs_sdk
def test_flush_and_shutdown_exports_pending_spans(memory_traces):
    with tracing.span("collect.cycle"):
        pass
    # BatchSpanProcessor ainda segura o span (atraso de 5s): sem o flush, um
    # processo que sai por os._exit perde isto.
    otel_common.flush_and_shutdown()
    assert [s.name for s in memory_traces.get_finished_spans()] == ["collect.cycle"]
    assert tracing.is_enabled() is False


def test_worker_process_shutdown_flushes_even_if_runtime_shutdown_fails():
    from backend.app.collectors import celery_app

    with patch(
        "backend.app.collectors.dispatch_runtime.shutdown_runtime", side_effect=RuntimeError("x")
    ), patch.object(otel_common, "flush_and_shutdown") as flush:
        with pytest.raises(RuntimeError):
            celery_app._close_dispatch_runtime()
    flush.assert_called_once()


def test_beat_initializes_the_otel_signals():
    from backend.app.collectors import celery_app

    with patch.object(otel_common, "init_all_signals") as init_all:
        celery_app._init_beat_observability()
    init_all.assert_called_once()


def test_kafka_dispatcher_redacts_and_flushes_on_exit():
    from backend.app.collectors.dataplane import dispatcher

    with patch.object(dispatcher.asyncio, "run", side_effect=RuntimeError("stop")), patch.object(
        dispatcher, "_init_observability"
    ), patch.object(lc, "attach_redaction_to_root_handlers") as redact, patch.object(
        otel_common, "flush_and_shutdown"
    ) as flush:
        with pytest.raises(RuntimeError):
            dispatcher.main()
    redact.assert_called_once()
    flush.assert_called_once()


# ── 4. Status do span ────────────────────────────────────────────────────


def _finished(exporter):
    tracing._provider.force_flush()
    return {s.name: s for s in exporter.get_finished_spans()}


@needs_sdk
def test_exception_is_recorded_redacted_and_marks_error(memory_traces):
    from opentelemetry.trace import StatusCode

    with pytest.raises(ValueError):
        with tracing.span("dispatch.destination"):
            raise ValueError(f"HTTP 401 em https://sink/?token={PAT} access_token=SEGREDO123")
    span = _finished(memory_traces)["dispatch.destination"]
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "ValueError"  # só o tipo, nunca a mensagem
    assert span.attributes["error.type"] == "ValueError"
    exception_events = [e for e in span.events if e.name == "exception"]
    assert len(exception_events) == 1  # só o nosso, redigido — nunca o do SDK, cru
    # Varre o span INTEIRO: atributos, todos os eventos e a descrição do status.
    everything = {
        "attributes": dict(span.attributes),
        "events": [dict(e.attributes or {}) for e in span.events],
        "status": span.status.description,
    }
    _assert_no_secret(json.dumps(everything, default=str))


@needs_sdk
@pytest.mark.parametrize(
    "kwargs, error_type",
    [
        ({"accepted": 7, "rejected": 3}, "partial_rejection"),
        ({"accepted": 0, "rejected": 4}, "rejected"),
        ({"rejected": 5, "dlq_reason": "destination_missing"}, "destination_missing"),
    ],
)
def test_handled_dlq_outcome_marks_the_span_as_error(memory_traces, kwargs, error_type):
    from opentelemetry.trace import StatusCode

    from backend.app.collectors.pipeline import DispatchOutcome

    with tracing.span("dispatch.destination", span_kind="consumer"):
        tracing.record_dispatch_outcome(DispatchOutcome(destination_kind="splunk_hec", **kwargs))
    span = _finished(memory_traces)["dispatch.destination"]
    assert span.status.status_code is StatusCode.ERROR
    assert span.attributes["error.type"] == error_type
    assert span.attributes["centralops.dlq"] is True
    assert span.attributes["centralops.destination.kind"] == "splunk_hec"


@needs_sdk
def test_clean_delivery_keeps_the_span_ok(memory_traces):
    from opentelemetry.trace import StatusCode

    from backend.app.collectors.pipeline import DispatchOutcome

    with tracing.span("dispatch.destination"):
        tracing.record_dispatch_outcome(DispatchOutcome(destination_kind="syslog", accepted=10))
    span = _finished(memory_traces)["dispatch.destination"]
    assert span.status.status_code is StatusCode.UNSET
    assert span.attributes["centralops.events.accepted"] == 10


def test_span_helpers_are_noop_when_tracing_is_off():
    tracing.mark_error("x")
    tracing.set_attributes(a=1)
    tracing.record_dispatch_outcome(object())
    tracing.shutdown()


# ── 5. API ───────────────────────────────────────────────────────────────


@needs_sdk
def test_api_request_span_url_attributes_are_redacted():
    from opentelemetry.sdk.trace import TracerProvider

    span = TracerProvider().get_tracer("t").start_span(
        "GET /api/x",
        attributes={
            "http.target": f"/api/x?token={PAT}",
            "http.url": "https://api.local/api/x?api_key=XYZ12345678",
            "url.query": "access_token=SEGREDO123&page=2",
            "http.method": "GET",
        },
    )
    api_tracing.scrub_request_span(span, {})
    span.end()
    _assert_no_secret(json.dumps(dict(span.attributes)))
    assert span.attributes["http.method"] == "GET"
    assert span.attributes["url.query"].endswith("&page=2")


def test_api_tracing_is_off_by_default(monkeypatch):
    monkeypatch.setattr(settings, "OTEL_ENABLED", True)
    assert settings.OTEL_API_TRACING_ENABLED is False
    assert api_tracing.instrument_app(MagicMock()) is False
    assert api_tracing.start() is False


def test_api_tracing_exports_as_its_own_service(monkeypatch):
    monkeypatch.setattr(settings, "OTEL_ENABLED", True)
    monkeypatch.setattr(settings, "OTEL_API_TRACING_ENABLED", True)
    with patch.object(tracing, "init_tracing", return_value=True) as init:
        assert api_tracing.start() is True
    init.assert_called_once_with(service_name=settings.OTEL_API_SERVICE_NAME)


@needs_sdk
def test_api_instrumentation_installs_on_a_fastapi_app(monkeypatch):
    pytest.importorskip("opentelemetry.instrumentation.fastapi")
    from fastapi import FastAPI
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    monkeypatch.setattr(settings, "OTEL_ENABLED", True)
    monkeypatch.setattr(settings, "OTEL_API_TRACING_ENABLED", True)
    app = FastAPI()
    try:
        assert api_tracing.instrument_app(app) is True
        assert getattr(app, "_is_instrumented_by_opentelemetry", False) is True
    finally:
        FastAPIInstrumentor.uninstrument_app(app)
