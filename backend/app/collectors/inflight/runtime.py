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
import hashlib
import json
import logging
from typing import Any, Mapping, Optional

from ...core.config import settings
from .matcher import CompiledClause, CompiledInflightRule, CompiledRuleSet

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
#: Governa APENAS ``collector_inflight_rules_rejected_total`` (falha de
#: COMPILAÇÃO, 1x por ciclo na carga). Não se mistura com ``ERROR_REASONS``,
#: que é a outra série e o outro momento (avaliação/flush).
REJECT_REASONS = ("bad_json", "empty_where", "unknown_op", "over_cap", "truncated")

#: Enum FECHADO de razões de ERRO de avaliação/flush — label de
#: ``collector_inflight_errors_total``. Mesma disciplina do enum acima: nunca
#: carrega valor de evento nem nome de regra.
#:
#: ``rule_id`` deliberadamente NÃO entra como label desta série: é id global,
#: multiplicaria a cardinalidade por ``reason`` e o lado OTLP não tem TTL para
#: envelhecer a série de uma regra apagada — a mesma recusa já escrita para
#: ``collector_capture_tap_disabled_total``. O breakdown por regra vai para o
#: ``observability_store`` (Redis, TTL 25h), que é de onde a UI lê.
#:
#: ``matcher`` é o único reason escrito de FORA deste módulo
#: (``pipeline.py``, no ``except`` que envolve ``evaluate_ruleset``) e o único
#: NÃO atribuível a uma regra: a exceção nasce antes de se saber qual regra
#: estava sendo avaliada, então não há ``rule_id`` honesto para carregar. Ele
#: pertence ao enum mesmo assim, porque chega a ``INFLIGHT_ERRORS.labels()`` em
#: produção — deixá-lo de fora tornaria a palavra "FECHADO" acima falsa, e um
#: invariante que é só comentário é exatamente a dívida que este arquivo existe
#: para não repetir.
ERROR_REASONS = (
    "group_by_unresolved",
    "key_cap",
    "flush_lost",
    "group_value_truncated",
    "matcher",
)

#: Razões que NÃO descem ao ``observability_store`` por regra, porque não há
#: ``rule_id`` honesto para carregar. Declarado aqui, e não escondido num teste,
#: para que a pergunta "por que esta razão não aparece no breakdown da UI?"
#: tenha resposta no mesmo lugar onde o enum vive. Uma razão nova escrita de
#: fora deste módulo entra AQUI ou ganha ``rule_id`` — nunca some em silêncio.
UNATTRIBUTED_ERROR_REASONS = ("matcher",)

#: Bytes do digest anexado ao token de group_by quando há corte (16 chars hex).
#: 64 bits é folgado para o universo real (chaves distintas por regra/ciclo é
#: teto de 2 dígitos), e o custo do sufixo entra no índice B-tree de
#: ``ix_detections_org_dedup`` — por isso curto, não sha256 inteiro.
GROUP_VALUE_DIGEST_BYTES = 8

#: Separador prefixo↔digest. Fora do alfabeto hex de propósito: o operador que
#: lê a dedup_key precisa enxergar onde o valor legível termina.
GROUP_VALUE_DIGEST_SEP = "~"

#: Granularidade dos contadores de disparo por regra (``observability_store``,
#: kind="rule") — HORÁRIA, não per-minute (default do store). Uma janela de
#: 24h em buckets de minuto seria 1440 campos no hash por regra; horária são
#: 24 — a mesma janela, hash 60x menor.
RULE_METRIC_BUCKET_SECONDS = 60 * 60

#: TTL dos contadores de disparo por regra: cobre a janela de leitura de 24h
#: (``RULE_METRIC_WINDOW_MINUTES``) com 1h de folga, para o bucket mais antigo
#: da janela nunca ter expirado no momento da leitura. O default do store
#: (3h) é insuficiente por construção para uma janela de 24h — é exatamente o
#: bug que estes dois valores existem para fechar.
RULE_METRIC_TTL_SECONDS = 25 * 60 * 60

#: Janela que a UI lê — "disparos nas últimas 24h" por regra.
RULE_METRIC_WINDOW_MINUTES = 24 * 60


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
            # Checagem explícita de None, NUNCA ``or``: ``0`` é um valor
            # LEGÍTIMO (supressão desligada) e ``or 3600`` o engoliria em
            # silêncio, dando ao operador uma janela de 1h que ele não pediu.
            # É a mesma classe do bug ``or 7`` do TTL de dedupe corrigido nesta
            # mesma branch — e eu o reintroduzi aqui.
            suppression_window_seconds=int(
                _sup if (_sup := getattr(row, "suppression_window_seconds", None)) is not None
                else 3600
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
) -> CompiledRuleSet:
    """Regras habilitadas em modo ``inflight`` da org, compiladas. SÍNCRONA.

    Chamada via ``asyncio.to_thread`` 1x por ciclo. Fail-safe para ``()`` em
    qualquer erro: um problema de DB não pode impedir a COLETA, que é o produto.
    ``organization_id is None`` ⇒ ``()`` (fail-CLOSED — nunca avaliar regra sem
    saber de quem é o evento).
    """
    if organization_id is None:
        return CompiledRuleSet(rules=(), share_paths=False)

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
                return CompiledRuleSet(rules=(), share_paths=False)
            rows = repo.list_inflight_for_org(organization_id, limit=cap)
            if len(rows) >= cap:
                # TRUNCAMENTO SILENCIOSO — o pior modo de falha desta feature.
                #
                # ``CORRELATION_MAX_RULES_PER_ORG`` (200) governa a CRIAÇÃO;
                # ``INFLIGHT_MAX_RULES_PER_CYCLE`` (50) governa a AVALIAÇÃO. Um
                # cliente pode criar 200 regras em voo e apenas 50 rodam. E como
                # a query ordena por ``id ASC``, as descartadas são sempre as
                # MAIS RECENTES — exatamente a regra que o operador acabou de
                # escrever e está testando. Sem este aviso ela fica verde na
                # lista, nunca dispara, e nada no sistema diz por quê.
                total = repo.count_inflight_for_org(organization_id)
                if total > cap:
                    INFLIGHT_RULES_REJECTED.labels(reason="truncated").inc(total - cap)
                    logger.warning(
                        "inflight: org %s tem %d regras em voo mas só as %d de "
                        "MENOR id são avaliadas por ciclo "
                        "(INFLIGHT_MAX_RULES_PER_CYCLE=%d) — %d regra(s) NÃO "
                        "estão sendo avaliadas, e são as mais RECENTES. "
                        "Desabilite regras antigas ou eleve o teto.",
                        organization_id, total, cap, cap, total - cap,
                    )
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
                return CompiledRuleSet(rules=(), share_paths=False)
    except Exception:  # noqa: BLE001 — coleta nunca cai por causa do detector
        logger.exception("inflight: falha carregando regras (org %s)", organization_id)
        return CompiledRuleSet(rules=(), share_paths=False)

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

    # Decide UMA VEZ se vale cachear resolução de path no laço por evento. Ver
    # ``CompiledRuleSet.share_paths``: o cache incondicional PIORA 1,21x quando
    # todo path é único, então a decisão tem de sair do caminho quente.
    seen: set[tuple[str, ...]] = set()
    share = False
    for rule in compiled:
        for clause in rule.clauses:
            if clause.path in seen:
                share = True
                break
            seen.add(clause.path)
        if share:
            break

    return CompiledRuleSet(rules=tuple(compiled), share_paths=share)


def _group_value_digest(value: str) -> str:
    """Digest curto e determinístico do valor COMPLETO de group_by.

    Sem sal e sem estado de propósito: a MESMA entidade tem de produzir a mesma
    ``dedup_key`` em qualquer worker e em qualquer ciclo, ou a supressão deixa
    de suprimir e o operador recebe o mesmo alerta a cada ciclo. ``blake2s`` e
    não sha256 porque o requisito aqui é injetividade prática, não resistência
    a adversário — e é mais barato no ramo em que roda.

    ``surrogatepass``: o valor vem do evento e pode carregar surrogate solto
    vindo de JSON malformado; ``encode`` estrito levantaria dentro de ``add``,
    que roda no laço de coleta.
    """
    return hashlib.blake2s(
        value.encode("utf-8", "surrogatepass"), digest_size=GROUP_VALUE_DIGEST_BYTES
    ).hexdigest()


class InflightAccumulator:
    """Matches do ciclo, em memória. Nada aqui toca I/O."""

    __slots__ = ("pending", "matches", "errors", "overflow", "_keys_per_rule", "_logged_once")

    def __init__(self) -> None:
        #: dedup_key → payload da Detection a criar
        self.pending: dict[str, dict[str, Any]] = {}
        #: rule_id → nº de eventos casados (pode ser >> len(pending))
        self.matches: dict[int, int] = {}
        #: razão → rule_id → contagem. ANINHADO: o total por razão continua
        #: sendo o que vai para o OTel (cardinalidade intacta), e o breakdown
        #: por regra — que responde "QUAL regra parou de alertar?" — vai para o
        #: observability_store. Ver ``count_error``.
        self.errors: dict[str, dict[int, int]] = {}
        #: rule_id → matches perdidos por teto de chaves
        self.overflow: dict[int, int] = {}
        #: rule_id → nº de chaves distintas já criadas. Contador dedicado, e não
        #: uma varredura de ``pending`` por match: varrer seria O(nº de chaves)
        #: POR EVENTO CASADO, um custo que cresce ao longo do ciclo dentro do
        #: laço de coleta — exatamente o que R1 existe para impedir.
        self._keys_per_rule: dict[int, int] = {}
        #: (razão, rule_id) já avisado neste ciclo. Chaveado pela DUPLA e não só
        #: pelo rule_id: uma regra pode estourar o teto de chaves E truncar o
        #: valor de group_by no mesmo ciclo, e calar o segundo aviso porque o
        #: primeiro saiu esconderia justamente a causa que o operador procura.
        #: ``rule_id=None`` é a razão NÃO atribuível a uma regra (falha do
        #: observability_store, que é um só para todas) — ali a dupla degenera
        #: em "1 aviso por razão por ciclo", que é exatamente o rate certo.
        self._logged_once: set[tuple[str, Optional[int]]] = set()

    def count_error(self, reason: str, rule_id: int, amount: int = 1) -> None:
        """Contabiliza ``amount`` erros de ``reason`` ATRIBUÍDOS a uma regra.

        ``reason`` deve pertencer a ``ERROR_REASONS`` — é label de métrica. O
        ``rule_id`` NÃO vira label (ver o comentário do enum); ele só desce até
        o ``observability_store`` no flush.
        """
        by_rule = self.errors.setdefault(reason, {})
        by_rule[rule_id] = by_rule.get(rule_id, 0) + amount

    def _warn_once(
        self, reason: str, rule_id: Optional[int], message: str, *args: Any
    ) -> None:
        """Rate-limit de log por (razão, regra) por CICLO — sem isso um evento
        ruim repetido troca degradação de detecção por amplificação de escrita
        no log, que é o dano maior.

        ``rule_id=None`` para a razão que NÃO é atribuível a uma regra: N regras
        falhando no mesmo ciclo por causa do mesmo Redis fora são N sintomas de
        UMA causa, e um aviso por regra ali seria a amplificação que esta função
        existe para impedir."""
        token = (reason, rule_id)
        if token in self._logged_once:
            return
        self._logged_once.add(token)
        logger.warning(message, *args)

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
                self.count_error("group_by_unresolved", rule.rule_id)
                return
            value = str(raw)
            cap = int(settings.INFLIGHT_MAX_GROUP_VALUE_LEN)
            token = value[:cap]
            if len(value) > cap:
                # FUSÃO SILENCIOSA DE ENTIDADES. Este token vai inteiro para a
                # dedup_key, que é PERSISTIDA em ``Detection.dedup_key`` e
                # governa a supressão: dois valores de group_by distintos que
                # compartilhassem o prefixo cortado viravam UMA única Detection,
                # sem erro nenhum — a segunda entidade some dentro do alerta da
                # primeira.
                #
                # Não é hipótese, foi MEDIDO em dado real de produção: com
                # group_by em ``rawData.cmdline``, 83,2% dos valores passam do
                # teto e 2 colisões reais foram observadas; em
                # ``rawData.parent_cmdline``, 6,3% passam e 9 colisões reais.
                #
                # O digest do valor COMPLETO restaura a injetividade; o prefixo
                # segue legível para quem investiga. Só neste ramo raro — o
                # caminho comum (valor abaixo do teto) continua sendo uma fatia
                # de string e produz a chave byte-idêntica à de antes. O digest
                # NUNCA vira label de métrica.
                token = f"{token}{GROUP_VALUE_DIGEST_SEP}{_group_value_digest(value)}"
                self.count_error("group_value_truncated", rule.rule_id)
                self._warn_once(
                    "group_value_truncated", rule.rule_id,
                    "inflight: regra %s (%s) — valor de group_by com %d chars "
                    "excede o teto de %d; a dedup_key passa a levar sufixo de "
                    "digest para não fundir entidades distintas numa Detection. "
                    "Sai só o COMPRIMENTO: o valor vem do evento e não entra em "
                    "log. group_by de alta cardinalidade textual (cmdline) é o "
                    "caso típico.",
                    rule.rule_id, rule.name, len(value), cap,
                )

        key = f"inflight:{organization_id}:{rule.rule_id}:{token}"
        if key in self.pending:
            return

        # O teto é sobre CHAVES DISTINTAS, não sobre matches: a variável
        # perigosa é a cardinalidade do group_by, não a taxa de acerto. Uma
        # regra que casa 100% dos eventos com group_by=None gera UMA chave.
        key_cap = int(settings.INFLIGHT_MAX_DEDUP_KEYS_PER_RULE_PER_CYCLE)
        if self._keys_per_rule.get(rule.rule_id, 0) >= key_cap:
            self.overflow[rule.rule_id] = self.overflow.get(rule.rule_id, 0) + 1
            self.count_error("key_cap", rule.rule_id)
            self._warn_once(
                "key_cap", rule.rule_id,
                "inflight: regra %s (%s) atingiu o teto de %d chaves de dedup "
                "no ciclo — matches seguem contados, nenhuma Detection nova é "
                "criada, nenhum evento é descartado. Teto atingido costuma "
                "indicar group_by_field de alta cardinalidade.",
                rule.rule_id, rule.name, key_cap,
            )
            return

        self._keys_per_rule[rule.rule_id] = self._keys_per_rule.get(rule.rule_id, 0) + 1
        self.pending[key] = {
            "rule": rule,
            "integration_id": integration_id,
        }


class InflightFlushInterrupted(Exception):
    """Falha no MEIO da escrita das Detections, carregando o que já foi gravado.

    ``DetectionRepository.record`` faz ``commit`` POR CHAVE: quando a escrita da
    n-ésima Detection estoura, as n-1 anteriores já estão DURÁVEIS no banco e
    não são perda. Contá-las mesmo assim inflava ``flush_lost``, que é
    justamente a série que responde "quanto de detecção o cliente deixou de
    receber" — um número inflado ali manda investigar prejuízo que não houve, e
    corrói a confiança no único contador que mede o dano real.

    O prejuízo viaja NESTA exceção porque o ``written`` que ``_flush_sync``
    devolve não sobrevive ao ``raise``: ``asyncio.to_thread`` propaga o objeto
    de exceção e descarta o valor de retorno. A alternativa — um out-param —
    mudaria a assinatura de ``_flush_sync``, que é ponto de monkeypatch em
    testes deste repo e do EE; um duplo com a assinatura antiga passaria a
    estourar ``TypeError`` dentro do ``finally`` do ciclo de coleta.
    """

    def __init__(self, written_keys: tuple[str, ...]) -> None:
        super().__init__(
            f"flush interrompido após {len(written_keys)} Detection(s) gravada(s)"
        )
        #: ``dedup_key`` das Detections que JÁ foram commitadas antes da falha.
        self.written_keys = written_keys


def _flush_sync(pending: dict[str, dict[str, Any]], organization_id: int) -> int:
    """Escreve as Detections. SÍNCRONA, roda em thread. Devolve quantas gravou.

    Em falha levanta ``InflightFlushInterrupted`` com as chaves já commitadas,
    encadeada (``from``) na exceção original — o ``logger.exception`` do call
    site continua imprimindo a causa verdadeira, e a contagem de perda deixa de
    ser um chute sobre ``pending`` inteiro.
    """
    from ...db import database, repository

    written: list[str] = []
    try:
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
                # DEPOIS do ``record``, nunca antes: ``record`` commita, então a
                # chave só entra aqui quando a linha está durável. Registrar
                # antes traria de volta a mentira, com o sinal invertido —
                # perda real contada como gravação.
                written.append(dedup_key)
    except Exception as exc:  # noqa: BLE001 — reembalado, não engolido
        raise InflightFlushInterrupted(tuple(written)) from exc
    return len(written)


def _record_rule_metric(metric: str, rule_id: int, count: int) -> bool:
    """Escreve UM contador de regra no observability_store (kind="rule").
    Devolve se a gravação foi CONFIRMADA — é esse veredito que
    ``_mirror_rule_metric`` transforma em métrica de perda.

    Best-effort e nunca deixa vazar: ``observability_store.record_counter`` já
    engole exceções internamente (é o contrato do módulo), mas esta função
    ainda envolve a chamada — inclusive o próprio ``import`` — em try/except,
    porque roda dentro do ``finally`` de ``flush_inflight`` e uma falha aqui
    JAMAIS pode mascarar a exceção original do ciclo de coleta nem derrubá-lo.

    ``is not False`` e não ``is True``: ``record_counter`` só passou a devolver
    veredito agora, e ela é ponto de monkeypatch em dezenas de testes deste repo
    e do EE, cujos duplos devolvem ``None``. Ler ``None`` como falha inverteria
    o sinal e faria a série de perda contar prejuízo que não houve — o mesmo
    erro de que ``flush_lost`` acabou de ser curado, com o sinal trocado. Sem
    veredito ⇒ nada a reportar.
    """
    try:
        from .. import observability_store as obs

        gravou = obs.record_counter(
            "rule",
            str(rule_id),
            metric,
            float(count),
            ttl_seconds=RULE_METRIC_TTL_SECONDS,
            bucket_seconds=RULE_METRIC_BUCKET_SECONDS,
        )
    except Exception:  # noqa: BLE001 — best-effort, nunca derruba o flush
        # O traceback fica em DEBUG porque sai POR CHAMADA (nº de regras ×
        # famílias de contador): promovê-lo a WARNING seria exatamente a
        # amplificação de escrita no log que o rate-limit existe para evitar. A
        # linha que o OPERADOR precisa ver sobe a WARNING uma vez por ciclo, em
        # ``_mirror_rule_metric``.
        logger.debug(
            "inflight: falha gravando '%s' no observability_store (rule %s)",
            metric, rule_id, exc_info=True,
        )
        return False
    return gravou is not False


def _mirror_rule_metric(
    acc: InflightAccumulator, metric: str, rule_id: int, count: int
) -> None:
    """Espelha um contador de regra e CONTA a escrita que se perdeu.

    DEGRADAÇÃO DECLARADA — o que o operador vê quando o Redis cai NA ESCRITA: a
    chave ``obs:rule:{rule_id}:{metric}`` nunca é criada. Depois, na leitura, o
    endpoint ENCONTRA a ausência (não um erro): ``read_window_total_strict``
    varre um hash vazio e soma ``0.0`` sem levantar nada, e a UI escreve
    "0 disparos" para uma regra que disparou. É a mentira que o ``strict`` foi
    escrito para matar, um passo antes de onde ele age.

    Por que o ``strict`` da leitura NÃO alcança, e não é questão de melhorá-lo:
    ele distingue "não sei" de "zero" quando é a LEITURA que falha. Aqui a
    leitura funciona perfeitamente — o que falta é o DADO. Ausência de chave e
    "a regra não disparou" são literalmente o mesmo estado no Redis, então
    nenhuma leitura, por mais estrita, consegue separá-los. A perda só é
    conhecível no lado que a produz, e é por isso que ela é contada aqui.

    Deliberadamente NÃO se tenta inferir buraco na série do lado da leitura: um
    heurístico ali transformaria toda regra genuinamente muda em suspeita de
    falha, e trocar um falso "0" por um falso "não sei" não é progresso. O
    objetivo é tornar a perda VISÍVEL (``increase(collector_inflight_rule_
    metric_write_failures_total) > 0`` ⇒ "o contador de disparos está
    sub-reportando agora"), não adivinhá-la.

    O contador do OTel (``INFLIGHT_MATCHES``) NÃO cobre esse buraco: ele é
    no-op quando ``OTEL_ENABLED=False``, que é o default da instalação padrão —
    justamente a razão de este espelhamento existir.
    """
    if _record_rule_metric(metric, rule_id, count):
        return
    try:
        from ..metrics import INFLIGHT_METRIC_WRITE_FAILURES

        INFLIGHT_METRIC_WRITE_FAILURES.labels(metric=metric).inc()
        # ``rule_id=None``: o aviso é 1x por CICLO e não por regra. Quem falhou
        # foi o STORE, que é um só — N regras falhando são N sintomas de UMA
        # causa. Mesma razão pela qual ``rule_id`` não é label da série.
        acc._warn_once(
            "obs_store_write_failed", None,
            "inflight: o observability_store recusou a escrita dos contadores "
            "por regra neste ciclo (1ª família a falhar: '%s') — os disparos "
            "deste ciclo NÃO entram na série que a UI lê e vão aparecer como "
            "'0 disparos' para regras que dispararam. A leitura strict não "
            "alcança: ausência de chave é indistinguível de regra muda. "
            "Ver collector_inflight_rule_metric_write_failures_total.",
            metric,
        )
    except Exception:  # noqa: BLE001
        # Best-effort dentro de best-effort, como na atribuição de
        # ``flush_lost``: quem estoura aqui é o CONTADOR do prejuízo, e ele não
        # pode agravá-lo levantando dentro do ``finally`` do ciclo de coleta.
        logger.debug(
            "inflight: falha contabilizando a perda de escrita de '%s'",
            metric, exc_info=True,
        )


async def flush_inflight(
    acc: Optional[InflightAccumulator], organization_id: Optional[int]
) -> None:
    """Persiste os matches do ciclo e emite as métricas. Best-effort.

    Chamada no ``finally`` do ciclo — cobre caminho feliz E caminho de exceção.
    Isso não é zelo: no data-plane default uma exceção no meio do ciclo NÃO
    solta as claims de dedupe, o retry re-busca os eventos e ``claim`` os
    descarta como duplicados. Sem flush aqui, os matches morreriam em memória
    sem log nem métrica.

    Além do instrumento OTel (``INFLIGHT_MATCHES``, no-op quando
    ``OTEL_ENABLED=False`` — o default da instalação padrão), espelha
    ``acc.matches``/``acc.overflow`` no ``observability_store`` (Redis,
    kind="rule") para a UI mostrar "disparos nas últimas 24h" por regra sem
    depender de um OTel Collector estar configurado.

    DUAS métricas, não uma: ``matches`` sozinho ao lado de uma lista de
    Detections muito menor pareceria produto quebrado, mas a divergência é
    ESTRUTURAL (dedup + teto de chaves por regra/ciclo + group_by não
    resolvido) — matches alto com ``overflow`` alto é o diagnóstico de que a
    cardinalidade do group_by estourou o teto, não um bug.

    Os ERROS seguem a mesma divisão, e é aqui que ela paga: o OTel recebe
    ``collector_inflight_errors_total{reason}`` exatamente como antes (soma do
    dict interno — zero mudança de cardinalidade), enquanto o breakdown
    ``err_{reason}`` por regra vai para o observability_store. Sem ele,
    "1200 group_by_unresolved" é um número que não aponta para nenhuma regra e
    o operador não tem o que corrigir.

    O espelhamento é best-effort mas não é MUDO: toda escrita que o store
    recusar é contada em ``collector_inflight_rule_metric_write_failures_total``
    e avisada em WARNING 1x por ciclo (ver ``_mirror_rule_metric``). Sem isso a
    perda era invisível dos dois lados — a chave não nascia na escrita e a
    leitura encontrava a AUSÊNCIA, que soma 0.0 sem erro e vira "a regra não
    disparou" na tela do operador.

    ``flush_lost`` conta a perda REAL, não ``len(acc.pending)``:
    ``DetectionRepository.record`` commita por chave, então uma falha no meio da
    escrita deixa parte das Detections DURÁVEIS no banco. Elas voltam em
    ``InflightFlushInterrupted.written_keys`` e saem da conta. Só o caminho em
    que a exceção nasce FORA do laço de escrita continua contando tudo — e ali
    o log diz explicitamente que o número é um teto, não uma medida.
    """
    if acc is None or organization_id is None:
        return

    from ..metrics import INFLIGHT_ERRORS, INFLIGHT_MATCHES

    if acc.pending:
        try:
            await asyncio.to_thread(_flush_sync, acc.pending, int(organization_id))
        except Exception as exc:  # noqa: BLE001
            # A perda é CONTADA, não presumida — e ATRIBUÍDA. "Perdi 900
            # Detections" não diz QUAL regra parou de alertar; ``item["rule"]``
            # já carrega a regra compilada, então agrupar por ``rule_id`` aqui
            # não custa nenhuma volta ao banco.
            #
            # E CONTADA sobre o que de fato se perdeu: ``record`` commita por
            # chave, então numa falha no meio do laço as Detections anteriores
            # estão no banco. Elas chegam aqui em ``written_keys`` e saem da
            # conta. Antes, ``pending`` inteiro virava ``flush_lost`` e a série
            # reportava prejuízo maior que o real.
            medido = isinstance(exc, InflightFlushInterrupted)
            gravadas = frozenset(exc.written_keys) if medido else frozenset()
            # Aritmética pura ANTES do laço guardado abaixo: é o número que vai
            # para o log, e ele não pode depender de a atribuição por regra ter
            # ido até o fim.
            perdidas = sum(1 for key in acc.pending if key not in gravadas)
            try:
                for dedup_key, item in acc.pending.items():
                    if dedup_key in gravadas:
                        continue
                    acc.count_error("flush_lost", int(item["rule"].rule_id))
            except Exception:  # noqa: BLE001
                # Best-effort dentro de best-effort: quem estoura aqui é o
                # CONTADOR do prejuízo, e ele não pode agravar o prejuízo
                # levantando dentro do ``finally`` do ciclo de coleta.
                logger.debug(
                    "inflight: falha atribuindo flush_lost por regra", exc_info=True
                )
            if medido:
                # ``len(acc.pending) - perdidas`` e não ``len(gravadas)``: as
                # duas contagens só coincidem quando todo ``written_key`` está
                # em ``pending``, e a soma no log tem de fechar sempre.
                logger.exception(
                    "inflight: flush interrompido — %d de %d Detection(s) já "
                    "estavam COMMITADAS (não são perda), %d perdida(s) (org %s)",
                    len(acc.pending) - perdidas, len(acc.pending), perdidas,
                    organization_id,
                )
            else:
                # DEGRADAÇÃO DECLARADA: a exceção não nasceu dentro do laço de
                # escrita (``_flush_sync`` substituído por um duplo, falha ao
                # despachar para a thread, ...), então não existe medida de
                # quantas foram gravadas. Conta tudo como perda — pessimismo do
                # lado certo, porque calar perda que houve é pior que reportar
                # perda que não houve — e o log diz que o número é um TETO, para
                # ninguém tratá-lo como medição.
                logger.exception(
                    "inflight: flush falhou por fora do laço de escrita — até "
                    "%d Detection(s) perdida(s) (org %s); o número é um TETO, "
                    "este caminho não sabe quantas já haviam sido gravadas",
                    perdidas, organization_id,
                )

    for rule_id, count in acc.matches.items():
        INFLIGHT_MATCHES.labels(rule_id=str(rule_id)).inc(count)
        _mirror_rule_metric(acc, "matches", rule_id, count)
    for rule_id, count in acc.overflow.items():
        _mirror_rule_metric(acc, "overflow", rule_id, count)
    for reason, by_rule in acc.errors.items():
        # DUAS superfícies, uma passada. OTel recebe SÓ ``reason``, somando o
        # dict interno: ``sum by (reason)`` fica idêntico ao de antes desta
        # mudança e a cardinalidade não se mexe. O breakdown por regra desce
        # para o observability_store, onde há TTL de 25h para envelhecer a
        # série de uma regra apagada — o que o OTLP não tem.
        if isinstance(by_rule, dict):
            total = sum(by_rule.values())
            per_rule = tuple(by_rule.items())
        else:
            # Forma legada (razão → int) escrita por call site fora deste
            # módulo — hoje ``pipeline.py`` com reason="matcher", que não é
            # atribuível a uma regra (a exceção veio do matcher, antes de saber
            # qual). Aceitar as duas formas aqui é o que impede um ``sum`` sobre
            # int de levantar DENTRO do ``finally`` do ciclo e mascarar a
            # exceção original que estivesse se propagando.
            total, per_rule = int(by_rule), ()
        if total:
            INFLIGHT_ERRORS.labels(reason=reason).inc(total)
        for rule_id, count in per_rule:
            _mirror_rule_metric(acc, f"err_{reason}", rule_id, count)
