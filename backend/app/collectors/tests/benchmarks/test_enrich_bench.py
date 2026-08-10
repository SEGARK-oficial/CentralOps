"""Baseline benchmark — enriquecimento em stream (ADR-LOCAL-0002, Fase 0.5).

Mede o ÚNICO código desta feature que roda por evento: ``enrich.applier.apply``.
A resolução (I/O) fica de fora de propósito — ela é amortizada sobre o lote inteiro
e não pertence ao caminho quente.

Por que estes benchmarks existem: o critério de aceite da Fase 1 do ADR era
*"apply_local p95 < 10 µs/evento"*, e sem baseline no repo esse número era
inverificável. Pior, ele estava **errado** como escrito — o custo é linear no número
de REGRAS, não constante por evento (ver ``bench_enrich_hit_1rule`` vs ``_3rules``).

Os três caminhos medidos são os três que existem, em ordem de frequência real:

``when_false``   a regra não se aplica ao evento — o caso mais comum num pipeline
                 com política de várias regras e fontes heterogêneas;
``miss``         a regra se aplica, a chave não está na tabela;
``hit``          a regra se aplica e escreve — o caso caro.

IDs seguem o padrão estável ``bench_enrich_<caminho>_<variante>`` para o
``check_regression.py`` comparar entre execuções.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from backend.app.collectors.enrich import enrichers as _enrichers  # noqa: F401 — registra
from backend.app.collectors.enrich.applier import ApplyStats, apply
from backend.app.collectors.enrich.dsl import compile_policy
from backend.app.collectors.enrich.runtime import (
    DictLookupTable,
    TableResolution,
    _distinct_keys,
)

#: Tabela de tamanho realista para um feed de indicadores de um cliente.
_TABLE_ENTRIES = 5_000


def _rule(idx: int, *, when: Any = None) -> Dict[str, Any]:
    rule: Dict[str, Any] = {
        "id": f"r{idx}",
        "enricher": "opencti",
        # opencti declara ``required_secrets``, e a DSL exige ``source`` para esses:
        # a credencial vive numa ``EnrichmentSource`` escopada à org, nunca no JSON
        # da regra. O benchmark mede o applier, que nunca resolve a fonte — o nome
        # aqui só satisfaz o compilador.
        "source": "fonte-de-bench",
        "key": {
            "source": "normalized.src_endpoint.ip",
            "kind": "ip",
            "normalize": ["strip", "lower"],
        },
        "outputs": [
            {"from": "score", "target": f"_centralops.enrichment.r{idx}.score"},
            {"from": "labels", "target": f"_centralops.enrichment.r{idx}.labels"},
        ],
        "tags": ["ti_known"],
    }
    if when is not None:
        rule["when"] = when
    return rule


def _envelope(ip: str) -> Dict[str, Any]:
    """Envelope no formato REAL pós-``build_envelope``."""
    return {
        "_centralops": {
            "event_id": "evt-1",
            "organization_id": 42,
            "vendor": "sophos",
            "stream": "detections",
            "event_type": "detection",
            "ocsf_valid": True,
        },
        "normalized": {
            "class_uid": 2004,
            "severity_id": 4,
            "time": 1754697600000,
            "src_endpoint": {"ip": ip},
            "actor": {"user": {"name": "j.silva"}},
        },
        "raw": {"id": "evt-1", "severity": "high"},
    }


@pytest.fixture(scope="session")
def enrich_table() -> DictLookupTable:
    return DictLookupTable(
        {
            f"10.0.{(i >> 8) & 255}.{i & 255}": {
                "score": i % 100,
                "labels": ["tor-exit-node", "scanner"],
            }
            for i in range(_TABLE_ENTRIES)
        }
    )


# ── caminho por evento ──────────────────────────────────────────────────────

_HIT_PARAMS = [1, 3, 8]


@pytest.mark.parametrize(
    "n_rules", _HIT_PARAMS, ids=[f"bench_enrich_hit_{n}rules" for n in _HIT_PARAMS]
)
def test_enrich_apply_hit_bench(benchmark: Any, n_rules: int, enrich_table) -> None:
    """Caminho caro: a regra casa e escreve 2 campos + tag + proveniência.

    ``pedantic`` com ``setup`` é OBRIGATÓRIO aqui: ``apply`` MUTA o envelope, e
    ``overwrite=False`` faz a segunda rodada virar no-op sobre o mesmo objeto —
    o benchmark mediria o caminho errado e reportaria um número bonito e falso.
    """
    policy = compile_policy([_rule(i) for i in range(n_rules)])
    resolution = TableResolution({r.rule_id: enrich_table for r in policy.rules})
    stats = ApplyStats()

    def setup():
        return (_envelope("10.0.1.7"), policy.rules, resolution, stats), {}

    benchmark.pedantic(apply, setup=setup, rounds=2000, warmup_rounds=200)


def test_enrich_apply_miss_bench(benchmark: Any, enrich_table) -> None:
    """A regra se aplica, a chave não está na tabela (IP fora do inventário)."""
    policy = compile_policy([_rule(0)])
    resolution = TableResolution({policy.rules[0].rule_id: enrich_table})
    stats = ApplyStats()

    def setup():
        return (_envelope("203.0.113.9"), policy.rules, resolution, stats), {}

    benchmark.pedantic(apply, setup=setup, rounds=2000, warmup_rounds=200)


def test_enrich_apply_when_false_bench(benchmark: Any, enrich_table) -> None:
    """A regra NÃO se aplica — o caminho mais frequente em política real.

    É o número que importa para o custo marginal de acrescentar regras a uma
    política: uma regra que não casa custa uma fração da que casa.
    """
    policy = compile_policy(
        [_rule(0, when={"equals": {"source": "_centralops.vendor", "value": "outro"}})]
    )
    resolution = TableResolution({policy.rules[0].rule_id: enrich_table})
    stats = ApplyStats()

    def setup():
        return (_envelope("10.0.1.7"), policy.rules, resolution, stats), {}

    benchmark.pedantic(apply, setup=setup, rounds=2000, warmup_rounds=200)


def test_enrich_disabled_is_free_bench(benchmark: Any) -> None:
    """Política vazia ⇒ ``apply`` retorna sem tocar em nada.

    Ancora o custo do caminho com ``ENRICHMENT_ENABLED=false``: no pipeline o
    call-site é guardado por ``if _enrich_local_res is not None``, então com a flag
    off nem esta chamada acontece. Este benchmark é o teto SUPERIOR do custo da
    feature desligada, e deve ficar na casa de dezenas de nanossegundos.
    """
    stats = ApplyStats()
    resolution = TableResolution({})
    env = _envelope("10.0.1.7")
    benchmark(apply, env, (), resolution, stats)


# ── caminho por lote (seam E2) ──────────────────────────────────────────────

def test_enrich_distinct_keys_bench(benchmark: Any) -> None:
    """Dedup de chaves de um lote inteiro — a economia central do seam remoto.

    ``collector_batch_size`` default é 200 (``config_loader.py:94``). O lote aqui
    tem 200 eventos e apenas 20 hosts distintos, que é o padrão real de um SOC:
    poucos ativos gerando muitos eventos. O resultado disso é o número de chamadas
    à API externa — 200 eventos viram 20 lookups, não 200.
    """
    policy = compile_policy([_rule(0)])
    rule = policy.rules[0]
    batch: List[Dict[str, Any]] = [
        _envelope(f"10.0.0.{i % 20}") for i in range(200)
    ]
    benchmark(_distinct_keys, batch, rule)
