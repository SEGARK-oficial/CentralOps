"""Tracing distribuído OTel (export vendor-neutro).

Por quê: o time interno de SRE quer enxergar o fluxo INTEIRO de um evento —
collect → normalize → dispatch → por-destino — em QUALQUER backend de tracing
(Grafana Tempo, Jaeger, Datadog, Honeycomb…), sem amarrar o CentralOps a um
vendor. OpenTelemetry é o padrão neutro: instrumentamos uma vez, exportamos via
OTLP, o operador pluga o backend que quiser.

Invariantes (não-negociáveis):

1. **OFF por default + byte-idêntico.** ``OTEL_ENABLED=False`` (default) ⇒ todas
   as funções aqui são no-op de custo desprezível; nenhum atributo extra entra no
   wire Wazuh nem no payload das mensagens Celery. Liga-se por ambiente, não por
   código.
2. **Degrada com graça.** Os pacotes ``opentelemetry-*`` são OPCIONAIS (extras de
   deploy). Se ``OTEL_ENABLED`` ligar mas os pacotes não estiverem instalados, o
   import falha de forma controlada → seguimos SEM tracing (warning único), nunca
   quebrando o pipeline.
3. **Sem cardinalidade explosiva / sem PII.** Atributos de span são identificadores
   de baixa cardinalidade (integration_id, destination_id, stream, contagens).
   ``event_id`` NUNCA vira atributo — mesma disciplina do observability_store.
4. **Cross-process.** O ``traceparent`` (W3C Trace Context) é propagado pelo
   boundary Celery: o produtor (ciclo de coleta) injeta; a task de dispatch extrai
   e abre seu span como FILHO — o trace sobrevive ao fork/serialização.

Init: ``init_tracing()` roda 1× por processo filho prefork em
``worker_process_init`` (NUNCA no pai — ver celery_app). O SDK (provider +
exporter) é montado lá; aqui mantemos apenas os helpers de span/propagação.
"""

from __future__ import annotations

import logging
import sys
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, Optional

from . import otel_common

logger = logging.getLogger(__name__)

# Estado de processo. Só vira ``True`` quando init_tracing monta o SDK com
# sucesso E a flag está ligada. Enquanto ``False``, todo helper é no-op.
_ENABLED: bool = False
_tracer: Any = None
# Guarda anti-init-duplo: worker_process_init pode, em teoria, disparar mais de
# uma vez por processo em recycles — montar dois providers vaza exporters.
_INITIALIZED: bool = False


def _otel_flag() -> bool:
    """Lê a flag de runtime (delegado ao base compartilhado)."""
    return otel_common.otel_flag()


def _build_sampler() -> Any:
    """Sampler de HEAD configurável via ``OTEL_TRACES_SAMPLER_RATIO`` (0..1).

    ``ParentBased(TraceIdRatioBased(ratio))`` — decisão consistente ao longo de
    um trace inteiro (filhos seguem o pai) e determinística por trace_id, então
    o boundary Celive (produtor→task) não fragmenta o trace. Default 1.0 (mantém
    tudo) deixando o **tail-sampling no Collector** decidir (reter 100% de
    erro/DLQ, amostrar sucesso). Operadores que querem reduzir
    egress na origem baixam o ratio (ex.: 0.1 = 10%)."""
    from opentelemetry.sdk.trace.sampling import (
        ParentBased,
        TraceIdRatioBased,
    )

    from ..core.config import settings

    try:
        ratio = float(getattr(settings, "OTEL_TRACES_SAMPLER_RATIO", 1.0))
    except (TypeError, ValueError) as exc:
        ratio = 1.0
        logger.warning(
            "OTEL_TRACES_SAMPLER_RATIO inválida (%s) — usando 1.0 (100%%). "
            "Sem este aviso, a amostragem divergiria do esperado em silêncio.",
            type(exc).__name__,
        )
    ratio = min(1.0, max(0.0, ratio))
    return ParentBased(TraceIdRatioBased(ratio))


def init_tracing() -> bool:
    """Monta o SDK OTel UMA vez por processo. Retorna ``True`` se o tracing
    ficou ativo, ``False`` caso contrário (flag off, pacotes ausentes ou erro).

    Idempotente: chamadas subsequentes não remontam o provider.
    """
    global _ENABLED, _tracer, _INITIALIZED

    if _INITIALIZED:
        return _ENABLED
    _INITIALIZED = True

    if not _otel_flag():
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )

        # Resource semântico completo (service.name/version/instance.id/
        # deployment.environment) — mesmo dos sinais de métricas/logs.
        resource = otel_common.build_resource()
        provider = TracerProvider(resource=resource, sampler=_build_sampler())

        # Endpoint explícito (config) tem precedência; vazio ⇒ o SDK lê os envs
        # padrão OTEL_EXPORTER_OTLP_ENDPOINT/_TRACES_ENDPOINT — comportamento que
        # o time de SRE espera de qualquer app OTel-nativa.
        endpoint = otel_common.otlp_endpoint_for("traces")
        # Fail-safe (idêntico a otel_metrics): endpoint irresolvível → o SDK monta
        # '/v1/traces' relativo (No scheme supplied) e o BatchSpanProcessor spamma
        # export falho. Desliga limpo com 1 warning em vez de poluir o log.
        if not endpoint and not otel_common.sdk_env_endpoint_valid():
            logger.warning(
                "OTEL_ENABLED=true mas nenhum endpoint OTLP com scheme "
                "(OTEL_EXPORTER_OTLP_ENDPOINT vazio/sem http[s]://) — tracing OTel "
                "DESLIGADO neste processo (evita spam '/v1/traces: No scheme supplied')"
            )
            _ENABLED = False
            return False
        exporter = OTLPSpanExporter(endpoint=endpoint) if endpoint else OTLPSpanExporter()
        provider.add_span_processor(BatchSpanProcessor(exporter))

        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer("centralops.collectors")
        _ENABLED = True
        logger.info(
            "OTel tracing ativo (resource=%s, endpoint=%s)",
            getattr(resource, "attributes", {}).get("service.name", "?"),
            endpoint or "<env padrão OTLP>",
        )
        return True
    except Exception:
        # Pacotes ausentes (deploy sem os extras) ou falha de setup: seguimos sem
        # tracing. Um único warning — não polui log por evento.
        logger.warning(
            "OTEL_ENABLED ligado mas tracing indisponível (pacotes opentelemetry-* "
            "não instalados?) — pipeline segue SEM tracing",
            exc_info=True,
        )
        _ENABLED = False
        return False


def is_enabled() -> bool:
    """``True`` quando há tracing ativo neste processo (teste/diagnóstico)."""
    return _ENABLED and _tracer is not None


def _apply_attrs(span: Any, attributes: Dict[str, Any]) -> None:
    """Anexa atributos não-None ao span, à prova de exceção (tracing nunca
    pode derrubar o pipeline)."""
    try:
        for key, value in attributes.items():
            if value is not None:
                span.set_attribute(key, value)
    except Exception:  # pragma: no cover — defensivo
        pass


def _managed_span(
    start: Callable[[], Any], attributes: Dict[str, Any]
) -> Iterator[Any]:
    """Generator compartilhado: dirige o ciclo de vida de um span de forma que
    QUALQUER falha do SDK ao CRIAR o span degrade para no-op (invariante #2:
    tracing nunca derruba o pipeline), enquanto uma exceção do CÓDIGO DE NEGÓCIO
    dentro do span continua propagando normalmente (preserva retry/ack do Celery).
    """
    try:
        cm = start()
        sp = cm.__enter__()
    except Exception:  # SDK falhou ao abrir o span — segue SEM tracing.
        logger.debug("tracing: falha ao abrir span — no-op", exc_info=True)
        yield None
        return
    try:
        _apply_attrs(sp, attributes)
        yield sp
    except BaseException:
        # Exceção do bloco do usuário: deixa o span registrar (status/erro) e
        # RE-PROPAGA (não suprime) — autoretry_for do Celery depende disto.
        if not cm.__exit__(*sys.exc_info()):
            raise
    else:
        cm.__exit__(None, None, None)


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """Abre um span no contexto CORRENTE (ou no-op quando tracing off).

    Uso: ``with span("collect.cycle", **{"centralops.integration_id": 7}): ...``.
    Funciona dentro de código async (contextvars seguem o ``await``).
    """
    if not _ENABLED or _tracer is None:
        yield None
        return
    yield from _managed_span(lambda: _tracer.start_as_current_span(name), attributes)


@contextmanager
def span_with_parent(
    name: str, carrier_in: Optional[Dict[str, str]], **attributes: Any
) -> Iterator[Any]:
    """Abre um span como FILHO de um carrier W3C recebido pelo boundary Celery —
    usado no lado da task de dispatch para linkar ao ciclo de coleta.

    ``carrier_in`` é o dict ``{"traceparent": ..., "tracestate": ...}`` propagado
    pela task; vazio/``None`` ⇒ span raiz (sem pai). Tracing off ⇒ no-op.
    """
    if not _ENABLED or _tracer is None:
        yield None
        return

    ctx = None
    if carrier_in:
        try:
            from opentelemetry.propagate import extract

            ctx = extract(carrier_in)
        except Exception:  # pragma: no cover — propagator sempre presente se SDK ok
            ctx = None

    yield from _managed_span(
        lambda: _tracer.start_as_current_span(name, context=ctx), attributes
    )


def carrier() -> Dict[str, str]:
    """Headers W3C (``traceparent``/``tracestate``) do contexto CORRENTE, para
    cruzar o boundary Celery. Dict VAZIO quando tracing off — o produtor faz
    ``kwargs={**base, **tracing.carrier()}``, então OFF ⇒ kwargs byte-idêntico
    ao caminho legado (invariante #1)."""
    if not _ENABLED:
        return {}
    try:
        from opentelemetry.propagate import inject

        out: Dict[str, str] = {}
        inject(out)
        # Só propaga as chaves de trace-context (evita poluir kwargs da task).
        return {k: v for k, v in out.items() if k in ("traceparent", "tracestate")}
    except Exception:  # pragma: no cover — defensivo
        return {}


def reset_for_tests() -> None:
    """Seam de teste: zera o estado de processo para reexercitar init_tracing."""
    global _ENABLED, _tracer, _INITIALIZED
    _ENABLED = False
    _tracer = None
    _INITIALIZED = False
