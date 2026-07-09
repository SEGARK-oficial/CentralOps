"""Testes do helper de endpoint OTLP por sinal (otel_common.otlp_endpoint_for).

Garante que UM endpoint base (OTEL_EXPORTER_OTLP_ENDPOINT=http://host:4318)
funcione para os 3 sinais — o exporter OTLP/HTTP usa o kwarg endpoint as-is
(não anexa /v1/<sinal>), então a app precisa anexar. Lógica de string pura,
sem depender dos pacotes opentelemetry-* (extras opcionais).
"""
import pytest

from backend.app.collectors import otel_common
from backend.app.core.config import settings


@pytest.fixture
def set_endpoint(monkeypatch):
    def _set(value):
        monkeypatch.setattr(settings, "OTEL_EXPORTER_OTLP_ENDPOINT", value, raising=False)
    return _set


def test_base_endpoint_gains_per_signal_path(set_endpoint):
    set_endpoint("http://otel-collector:4318")
    assert otel_common.otlp_endpoint_for("traces") == "http://otel-collector:4318/v1/traces"
    assert otel_common.otlp_endpoint_for("metrics") == "http://otel-collector:4318/v1/metrics"
    assert otel_common.otlp_endpoint_for("logs") == "http://otel-collector:4318/v1/logs"


def test_trailing_slash_is_normalized(set_endpoint):
    set_endpoint("http://otel-collector:4318/")
    assert otel_common.otlp_endpoint_for("logs") == "http://otel-collector:4318/v1/logs"


def test_explicit_v1_path_is_respected(set_endpoint):
    # Operador que já apontou um path /v1/... explícito não é reescrito.
    set_endpoint("http://otel-collector:4318/v1/traces")
    assert otel_common.otlp_endpoint_for("traces") == "http://otel-collector:4318/v1/traces"
    assert otel_common.otlp_endpoint_for("logs") == "http://otel-collector:4318/v1/traces"


def test_empty_endpoint_stays_empty(set_endpoint):
    # Vazio ⇒ vazio: o init_* cai no construtor sem kwarg e o SDK usa os
    # envs padrão (OTEL_EXPORTER_OTLP_ENDPOINT/_TRACES/_METRICS/_LOGS_ENDPOINT).
    set_endpoint("")
    assert otel_common.otlp_endpoint_for("traces") == ""
    assert otel_common.otlp_endpoint_for("logs") == ""
