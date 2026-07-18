"""Carga, compilação, acumulação e flush das regras em voo (ADR-0015, Fase 1).

Tudo que toca o mundo mora aqui, para que ``matcher`` possa permanecer puro. Nada
neste módulo roda por evento exceto ``InflightAccumulator.add``, que é aritmética
em memória sobre dicts.

Fluxo por ciclo de coleta:

1. ``load_inflight_rules_for_org`` — 1x, fora do laço, via ``asyncio.to_thread``.
   Abre e fecha a própria sessão (não há sessão de DB aberta no hot path).
2. ``InflightAccumulator.add`` — por evento, só quando há match. Em memória.
3. ``flush_inflight`` — 1x, no ``finally`` do ciclo. Escreve as Detections
   off-loop e emite as métricas de fim de ciclo.

Por que acumular em vez de escrever por match: ``DetectionRepository.record``
faz SELECT + commit + refresh, ou seja ≥3 round-trips de Postgres por chamada.
Escrever uma Detection por evento casado dentro do ``async for`` de coleta
violaria R1 e reproduziria a forma do poison-loop de coletor já vivido em
produção — o laço de coleta awaitando I/O de escrita proporcional ao backlog.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Iterable, Mapping, Optional

from ...core.config import settings
from .matcher import CompiledClause, CompiledInflightRule

logger = logging.getLogger(__name__)

#: Vocabulário aceito em ``where_json`` no modo ``inflight``. Superconjunto do
#: batch (``services/correlation_engine._OPS``) com os três de tuning.
INFLIGHT_ALLOWED_OPS = frozenset(
    {"eq", "ne", "contains", "gt", "lt", "gte", "lte", "in", "nin", "exists"}
)

#: Operadores cujo lado esquerdo é coagido a ``float`` na avaliação. Ver o
#: docstring de ``CompiledClause.numeric``: sem isso, severidade serializada
#: como string nunca casa e o sintoma é um contador zerado.
NUMERIC_OPS = frozenset({"gt", "lt", "gte", "lte"})

#: Operadores negativos, que casam por VACUIDADE quando o campo está ausente.
#: Todo path usado por um deles ganha uma cláusula ``exists`` auto-injetada.
NEGATIVE_OPS = frozenset({"ne", "nin"})

#: Enum FECHADO de razões de rejeição — vira label de métrica, logo nunca pode
#: conter valor vindo de evento ou nome de regra (esses vão no log).
REJECT_REASONS = ("bad_json", "empty_where", "unknown_op", "over_cap")


def validate_where_json(raw: Optional[str]) -> tuple[list[dict], Optional[str]]:
    """``(cláusulas, None)`` ou ``([], razão)``. Público: o CRUD do EE deve
    reusar isto para rejeitar com 422 na escrita, em vez de deixar a regra
    entrar no banco e falhar silenciosamente na compilação."""
    try:
        parsed = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return [], "bad_json"
    if not isinstance(parsed, list):
        return [], "bad_json"
    clauses = [c for c in parsed if isinstance(c, dict)]
    if not clauses:
        # Uma regra em voo sem predicado casaria 100% dos eventos. No batch isso
        # é atenuado por min_count/janela; aqui não há nada atenuando.
        return [], "empty_where"
    return clauses, None


def compile_rule(row: Any) -> tuple[Optional[CompiledInflightRule], Optional[str]]:
    """``CorrelationRule`` (ORM) → regra compilada, ou ``(None, razão)``.

    Roda 1x por ciclo, fora do laço — pode ser generosa em validação.
    """
    clauses_raw, reason = validate_where_json(getattr(row, "where_json", None))
    if reason is not None:
        return None, reason

    cap = int(settings.INFLIGHT_MAX_WHERE_CLAUSES)
    if len(clauses_raw) > cap:
        return None, "over_cap"

    compiled: list[CompiledClause] = []
    negative_paths: set[tuple[str, ...]] = set()
    paths_with_exists: set[tuple[str, ...]] = set()

    for c in clauses_raw:
        op = str(c.get("op") or "eq")
        if op not in INFLIGHT_ALLOWED_OPS:
            return None, "unknown_op"
        field = c.get("field")
        if not field or not isinstance(field, str):
            return None, "bad_json"
        path = tuple(field.split("."))
        value = c.get("value")
        numeric = False

        if op in ("in", "nin"):
            # String CSV é REJEITADA de propósito: aceitá-la faria
            # ``"a,b"`` virar uma lista de 3 caracteres em silêncio.
            if not isinstance(value, list):
                return None, "bad_json"
            try:
                value = frozenset(value)
            except TypeError:
                value = tuple(value)  # elementos não-hashable
        elif op == "exists":
            value = bool(value)
            paths_with_exists.add(path)
        elif op in NUMERIC_OPS:
            coerced = _as_float(value)
            if coerced is None:
                # Operador numérico com alvo não-numérico nunca casaria nada.
                return None, "bad_json"
            value, numeric = coerced, True

        if op in NEGATIVE_OPS:
            negative_paths.add(path)

        compiled.append(CompiledClause(path=path, op=op, value=value, numeric=numeric))

    # Fail-open de allowlist, fechado por construção: ``nin``/``ne`` casam por
    # vacuidade em campo ausente, então um evento cujo ``raw.user`` sumiu (path
    # atravessa lista, ou o raw foi trimado) passaria pelo filtro que deveria
    # excluí-lo — disparando exatamente sobre o que o operador quis calar.
    # Exigir que o campo EXISTA torna o operador fail-closed sem mudar sua
    # semântica nem obrigar o operador a conhecer o idioma.
    for path in sorted(negative_paths - paths_with_exists):
        compiled.append(CompiledClause(path=path, op="exists", value=True))
        logger.debug(
            "inflight: regra %s — cláusula exists auto-injetada para %s "
            "(fecha o fail-open de allowlist em campo ausente)",
            getattr(row, "id", "?"), ".".join(path),
        )

    group_by = getattr(row, "group_by_field", None)
    return (
        CompiledInflightRule(
            rule_id=int(row.id),
            name=str(row.name),
            severity_id=int(getattr(row, "severity_id", 4) or 4),
            suppression_window_seconds=int(
                getattr(row, "suppression_window_seconds", 3600) or 3600
            ),
            group_by_path=tuple(str(group_by).split(".")) if group_by else None,
            clauses=tuple(compiled),
        ),
        None,
    )


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_inflight_rules_for_org(
    organization_id: Optional[int],
) -> tuple[CompiledInflightRule, ...]:
    """Regras habilitadas em modo ``inflight`` da org, compiladas. SÍNCRONA.

    Chamada via ``asyncio.to_thread`` 1x por ciclo. Fail-safe para ``()`` em
    qualquer erro: um problema de DB não pode impedir a COLETA, que é o produto.
    ``organization_id is None`` ⇒ ``()`` (fail-CLOSED — nunca avaliar regra sem
    saber de quem é o evento).
    """
    if organization_id is None:
        return ()

    from ...db import database, repository
    from ..metrics import INFLIGHT_RULES_LOADED, INFLIGHT_RULES_REJECTED

    cap = int(settings.INFLIGHT_MAX_RULES_PER_CYCLE)
    try:
        with database.SessionLocal() as db:
            repo = repository.CorrelationRuleRepository(db)
            if cap <= 0:
                # Kill-switch de ambiente. COUNT antes de sair, para que o
                # diagnóstico não vire "não há regras" quando na verdade o
                # operador desligou a feature.
                total = repo.count_enabled_for_org(organization_id)
                if total:
                    logger.warning(
                        "inflight: %d regras habilitadas na org %s, mas "
                        "INFLIGHT_MAX_RULES_PER_CYCLE=0 desliga a avaliação",
                        total, organization_id,
                    )
                return ()
            rows = repo.list_inflight_for_org(organization_id, limit=cap)
            if not rows:
                # Diagnóstico do caso mais comum de suporte: o operador criou a
                # regra e ela ficou em modo batch.
                total = repo.count_enabled_for_org(organization_id)
                if total:
                    logger.info(
                        "inflight: org %s tem %d regra(s) habilitada(s), 0 em "
                        "modo inflight (eval_mode='batch')",
                        organization_id, total,
                    )
                INFLIGHT_RULES_LOADED.labels(org_id=str(organization_id)).set(0)
                return ()
    except Exception:  # noqa: BLE001 — coleta nunca cai por causa do detector
        logger.exception("inflight: falha carregando regras (org %s)", organization_id)
        return ()

    compiled: list[CompiledInflightRule] = []
    for row in rows:
        rule, reason = compile_rule(row)
        if rule is None:
            INFLIGHT_RULES_REJECTED.labels(reason=reason).inc()
            logger.warning(
                "inflight: regra %s (%s) rejeitada na compilação: %s",
                getattr(row, "id", "?"), getattr(row, "name", "?"), reason,
            )
            continue
        compiled.append(rule)

    INFLIGHT_RULES_LOADED.labels(org_id=str(organization_id)).set(len(compiled))
    return tuple(compiled)


class InflightAccumulator:
    """Matches do ciclo, em memória. Nada aqui toca I/O."""

    __slots__ = ("pending", "matches", "errors", "overflow", "_keys_per_rule", "_logged_overflow")

    def __init__(self) -> None:
        #: dedup_key → payload da Detection a criar
        self.pending: dict[str, dict[str, Any]] = {}
        #: rule_id → nº de eventos casados (pode ser >> len(pending))
        self.matches: dict[int, int] = {}
        #: razão → contagem
        self.errors: dict[str, int] = {}
        #: rule_id → matches perdidos por teto de chaves
        self.overflow: dict[int, int] = {}
        #: rule_id → nº de chaves distintas já criadas. Contador dedicado, e não
        #: uma varredura de ``pending`` por match: varrer seria O(nº de chaves)
        #: POR EVENTO CASADO, um custo que cresce ao longo do ciclo dentro do
        #: laço de coleta — exatamente o que R1 existe para impedir.
        self._keys_per_rule: dict[int, int] = {}
        self._logged_overflow: set[int] = set()

    def add(
        self,
        rule: CompiledInflightRule,
        envelope: Mapping[str, Any],
        organization_id: int,
        integration_id: Optional[int] = None,
    ) -> None:
        from .matcher import _resolve

        self.matches[rule.rule_id] = self.matches.get(rule.rule_id, 0) + 1

        if rule.group_by_path is None:
            token = "*"
        else:
            raw = _resolve(envelope, rule.group_by_path)
            if raw is None:
                # Agrupar os não-resolvidos numa Detection genérica esconderia
                # "regra apontando para campo errado" dentro de um alerta que
                # parece legítimo. Vira erro contado, não alerta.
                self.errors["group_by_unresolved"] = (
                    self.errors.get("group_by_unresolved", 0) + 1
                )
                return
            token = str(raw)[: int(settings.INFLIGHT_MAX_GROUP_VALUE_LEN)]

        key = f"inflight:{organization_id}:{rule.rule_id}:{token}"
        if key in self.pending:
            return

        # O teto é sobre CHAVES DISTINTAS, não sobre matches: a variável
        # perigosa é a cardinalidade do group_by, não a taxa de acerto. Uma
        # regra que casa 100% dos eventos com group_by=None gera UMA chave.
        cap = int(settings.INFLIGHT_MAX_DEDUP_KEYS_PER_RULE_PER_CYCLE)
        if self._keys_per_rule.get(rule.rule_id, 0) >= cap:
            self.overflow[rule.rule_id] = self.overflow.get(rule.rule_id, 0) + 1
            self.errors["key_cap"] = self.errors.get("key_cap", 0) + 1
            if rule.rule_id not in self._logged_overflow:
                self._logged_overflow.add(rule.rule_id)
                logger.warning(
                    "inflight: regra %s (%s) atingiu o teto de %d chaves de dedup "
                    "no ciclo — matches seguem contados, nenhuma Detection nova é "
                    "criada, nenhum evento é descartado. Teto atingido costuma "
                    "indicar group_by_field de alta cardinalidade.",
                    rule.rule_id, rule.name, cap,
                )
            return

        self._keys_per_rule[rule.rule_id] = self._keys_per_rule.get(rule.rule_id, 0) + 1
        self.pending[key] = {
            "rule": rule,
            "integration_id": integration_id,
        }


def _flush_sync(pending: dict[str, dict[str, Any]], organization_id: int) -> int:
    """Escreve as Detections. SÍNCRONA, roda em thread. Devolve quantas gravou."""
    from ...db import database, repository

    written = 0
    with database.SessionLocal() as db:
        repo = repository.DetectionRepository(db)
        for dedup_key, item in pending.items():
            rule: CompiledInflightRule = item["rule"]
            repo.record(
                organization_id=organization_id,
                source="inflight",
                dedup_key=dedup_key,
                severity_id=rule.severity_id,
                rule_id=str(rule.rule_id),
                rule_name=rule.name,
                integration_id=item.get("integration_id"),
                suppression_window_seconds=rule.suppression_window_seconds,
            )
            written += 1
    return written


async def flush_inflight(
    acc: Optional[InflightAccumulator], organization_id: Optional[int]
) -> None:
    """Persiste os matches do ciclo e emite as métricas. Best-effort.

    Chamada no ``finally`` do ciclo — cobre caminho feliz E caminho de exceção.
    Isso não é zelo: no data-plane default uma exceção no meio do ciclo NÃO
    solta as claims de dedupe, o retry re-busca os eventos e ``claim`` os
    descarta como duplicados. Sem flush aqui, os matches morreriam em memória
    sem log nem métrica.
    """
    if acc is None or organization_id is None:
        return

    from ..metrics import INFLIGHT_ERRORS, INFLIGHT_MATCHES

    if acc.pending:
        try:
            await asyncio.to_thread(_flush_sync, acc.pending, int(organization_id))
        except Exception:  # noqa: BLE001
            # A perda é CONTADA, não presumida.
            INFLIGHT_ERRORS.labels(reason="flush_lost").inc(len(acc.pending))
            logger.exception(
                "inflight: flush falhou — %d Detection(s) perdida(s) (org %s)",
                len(acc.pending), organization_id,
            )

    for rule_id, count in acc.matches.items():
        INFLIGHT_MATCHES.labels(rule_id=str(rule_id)).inc(count)
    for reason, count in acc.errors.items():
        INFLIGHT_ERRORS.labels(reason=reason).inc(count)


def iter_reject_reasons() -> Iterable[str]:
    """Para o teste que trava o enum fechado de labels."""
    return REJECT_REASONS
