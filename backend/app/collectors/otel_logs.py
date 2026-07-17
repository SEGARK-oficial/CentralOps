"""Superfície B (ops): LOGS OTel-native (OTLP export).

Completa o trio de sinais OTel (traces + métricas + **logs**) — "uma
instrumentação, N sinais". Faz a ponte do ``logging`` padrão do Python para
``LogRecord`` OTel, então cada log do pipeline (erro de normalização, retry,
DLQ) sai correlacionado por ``trace_id``/``span_id`` ao trace correspondente, no
backend de ops (Loki/Datadog/…).

Toggle SEPARADO ``OTEL_LOGS_ENABLED`` (além de ``OTEL_ENABLED``): volume de logs
pode ser alto, então é opt-in independente das métricas/traces. OFF (default) ⇒
no-op total. Degrada para no-op se os pacotes ``opentelemetry-*`` faltarem.

Init por filho prefork em ``worker_process_init`` (como tracing/metrics).
"""

from __future__ import annotations

import logging
from typing import Any

from . import otel_common

logger = logging.getLogger(__name__)

_ENABLED: bool = False
_INITIALIZED: bool = False
_provider: Any = None
_handler: Any = None


def init_logs() -> bool:
    """Monta o LoggerProvider + anexa o handler OTel ao root logger UMA vez por
    processo. Retorna ``True`` se os logs OTel ficaram ativos. Idempotente."""
    global _ENABLED, _INITIALIZED, _provider, _handler

    if _INITIALIZED:
        return _ENABLED
    _INITIALIZED = True

    if not otel_common.otel_logs_flag():
        return False

    try:
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.exporter.otlp.proto.http._log_exporter import (
            OTLPLogExporter,
        )
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

        endpoint = otel_common.otlp_endpoint_for("logs")
        # Fail-safe (idêntico a otel_metrics/tracing): endpoint irresolvível → o
        # SDK monta '/v1/logs' relativo (No scheme supplied) e o BatchLogRecord
        # Processor spamma export falho. Desliga limpo com 1 warning.
        if not endpoint and not otel_common.sdk_env_endpoint_valid():
            logger.warning(
                "OTEL_LOGS_ENABLED=true mas nenhum endpoint OTLP com scheme "
                "(OTEL_EXPORTER_OTLP_ENDPOINT vazio/sem http[s]://) — logs OTel "
                "DESLIGADOS neste processo (evita spam '/v1/logs: No scheme supplied')"
            )
            _ENABLED = False
            return False
        exporter = OTLPLogExporter(endpoint=endpoint) if endpoint else OTLPLogExporter()
        _provider = LoggerProvider(resource=otel_common.build_resource())
        _provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
        set_logger_provider(_provider)

        # NÍVEL INFO+ no handler: evita inundar o backend com DEBUG, mas mantém o
        # sinal operacional. O root logger continua filtrando pelo seu próprio
        # nível antes de chegar aqui.
        _handler = LoggingHandler(level=logging.INFO, logger_provider=_provider)
        # Idempotência defensiva: worker_process_init pode, em recycles, disparar
        # mais de uma vez por processo — anexar 2 handlers duplicaria os logs no
        # backend OTel. Remove qualquer LoggingHandler OTel órfão antes de anexar.
        _root = logging.getLogger()
        for _h in list(_root.handlers):
            if isinstance(_h, LoggingHandler):
                _root.removeHandler(_h)
        _root.addHandler(_handler)

        _ENABLED = True
        logger.info(
            "OTel logs ativo (OTLP export, endpoint=%s)",
            endpoint or "<env padrão OTLP>",
        )
        return True
    except Exception:
        logger.warning(
            "OTEL_LOGS_ENABLED ligado mas logs OTel indisponíveis (pacotes "
            "opentelemetry-* ausentes?) — segue só com logging local",
            exc_info=True,
        )
        _ENABLED = False
        return False


def is_enabled() -> bool:
    return _ENABLED


def reset_for_tests() -> None:
    """Seam de teste: remove TODO handler OTel do root logger e zera o estado
    (robusto contra handler órfão de um init duplo não capturado)."""
    global _ENABLED, _INITIALIZED, _provider, _handler
    root = logging.getLogger()
    for h in list(root.handlers):
        # Casa por nome de classe p/ não importar o SDK (pacotes opcionais).
        if type(h).__name__ == "LoggingHandler":
            try:
                root.removeHandler(h)
            except Exception:  # pragma: no cover
                pass
    _ENABLED = False
    _INITIALIZED = False
    _provider = None
    _handler = None
