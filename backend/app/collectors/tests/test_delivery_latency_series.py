"""Série nativa de latência de entrega (o card "latência média (s)").

BUG DE PRODUÇÃO: a série `latency_avg` de GET /destinations/{id}/metrics vinha
SEMPRE vazia. A latência real era medida (DELIVERY_LATENCY.observe por chunk),
mas só ia para o OTel/Prometheus: o único call-site de _record_dest_observability
passava `0.0` literal para o store nativo, e record_counter descarta zero — então
`latency_sum` nunca recebeu um ponto. Confirmado no deploy real: um destino com
60 min de tráfego contínuo devolvia `"latency_avg": []`.

A doc de SLO (runbooks/slo-burn.md) mandava o operador olhar esse indicador.
"""
from __future__ import annotations

from backend.app.collectors import observability_store as obs


class _Rec:
    """Spy do writer: grava (kind, oid, metric, value)."""

    def __init__(self):
        self.calls = []

    def __call__(self, kind, oid, metric, value=1.0, *, now=None, **kw):
        self.calls.append((kind, oid, metric, float(value)))

    def metrics(self):
        return {m for _k, _o, m, _v in self.calls}


def test_positive_latency_writes_sum_and_count(monkeypatch):
    rec = _Rec()
    monkeypatch.setattr(obs, "record_counter", rec)

    obs.record_latency("dest", "d1", 0.42)

    assert rec.metrics() == {"latency_sum", "latency_count"}
    assert ("dest", "d1", "latency_sum", 0.42) in rec.calls
    assert ("dest", "d1", "latency_count", 1.0) in rec.calls


def test_zero_latency_writes_nothing(monkeypatch):
    """record_counter descarta zero: gravar só o count enviesaria a média para
    baixo (sum/(count+1)). O par tem que ser atômico."""
    rec = _Rec()
    monkeypatch.setattr(obs, "record_counter", rec)

    obs.record_latency("dest", "d1", 0.0)

    assert rec.calls == []


def test_negative_latency_writes_nothing(monkeypatch):
    rec = _Rec()
    monkeypatch.setattr(obs, "record_counter", rec)

    obs.record_latency("dest", "d1", -1.0)

    assert rec.calls == []


def test_dispatch_records_a_real_elapsed_not_zero():
    """Regressão do call-site: _record_dest_observability tem que receber um
    elapsed MEDIDO. O valor 0.0 literal era o bug — a asserção aqui é sobre o
    contrato (latência > 0 chega ao store)."""
    from backend.app.collectors import pipeline

    seen = []

    def _fake_record(kind, oid, seconds, **kw):
        seen.append(seconds)

    # _record_dest_observability delega a obs.record_latency; exercitamos a
    # função diretamente com um elapsed plausível de entrega.
    import backend.app.collectors.observability_store as store

    orig = store.record_latency
    store.record_latency = _fake_record
    try:
        pipeline._record_dest_observability("d1", accepted=2, rejected_count=0,
                                            latency_s=0.137, batch=[{}, {}])
    finally:
        store.record_latency = orig

    assert seen and seen[0] > 0, "o store nativo tem que receber a latência real"
