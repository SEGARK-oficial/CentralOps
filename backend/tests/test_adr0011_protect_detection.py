"""Fail-safe de detecção ``Route.protect_detection``.

Esta é a PRÉ-CONDIÇÃO de segurança (redução de volume) que aterrissa ANTES do
sampling. Cobre:
  * defaults das flags ``REDUCTION_SAMPLE_*``;
  * a coluna ``routes.protect_detection`` (default TRUE = protege) via ORM;
  * a propagação para ``CompiledRoute`` (None/ausente → default seguro True; False
    explícito do operador preservado);
  * a migration leve (adiciona a coluna, idempotente, default seguro em linhas legadas).

Nenhuma alavanca de redução AINDA lê isto — o sampling que o consome vem depois.
"""
from __future__ import annotations

import asyncio
import os
import types

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.collectors.pipeline import _compile_route_row
from backend.app.collectors.routing.engine import CompiledRoute, SamplingConfig, route_batch
from backend.app.core.config import settings
from backend.app.db import database as _db_module
from backend.app.db import models


def _row(**over):
    """Um ``Route``-like mínimo p/ ``_compile_route_row`` (lê getattr + JSON str)."""
    base = dict(
        id=1, name="r", priority=100, condition="{}", action="route",
        destination_ids="[]", is_final=True, enabled=True,
        canary_percent=100, protect_detection=True, pii_redaction=None,
    )
    base.update(over)
    return types.SimpleNamespace(**base)


# ── Flags (defaults seguros) ─────────────────────────────────────────────────

def test_config_flag_defaults():
    # Protege por default; sampling desligado (forward-looking, no-op).
    assert settings.REDUCTION_SAMPLE_PROTECT_DETECTION is True
    assert settings.REDUCTION_SAMPLE_ENABLED is False


# ── CompiledRoute default + wiring de compilação ─────────────────────────────

def test_compiledroute_defaults_protect_true():
    r = CompiledRoute(id="x", name="x", priority=1, condition={}, action="route", destination_ids=(), is_final=True)
    assert r.protect_detection is True


@pytest.mark.parametrize("stored,expected", [(True, True), (False, False), (None, True)])
def test_compile_row_preserves_protect_detection(stored, expected):
    # None/ausente → default seguro (True); False EXPLÍCITO do operador é preservado.
    assert _compile_route_row(_row(protect_detection=stored)).protect_detection is expected


def test_compile_row_missing_attr_defaults_true():
    row = _row()
    delattr(row, "protect_detection")  # rota vinda de schema legado (pré-migration)
    assert _compile_route_row(row).protect_detection is True


# ── Coluna no modelo (default via ORM) ───────────────────────────────────────

def test_model_column_defaults_true(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/m.db", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    _db_module.Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    with Session() as s:
        r = models.Route(name="r", priority=100, condition="{}", action="route", destination_ids="[]")
        s.add(r)
        s.commit()
        s.refresh(r)
        assert r.protect_detection is True
        assert r.sample_percent == 100  # default: sem amostragem (byte-idêntico)


# ── Migration leve: adiciona a coluna, default seguro, idempotente ───────────

def test_migration_adds_column_default_true_idempotent(monkeypatch, tmp_path):
    url = f"sqlite:///{tmp_path}/mig.db"
    engine = create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    # Schema LEGADO mínimo: routes SEM protect_detection (mas COM canary_percent, p/ o
    # bloco de migration de routes só precisar adicionar pii_redaction + protect_detection).
    with engine.begin() as c:
        c.execute(text(
            "CREATE TABLE routes (id INTEGER PRIMARY KEY, name TEXT, canary_percent INTEGER DEFAULT 100)"
        ))
        c.execute(text("INSERT INTO routes (id, name) VALUES (1, 'legacy')"))
    assert "protect_detection" not in {col["name"] for col in inspect(engine).get_columns("routes")}

    monkeypatch.setattr(_db_module, "engine", engine)
    _db_module._run_lightweight_migrations()  # adiciona

    cols = {col["name"] for col in inspect(engine).get_columns("routes")}
    assert "protect_detection" in cols
    with engine.connect() as c:
        # a linha legada ganhou o default SEGURO (protege) — não vira sampling silencioso.
        assert c.execute(text("SELECT protect_detection FROM routes WHERE id=1")).scalar() in (1, True)

    _db_module._run_lightweight_migrations()  # idempotente: re-roda sem erro
    cols2 = {col["name"] for col in inspect(engine).get_columns("routes")}
    assert "protect_detection" in cols2
    # a mesma migration leve também adiciona sample_percent + suppress_*.
    assert "sample_percent" in cols2
    assert {"suppress_key", "suppress_allow", "suppress_window_s"} <= cols2
    with engine.connect() as c:
        assert c.execute(text("SELECT sample_percent FROM routes WHERE id=1")).scalar() == 100
        # defaults = desligado (suppress_key NULL, allow 0, window 30)
        row = c.execute(text(
            "SELECT suppress_key, suppress_allow, suppress_window_s FROM routes WHERE id=1"
        )).first()
        assert row == (None, 0, 30)


# ── sampling estatístico de redução (Community) ─────

def _env(i, **labels):
    base = {"event_id": f"ev-{i}", "organization_id": 1}
    base.update(labels)
    return {"_centralops": base, "normalized": {}, "raw": {}}


def _sroute(rid="r1", *, dests=("d1",), sample_percent=100, protect=True, condition=None):
    return CompiledRoute(
        id=rid, name=rid, priority=100, condition=condition or {}, action="route",
        destination_ids=tuple(dests), is_final=True,
        protect_detection=protect, sample_percent=sample_percent,
    )


def test_compile_row_wires_sample_percent():
    assert _compile_route_row(_row(sample_percent=25)).sample_percent == 25
    # 0 explícito (drena tudo por sampling) é preservado; None/ausente → 100.
    assert _compile_route_row(_row(sample_percent=0)).sample_percent == 0
    assert _compile_route_row(_row(sample_percent=None)).sample_percent == 100


def test_sampling_reduces_volume_toward_percent():
    batch = [_env(i) for i in range(200)]
    res = route_batch(batch, [_sroute(sample_percent=50, protect=False)], sampling=SamplingConfig(enabled=True))
    delivered = len(res.sub_batches.get("d1", []))
    assert 60 <= delivered <= 140  # ~50% de 200 (consistent-hash determinístico, banda larga)
    assert res.sampled == 200 - delivered
    assert res.sampled_per_route.get("r1") == 200 - delivered


def test_protect_detection_route_never_sampled():
    batch = [_env(i) for i in range(50)]
    # sample_percent=0 amostraria TUDO p/ fora — mas a rota é protect_detection.
    res = route_batch(batch, [_sroute(sample_percent=0, protect=True)],
                      sampling=SamplingConfig(enabled=True, protect_detection_enforced=True))
    assert len(res.sub_batches.get("d1", [])) == 50  # nada amostrado (fail-safe)
    assert res.sampled == 0


def test_global_override_can_sample_protected_route():
    batch = [_env(i) for i in range(50)]
    res = route_batch(batch, [_sroute(sample_percent=0, protect=True)],
                      sampling=SamplingConfig(enabled=True, protect_detection_enforced=False))
    assert res.sub_batches.get("d1", []) == []  # override global: 0% amostra tudo
    assert res.sampled == 50


def test_sampling_off_is_byte_identical():
    batch = [_env(i) for i in range(50)]
    r = _sroute(sample_percent=10, protect=False)
    assert len(route_batch(batch, [r], sampling=None).sub_batches.get("d1", [])) == 50
    assert len(route_batch(batch, [r], sampling=SamplingConfig(enabled=False)).sub_batches.get("d1", [])) == 50


def test_sample_percent_100_is_noop_even_enabled():
    batch = [_env(i) for i in range(30)]
    res = route_batch(batch, [_sroute(sample_percent=100, protect=False)], sampling=SamplingConfig(enabled=True))
    assert len(res.sub_batches.get("d1", [])) == 30 and res.sampled == 0


def test_kept_events_decorated_with_sample_rate_without_mutating_original():
    batch = [_env(i) for i in range(200)]
    res = route_batch(batch, [_sroute(sample_percent=50, protect=False)], sampling=SamplingConfig(enabled=True))
    kept = res.sub_batches.get("d1", [])
    assert kept and all(e["_centralops"].get("sample_rate") == 0.5 for e in kept)
    # Full-fidelity: o envelope ORIGINAL não é mutado (sample_rate só na cópia entregue).
    assert all("sample_rate" not in b["_centralops"] for b in batch)


# ── CostConfig (Community) + contabilização do trimming ────

def test_costconfig_defaults_and_validation():
    from backend.app.collectors.output.delivery_config import CostConfig, DeliveryConfig

    c = CostConfig()
    assert c.cost_per_gb == 0.0 and c.currency == "USD" and c.tier_label == ""
    assert CostConfig(cost_per_gb=2.5, currency="EUR", tier_label="Sentinel").cost_per_gb == 2.5
    # DeliveryConfig ganha o sub-bloco cost (default 0 = sem preço).
    assert DeliveryConfig().cost.cost_per_gb == 0.0
    for bad in (dict(cost_per_gb=-1), dict(currency="US"), dict(cost_per_gb=1e9)):
        with pytest.raises(Exception):
            CostConfig(**bad)


def test_record_trim_saving_is_gated_and_measures_delta(monkeypatch):
    from backend.app.collectors.reduction import metering

    captured = []
    monkeypatch.setattr(metering, "record_saving",
                        lambda org, dest, reason, *, bytes_: captured.append((org, dest, reason, bytes_)))
    raw = {"a": "x" * 1000, "b": 1}
    reduced = {"a": "x" * 10, "b": 1}  # trim tirou ~990 bytes

    # Ambas as flags OFF → no-op (sem serialização, sem record).
    monkeypatch.setattr(metering.settings, "COST_METERING_ENABLED", False, raising=False)
    monkeypatch.setattr(metering.settings, "REDUCTION_TRIM_ENABLED", False, raising=False)
    metering.record_trim_saving(1, raw, reduced)
    assert captured == []

    # metering on mas TRIM off → ainda no-op.
    monkeypatch.setattr(metering.settings, "COST_METERING_ENABLED", True, raising=False)
    metering.record_trim_saving(1, raw, reduced)
    assert captured == []

    # ambas on → mede o delta e contabiliza reason=trim (dest None = pré-fan-out).
    monkeypatch.setattr(metering.settings, "REDUCTION_TRIM_ENABLED", True, raising=False)
    metering.record_trim_saving(1, raw, reduced)
    assert len(captured) == 1
    org, dest, reason, saved = captured[0]
    assert org == 1 and dest is None and reason == "trim" and saved > 900

    # reduced is None (engine não trimou) → no-op mesmo com flags on.
    metering.record_trim_saving(1, raw, None)
    assert len(captured) == 1


# ── suppression durável por assinatura ──────────────────────────────

def _run(coro):
    return asyncio.run(coro)


def test_suppress_signature_stable_and_key_scoped():
    from backend.app.collectors.state.dedupe import suppress_signature

    labels = {"src_ip": "1.2.3.4", "event_type": "login"}
    s1 = suppress_signature(labels, "src_ip,event_type")
    # ignora labels FORA da chave (agrupa pelo que importa)
    s2 = suppress_signature({**labels, "x": 9}, "src_ip,event_type")
    assert s1 == s2 and len(s1) == 16
    assert suppress_signature(labels, "src_ip") != s1  # chave diferente → assinatura diferente


def test_claim_suppress_allows_first_n_then_suppresses():
    import fakeredis.aioredis
    from backend.app.collectors.state.dedupe import claim_suppress

    async def go():
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        out = [await claim_suppress(r, "r1", "sig", 2, 60) for _ in range(4)]
        await r.aclose()
        return out

    res = _run(go())
    assert [k for k, _ in res] == [True, True, False, False]  # 2 passam, resto suprime
    assert [c for _, c in res] == [1, 2, 3, 4]                 # count preserva a contagem real


def test_claim_suppress_disabled_when_allow_zero():
    import fakeredis.aioredis
    from backend.app.collectors.state.dedupe import claim_suppress

    async def go():
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        out = await claim_suppress(r, "r1", "sig", 0, 60)  # allow=0 → desligado, sem I/O
        await r.aclose()
        return out

    assert _run(go()) == (True, 0)


def test_compile_row_wires_suppress_fields():
    cr = _compile_route_row(_row(suppress_key="src_ip", suppress_allow=5, suppress_window_s=10))
    assert cr.suppress_key == "src_ip" and cr.suppress_allow == 5 and cr.suppress_window_s == 10
    cr2 = _compile_route_row(_row(suppress_key=None, suppress_allow=None))  # None → desligado
    assert cr2.suppress_key is None and cr2.suppress_allow == 0 and cr2.suppress_window_s == 30


def _suppress_route(**over):
    base = dict(id="r1", name="r1", priority=1, condition={}, action="route",
                destination_ids=("d1",), is_final=True,
                suppress_key="src_ip", suppress_allow=1, suppress_window_s=60)
    base.update(over)
    return CompiledRoute(**base)


def test_maybe_suppress_keeps_first_then_suppresses_and_decorates():
    import fakeredis.aioredis
    from backend.app.collectors import pipeline as _pl

    route = _suppress_route(condition={"event_type": "login"})

    async def go():
        r = fakeredis.aioredis.FakeRedis(decode_responses=True)
        e1 = {"_centralops": {"event_type": "login", "src_ip": "1.1.1.1"}}
        e2 = {"_centralops": {"event_type": "login", "src_ip": "1.1.1.1"}}
        e3 = {"_centralops": {"event_type": "other", "src_ip": "1.1.1.1"}}  # não casa a condição
        a = await _pl._maybe_suppress(r, e1, [route])
        b = await _pl._maybe_suppress(r, e2, [route])
        c = await _pl._maybe_suppress(r, e3, [route])
        await r.aclose()
        return a, b, c, e1

    a, b, c, e1 = _run(go())
    assert a is None and b == "r1" and c is None       # 1ª passa, 2ª suprime, não-match entrega
    assert e1["_centralops"]["suppress_count"] == 1     # o liberado é decorado (preserva contagem)


def test_maybe_suppress_is_fail_open_on_redis_error():
    from backend.app.collectors import pipeline as _pl

    class _Boom:
        async def incr(self, *a, **k):
            raise RuntimeError("redis down")

    env = {"_centralops": {"src_ip": "1.1.1.1"}}
    # Erro de Redis NÃO derruba nem descarta — fail-open entrega o evento (None).
    assert _run(_pl._maybe_suppress(_Boom(), env, [_suppress_route()])) is None


# ── agregação/rollup log→métrica (fail-open anti-OOM) ────────────────

def test_aggregateconfig_defaults_and_validation():
    from backend.app.collectors.output.delivery_config import AggregateConfig, DeliveryConfig

    a = AggregateConfig()
    assert a.group_by == [] and a.max_groups == 1000
    assert DeliveryConfig().aggregate.group_by == []  # default = desligado
    with pytest.raises(Exception):
        AggregateConfig(max_groups=0)  # abaixo do mínimo


def _cev(**labels):
    return {"_centralops": dict(labels), "normalized": {}, "raw": {"x": "y" * 50}}


def test_coalesce_collapses_repeated_groups_and_counts():
    from backend.app.collectors.reduction.aggregate import coalesce

    batch = [_cev(src="a", et="login") for _ in range(5)] + [_cev(src="b", et="login")]
    out, saved_bytes, saved_events = coalesce(batch, ["src"], max_groups=1000)
    # grupo "a" (5) → 1 metric-event; grupo "b" (1) → passa íntegro. 2 eventos saem.
    assert len(out) == 2
    assert saved_events == 4  # 5-1 do grupo a
    assert saved_bytes > 0
    agg = next(e for e in out if e["_centralops"]["src"] == "a")
    assert agg["_aggregate"]["count"] == 5 and agg["_aggregate"]["group"] == {"src": "a"}


def test_coalesce_noop_when_no_group_by():
    from backend.app.collectors.reduction.aggregate import coalesce

    batch = [_cev(src="a") for _ in range(3)]
    out, sb, se = coalesce(batch, [], max_groups=1000)
    assert out == batch and sb == 0 and se == 0  # sem group_by → intacto


def test_coalesce_fail_open_on_cardinality_explosion():
    from backend.app.collectors.reduction.aggregate import coalesce

    # 10 grupos distintos, teto 3 → passthrough (fail-open anti-OOM): lote INTACTO.
    batch = [_cev(src=str(i), et="x") for i in range(10)]
    out, sb, se = coalesce(batch, ["src"], max_groups=3)
    assert len(out) == 10 and sb == 0 and se == 0
