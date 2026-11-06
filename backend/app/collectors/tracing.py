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
_provider: Any = None
# Guarda anti-init-duplo: worker_process_init pode, em teoria, disparar mais de
# uma vez por processo em recycles — montar dois providers vaza exporters.
_INITIALIZED: bool = False

# Nome → ``SpanKind``. Resolvido tarde (pacotes opcionais). O tipo do span é o
# que os backends de APM usam para montar mapa de serviços e separar
# processamento de mensagem (CONSUMER) de chamada de saída (CLIENT).
_SPAN_KINDS = ("internal", "server", "client", "producer", "consumer")


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


def init_tracing(service_name: Optional[str] = None) -> bool:
    """Monta o SDK OTel UMA vez por processo. Retorna ``True`` se o tracing
    ficou ativo, ``False`` caso contrário (flag off, pacotes ausentes ou erro).

    ``service_name`` sobrepõe ``OTEL_SERVICE_NAME`` (a API exporta como serviço
    próprio). Idempotente: chamadas subsequentes não remontam o provider.
    """
    global _ENABLED, _tracer, _INITIALIZED, _provider

    if _INITIALIZED:
        return _ENABLED
    _INITIALIZED = True

    if not _otel_flag():
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        # Protocolo (http/protobuf ou grpc) e endpoint por sinal seguem a
        # especificação OTLP; config inválida ou pacote ausente ⇒ None com 1
        # warning, em vez de exportar para o lugar errado a cada ciclo.
        built = otel_common.build_exporter("traces")
        if built is None:
            _ENABLED = False
            return False
        exporter, endpoint_desc = built

        # Resource semântico completo (service.name/version/instance.id/
        # deployment.environment) — mesmo dos sinais de métricas/logs.
        resource = otel_common.build_resource(service_name)
        provider = TracerProvider(resource=resource, sampler=_build_sampler())
        provider.add_span_processor(BatchSpanProcessor(exporter))

        trace.set_tracer_provider(provider)
        _provider = provider
        # Do PRÓPRIO provider, não do global: o OTel só aceita definir o provider
        # global uma vez por processo, e um tracer global apontaria para o antigo.
        _tracer = provider.get_tracer("centralops.collectors")
        _ENABLED = True
        logger.info(
            "OTel tracing ativo (resource=%s, endpoint=%s)",
            getattr(resource, "attributes", {}).get("service.name", "?"),
            endpoint_desc,
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


def _span_kind(name: str) -> Any:
    """``SpanKind`` pelo nome (``internal`` quando desconhecido)."""
    from opentelemetry.trace import SpanKind

    key = (name or "internal").lower()
    return getattr(SpanKind, key.upper()) if key in _SPAN_KINDS else SpanKind.INTERNAL


def _record_exception(sp: Any, exc: BaseException) -> None:
    """Grava a exceção no span JÁ REDIGIDA e marca o span como erro.

    O SDK, por padrão, grava ``str(exc)`` e o traceback inteiro como evento — e a
    mensagem de exceção de cliente HTTP/banco carrega URL com token, parâmetro de
    query e credencial. Os spans abrem com ``record_exception=False`` e a
    gravação passa por aqui: mesmo evento ``exception`` da convenção semântica,
    com o texto redigido. A descrição do status leva só o TIPO da exceção."""
    try:
        import traceback

        from opentelemetry.trace import Status, StatusCode

        from ..core.logging_config import scrub_sensitive_text

        exc_type = type(exc).__name__
        stacktrace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        sp.add_event(
            "exception",
            attributes={
                "exception.type": exc_type,
                "exception.message": scrub_sensitive_text(str(exc)),
                "exception.stacktrace": scrub_sensitive_text(stacktrace),
            },
        )
        sp.set_status(Status(StatusCode.ERROR, exc_type))
        if not (getattr(sp, "attributes", None) or {}).get("error.type"):
            sp.set_attribute("error.type", exc_type)
    except Exception:  # pragma: no cover — tracing nunca derruba o pipeline
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
    except BaseException as exc:
        # Exceção do bloco do usuário: registra (redigida) e RE-PROPAGA (não
        # suprime) — autoretry_for do Celery depende disto. GeneratorExit /
        # KeyboardInterrupt não são falha da operação e não marcam erro.
        if isinstance(exc, Exception):
            _record_exception(sp, exc)
        if not cm.__exit__(*sys.exc_info()):
            raise
    else:
        cm.__exit__(None, None, None)


def _start(name: str, span_kind: str, context: Any = None) -> Callable[[], Any]:
    def _open() -> Any:
        kwargs: Dict[str, Any] = {
            "kind": _span_kind(span_kind),
            # A gravação de exceção é NOSSA (redigida) — ver _record_exception.
            "record_exception": False,
            "set_status_on_exception": False,
        }
        if context is not None:
            kwargs["context"] = context
        return _tracer.start_as_current_span(name, **kwargs)

    return _open


@contextmanager
def span(name: str, span_kind: str = "internal", **attributes: Any) -> Iterator[Any]:
    """Abre um span no contexto CORRENTE (ou no-op quando tracing off).

    Uso: ``with span("collect.cycle", **{"centralops.integration_id": 7}): ...``.
    Funciona dentro de código async (contextvars seguem o ``await``).
    """
    if not _ENABLED or _tracer is None:
        yield None
        return
    yield from _managed_span(_start(name, span_kind), attributes)


@contextmanager
def span_with_parent(
    name: str,
    carrier_in: Optional[Dict[str, str]],
    span_kind: str = "internal",
    **attributes: Any,
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

    yield from _managed_span(_start(name, span_kind, ctx), attributes)


def set_attributes(**attributes: Any) -> None:
    """Anexa atributos ao span CORRENTE (no-op com tracing off ou sem span)."""
    if not _ENABLED:
        return
    try:
        from opentelemetry import trace

        current = trace.get_current_span()
        if current.is_recording():
            _apply_attrs(current, attributes)
    except Exception:  # pragma: no cover — defensivo
        pass


def mark_error(error_type: str, **attributes: Any) -> None:
    """Marca o span CORRENTE como erro numa falha TRATADA (que não levanta).

    Um lote desviado para a DLQ sem exceção (disjuntor aberto, destino ausente,
    rejeição pelo sink) terminava o span como SUCESSO: o backend de APM mostrava
    0% de erro e uma amostragem "reter 100% dos erros" descartava justamente esses
    traces. ``error_type`` é um código curto e estável (vai em ``error.type``)."""
    if not _ENABLED:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.trace import Status, StatusCode

        current = trace.get_current_span()
        if not current.is_recording():
            return
        current.set_status(Status(StatusCode.ERROR, error_type))
        current.set_attribute("error.type", error_type)
        _apply_attrs(current, attributes)
    except Exception:  # pragma: no cover — defensivo
        pass


def record_dispatch_outcome(outcome: Any) -> None:
    """Grava no span CORRENTE o desfecho de ``dispatch_batch_to_destination``.

    O desfecho volta por VALOR (não pelo contexto) porque, com o loop persistente
    de dispatch, a corrotina roda em outra thread, onde o span da task não é o
    corrente. Quem abriu o span chama esta função com o retorno."""
    if not _ENABLED or outcome is None:
        return
    accepted = int(getattr(outcome, "accepted", 0) or 0)
    rejected = int(getattr(outcome, "rejected", 0) or 0)
    set_attributes(
        **{
            "centralops.destination.kind": getattr(outcome, "destination_kind", None),
            "centralops.events.accepted": accepted,
            "centralops.events.rejected": rejected,
            "centralops.shadow": True if getattr(outcome, "shadow", False) else None,
        }
    )
    dlq_reason = getattr(outcome, "dlq_reason", None)
    if dlq_reason:
        mark_error(str(dlq_reason), **{"centralops.dlq": True})
    elif rejected:
        mark_error(
            "partial_rejection" if accepted else "rejected", **{"centralops.dlq": True}
        )


def shutdown(timeout_millis: int = 5000) -> None:
    """Esvazia os spans em buffer e desliga o provider. Idempotente."""
    global _ENABLED
    provider = _provider
    _ENABLED = False
    if provider is None:
        return
    try:
        provider.force_flush(timeout_millis)
    except Exception:  # pragma: no cover — desligamento nunca levanta
        logger.debug("tracing: force_flush falhou", exc_info=True)
    try:
        provider.shutdown()
    except Exception:  # pragma: no cover
        logger.debug("tracing: shutdown falhou", exc_info=True)


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
    global _ENABLED, _tracer, _INITIALIZED, _provider
    _ENABLED = False
    _tracer = None
    _provider = None
    _INITIALIZED = False
