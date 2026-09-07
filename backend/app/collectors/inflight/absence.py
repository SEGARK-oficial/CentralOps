"""Ausência de evento em voo (ADR-0016) — ``rule_type='absence'``.

Os dois motores existentes só acordam quando um evento CHEGA: o lote dispara em
``count >= min_count`` (e recusa ``min_count <= 0``), e o motor em voo é
acionado por evento. Nenhum dos dois consegue dizer que o backup de hoje NÃO
criou o ponto de restauração. A ausência precisa de um terceiro acionador — um
relógio — e este módulo é ele, em duas metades que nunca se encontram:

* **presença**, escrita no FLUSH do ciclo (``apply_absence_presence``), como a
  janela deslizante e a sequência já fazem: uma chave por (regra, valor do
  ``group_by``), com o instante do último avistamento, num HASH do Redis. O
  caminho por evento continua puro; a regra de ausência custa ao matcher o
  mesmo que uma regra de limiar.
* **silêncio**, decidido por um TIQUE periódico (``evaluate_absence_rules_once``)
  que lê o hash e alerta a chave conhecida cujo último avistamento passou do
  prazo. O tique mora no beat do EE; o corpo está aqui, síncrono e testável com
  relógio e Redis injetados.

O problema de desenho é o CONJUNTO ESPERADO: sem ele "ausente" e "nunca
existiu" são a mesma coisa. A resposta é a vigília aprendida com esquecimento:
uma chave entra ao ser vista pela primeira vez e sai depois de
``absence_forget_seconds`` de silêncio. Um Redis esvaziado zera a vigília sem
alarme falso — a ausência nunca alerta sobre o que não aprendeu.

FAIL-CLOSED em três frentes, e é isso que torna um alerta de ausência honesto:
o tique só alerta quando prova que estava OLHANDO (o flush grava um batimento
em ``meta.last_cycle`` mesmo com zero matches; batimento velho ⇒ nenhum alerta
para a regra, contado em ``absence_unobservable``), que a FONTE estava chegando
(watermark do coletor atrasado + teto atingido ⇒ ``absence_source_lagging``,
quando o ``where`` fixa o stream) e que o Redis respondeu. Um alerta de
ausência falso afirma que o backup falhou quando foi o coletor que atrasou —
é o pior alerta possível, e cada uma das três guardas existe para ele.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional

from ...core.config import settings

if TYPE_CHECKING:  # pragma: no cover — só tipos
    from .matcher import CompiledInflightRule
    from .runtime import InflightAccumulator

logger = logging.getLogger(__name__)

ABSENCE_RULE_TYPE = "absence"
KEY_PREFIX = "absence"
#: Piso do prazo: abaixo de 60 s a ausência é ruído de relógio, não sinal.
ABSENCE_MIN_WINDOW_SECONDS = 60
#: Folga do TTL das chaves de estado além do esquecimento: o tique tem de
#: conseguir LER a chave esquecida para contá-la antes de o Redis recolhê-la.
_STATE_TTL_SLACK = 24 * 60 * 60

#: Estado do último tique, gravado em ``meta.state`` — é o que o painel de
#: limites lê para responder "por que a ausência não alertou".
TICK_STATE_OK = "ok"
TICK_STATE_UNOBSERVABLE = "unobservable"
TICK_STATE_LAGGING = "lagging"
TICK_STATE_UNAVAILABLE = "unavailable"


def seen_key(organization_id: int, rule_id: int) -> str:
    return f"{KEY_PREFIX}:{int(organization_id)}:{int(rule_id)}:seen"


def meta_key(organization_id: int, rule_id: int) -> str:
    return f"{KEY_PREFIX}:{int(organization_id)}:{int(rule_id)}:meta"


def alerted_key(organization_id: int, rule_id: int) -> str:
    return f"{KEY_PREFIX}:{int(organization_id)}:{int(rule_id)}:alerted"


def lock_key(rule_id: int) -> str:
    return f"corr:absence:{int(rule_id)}"


def dedup_key(organization_id: int, rule_id: int, token: str) -> str:
    """A ``dedup_key`` da Detection. O prefixo é o que separa um alerta de
    ausência de um de presença — na lista, no SIEM e no MCP."""
    return f"{KEY_PREFIX}:{int(organization_id)}:{int(rule_id)}:{token}"


def forget_seconds_for(row: Any, window_seconds: int) -> int:
    """Esquecimento efetivo: a coluna quando existe, senão 3× o prazo."""
    raw = getattr(row, "absence_forget_seconds", None)
    if raw is None or int(raw) <= 0:
        return 3 * int(window_seconds)
    return int(raw)


def stream_pinned_by(clauses: Any) -> Optional[str]:
    """O stream que o ``where`` FIXA (``_centralops.stream eq X``), se houver.

    É a única forma de a ausência saber qual coletor vigiar: o ``where`` de
    uma regra de ausência normalmente começa por ele, e é ele que habilita a
    guarda do watermark. Sem stream fixo a regra fica só com o batimento do
    observador.
    """
    for clause in clauses or ():
        path = getattr(clause, "path", None)
        op = getattr(clause, "op", None)
        if tuple(path or ()) == ("_centralops", "stream") and op == "eq":
            value = getattr(clause, "value", None)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


# ── metade 1: presença, no flush ─────────────────────────────────────────


def apply_absence_presence(
    acc: "InflightAccumulator", organization_id: int, *, now: Optional[float] = None
) -> int:
    """Retira do ``pending`` toda chave de regra de ausência e grava a
    presença dela no Redis. Devolve quantas chaves foram retiradas.

    Roda ANTES da sequência, da janela e do teto de flush: presença nunca
    vira Detection no flush, e uma chave que não vai virar Detection não deve
    consumir vaga do teto.

    Dois pipelines para TODAS as regras de ausência do ciclo, nunca por
    regra nem por evento: (1) ``HLEN`` do hash + ``HMGET`` das chaves vistas,
    para saber quais são novas; (2) ``HSET`` das vistas (as novas só até o
    teto total ``ABSENCE_MAX_KEYS_PER_RULE``), ``HSET meta.last_cycle`` — o
    batimento, gravado MESMO com zero matches — e os ``EXPIRE``.

    Redis fora: conta ``absence_unavailable`` por regra, avisa 1× por ciclo e
    segue. O ``meta`` também não avança, então o tique cai no caso "observador
    parado" e não alerta — é assim que a perda de presença vira silêncio do
    motor, e não alerta falso.
    """
    ts = int(time.time() if now is None else now)

    seen_now: dict[int, dict[str, int]] = {}
    rules: dict[int, Any] = dict(getattr(acc, "absence_rules", {}) or {})
    for key in list(acc.pending):
        item = acc.pending[key]
        rule = item.get("rule")
        if not bool(getattr(rule, "absence", False)):
            continue
        token = item.get("token")
        if token is None:
            token = key.rsplit(":", 1)[-1]
        rid = int(rule.rule_id)
        rules.setdefault(rid, rule)
        seen_now.setdefault(rid, {})[str(token)] = ts
        del acc.pending[key]

    if not rules:
        return 0

    removed = sum(len(v) for v in seen_now.values())
    cap = int(settings.ABSENCE_MAX_KEYS_PER_RULE)
    try:
        from ..observability_store import _redis

        redis = _redis()
        order = sorted(rules)
        pipe = redis.pipeline()
        for rid in order:
            tokens = list(seen_now.get(rid, {}))
            pipe.hlen(seen_key(organization_id, rid))
            if tokens:
                pipe.hmget(seen_key(organization_id, rid), tokens)
        raw = pipe.execute()

        pipe = redis.pipeline()
        cursor = 0
        for rid in order:
            rule = rules[rid]
            tokens = list(seen_now.get(rid, {}))
            hlen = int(raw[cursor] or 0)
            cursor += 1
            existing: list[Any] = []
            if tokens:
                existing = list(raw[cursor] or [])
                cursor += 1
            mapping: dict[str, int] = {}
            budget = max(0, cap - hlen)
            over = 0
            for token, present in zip(tokens, existing):
                if present is not None:
                    mapping[token] = ts
                elif budget > 0:
                    mapping[token] = ts
                    budget -= 1
                else:
                    over += 1
            if over:
                acc.count_error("absence_key_cap", rid, over)
                acc._warn_once(
                    "absence_key_cap", rid,
                    "absence: regra %s (%s) atingiu o teto de %d chaves vigiadas — "
                    "%d chave(s) nova(s) deste ciclo NÃO entraram na vigília; as "
                    "já vigiadas seguem. Teto atingido costuma indicar group_by de "
                    "alta cardinalidade para uma regra de ausência.",
                    rid, getattr(rule, "name", "?"), cap, over,
                )
            ttl = int(getattr(rule, "forget_seconds", 0) or 0) + _STATE_TTL_SLACK
            if mapping:
                pipe.hset(seen_key(organization_id, rid), mapping=mapping)
            pipe.hset(meta_key(organization_id, rid), "last_cycle", ts)
            pipe.expire(seen_key(organization_id, rid), ttl)
            pipe.expire(meta_key(organization_id, rid), ttl)
        pipe.execute()
    except Exception:  # noqa: BLE001 — Redis fora: a coleta nunca cai (R3)
        for rid in rules:
            acc.count_error("absence_unavailable", rid)
        acc._warn_once(
            "absence_unavailable", None,
            "absence: Redis indisponível para gravar presença — %d regra(s) de "
            "ausência sem batimento neste ciclo (fail-closed: o tique não "
            "alerta enquanto o observador não provar que estava olhando)",
            len(rules),
        )
    return removed


# ── metade 2: silêncio, no tique ─────────────────────────────────────────


@dataclass
class AbsenceTickResult:
    """Contadores de UM tique. Todos somados sobre as regras avaliadas."""

    rules: int = 0
    evaluated: int = 0
    skipped_lock: int = 0
    unobservable: int = 0
    lagging: int = 0
    unavailable: int = 0
    rejected: int = 0
    alerted: int = 0
    bumped: int = 0
    recovered: int = 0
    forgotten: int = 0
    tracked: int = 0
    silent: int = 0
    errors: int = 0
    states: dict[int, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        out = {k: v for k, v in self.__dict__.items() if k != "states"}
        out["states"] = dict(self.states)
        return out


def _acquire_lock(redis: Any, rule_id: int, ttl_s: int) -> bool:
    """``True`` = este processo é dono do tique da regra. Fail-OPEN sem Redis,
    como o tique do lote: um alerta duplicado é absorvido pelo dedup da
    Detection; um tique perdido é uma ausência que ninguém viu."""
    try:
        return bool(redis.set(lock_key(rule_id), "1", nx=True, ex=max(int(ttl_s), 5)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("absence: lock do tique falhou (regra %s): %s — seguindo", rule_id, exc)
        return True


def _source_lagging(db: Any, organization_id: int, stream: str, window_seconds: int, now: float) -> bool:
    """A fonte que a regra fixa está com BACKLOG? Watermark mais velho que o
    prazo E ``last_run_capped`` no mesmo estado — é a combinação que a
    memória do watermark exige: atraso sozinho não prova nada (um stream sem
    eventos mantém o watermark legitimamente parado)."""
    try:
        from ...db import models

        rows = (
            db.query(models.CollectionState)
            .join(models.Integration, models.Integration.id == models.CollectionState.integration_id)
            .filter(
                models.Integration.organization_id == int(organization_id),
                models.CollectionState.stream == stream,
            )
            .all()
        )
    except Exception:  # noqa: BLE001 — sem leitura, sem guarda (o batimento segue)
        logger.debug("absence: leitura do estado de coleta falhou", exc_info=True)
        return False
    for row in rows:
        wm = getattr(row, "watermark_at", None)
        if wm is None or not bool(getattr(row, "last_run_capped", False)):
            continue
        # ``watermark_at`` é gravado INGÊNUO em UTC (``datetime.utcnow``).
        # ``.timestamp()`` de um datetime ingênuo assume hora LOCAL, e num
        # worker em UTC-3 o watermark "avança" 3 h — um atraso real de 1 h
        # viraria "em dia". Carimbar UTC antes de converter é o que fecha isso.
        try:
            from datetime import timezone as _tz

            wm_utc = wm if wm.tzinfo is not None else wm.replace(tzinfo=_tz.utc)
            age = now - wm_utc.timestamp()
        except Exception:  # noqa: BLE001
            continue
        if age > int(window_seconds):
            return True
    return False


def _close_detection(db: Any, detection_id: Any, *, reason: str) -> bool:
    """Fecha a Detection do alerta (auto-fechamento) e emite o 2004 de
    fechamento quando a flag de ciclo de vida está ligada. ``True`` se
    fechou de fato."""
    if not bool(settings.ABSENCE_AUTO_CLOSE):
        return False
    try:
        from ...db import repository
        from ..detection_events import emit_status_event

        repo = repository.DetectionRepository(db)
        det = repo.get(int(detection_id), organization_ids=None)
        if det is None or det.status == "closed":
            return False
        previous = det.status
        updated = repo.set_status(det, "closed")
        emit_status_event(db, updated, previous_status=previous, actor_user_id=None)
        logger.info("absence: Detection %s fechada (%s)", detection_id, reason)
        return True
    except Exception:  # noqa: BLE001 — fechar é cortesia; nunca derruba o tique
        logger.exception("absence: falha fechando a Detection %s", detection_id)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False


def _mirror(metric: str, rule_id: int, count: int) -> None:
    """Contador por regra no observability_store (kind='rule'), a mesma série
    que a UI lê como ``err_*``. Best-effort, como no flush."""
    if count <= 0:
        return
    try:
        from .runtime import _record_rule_metric

        _record_rule_metric(metric, int(rule_id), int(count))
    except Exception:  # noqa: BLE001
        logger.debug("absence: falha espelhando %s da regra %s", metric, rule_id, exc_info=True)


def evaluate_absence_rule(
    rule: "CompiledInflightRule",
    *,
    organization_id: int,
    redis: Any,
    db: Any,
    now: float,
    result: AbsenceTickResult,
    alert_budget: list[int],
    lock_ttl_s: int,
) -> None:
    """Decide o silêncio de UMA regra. Nunca levanta: toda falha vira
    contador e log, e as outras regras do tique seguem.

    ``alert_budget`` é uma lista de um inteiro — o teto GLOBAL de alertas do
    tique (``ABSENCE_MAX_ALERTS_PER_TICK``), compartilhado entre as regras e
    decrementado aqui. Uma regra de alta cardinalidade que acorde calada
    inteira não pode gastar o tique das outras.
    """
    rid = int(rule.rule_id)
    org = int(organization_id)
    result.rules += 1

    if not _acquire_lock(redis, rid, lock_ttl_s):
        result.skipped_lock += 1
        return

    window = int(rule.window_seconds)
    forget = int(getattr(rule, "forget_seconds", 0) or 0) or 3 * window
    grace = int(settings.ABSENCE_GRACE_SECONDS)
    observer_max_age = int(settings.ABSENCE_OBSERVER_MAX_AGE_SECONDS)
    ttl = forget + _STATE_TTL_SLACK

    def _state(state: str, **extra: Any) -> None:
        result.states[rid] = state
        try:
            fields: dict[str, Any] = {"last_tick": int(now), "state": state}
            fields.update({k: v for k, v in extra.items() if v is not None})
            pipe = redis.pipeline()
            pipe.hset(meta_key(org, rid), mapping=fields)
            pipe.expire(meta_key(org, rid), ttl)
            pipe.execute()
        except Exception:  # noqa: BLE001
            logger.debug("absence: falha gravando meta da regra %s", rid, exc_info=True)

    try:
        pipe = redis.pipeline()
        pipe.hgetall(meta_key(org, rid))
        pipe.hgetall(seen_key(org, rid))
        pipe.hgetall(alerted_key(org, rid))
        meta, seen, alerted = pipe.execute()
    except Exception as exc:  # noqa: BLE001 — Redis fora: sem estado, sem alerta
        result.unavailable += 1
        result.states[rid] = TICK_STATE_UNAVAILABLE
        _mirror("err_absence_unavailable", rid, 1)
        logger.warning("absence: Redis indisponível no tique (regra %s): %s", rid, exc)
        return

    meta = meta or {}
    seen = seen or {}
    alerted = alerted or {}

    # ── guarda 1: o observador estava olhando? ──
    try:
        last_cycle = float(meta.get("last_cycle")) if meta.get("last_cycle") is not None else None
    except (TypeError, ValueError):
        last_cycle = None
    if last_cycle is None or now - last_cycle > observer_max_age:
        result.unobservable += 1
        _mirror("err_absence_unobservable", rid, 1)
        _state(TICK_STATE_UNOBSERVABLE, tracked=len(seen), silent=None)
        logger.warning(
            "absence: regra %s (%s) sem batimento do observador há %s — nenhum "
            "alerta neste tique (o worker parou, a regra está fora do teto por "
            "ciclo ou o Redis falhou no flush). Isto NÃO é 'a fonte está calada'.",
            rid, rule.name,
            "sempre" if last_cycle is None else f"{int(now - last_cycle)} s",
        )
        return

    # ── guarda 2: a fonte fixada está chegando? ──
    stream = stream_pinned_by(rule.clauses)
    if stream and _source_lagging(db, org, stream, window, now):
        result.lagging += 1
        _mirror("err_absence_source_lagging", rid, 1)
        _state(TICK_STATE_LAGGING, tracked=len(seen), silent=None)
        logger.warning(
            "absence: regra %s (%s) segurada — o coletor de %s está com backlog "
            "(watermark além do prazo com teto atingido); silêncio aqui seria "
            "atraso de coleta, não ausência da fonte.",
            rid, rule.name, stream,
        )
        return

    # ── decisão por chave ──
    to_forget: list[str] = []
    candidates: list[tuple[str, float]] = []
    recovered: list[str] = []
    for token, raw_ts in seen.items():
        try:
            last_seen = float(raw_ts)
        except (TypeError, ValueError):
            to_forget.append(token)
            continue
        age = now - last_seen
        if age > forget:
            to_forget.append(token)
        elif age > window + grace:
            candidates.append((token, last_seen))
        elif token in alerted:
            recovered.append(token)

    # esquecer: sai da vigília; se estava alertada, o alerta fecha junto
    if to_forget:
        try:
            pipe = redis.pipeline()
            pipe.hdel(seen_key(org, rid), *to_forget)
            forgotten_alerted = [t for t in to_forget if t in alerted]
            if forgotten_alerted:
                pipe.hdel(alerted_key(org, rid), *forgotten_alerted)
            pipe.execute()
        except Exception:  # noqa: BLE001
            logger.debug("absence: falha esquecendo chaves da regra %s", rid, exc_info=True)
        for token in to_forget:
            det_id = alerted.get(token)
            if det_id is not None and _close_detection(db, det_id, reason="forgotten"):
                result.recovered += 1
        result.forgotten += len(to_forget)

    # recuperação: a chave voltou → o silêncio acabou por definição
    if recovered:
        for token in recovered:
            if _close_detection(db, alerted[token], reason="recovered"):
                result.recovered += 1
        try:
            redis.hdel(alerted_key(org, rid), *recovered)
        except Exception:  # noqa: BLE001
            logger.debug("absence: falha limpando alertas recuperados da regra %s", rid, exc_info=True)

    # silêncio: Detection (nova ou bump dentro da supressão) + emissão
    emits: list[Any] = []
    bumped = 0
    newly_alerted: dict[str, Any] = {}
    if candidates:
        from ...db import repository
        from .runtime import DetectionEmit

        repo = repository.DetectionRepository(db)
        for token, last_seen in candidates:
            if alert_budget[0] <= 0:
                logger.warning(
                    "absence: teto de %d alertas por tique atingido — %d chave(s) "
                    "calada(s) da regra %s ficam para o próximo tique",
                    int(settings.ABSENCE_MAX_ALERTS_PER_TICK), len(candidates), rid,
                )
                break
            try:
                det = repo.record(
                    organization_id=org,
                    source="inflight",
                    dedup_key=dedup_key(org, rid, token),
                    severity_id=int(rule.severity_id),
                    rule_id=str(rid),
                    rule_name=rule.name,
                    integration_id=None,
                    suppression_window_seconds=int(rule.suppression_window_seconds),
                )
            except Exception:  # noqa: BLE001 — uma chave ruim não derruba a regra
                result.errors += 1
                logger.exception("absence: falha gravando Detection (regra %s)", rid)
                try:
                    db.rollback()
                except Exception:  # noqa: BLE001
                    pass
                continue
            alert_budget[0] -= 1
            silent_for = int(now - last_seen)
            if getattr(det, "count", None) == 1:
                newly_alerted[token] = getattr(det, "id", None)
                emits.append(
                    DetectionEmit(
                        dedup_key=dedup_key(org, rid, token),
                        detection_id=getattr(det, "id", None),
                        rule_id=rid,
                        rule_name=rule.name,
                        severity_id=int(rule.severity_id),
                        integration_id=None,
                        source={
                            "group_field": ".".join(rule.group_by_path or ()) or None,
                            "group_value": token,
                            "stream": stream,
                            "absence": {
                                "last_seen": int(last_seen),
                                "silent_for_seconds": silent_for,
                                "expected_within_seconds": window,
                                "forget_after_seconds": forget,
                            },
                        },
                        emit_event=bool(getattr(rule, "emit_event", False)),
                    )
                )
            else:
                bumped += 1
                if token not in alerted and getattr(det, "id", None) is not None:
                    newly_alerted[token] = getattr(det, "id", None)

    if newly_alerted:
        try:
            pipe = redis.pipeline()
            pipe.hset(alerted_key(org, rid), mapping={k: str(v) for k, v in newly_alerted.items()})
            pipe.expire(alerted_key(org, rid), ttl)
            pipe.execute()
        except Exception:  # noqa: BLE001
            logger.debug("absence: falha marcando alertas da regra %s", rid, exc_info=True)

    if emits:
        try:
            from .runtime import InflightAccumulator, _emit_detection_events

            acc = InflightAccumulator()
            asyncio.run(_emit_detection_events(acc, tuple(emits), org, suppressed=bumped))
            for reason, by_rule in acc.errors.items():
                if isinstance(by_rule, dict):
                    for r, n in by_rule.items():
                        _mirror(f"err_{reason}", r, n)
        except Exception:  # noqa: BLE001 — as Detections já estão gravadas
            logger.exception("absence: emissão falhou (regra %s) — Detections gravadas", rid)

    result.evaluated += 1
    result.alerted += len(emits)
    result.bumped += bumped
    tracked = len(seen) - len(to_forget)
    silent = len(candidates)
    result.tracked += tracked
    result.silent += silent
    _mirror("absence_alerted", rid, len(emits))
    _state(TICK_STATE_OK, tracked=tracked, silent=silent)


def evaluate_absence_rules_once(
    *,
    now: Optional[float] = None,
    redis: Any = None,
    session_factory: Optional[Callable[[], Any]] = None,
    limit: int = 500,
) -> AbsenceTickResult:
    """Corpo do tique, síncrono. Uma consulta indexada pelas regras de
    ausência habilitadas; compila cada uma (a mesma compilação da carga do
    ciclo, então uma regra que o ciclo rejeita o tique também rejeita) e
    avalia. Nunca levanta."""
    ts = float(time.time() if now is None else now)
    result = AbsenceTickResult()
    if not bool(settings.ABSENCE_RULES_ENABLED):
        return result

    from ...db import database, repository
    from .runtime import compile_rule

    if redis is None:
        try:
            from ..observability_store import _redis

            redis = _redis()
        except Exception as exc:  # noqa: BLE001
            logger.warning("absence: Redis indisponível para o tique: %s", exc)
            result.unavailable += 1
            return result

    factory = session_factory or database.SessionLocal
    budget = [int(settings.ABSENCE_MAX_ALERTS_PER_TICK)]
    lock_ttl = max(5, int(settings.ABSENCE_TICK_SECONDS) - 5)
    db = factory()
    try:
        rows = repository.CorrelationRuleRepository(db).list_absence_enabled(limit=limit)
        for row in rows:
            compiled, reason = compile_rule(row)
            if compiled is None or not getattr(compiled, "absence", False):
                result.rejected += 1
                logger.warning(
                    "absence: regra %s (%s) rejeitada na compilação do tique: %s",
                    getattr(row, "id", "?"), getattr(row, "name", "?"), reason or "not_absence",
                )
                continue
            try:
                evaluate_absence_rule(
                    compiled,
                    organization_id=int(row.organization_id),
                    redis=redis,
                    db=db,
                    now=ts,
                    result=result,
                    alert_budget=budget,
                    lock_ttl_s=lock_ttl,
                )
            except Exception:  # noqa: BLE001 — uma regra ruim não derruba o tique
                result.errors += 1
                logger.exception("absence: regra %s falhou no tique", getattr(row, "id", "?"))
                try:
                    db.rollback()
                except Exception:  # noqa: BLE001
                    pass
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass
    return result


__all__ = [
    "ABSENCE_RULE_TYPE",
    "ABSENCE_MIN_WINDOW_SECONDS",
    "AbsenceTickResult",
    "TICK_STATE_OK",
    "TICK_STATE_UNOBSERVABLE",
    "TICK_STATE_LAGGING",
    "TICK_STATE_UNAVAILABLE",
    "apply_absence_presence",
    "evaluate_absence_rule",
    "evaluate_absence_rules_once",
    "dedup_key",
    "seen_key",
    "meta_key",
    "alerted_key",
    "forget_seconds_for",
    "stream_pinned_by",
]
