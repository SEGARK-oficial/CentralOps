"""tracing distribuído OTel: gated + degrada com graça.

Estes testes rodam SEM exigir os pacotes ``opentelemetry-*`` instalados (extras
opcionais de deploy). Eles provam os invariantes que o tracing PROMETE:

1. OFF (default) ⇒ no-op total: ``carrier()`` vazio, spans são None, init False.
2. ON sem os pacotes ⇒ degrada para no-op (init False, warning), NUNCA levanta.
3. Producer byte-idêntico: com OTEL off, ``_enqueue_dispatch`` NÃO injeta
   ``traceparent`` nos kwargs Celery (wire/payload inalterado vs. legado).
4. Tasks de dispatch aceitam os kwargs de trace-context (back-compat de
   assinatura — mensagens antigas sem eles desserializam).
"""

from __future__ import annotations

import importlib.util
import inspect
import os
from unittest.mock import patch

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import pytest

from backend.app.collectors import tracing
from backend.app.core.config import settings

_HAS_OTEL = importlib.util.find_spec("opentelemetry") is not None


@pytest.fixture(autouse=True)
def _reset_tracing():
    """Cada teste parte de um estado de processo limpo (init é idempotente)."""
    tracing.reset_for_tests()
    yield
    tracing.reset_for_tests()


def _ev(sev: int) -> dict:
    """Evento mínimo com envelope ``_centralops`` (org=None = global)."""
    return {
        "_centralops": {"severity_id": sev, "integration_id": 1, "organization_id": None},
        "rule": {"level": sev},
    }


# ── Invariante 1: OFF é no-op total ──────────────────────────────────────


def test_off_is_total_noop():
    with patch.object(settings, "OTEL_ENABLED", False):
        assert tracing.init_tracing() is False
        assert tracing.is_enabled() is False
        assert tracing.carrier() == {}
        with tracing.span("collect.cycle", **{"centralops.integration_id": 7}) as sp:
            assert sp is None
        with tracing.span_with_parent("dispatch.wazuh", {"traceparent": "x"}, k=1) as sp:
            assert sp is None


# ── Invariante 2: ON sem pacotes degrada (não levanta) ───────────────────


def test_on_without_packages_degrades_gracefully():
    """OTEL_ENABLED ligado: se os pacotes existem, ativa; se não, no-op limpo."""
    with patch.object(settings, "OTEL_ENABLED", True):
        result = tracing.init_tracing()
        if _HAS_OTEL:
            assert result is True
            assert tracing.is_enabled() is True
        else:
            # Sem os extras instalados: NÃO levanta, retorna False, segue no-op.
            assert result is False
            assert tracing.is_enabled() is False
            assert tracing.carrier() == {}
            with tracing.span("x") as sp:
                assert sp is None


def test_init_is_idempotent():
    with patch.object(settings, "OTEL_ENABLED", False):
        assert tracing.init_tracing() is False
        # 2ª chamada não remonta provider nem muda o resultado.
        assert tracing.init_tracing() is False
        assert tracing.is_enabled() is False


# ── Invariante 3: producer byte-idêntico quando OFF ──────────────────────


def test_enqueue_dispatch_off_injects_no_traceparent(monkeypatch):
    """OTEL off ⇒ kwargs Celery byte-idênticos (sem traceparent/tracestate).

    O ``wazuh-default`` deixou de ser
    special-case — ele flui pela MESMA via uniforme ``dispatch_to_destination``
    (celery-mode, o default do ambiente de teste). A invariante #3 do tracing
    permanece: com OTEL off, ``tracing.carrier()`` é vazio, então NENHUM
    ``traceparent``/``tracestate`` é injetado nos kwargs do dispatch — o wire
    fica byte-idêntico ao legado (só ``destination_id`` + ``batch``).
    """
    import types as _types

    from backend.app.collectors import pipeline, routing
    from backend.app.collectors import tasks as _tasks

    # Isola o fan-out do DB: roteamento → sub-lote único p/ wazuh-default.
    monkeypatch.setattr(pipeline, "_load_destination_residency", lambda ids: {})
    monkeypatch.setattr(pipeline, "_load_wazuh_loop_destination_ids", lambda ids: frozenset())
    monkeypatch.setattr(pipeline, "_load_fallback_destination_id", lambda org: "wazuh-default")
    monkeypatch.setattr(pipeline, "_load_routes_for_org", lambda org: [])
    monkeypatch.setattr(
        routing,
        "route_batch",
        lambda *a, **k: _types.SimpleNamespace(
            routed=0, dropped=0, fallback=2, residency_blocked=0,
            loop_blocked=0, unrouted=0, unrouted_events=[], per_route={},
            sub_batches={"wazuh-default": [_ev(5), _ev(2)]},
        ),
    )

    with (
        patch.object(settings, "OTEL_ENABLED", False),
        patch.object(_tasks.dispatch_to_destination, "apply_async") as md,
    ):
        pipeline._enqueue_dispatch([_ev(5), _ev(2)])

    md.assert_called_once()
    call = md.call_args
    sent_kwargs = call.kwargs["kwargs"]
    # wazuh-default agora vai pela via uniforme dispatch_to_destination.
    assert sent_kwargs["destination_id"] == "wazuh-default"
    # OTEL off ⇒ kwargs byte-idênticos: só destination_id + batch, sem trace-context.
    assert set(sent_kwargs.keys()) == {"destination_id", "batch"}
    assert "traceparent" not in sent_kwargs and "tracestate" not in sent_kwargs


# ── Invariante 4: tasks aceitam trace-context (back-compat) ──────────────


def test_dispatch_tasks_accept_trace_context_kwargs():
    """Assinaturas aceitam traceparent/tracestate com default None.

    A via dupla Wazuh foi removida — ``dispatch_to_wazuh``
    não existe mais. A única tarefa de dispatch é ``dispatch_to_destination``
    (rota uniforme p/ TODO destino, incl. wazuh-default).
    """
    from backend.app.collectors.tasks import dispatch_to_destination

    for task in (dispatch_to_destination,):
        params = inspect.signature(task.run).parameters
        assert "traceparent" in params and params["traceparent"].default is None
        assert "tracestate" in params and params["tracestate"].default is None


class _ExplodingTracer:
    """Tracer cujo start_as_current_span SEMPRE levanta — simula falha do SDK."""

    def start_as_current_span(self, *a, **k):
        raise RuntimeError("SDK boom")


class _FakeSpan:
    def __init__(self):
        self.attrs = {}

    def set_attribute(self, k, v):
        self.attrs[k] = v


class _FakeCM:
    def __init__(self, span):
        self._span = span

    def __enter__(self):
        return self._span

    def __exit__(self, *exc):
        return False  # nunca suprime — espelha o use_span do OTel


class _OkTracer:
    def __init__(self):
        self.span = _FakeSpan()

    def start_as_current_span(self, *a, **k):
        return _FakeCM(self.span)


def test_span_degrades_to_noop_when_sdk_raises_on_create(monkeypatch):
    """Invariante #2: se o SDK falha ao ABRIR o span, degrada p/ no-op — NÃO
    deixa a exceção do tracing derrubar o pipeline (collect/dispatch)."""
    monkeypatch.setattr(tracing, "_ENABLED", True)
    monkeypatch.setattr(tracing, "_tracer", _ExplodingTracer())
    ran = False
    with tracing.span("collect.cycle", a=1) as sp:
        assert sp is None  # no-op
        ran = True
    assert ran  # o corpo executou normalmente, sem exceção
    # span_with_parent idem
    with tracing.span_with_parent("dispatch.wazuh", {"traceparent": "x"}) as sp:
        assert sp is None


def test_span_reraises_business_exception(monkeypatch):
    """Exceção do CÓDIGO DE NEGÓCIO dentro do span re-propaga (Celery autoretry
    depende disso) — o span registra mas não suprime."""
    monkeypatch.setattr(tracing, "_ENABLED", True)
    monkeypatch.setattr(tracing, "_tracer", _OkTracer())

    class _Boom(Exception):
        pass

    with pytest.raises(_Boom):
        with tracing.span("dispatch.destination", **{"centralops.destination_id": "d1"}) as sp:
            assert sp is not None
            assert sp.attrs["centralops.destination_id"] == "d1"
            raise _Boom()


def test_carrier_filters_to_trace_context_keys_only():
    """Quando ativo, carrier() só emite traceparent/tracestate (sem poluir kwargs)."""
    if not _HAS_OTEL:
        pytest.skip("opentelemetry não instalado neste ambiente")
    with patch.object(settings, "OTEL_ENABLED", True):
        assert tracing.init_tracing() is True
        with tracing.span("collect.cycle"):
            c = tracing.carrier()
        assert set(c.keys()) <= {"traceparent", "tracestate"}
        assert "traceparent" in c  # há span ativo ⇒ contexto propagável
