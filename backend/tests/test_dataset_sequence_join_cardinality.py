"""Harness opt-in (X0): o que o volume real diz sobre a sequência entre fontes.

A regra ``sequence`` guarda, no flush, UM hash por (regra, valor de junção)
com TTL igual à janela. Duas perguntas só o tráfego real responde, e os tetos
do motor (``INFLIGHT_MAX_LEGS``, ``INFLIGHT_MAX_DEDUP_KEYS_PER_RULE_PER_CYCLE``
aplicado à chave de junção) foram escolhidos sem elas:

1. **Quantas chaves de junção distintas um ciclo produz** por candidato de
   ``join_path`` — é o número de hashes que uma perna escreve no Redis por
   ciclo, e o que o teto de chaves por regra corta.
2. **Quanto duas fontes de fato se encontram** na mesma entidade dentro de uma
   janela — se a interseção for zero, a regra nunca fecha e o estado só
   envelhece até o TTL.

O dataset local (ver ``test_dataset_local_harness``) tem Sophos (siem_event,
detection, case, alert) e Wazuh; não tem Okta nem Entra. Por isso o par medido
aqui é ENTRE STREAMS do mesmo vendor — ``sophos.siem_event`` × ``sophos.
detection`` pelo id do endpoint — que é a mesma mecânica de junção (caminhos
diferentes em cada perna, valor comum) sobre dado que existe.

Nenhum assert falha por causa do número: o objetivo é torná-lo VISÍVEL. Os
valores impressos são CONTAGENS; nenhum valor de evento sai no log.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path
from typing import Any, Iterator, Optional

import pytest

from backend.app.core.config import settings

ENV_VAR = "CENTRALOPS_DATASET_DIR"
_SHARD_GLOB = "org-*/{stream}/*.ndjson"
SKIP_REASON = f"{ENV_VAR} não definida — aponte para o diretório 'ds-bruto' do dataset local."

pytestmark = pytest.mark.dataset

CYCLE = 200


def _root() -> Path:
    raw = os.environ.get(ENV_VAR, "").strip()
    root = Path(raw).expanduser() if raw else None
    if root is None or not root.is_dir():
        pytest.skip(SKIP_REASON)
    return root


def _events(stream: str, *, limit: int) -> Iterator[dict[str, Any]]:
    seen = 0
    for shard in sorted(_root().glob(_SHARD_GLOB.format(stream=stream))):
        with shard.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
                seen += 1
                if seen >= limit:
                    return


def _get(obj: Any, path: str) -> Optional[str]:
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    if cur is None or isinstance(cur, (dict, list)):
        return None
    text = str(cur).strip()
    return text or None


#: Candidatos de ``join_path`` por stream, no dado CRU (o dataset é
#: pré-normalização). O caminho OCSF equivalente vai ao lado, para ligar o
#: número ao que a regra escreveria.
JOIN_CANDIDATES: dict[str, list[tuple[str, str]]] = {
    "sophos.siem_event": [
        ("endpoint_id", "normalized.device.uid"),
        ("user_id", "normalized.actor.user.uid"),
        ("location", "normalized.device.hostname"),
        ("source_info.ip", "normalized.device.ip"),
    ],
    "sophos.detection": [
        ("device.id", "normalized.device.uid"),
        ("rawData.meta_hostname", "normalized.device.hostname"),
        ("rawData.meta_ip_address", "normalized.device.ip"),
    ],
    "wazuh.detection": [
        ("agent.name", "normalized.device.hostname"),
        ("data.srcip", "normalized.src_endpoint.ip"),
        ("data.win.eventdata.targetUserName", "normalized.user.name"),
    ],
}


def test_join_value_cardinality_per_cycle():
    """Chaves de junção distintas por ciclo de 200 eventos, por candidato."""
    cap = int(settings.INFLIGHT_MAX_DEDUP_KEYS_PER_RULE_PER_CYCLE)
    print(f"\n[dataset] chaves de junção por ciclo de {CYCLE} (teto por regra/ciclo = {cap})")
    any_stream = False
    for stream, candidates in JOIN_CANDIDATES.items():
        events = list(_events(stream, limit=20_000))
        if len(events) < CYCLE:
            print(f"  {stream}: só {len(events)} eventos, pulado")
            continue
        any_stream = True
        for raw_path, ocsf_path in candidates:
            per_cycle: list[int] = []
            missing = 0
            current: set[str] = set()
            for i, ev in enumerate(events, 1):
                value = _get(ev, raw_path)
                if value is None:
                    missing += 1
                else:
                    current.add(value)
                if i % CYCLE == 0:
                    per_cycle.append(len(current))
                    current = set()
            if not per_cycle:
                continue
            per_cycle.sort()
            p50 = per_cycle[len(per_cycle) // 2]
            p95 = per_cycle[int(len(per_cycle) * 0.95) - 1]
            over = sum(1 for n in per_cycle if n > cap)
            print(
                f"  {stream} · {raw_path} → {ocsf_path}: p50={p50} p95={p95} "
                f"max={per_cycle[-1]} chaves/ciclo; {over}/{len(per_cycle)} ciclos > teto; "
                f"sem valor em {missing / len(events):.0%} dos eventos"
            )
    if not any_stream:
        pytest.skip("nenhum stream com volume suficiente no dataset")


def test_cross_stream_join_hit_rate():
    """Duas pernas de fato se encontram? ``sophos.detection.device.id`` ×
    ``sophos.siem_event.endpoint_id`` (o mesmo id de endpoint em dois streams).

    Mede, para cada detecção, se o endpoint dela aparece em algum siem_event
    dentro de ±``window`` segundos — é exatamente o que ``_apply_sequences``
    faria com duas pernas por ``device.uid``. Uma taxa baixa não é bug: diz
    que a maioria das detecções acontece em hosts sem evento de siem na
    janela, e por isso a janela default de uma regra real tem de ser escolhida
    olhando este número, não chutada.
    """
    from datetime import datetime

    def _ts(value: Optional[str]) -> Optional[float]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None

    siem: dict[str, list[float]] = {}
    n_siem = 0
    for ev in _events("sophos.siem_event", limit=50_000):
        endpoint = _get(ev, "endpoint_id")
        when = _ts(_get(ev, "when")) or _ts(_get(ev, "created_at"))
        if endpoint and when is not None:
            siem.setdefault(endpoint, []).append(when)
            n_siem += 1
    detections = [
        (_get(ev, "device.id"), _ts(_get(ev, "sensorGeneratedAt")) or _ts(_get(ev, "time")))
        for ev in _events("sophos.detection", limit=50_000)
    ]
    detections = [(d, t) for d, t in detections if d and t is not None]
    if n_siem < CYCLE or len(detections) < 50:
        pytest.skip("dataset insuficiente para medir a junção entre streams")
    for endpoint in siem:
        siem[endpoint].sort()

    print(f"\n[dataset] junção sophos.detection × sophos.siem_event por endpoint: "
          f"{len(detections)} detecções, {n_siem} siem_events, {len(siem)} endpoints com siem")
    max_window = int(settings.INFLIGHT_MAX_WINDOW_SECONDS)
    for window in sorted({300, 900, 3600, max_window, 6 * 3600, 24 * 3600}):
        hits = 0
        for endpoint, when in detections:
            times = siem.get(endpoint)
            if not times:
                continue
            # busca linear bastaria; bisect deixa o teste rápido em 50k
            import bisect

            lo = bisect.bisect_left(times, when - window)
            if lo < len(times) and times[lo] <= when + window:
                hits += 1
        tag = " (teto do motor)" if window == max_window else ""
        print(f"  janela ±{window}s{tag}: {hits}/{len(detections)} detecções "
              f"com siem_event do mesmo endpoint ({hits / len(detections):.0%})")
    distinct_endpoints = {d for d, _ in detections}
    print(f"  endpoints distintos nas detecções: {len(distinct_endpoints)}; "
          f"dos quais {sum(1 for d in distinct_endpoints if d in siem)} têm siem_event no dataset")


def test_state_keys_per_cycle_stay_within_the_leg_budget():
    """O único assert: uma perna por ``device.uid`` no stream mais volumoso não
    produz, em p95, mais chaves por ciclo do que o teto de chaves por regra —
    senão o teto de pernas (``INFLIGHT_MAX_LEGS``) multiplica um número que já
    estoura sozinho, e a sequência viveria cortada. Frouxo de propósito: mede
    regressão de forma (um dataset que mude de cara), não performance."""
    cap = int(settings.INFLIGHT_MAX_DEDUP_KEYS_PER_RULE_PER_CYCLE)
    per_cycle: list[int] = []
    current: set[str] = set()
    n = 0
    for ev in _events("sophos.siem_event", limit=20_000):
        value = _get(ev, "endpoint_id")
        if value:
            current.add(value)
        n += 1
        if n % CYCLE == 0:
            per_cycle.append(len(current))
            current = set()
    if len(per_cycle) < 5:
        pytest.skip("dataset insuficiente")
    per_cycle.sort()
    p95 = per_cycle[int(len(per_cycle) * 0.95) - 1]
    print(f"\n[dataset] sophos.siem_event · endpoint_id: p95={p95} chaves/ciclo, teto={cap}, "
          f"legs max={int(settings.INFLIGHT_MAX_LEGS)}")
    # Uma perna sozinha acima do teto em p95 significaria que a chave de
    # junção sugerida pela doc (device.uid) não cabe no orçamento default.
    assert p95 <= cap * 4, (
        f"p95 de {p95} chaves de junção por ciclo passa de 4x o teto ({cap}); "
        f"a chave device.uid não serve de default para sequência neste tráfego"
    )
