"""Traces da API HTTP (FastAPI) — ingestão push, MCP e console.

Até aqui só os processos de coleta/entrega exportavam OTel; a API era invisível
para o APM. OFF por default e com toggle próprio (``OTEL_API_TRACING_ENABLED``,
além de ``OTEL_ENABLED``): o volume é proporcional ao tráfego HTTP.

Dois tempos, de propósito:

* :func:`instrument_app` roda no IMPORT, logo após ``app = FastAPI(...)`` — o
  middleware precisa estar na pilha antes do primeiro request. O tracer obtido
  nesse momento é o proxy global do OpenTelemetry;
* :func:`start` roda no ``lifespan`` (por processo, depois de um eventual fork) e
  monta o provider real; a partir daí o proxy passa a produzir spans.

Segredo em URL: a instrumentação grava a URL com a query string e só redige
parâmetros de assinatura de nuvem. ``?token=...`` sairia no span — o hook do
servidor reescreve os atributos de URL com a mesma redação dos logs.
"""

from __future__ import annotations

import logging
from typing import Any

from .config import settings

logger = logging.getLogger(__name__)

#: Atributos de URL gravados pela instrumentação ASGI (convenções antiga e nova).
_URL_ATTRIBUTES = ("http.target", "http.url", "url.full", "url.query")


def enabled() -> bool:
    return bool(settings.OTEL_ENABLED and settings.OTEL_API_TRACING_ENABLED)


def scrub_request_span(span: Any, _scope: Any = None) -> None:
    """``server_request_hook``: redige os atributos de URL do span do request."""
    try:
        from .logging_config import scrub_sensitive_text

        attributes = getattr(span, "attributes", None) or {}
        for key in _URL_ATTRIBUTES:
            value = attributes.get(key)
            if isinstance(value, str):
                scrubbed = scrub_sensitive_text(value)
                if scrubbed != value:
                    span.set_attribute(key, scrubbed)
    except Exception:  # pragma: no cover — tracing nunca derruba o request
        pass


def instrument_app(app: Any) -> bool:
    """Instala a instrumentação FastAPI se o tracing da API estiver ligado."""
    if not enabled():
        return False
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError:
        logger.warning(
            "OTEL_API_TRACING_ENABLED=true mas o pacote "
            "opentelemetry-instrumentation-fastapi não está instalado — API segue sem traces"
        )
        return False
    try:
        FastAPIInstrumentor.instrument_app(
            app,
            server_request_hook=scrub_request_span,
            excluded_urls=settings.OTEL_API_EXCLUDED_URLS or None,
            # Os spans internos de receive/send do ASGI multiplicam o volume sem
            # informação útil para quem olha latência e erro por rota.
            exclude_spans=["receive", "send"],
        )
        return True
    except Exception:  # pragma: no cover — instrumentação nunca impede o boot
        logger.warning("Falha ao instrumentar a API com OTel — segue sem traces", exc_info=True)
        return False


def start() -> bool:
    """Monta o provider de traces da API (chamado no ``lifespan``)."""
    if not enabled():
        return False
    from ..collectors import tracing

    return tracing.init_tracing(service_name=settings.OTEL_API_SERVICE_NAME)


def stop() -> None:
    """Esvazia os spans em buffer no desligamento da API."""
    if not enabled():
        return
    from ..collectors import tracing

    tracing.shutdown()
