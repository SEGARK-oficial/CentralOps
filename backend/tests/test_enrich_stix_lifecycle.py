"""Ciclo de vida STIX (W4.5): o que a carga descarta é CONTADO, e o que vence
depois da carga é recusado no HIT.

Antes: um feed com 80% de intel vencida virava "tabela de 20% do tamanho" sem
explicação; e um indicador com ``valid_until`` daqui a 20 min continuava HIT
até a próxima carga (horas). Os testes provam as duas pontas e o caminho até a
métrica/atividade — sem tocar rede (sessão HTTP falsa).
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend.app.collectors.enrich import enrichers as _enrichers  # noqa: F401
from backend.app.collectors.enrich import registry as registry_mod
from backend.app.collectors.enrich import runtime as runtime_mod
from backend.app.collectors.enrich.contract import EnrichContext, EnricherCapabilities, EnricherRegistration
from backend.app.collectors.enrich.enrichers.taxii import TaxiiEnricher
from backend.app.collectors.enrich.runtime import ExpiringLookupTable
from backend.app.collectors.enrich.stix import SKIP_REASONS, parse_indicator, valid_until_epoch


def _iso(delta_s: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_s)).isoformat().replace("+00:00", "Z")


# ── parse: cada descarte tem motivo ────────────────────────────────────────

def test_parse_indicator_conta_cada_motivo_de_descarte():
    stats: dict = {}
    objs = [
        {"type": "malware", "name": "x"},
        {"type": "indicator", "pattern": "[ipv4-addr:value = '1.1.1.1']", "revoked": True},
        {"type": "indicator", "pattern": "[ipv4-addr:value = '2.2.2.2']", "valid_until": _iso(-60)},
        {"type": "indicator", "pattern": "[ipv4-addr:value = '3.3.3.3']", "confidence": 10},
        # confiança alta de propósito: a ordem de descarte é confiança ANTES do
        # padrão, e sem isso o objeto seria contado como low_confidence.
        {"type": "indicator", "pattern": "[x-custom:foo = 'bar']", "confidence": 90},
        {"type": "indicator", "pattern": "[ipv4-addr:value = '4.4.4.4']", "confidence": 90, "valid_until": _iso(3600)},
    ]
    kept = [parse_indicator(o, min_confidence=50, stats=stats) for o in objs]
    assert [k for k in kept if k] == [kept[-1]]
    assert stats == {
        "not_indicator": 1, "revoked": 1, "expired": 1, "low_confidence": 1, "unsupported_pattern": 1,
    }
    assert set(stats) <= set(SKIP_REASONS)


def test_parse_sem_stats_continua_funcionando():
    assert parse_indicator({"type": "indicator", "pattern": "[ipv4-addr:value = '5.5.5.5']"}) is not None


def test_valid_until_epoch_le_iso_e_tolera_lixo():
    assert valid_until_epoch(None) is None
    assert valid_until_epoch("nunca") is None
    ep = valid_until_epoch("2030-01-01T00:00:00Z")
    assert ep is not None and ep > time.time()


# ── lookup: honra valid_until DEPOIS da carga ──────────────────────────────

def test_hit_vira_miss_quando_o_indicador_vence_entre_cargas(monkeypatch):
    counted: list = []
    monkeypatch.setattr(runtime_mod, "_count_skipped", lambda enricher, reason, n=1: counted.append((enricher, reason, n)))
    tabela = ExpiringLookupTable(
        {
            "1.1.1.1": {"kind": "ip", "valid_until": _iso(120)},
            "2.2.2.2": {"kind": "ip"},  # sem validade: nunca vence
        },
        enricher="taxii",
    )
    assert tabela.lookup("1.1.1.1") is not None
    assert tabela.lookup("2.2.2.2") is not None
    # O relógio anda 10 minutos sem recarga (guarda o relógio real: o módulo
    # ``time`` é o mesmo objeto aqui e no runtime — patchar direto recursa).
    real_now = time.time()
    monkeypatch.setattr(runtime_mod.time, "time", lambda: real_now + 600)
    assert tabela.lookup("1.1.1.1") is None
    assert tabela.lookup("2.2.2.2") is not None
    assert counted == [("taxii", "expired_at_lookup", 1)]
    assert tabela.entry_count == 2  # a tabela não muda; só a resposta


def test_load_stats_e_exposto_e_vazio_por_padrao():
    t = ExpiringLookupTable({"a": {"x": 1}}, enricher="opencti", load_stats={"expired": 3, "revoked": 0})
    assert t.load_stats == {"expired": 3, "revoked": 0}
    assert runtime_mod._load_stats_of(t) == {"expired": 3}  # zero não é notícia
    assert runtime_mod._load_stats_of(object()) == {}


# ── TAXII: a carga devolve a tabela expirável com as estatísticas ──────────

class _Resp:
    def __init__(self, body): self._b = body; self.status = 200
    async def json(self, content_type=None): return self._b
    def raise_for_status(self): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


class _Session:
    def __init__(self, body): self._b = body
    def get(self, url, headers=None, params=None): return _Resp(self._b)
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


def test_taxii_carrega_tabela_expiravel_com_estatisticas(monkeypatch):
    import backend.app.collectors.enrich.enrichers.taxii as mod

    body = {"more": False, "objects": [
        {"type": "indicator", "pattern": "[ipv4-addr:value = '1.1.1.1']", "confidence": 90, "valid_until": _iso(3600)},
        {"type": "indicator", "pattern": "[ipv4-addr:value = '2.2.2.2']", "confidence": 90, "valid_until": _iso(-3600)},
        {"type": "indicator", "pattern": "[ipv4-addr:value = '3.3.3.3']", "confidence": 90, "revoked": True},
    ]}
    monkeypatch.setattr(mod.aiohttp, "ClientSession", lambda *a, **k: _Session(body))
    monkeypatch.setattr(mod.aiohttp, "TCPConnector", lambda *a, **k: None)
    tabela = asyncio.run(TaxiiEnricher({"url": "https://tip.exemplo", "collection": "c1"}).load(EnrichContext(organization_id=1)))
    assert isinstance(tabela, ExpiringLookupTable)
    assert tabela.entry_count == 1 and tabela.lookup("1.1.1.1") is not None
    assert tabela.load_stats == {"expired": 1, "revoked": 1}


# ── runtime: estatísticas viram métrica e detail do log de atividade ───────

class _StatsTable:
    load_stats = {"expired": 7, "revoked": 2}
    entry_count = 3
    approx_bytes = 100
    def lookup(self, key): return None


def test_runtime_emite_descartes_na_metrica_e_no_log_de_atividade(monkeypatch):
    class _Enr:
        caps = EnricherCapabilities(key_kinds=frozenset({"ip"}), mode="local")
        def __init__(self, cfg): pass
        async def load(self, ctx): return _StatsTable()

    registry_mod.register(EnricherRegistration(name="fake_stats", factory=lambda cfg: _Enr(cfg), caps=_Enr.caps))
    counted: list = []; activity: list = []
    monkeypatch.setattr(runtime_mod, "_count_skipped", lambda e, r, n=1: counted.append((e, r, n)))

    async def _act(org_id, kind, **kw): activity.append((kind, kw))
    monkeypatch.setattr(runtime_mod, "_activity", _act)

    rt = runtime_mod.EnrichRuntime(max_table_bytes=1 << 20, lru_bytes=1 << 20)
    rule = SimpleNamespace(rule_id="r1", enricher="fake_stats", table=None, source=None)
    policy = SimpleNamespace(local_rules=lambda: [rule])
    tables = asyncio.run(rt.load_tables(policy, EnrichContext(organization_id=9)))

    assert "r1" in tables
    assert sorted(counted) == [("fake_stats", "expired", 7), ("fake_stats", "revoked", 2)]
    kind, kw = activity[-1]
    assert kind == "table_load" and kw["ok"] is True and kw["entries"] == 3
    assert kw["detail"] == "descartados na carga: expired=7, revoked=2"
