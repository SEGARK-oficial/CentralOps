"""Série obs:source:{id}:ingested — atribuição temporal correta.

BUG DE PRODUÇÃO (visto via MCP no /flow): a rota de drop exibia 9.051 ev/min
enquanto as fontes wazuh que a alimentam somavam 5.999 ev/min — 1,5× MAIS do que
existia, aritmeticamente impossível.

Causa: `ingested` era gravado UMA vez, no fim do ciclo de coleta, com o total.
Como record_counter bucketiza pelo relógio da CHAMADA, um ciclo de vários minutos
despejava tudo no minuto de encerramento — enquanto os contadores de ROTA são
escritos por lote, continuamente. Lidas na mesma janela de 5 min, as séries
ficavam incomparáveis: minutos sem cycle-end mostravam rota sem fonte.

É a mesma armadilha que o InVolumeAccumulator do metering já documenta ("um ciclo
bulk de 12min não pode atribuir tudo ao minuto final").
"""
from __future__ import annotations

from backend.app.collectors import pipeline


class _Spy:
    def __init__(self):
        self.calls = []

    def __call__(self, kind, oid, metric, value=1.0, *, now=None, **kw):
        self.calls.append((kind, oid, metric, float(value)))


def test_records_into_the_source_series(monkeypatch):
    from backend.app.collectors import observability_store as obs

    spy = _Spy()
    monkeypatch.setattr(obs, "record_counter", spy)

    pipeline._record_source_ingested(42, 200)

    assert spy.calls == [("source", "42", "ingested", 200.0)]


def test_incremental_flushes_accumulate(monkeypatch):
    """Três flushes de lote no ciclo → três gravações, cada uma no seu minuto.
    Antes havia UMA gravação de 600 no minuto final."""
    from backend.app.collectors import observability_store as obs

    spy = _Spy()
    monkeypatch.setattr(obs, "record_counter", spy)

    for n in (200, 200, 200):
        pipeline._record_source_ingested(42, n)

    assert len(spy.calls) == 3
    assert sum(v for *_r, v in spy.calls) == 600.0


def test_zero_or_negative_is_a_noop(monkeypatch):
    from backend.app.collectors import observability_store as obs

    spy = _Spy()
    monkeypatch.setattr(obs, "record_counter", spy)

    pipeline._record_source_ingested(42, 0)
    pipeline._record_source_ingested(42, -5)

    assert spy.calls == []


def test_never_raises_even_if_the_store_explodes(monkeypatch):
    """A instrumentação jamais pode derrubar a coleta."""
    from backend.app.collectors import observability_store as obs

    def _boom(*a, **k):
        raise RuntimeError("redis down")

    monkeypatch.setattr(obs, "record_counter", _boom)
    pipeline._record_source_ingested(42, 10)  # não levanta
