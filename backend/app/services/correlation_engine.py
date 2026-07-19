"""Motor de correlação por threshold — funções PURAS, testáveis.

Avalia uma regra threshold sobre uma lista de eventos (os resultados cross-source
de uma query federada): filtra por ``where``, agrupa por ``group_by_field`` e, com
janela deslizante de ``window_seconds`` sobre ``timestamp_field``, devolve os grupos
que atingiram ``min_count``. Sem efeito colateral nem dependência de pySigma/OCSF —
opera sobre dicts (vendor-native ou normalizado); o ``CorrelationService`` é quem
persiste as ``Detection``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

_OPS = ("eq", "ne", "contains", "gt", "lt", "gte", "lte")


def extract_path(item: Any, dotted: Optional[str]) -> Any:
    """Navega um caminho ``a.b.c`` num dict aninhado. ``None`` se ausente/inválido."""
    if not dotted:
        return None
    cur = item
    for part in dotted.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
        if cur is None:
            return None
    return cur


def _coerce_number(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def matches_where(item: dict, filters: Iterable[dict]) -> bool:
    """True se ``item`` casa TODOS os filtros (AND). Filtro inválido ⇒ não casa."""
    for flt in filters or ():
        field = flt.get("field")
        op = flt.get("op", "eq")
        target = flt.get("value")
        if op not in _OPS:
            return False
        actual = extract_path(item, field)
        if op == "eq":
            # campo ausente (None) nunca casa um valor concreto — evita o footgun
            # de stringificar None para "None".
            if actual is None or str(actual) != str(target):
                return False
        elif op == "ne":
            # ausente é "diferente" de qualquer valor concreto → mantém o evento.
            if actual is not None and str(actual) == str(target):
                return False
        elif op == "contains":
            if actual is None or str(target) not in str(actual):
                return False
        else:  # gt/lt/gte/lte — numérico
            a, b = _coerce_number(actual), _coerce_number(target)
            if a is None or b is None:
                return False
            if op == "gt" and not a > b:
                return False
            if op == "lt" and not a < b:
                return False
            if op == "gte" and not a >= b:
                return False
            if op == "lte" and not a <= b:
                return False
    return True


# Limiar segundos-vs-ms para epochs numéricos. Espelha (deliberadamente, para
# manter este módulo sem dependência de collectors/OCSF — ver docstring do
# módulo) ``collectors.normalize.operators._EPOCH_MS_THRESHOLD``: como segundos
# 1e11 ≈ ano 5138, como ms ≈ 1973-03-03. Necessário porque ``timestamp_field``
# pode apontar para um campo ``timestamp_t`` do OCSF, que é em MILISSEGUNDOS —
# sem a conversão, ``window_seconds`` compararia deltas 1000× maiores e as
# regras threshold simplesmente parariam de disparar.
_EPOCH_MS_THRESHOLD = 100_000_000_000  # 1e11 — acima disto, ms (1e11 ms = 1973)
_EPOCH_US_THRESHOLD = 100_000_000_000_000  # 1e14 — acima disto, µs
_EPOCH_NS_THRESHOLD = 100_000_000_000_000_000  # 1e17 — acima disto, ns


def _parse_ts(value: Any) -> Optional[float]:
    """ISO-8601 ou epoch (s/ms/µs/ns) → SEGUNDOS UTC. ``None`` se não parsear."""
    if value is None:
        return None
    num = _coerce_number(value)
    if num is not None:
        # Epoch numérico de unidade DESCONHECIDA, escalonado por faixas. Os cortes
        # são calibrados para que um instante moderno caia na faixa certa e um
        # instante antigo nunca suba de faixa (todos os limiares equivalem a
        # 1973-03-03 na unidade seguinte). ADR-0015 Fase 2: antes havia UM degrau
        # (ms acima de 1e11), então epoch em µs ou ns — comum em OTel e eBPF —
        # virava um "segundo" inflado por 1e6/1e9, jogando o evento milhões de
        # anos no futuro. A janela deslizante nunca fechava e a regra ficava muda,
        # com contador em zero: indistinguível de "não houve evento".
        a = abs(num)
        if a >= _EPOCH_NS_THRESHOLD:
            return num / 1_000_000_000.0
        if a >= _EPOCH_US_THRESHOLD:
            return num / 1_000_000.0
        if a >= _EPOCH_MS_THRESHOLD:
            return num / 1000.0
        return num
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    # ISO SEM timezone é interpretado como UTC, não como o fuso LOCAL do processo.
    # ``datetime.timestamp()`` em datetime naive usa o TZ do sistema — o mesmo
    # dado produziria timestamps diferentes em dev (UTC) e em produção
    # (America/Sao_Paulo), com 3h de deslocamento. Numa correlação CROSS-SOURCE
    # basta uma fonte emitir "...Z" e outra naive para os eventos se espalharem
    # por horas e a regra nunca mais disparar. Mesma normalização de
    # ``providers/lake/provider.py`` (``_parse_iso``).
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _window_max_count(timestamps: list[float], window_seconds: int) -> int:
    """Máximo de timestamps dentro de QUALQUER janela deslizante de ``window_seconds``
    (two-pointer sobre a lista ordenada)."""
    if not timestamps:
        return 0
    ts = sorted(timestamps)
    best = 1
    left = 0
    for right in range(len(ts)):
        while ts[right] - ts[left] > window_seconds:
            left += 1
        best = max(best, right - left + 1)
    return best


def evaluate_threshold(rule: Any, items: list[dict]) -> list[dict]:
    """Avalia uma regra threshold. Devolve ``[{group, count, sample}]`` p/ grupos que
    dispararam. ``rule`` precisa de: ``group_by_field``, ``min_count``,
    ``window_seconds``, ``timestamp_field``, ``where_json`` (parseado)."""
    import json

    rule_id = getattr(rule, "id", "?")
    min_count = int(getattr(rule, "min_count", 0) or 0)
    group_field = getattr(rule, "group_by_field", None)
    window = int(getattr(rule, "window_seconds", 0) or 0)
    ts_field = getattr(rule, "timestamp_field", None)

    raw_where = getattr(rule, "where_json", None)
    try:
        filters = json.loads(raw_where or "[]")
    except (ValueError, TypeError):
        # FAIL-CLOSED (ADR-0015 Fase 2). Antes isto degradava para ``filters = []``
        # e, como o laço abaixo só filtra ``if filters``, a regra passava a NÃO
        # FILTRAR NADA: "5 eventos ONDE event=auth_failed" virava "5 eventos
        # QUAISQUER". Era a única inversão de doutrina do arquivo — exatamente a
        # inundação de falso-positivo que o comentário do fail-closed de
        # timestamp (abaixo) diz querer evitar, entrando pela porta ao lado.
        # Uma regra que não sabe o que filtrar não pode disparar sobre tudo.
        logger.error(
            "correlação: regra %s tem where_json inválido (JSON malformado) — "
            "regra NÃO avaliada. Sem isto ela dispararia sobre TODOS os eventos.",
            rule_id,
        )
        return []
    if not isinstance(filters, list):
        logger.error(
            "correlação: regra %s tem where_json que não é lista (%s) — regra NÃO "
            "avaliada.", rule_id, type(filters).__name__,
        )
        return []

    if not group_field:
        logger.warning(
            "correlação: regra %s sem group_by_field — nada a agrupar, 0 detecções.",
            rule_id,
        )
        return []
    if min_count <= 0:
        logger.warning(
            "correlação: regra %s com min_count=%s (<=0) — nunca dispara.",
            rule_id, min_count,
        )
        return []

    # Contadores por CAUSA de descarte. Um detector que fica mudo sem dizer por quê
    # é pior que um detector ausente: o operador acredita estar coberto e para de
    # procurar cobertura em outro lugar. Antes desta fase o ``logger`` do módulo
    # era declarado e NUNCA chamado — todas as causas abaixo produziam o mesmo
    # sintoma observável (silêncio, regra verde na UI).
    seen = dropped_not_dict = dropped_where = dropped_no_group = 0
    groups: dict[str, list[dict]] = {}
    for it in items:
        seen += 1
        if not isinstance(it, dict):
            dropped_not_dict += 1
            continue
        if filters and not matches_where(it, filters):
            dropped_where += 1
            continue
        key = extract_path(it, group_field)
        if key is None:
            dropped_no_group += 1
            continue
        groups.setdefault(str(key), []).append(it)

    if seen and not groups:
        # O caso de suporte mais comum: "criei a regra e ela não dispara".
        logger.info(
            "correlação: regra %s viu %d evento(s) e formou 0 grupos "
            "(descartados: %d não-dict, %d pelo where, %d sem '%s') — "
            "0 detecções possíveis.",
            rule_id, seen, dropped_not_dict, dropped_where,
            dropped_no_group, group_field,
        )

    if window > 0 and not ts_field:
        # A janela é DESLIGADA em silêncio quando não há campo de timestamp: o
        # ``else`` abaixo conta todos os eventos do grupo, então "10 falhas em 5
        # minutos" vira "10 falhas em qualquer tempo dentro do resultado da
        # busca". É falso-positivo no caminho feliz, e a UI chegou a prometer um
        # fallback de timestamp de ingestão que nunca existiu (corrigido na
        # Fase 0). Aqui o comportamento é preservado por compatibilidade, mas
        # deixa de ser mudo.
        logger.warning(
            "correlação: regra %s tem window_seconds=%d mas timestamp_field vazio "
            "— a JANELA ESTÁ DESLIGADA e a contagem considera todos os eventos do "
            "grupo no resultado da busca.", rule_id, window,
        )

    hits: list[dict] = []
    groups_no_valid_ts = 0
    for key, evs in groups.items():
        if window > 0 and ts_field:
            tss = [t for t in (_parse_ts(extract_path(e, ts_field)) for e in evs) if t is not None]
            if evs and not tss:
                groups_no_valid_ts += 1
            # FAIL-CLOSED: timestamps inválidos/ausentes ⇒ tss vazio ⇒ count 0 (NÃO
            # len(evs)). Senão a regra "N em W segundos" viraria "N em qualquer tempo"
            # silenciosamente → falso-positivo.
            count = _window_max_count(tss, window)
        else:
            count = len(evs)  # sem janela/timestamp ⇒ conta todos do grupo (por design)
        if count >= min_count:
            hits.append({"group": key, "count": count, "sample": evs[0]})

    if groups_no_valid_ts:
        # Fail-closed correto (contagem 0), mas invisível até aqui. É o sintoma de
        # ``timestamp_field`` apontando para campo errado, para dentro de um array,
        # ou para um formato que ``_parse_ts`` não reconhece.
        logger.warning(
            "correlação: regra %s — %d de %d grupo(s) não tinham NENHUM timestamp "
            "válido em '%s'; contagem 0 (fail-closed). Verifique o campo.",
            rule_id, groups_no_valid_ts, len(groups), ts_field,
        )
    if groups and not hits:
        logger.info(
            "correlação: regra %s formou %d grupo(s), nenhum atingiu min_count=%d "
            "— 0 detecções.", rule_id, len(groups), min_count,
        )
    return hits
