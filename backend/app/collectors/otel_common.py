"""Base compartilhada do export OTel (vendor-neutro).

Helpers usados pelos três sinais OTel — traces (``tracing.py``), métricas
(``otel_metrics.py``) e logs (``otel_logs.py``): a leitura da flag de runtime, a
construção do ``Resource`` **idêntico** entre os sinais (o backend de ops
correlaciona tudo pelo mesmo ``service.name`` / ``service.instance.id`` /
``deployment.environment``) e a montagem do exporter OTLP de cada sinal.

O exporter segue a especificação de configuração OTLP, para que o MESMO código
sirva tanto um OpenTelemetry Collector quanto um backend que recebe OTLP direto:

* protocolo por sinal — ``OTEL_EXPORTER_OTLP_PROTOCOL`` e
  ``OTEL_EXPORTER_OTLP_{TRACES,METRICS,LOGS}_PROTOCOL`` (``http/protobuf`` ou
  ``grpc``; alguns recebedores, como o APM collector do proxy Zabbix 8, só falam
  gRPC);
* endpoint por sinal — ``OTEL_EXPORTER_OTLP_{TRACES,METRICS,LOGS}_ENDPOINT`` tem
  PRECEDÊNCIA sobre o endpoint base e é usado como veio;
* headers, TLS/mTLS, compressão e timeout ficam com o SDK, que os lê das variáveis
  padrão (``OTEL_EXPORTER_OTLP_HEADERS``, ``..._CERTIFICATE``, ``..._COMPRESSION``…)
  mesmo quando o endpoint é passado explicitamente.

Mantido fino e SEM import de ``opentelemetry`` no topo (os pacotes são extras
opcionais — ver ``requirements-otel.txt``); os imports vivem dentro das funções
para degradar com graça quando ausentes.
"""

from __future__ import annotations

import importlib
import logging
import os
import socket
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

SIGNALS: Tuple[str, ...] = ("traces", "metrics", "logs")
DEFAULT_PROTOCOL = "http/protobuf"
_SUPPORTED_PROTOCOLS = ("grpc", "http/protobuf")

# (sinal, protocolo) → (módulo, classe) do exporter OTLP.
_EXPORTER_CLASSES = {
    ("traces", "http/protobuf"): (
        "opentelemetry.exporter.otlp.proto.http.trace_exporter", "OTLPSpanExporter"),
    ("traces", "grpc"): (
        "opentelemetry.exporter.otlp.proto.grpc.trace_exporter", "OTLPSpanExporter"),
    ("metrics", "http/protobuf"): (
        "opentelemetry.exporter.otlp.proto.http.metric_exporter", "OTLPMetricExporter"),
    ("metrics", "grpc"): (
        "opentelemetry.exporter.otlp.proto.grpc.metric_exporter", "OTLPMetricExporter"),
    ("logs", "http/protobuf"): (
        "opentelemetry.exporter.otlp.proto.http._log_exporter", "OTLPLogExporter"),
    ("logs", "grpc"): (
        "opentelemetry.exporter.otlp.proto.grpc._log_exporter", "OTLPLogExporter"),
}
_PROTOCOL_PACKAGES = {
    "http/protobuf": "opentelemetry-exporter-otlp-proto-http",
    "grpc": "opentelemetry-exporter-otlp-proto-grpc",
}

# Avisos de configuração saem UMA vez por processo (não a cada init/sinal).
_warned: set = set()


def _warn_once(key: str, message: str, *args: Any) -> None:
    if key in _warned:
        return
    _warned.add(key)
    logger.warning(message, *args)


def otel_flag() -> bool:
    """``True`` se ``OTEL_ENABLED`` está ligado (import tardio evita ciclo)."""
    try:
        from ..core.config import settings

        return bool(getattr(settings, "OTEL_ENABLED", False))
    except Exception:  # pragma: no cover — config sempre disponível em runtime
        return False


def otel_logs_flag() -> bool:
    """``True`` se OTel **e** o sinal de LOGS estão ligados (toggle separado —
    volume de logs pode ser alto, então é opt-in independente das métricas)."""
    try:
        from ..core.config import settings

        return bool(getattr(settings, "OTEL_ENABLED", False)) and bool(
            getattr(settings, "OTEL_LOGS_ENABLED", False)
        )
    except Exception:  # pragma: no cover
        return False


def _setting(name: str) -> str:
    try:
        from ..core.config import settings

        return str(getattr(settings, name, "") or "").strip()
    except Exception:  # pragma: no cover
        return ""


def otlp_endpoint() -> str:
    """Endpoint OTLP base da config (vazio ⇒ o SDK usa os envs padrão
    OTEL_EXPORTER_OTLP_ENDPOINT/_TRACES/_METRICS/_LOGS_ENDPOINT)."""
    return _setting("OTEL_EXPORTER_OTLP_ENDPOINT")


def otlp_protocol_for(signal: str) -> Optional[str]:
    """Protocolo OTLP do sinal: o específico do sinal vence o global.

    ``http/json`` é válido na especificação mas o SDK Python não o implementa; o
    recebedor OTLP/HTTP aceita protobuf na mesma porta, então cai para
    ``http/protobuf`` com um aviso. Valor desconhecido ⇒ ``None`` (o sinal fica
    desligado com um aviso, em vez de exportar para o lugar errado)."""
    raw = (
        _setting(f"OTEL_EXPORTER_OTLP_{signal.upper()}_PROTOCOL")
        or _setting("OTEL_EXPORTER_OTLP_PROTOCOL")
    ).lower()
    if not raw:
        return DEFAULT_PROTOCOL
    if raw in _SUPPORTED_PROTOCOLS:
        return raw
    if raw == "http/json":
        _warn_once(
            f"protocol-json-{signal}",
            "OTLP %s: protocolo http/json não é suportado pelo SDK Python — usando "
            "http/protobuf (todo recebedor OTLP/HTTP aceita protobuf)",
            signal,
        )
        return DEFAULT_PROTOCOL
    _warn_once(
        f"protocol-bad-{signal}",
        "OTLP %s: protocolo %r desconhecido (use grpc ou http/protobuf) — sinal OTel "
        "DESLIGADO neste processo",
        signal,
        raw,
    )
    return None


def otlp_endpoint_for(signal: str, protocol: Optional[str] = None) -> str:
    """Endpoint OTLP do sinal (``signal`` ∈ {traces, metrics, logs}).

    Precedência da especificação: o endpoint ESPECÍFICO do sinal vence o base e é
    usado como veio. Sem ele, o base vale para os três sinais:

    * ``http/protobuf`` — o exporter usa o kwarg ``endpoint=`` EXATAMENTE como
      recebido (não anexa ``/v1/<sinal>``), então anexamos aqui a uma BASE, de modo
      que ``OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318`` funcione para
      traces, metrics E logs. Um path ``/v1/<sinal>`` explícito é respeitado.
    * ``grpc`` — não há path; o base vai como está (``http://host:4317`` = sem TLS,
      ``https://`` = TLS).

    Vazio ⇒ vazio (o SDK cai nos envs padrão)."""
    specific = _setting(f"OTEL_EXPORTER_OTLP_{signal.upper()}_ENDPOINT")
    if specific:
        return specific
    base = otlp_endpoint()
    if not base:
        return ""
    if (protocol or otlp_protocol_for(signal) or DEFAULT_PROTOCOL) == "grpc":
        return base
    # Se o operador já informou um path /v1/<sinal> explícito (de QUALQUER sinal),
    # respeitamos a URL como veio para TODOS os sinais — não reescrevemos nem
    # anexamos. Sem isso, "…/v1/traces" para o sinal "logs" virava
    # "…/v1/traces/v1/logs" (path duplicado). Ver test_explicit_v1_path_is_respected.
    stripped = base.rstrip("/")
    if any(stripped.endswith(f"/v1/{s}") for s in SIGNALS):
        return base
    return stripped + "/v1/" + signal


def sdk_env_endpoint_valid(signal: Optional[str] = None) -> bool:
    """``True`` se — na AUSÊNCIA de endpoint explícito na config — delegar ao SDK
    ainda produz um endpoint OTLP ABSOLUTO (com scheme).

    Detecta EXCLUSIVAMENTE a armadilha REAL de deploy: o compose/Helm setam a
    variável como string VAZIA (``${OTEL_..._ENDPOINT:-}``). Presente-porém-vazia
    derrota o fallback-localhost do SDK → ele monta uma URL RELATIVA ``/v1/<sinal>``
    e o exporter falha a CADA ciclo ("No scheme supplied"). Segue a mesma ordem do
    SDK: a variável do sinal (quando ``signal`` é dado) antes da base."""
    names = [f"OTEL_EXPORTER_OTLP_{signal.upper()}_ENDPOINT"] if signal else []
    names.append("OTEL_EXPORTER_OTLP_ENDPOINT")
    for name in names:
        if name in os.environ:
            value = (os.environ.get(name) or "").strip()
            return value.startswith("http://") or value.startswith("https://")
    return True


def build_exporter(signal: str) -> Optional[Tuple[Any, str]]:
    """Monta o exporter OTLP do sinal conforme protocolo/endpoint configurados.

    Devolve ``(exporter, descrição_do_endpoint)`` ou ``None`` — config inválida
    ou pacote do protocolo ausente —, sempre com um aviso único em vez de exportar
    para o lugar errado ou poluir o log a cada ciclo. Cada falha de export passa a
    contar em ``collector_otel_export_failures_total{signal}``."""
    protocol = otlp_protocol_for(signal)
    if protocol is None:
        return None
    endpoint = otlp_endpoint_for(signal, protocol)
    if not endpoint and not sdk_env_endpoint_valid(signal):
        _warn_once(
            f"endpoint-{signal}",
            "OTEL_ENABLED=true mas nenhum endpoint OTLP com scheme "
            "(OTEL_EXPORTER_OTLP_ENDPOINT vazio/sem http[s]://) — %s OTel "
            "DESLIGADO neste processo (evita spam '/v1/%s: No scheme supplied')",
            signal,
            signal,
        )
        return None
    module_name, class_name = _EXPORTER_CLASSES[(signal, protocol)]
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        _warn_once(
            f"package-{signal}-{protocol}",
            "OTLP %s: o protocolo %s exige o pacote %s, que não está instalado — "
            "sinal OTel DESLIGADO neste processo",
            signal,
            protocol,
            _PROTOCOL_PACKAGES[protocol],
        )
        return None
    exporter_cls = getattr(module, class_name)
    exporter = exporter_cls(endpoint=endpoint) if endpoint else exporter_cls()
    _count_export_failures(exporter, signal)
    return exporter, endpoint or f"<env padrão OTLP, {protocol}>"


def _record_export_failure(signal: str) -> None:
    try:
        from .metrics import OTEL_EXPORT_FAILURES

        OTEL_EXPORT_FAILURES.labels(signal=signal).inc()
    except Exception:  # pragma: no cover — contagem nunca afeta o export
        pass


def _count_export_failures(exporter: Any, signal: str) -> None:
    """Conta as falhas de export do sinal, sem trocar a classe do exporter.

    Sem um Collector no meio, o processo é o único que sabe que a telemetria está
    se perdendo: o log ``OTel ... ativo`` só prova que o SDK subiu, não que alguém
    está recebendo. A troca é no MÉTODO da instância (não um proxy) porque o
    ``PeriodicExportingMetricReader`` lê atributos privados do exporter de métricas
    (temporalidade/agregação preferidas)."""
    original = getattr(exporter, "export", None)
    if original is None:
        return

    def export(*args: Any, **kwargs: Any) -> Any:
        try:
            result = original(*args, **kwargs)
        except Exception:
            _record_export_failure(signal)
            raise
        if getattr(result, "name", None) == "FAILURE":
            _record_export_failure(signal)
        return result

    try:
        exporter.export = export
    except Exception:  # pragma: no cover — exporter com __slots__
        pass


def init_all_signals() -> None:
    """Monta os três sinais NESTE processo (cada um é no-op com o OTel desligado).

    Para processos que não passam pelo ``worker_process_init`` do Celery: o
    dispatcher Kafka (processo standalone) e o beat."""
    for label, module_name, init_name in (
        ("tracing", "tracing", "init_tracing"),
        ("metrics", "otel_metrics", "init_metrics"),
        ("logs", "otel_logs", "init_logs"),
    ):
        try:
            getattr(importlib.import_module(f".{module_name}", __package__), init_name)()
        except Exception:  # pragma: no cover — telemetria jamais derruba o processo
            logger.warning("init OTel %s falhou (segue sem)", label, exc_info=True)


def flush_and_shutdown(timeout_millis: int = 5000) -> None:
    """Esvazia e desliga os três sinais. Idempotente; no-op com o OTel desligado.

    Necessário onde o processo NÃO sai pelo caminho normal do interpretador: o
    filho prefork do Celery termina com ``os._exit()``, que pula o ``atexit`` —
    o único flush automático do SDK. Ordem: traces, métricas e por último logs,
    para os logs do próprio desligamento ainda saírem."""
    for module_name in ("tracing", "otel_metrics", "otel_logs"):
        try:
            importlib.import_module(f".{module_name}", __package__).shutdown(timeout_millis)
        except Exception:  # pragma: no cover — desligamento nunca levanta
            logger.debug("flush OTel de %s falhou", module_name, exc_info=True)


def service_role() -> str:
    """Papel do processo (``worker`` / ``beat`` / ``dispatcher``) lido da env
    ``SERVICE_ROLE`` — a MESMA que o ``celery_app`` usa para decidir signals.
    Usado como label de séries por-papel (ex.: ``collector_up``), permitindo SLO
    de disponibilidade segmentado por papel. ``unknown`` quando ausente."""
    return os.environ.get("SERVICE_ROLE", "").strip() or "unknown"


def build_resource(service_name: Optional[str] = None) -> Any:
    """``Resource`` OTel com os atributos semânticos padrão (mesma identidade
    nos 3 sinais). Requer os pacotes ``opentelemetry-sdk`` — chamado só de dentro
    dos ``init_*`` já protegidos por try/except.

    ``OTEL_RESOURCE_ATTRIBUTES`` (ex.: ``service.namespace``, ``k8s.*``) é somado
    pelo próprio SDK; os atributos abaixo têm precedência."""
    from opentelemetry.sdk.resources import Resource

    from ..core.config import settings

    service = service_name or str(getattr(settings, "OTEL_SERVICE_NAME", "centralops-collector"))
    version = str(getattr(settings, "APP_VERSION", "") or "unknown")
    env = str(getattr(settings, "APP_ENV", "") or "unknown")
    try:
        host = socket.gethostname()
    except Exception:  # pragma: no cover — hostname sempre resolve
        host = "unknown"
    # service.instance.id distingue cada filho prefork (host:pid) — sem isso o
    # backend não separa as séries por processo (gauges multiproc, p.ex.).
    instance = f"{host}:{os.getpid()}"
    return Resource.create(
        {
            "service.name": service,
            "service.version": version,
            "service.instance.id": instance,
            "deployment.environment": env,
        }
    )


def reset_for_tests() -> None:
    """Seam de teste: zera os avisos únicos."""
    _warned.clear()
