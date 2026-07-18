"""Routing engine. PURE: no DB / Redis / dispatch.

Cribl-style label routing:
  - Routes are evaluated by ``priority`` ASCENDING (lower first; ties broken by
    id for determinism).
  - First-match with ``is_final``:
      * ``is_final=True``  → stop at the first matching route (exclusive).
      * ``is_final=False`` → CLONE the event into that route's destinations and
        CONTINUE to the next route (fan-out: one event → many destinations).
  - ``action="drop"`` → the matching event is discarded (cost-control / noise
    filter). A drop is terminal for that event.
  - **Zero silent loss (default/catch-all):** an event that matches NO route
    falls back to ``wazuh-default`` (back-compat). Operators usually add an
    explicit catch-all route (``condition={}``, lowest priority, is_final) which
    makes every event either routed or dropped; the wazuh-default fallback is the
    safety net when no catch-all exists.
  - **Conditions are label-driven JSON** over the ``_centralops`` envelope labels
    (no regex): ``{field: scalar}`` (eq shorthand) or ``{field: {op: value}}``;
    multiple fields are ANDed. ``condition={}`` matches everything.

The producer (pipeline) loads ``Route`` rows, compiles them to ``CompiledRoute``,
and calls :func:`route_batch` to split a batch into per-destination sub-batches.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from .pii_redaction import CompiledRedactionRule, apply_pii_redaction

# ── Vocabulary ─────────────────────────────────────────────────────────

#: Routing labels available in a condition — the documented ``_centralops``
#: fields. Kept to an allowlist for predictability + a clean UI.
ALLOWED_FIELDS = frozenset(
    {
        "vendor",
        # ``platform`` (Integration.platform: "sophos",
        # "microsoft_defender", ...) is one of the 6 first-class routing labels.
        # Emitted in the envelope by ``build_envelope`` (defaults to ``vendor``).
        "platform",
        "organization_id",
        "severity_id",
        "stream",
        "event_type",
        "integration_id",
        "customer_id",
        # data_geography from the origin integration (Sophos dataRegion)
        "data_geography",
    }
)

#: Some routes spell the tenant label ``org_id``; the canonical envelope field
#: is ``organization_id``. We accept ``org_id`` as a write-time ALIAS in route
#: conditions and normalize it to the canonical field before evaluation, so a
#: route authored ``{"org_id": 42}`` matches against ``organization_id``.
_FIELD_ALIASES: Mapping[str, str] = {"org_id": "organization_id"}


def _canonical_field(field_name: str) -> str:
    """Resolve a condition field name to its canonical envelope label.

    Maps documented aliases (e.g. ``org_id`` → ``organization_id``) to the field
    actually present in ``_centralops``. Unknown names pass through unchanged so
    :func:`validate_condition` can reject them.
    """
    return _FIELD_ALIASES.get(field_name, field_name)


#: Comparison operators. ``in``/``nin`` take a list; ``exists`` takes a bool.
ALLOWED_OPS = frozenset({"eq", "ne", "gt", "gte", "lt", "lte", "in", "nin", "exists"})

ACTION_ROUTE = "route"
ACTION_DROP = "drop"

_WAZUH_DEFAULT_ID = "wazuh-default"


# ── Compiled route ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class CompiledRoute:
    """A validated, ready-to-evaluate route (DB row → this)."""

    id: str
    name: str
    priority: int
    condition: Mapping[str, Any]
    action: str
    destination_ids: tuple[str, ...]
    is_final: bool
    enabled: bool = True
    #: Canary rollout (0-100). 100 = full (no canary). <100 = this route applies
    #: to only that % of MATCHING events (deterministic by event_id); the rest
    #: fall through to the next route. Enables gradual SIEM-to-SIEM cutover.
    canary_percent: int = 100
    #: fail-safe de detecção. True (default) =
    #: esta rota alimenta detecção e NUNCA é amostrada/agregada pelas alavancas de
    #: redução (mesmo com sample/aggregate ligados). Opt-out explícito por-rota
    #: (False) onde reduzir volume é seguro. Default-protege.
    protect_detection: bool = True
    #: sampling estatístico de REDUÇÃO (0-100). 100 = sem amostragem.
    #: <100 = só essa fração (consistent-hash por event_id, determinístico) dos eventos
    #: que casam É ENTREGUE aos destinos DESTA rota; o resto é reduzido (economia de
    #: volume). Distinto de ``canary_percent`` (cutover): sampling NUNCA se aplica a
    #: rotas ``protect_detection=True``. Só tem efeito com ``SamplingConfig.enabled``.
    sample_percent: int = 100
    #: suppression durável por assinatura. ``suppress_key`` = CSV de
    #: labels p/ a assinatura (None/"" = sem supressão); ``suppress_allow`` = N por janela
    #: (0 = desligado); ``suppress_window_s`` = janela (s). Reduz ruído repetitivo sem
    #: perder a 1ª ocorrência. Só tem efeito com ``REDUCTION_SUPPRESS_ENABLED`` on.
    suppress_key: Optional[str] = None
    suppress_allow: int = 0
    suppress_window_s: int = 30
    #: compiled PII redaction rules applied to the events
    #: THIS route sends to ITS destinations, BEFORE dispatch. Empty = no redaction
    #: (the event reaches this route's destinations untouched). The same source
    #: event reaches a non-redacting route (e.g. the lake) full-fidelity.
    redaction: Tuple["CompiledRedactionRule", ...] = ()


def _canary_bucket(event_id: str) -> int:
    """Deterministic 0-99 bucket from event_id (stable across retries → an event
    always takes the same canary path; idempotent).

    Degenerate case: an empty event_id hashes to a FIXED
    bucket, so un-ID'd events aren't sampled uniformly — they all land together.
    Não acontece em produção: ``build_envelope`` sempre popula event_id
    (compute_event_id, não-vazio). Só ocorreria com envelope malformado/test."""
    digest = hashlib.sha1((event_id or "").encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100


def _canary_pass(canary_percent: int, event_id: str) -> bool:
    """True if the event is inside the canary fraction."""
    if canary_percent >= 100:
        return True
    if canary_percent <= 0:
        return False
    return _canary_bucket(event_id) < canary_percent


@dataclass(frozen=True)
class SamplingConfig:
    """Parâmetros do sampling de redução, resolvidos pelo pipeline a
    partir das flags (mantém ``route_batch`` puro/testável, sem ler ``settings``).

    ``enabled`` já embute ``REDUCTION_SAMPLE_ENABLED and COST_METERING_ENABLED`` (não se
    reduz sem medir). ``protect_detection_enforced`` = ``REDUCTION_SAMPLE_PROTECT_DETECTION``
    (kill-switch global do fail-safe; default True). ``None`` em route_batch = sem sampling.
    """

    enabled: bool = False
    protect_detection_enforced: bool = True


def _should_sample_out(route: "CompiledRoute", event_id: str, sampling: Optional["SamplingConfig"]) -> bool:
    """True ⇒ o evento é AMOSTRADO PARA FORA dos destinos DESTA rota (redução).

    Fail-safe de detecção: uma rota ``protect_detection=True`` (default) NUNCA é
    amostrada enquanto ``protect_detection_enforced`` estiver ligado. ``sample_percent``
    ≥100 é no-op. Determinístico por ``event_id`` (consistent-hash) — o mesmo evento
    sempre toma o mesmo caminho (idempotente sob retry)."""
    if sampling is None or not sampling.enabled or route.sample_percent >= 100:
        return False
    if route.protect_detection and sampling.protect_detection_enforced:
        return False
    return not _canary_pass(route.sample_percent, event_id)


def _with_sample_rate(env: Mapping[str, Any], sample_percent: int) -> dict:
    """Cópia rasa do envelope com ``_centralops.sample_rate`` decorado (fração 0-1) p/
    reescalar contagens downstream. NÃO muta o ``env`` compartilhado (fan-out full-
    fidelity): só a cópia entregue à rota amostrada carrega o rótulo."""
    out = dict(env)
    labels = dict(out.get("_centralops") or {})
    labels["sample_rate"] = round(sample_percent / 100.0, 6)
    out["_centralops"] = labels
    return out


# ── Condition evaluation ───────────────────────────────────────────────


def event_labels(envelope: Mapping[str, Any]) -> dict:
    """Extract the routing label view (the ``_centralops`` namespace)."""
    return dict(envelope.get("_centralops") or {})


def _envelope_bytes(envelope: Any) -> int:
    """Tamanho lógico (pré-compressão) do envelope ORIGINAL via o mesmo serializador da
    entrega (``dumps_bytes``) — estima o volume evitado por amostragem.

    APROXIMAÇÃO (documentada honestamente): mede o envelope-fonte, NÃO a cópia
    contrafactual entregue. Exato para rotas amostradas SEM redação; para uma rota que
    amostra E redige, super-estima levemente (a entrega seria a cópia redigida, menor);
    ignora a decoração ``sample_rate`` (~20B, sub-estima de forma segura). No caso
    dominante (amostragem sem redação) fica na mesma unidade de ``bytes_out``.

    BEST-EFFORT: retorna 0 se a serialização falhar (ref. circular, aninhamento
    profundo, ``__str__`` que levanta) — o metering NUNCA pode derrubar o roteamento/
    coleta (invariante do módulo ``reduction/metering``). Import lazy p/ não acoplar o
    engine ao serializador no import-time."""
    try:
        from ..output._fastjson import dumps_bytes

        return len(dumps_bytes(envelope))
    except Exception:  # noqa: BLE001 — best-effort: a métrica nunca derruba o roteamento
        import logging

        logging.getLogger(__name__).debug("metering: _envelope_bytes falhou", exc_info=True)
        return 0


def compare_values(op: str, actual: Any, expected: Any) -> bool:
    """Evaluate one operator. Missing field (actual is None) matches only ``ne``
    / ``nin`` (and ``exists:false``); all positive comparisons are False.

    VOCABULÁRIO ÚNICO DE OPERADORES do caminho de ingestão (ADR-0015, Fase 1).
    Consumido por dois avaliadores: as condições de ROTA (aqui) e as regras de
    CLASSIFICAÇÃO EM VOO (``collectors/inflight/matcher.py``). Uma segunda
    implementação divergiria em silêncio nos casos de borda que mais importam —
    campo ausente, tipos incomparáveis — e um operador que se comporta diferente
    conforme onde é usado é indefensável num produto de segurança.

    Contratos que os chamadores DEVEM conhecer:

    * **Igualdade nativa, sem coerção.** ``1`` não casa ``"1"``; comparação de
      string é case-sensitive. Tipos incomparáveis (``str`` vs ``int`` em
      ``gt``) caem no ``except TypeError`` e devolvem False — nunca levantam.
    * **Coerção numérica é responsabilidade de QUEM COMPILA a cláusula**, não
      desta função. Um vendor que serializa severidade como ``"5"`` faria
      ``'5' > 3`` levantar TypeError e a regra nunca casaria, com contador zerado
      indistinguível de "o valor não bateu". O compilador de regras em voo
      resolve isso convertendo o lado ``actual`` antes de chamar
      (``inflight/runtime.py``); rotas mantêm a semântica atual, que já tem
      cobertura própria.
    * **``nin``/``ne`` casam por VACUIDADE em campo ausente** (``:224``). Numa
      allowlist isso é fail-OPEN: o evento sem o campo passa pelo filtro que
      deveria excluí-lo. O compilador em voo fecha isso auto-injetando uma
      cláusula ``exists`` para todo path usado em ``nin``/``ne``.
    """
    if op == "exists":
        return (actual is not None) == bool(expected)
    if actual is None:
        return op in ("ne", "nin")
    try:
        if op == "eq":
            return actual == expected
        if op == "ne":
            return actual != expected
        if op == "gt":
            return actual > expected
        if op == "gte":
            return actual >= expected
        if op == "lt":
            return actual < expected
        if op == "lte":
            return actual <= expected
        if op == "in":
            return actual in expected  # type: ignore[operator]
        if op == "nin":
            return actual not in expected  # type: ignore[operator]
    except TypeError:
        # incomparable types (e.g. str vs int) → no match, never crash
        return False
    return False


# Alias interno preservado: os chamadores existentes deste módulo continuam
# usando ``_cmp``. Renomear todos seria ruído num diff que não muda semântica.
_cmp = compare_values


def _match_clause(spec: Any, actual: Any) -> bool:
    """A clause is a scalar (eq shorthand) or an ``{op: value}`` map (ANDed)."""
    if isinstance(spec, Mapping):
        return all(_cmp(op, actual, val) for op, val in spec.items())
    return _cmp("eq", actual, spec)


def matches(condition: Mapping[str, Any], labels: Mapping[str, Any]) -> bool:
    """True if ``labels`` satisfy ALL clauses of ``condition`` (AND). An empty
    condition matches everything (catch-all).

    Field names are resolved through :func:`_canonical_field` so write-time
    aliases (``org_id`` → ``organization_id``) match the canonical envelope label.
    """
    for field_name, spec in condition.items():
        if not _match_clause(spec, labels.get(_canonical_field(field_name))):
            return False
    return True


# ── Per-event evaluation ───────────────────────────────────────────────


@dataclass(frozen=True)
class EventRouting:
    """Routing outcome for ONE event."""

    destinations: frozenset
    dropped: bool
    matched: bool  # matched at least one route (route OR drop)
    matched_route_ids: frozenset = frozenset()  # routes that matched + applied
    #: ordered (destination_id, route) pairs this event was
    #: assigned to, preserving the route association that ``destinations`` (a
    #: flat frozenset) collapses. route_batch uses it to apply each ROUTE's PII
    #: redaction to the copy bound for THAT route's destination(s).
    assignments: Tuple[Tuple[str, "CompiledRoute"], ...] = ()
    #: id da rota ``action=drop`` que descartou o evento (``None`` se não houve drop).
    #: ``matched_route_ids`` é um frozenset (perde a ordem), então guardamos a rota
    #: RESPONSÁVEL para o tap de captura mostrar o MOTIVO do descarte.
    drop_route_id: Optional[str] = None


def order_routes(routes: Sequence[CompiledRoute]) -> List[CompiledRoute]:
    """Deterministic order: priority ASC, then id ASC."""
    return sorted(routes, key=lambda r: (r.priority, r.id))


def evaluate_event(
    labels: Mapping[str, Any], ordered_routes: Sequence[CompiledRoute]
) -> EventRouting:
    """Evaluate one event's labels against pre-ordered routes (use
    :func:`order_routes`). Disabled routes are skipped."""
    dests: set = set()
    matched_ids: list = []
    assignments: list = []  # ordered (dest_id, route) — preserves route assoc
    event_id = str(labels.get("event_id") or "")
    for r in ordered_routes:
        if not r.enabled:
            continue
        if not matches(r.condition, labels):
            continue
        # Canary gate: a route with canary_percent<100 applies to only that
        # deterministic fraction of matching events; the rest FALL THROUGH to the
        # next route (continue), enabling a gradual cutover.
        if not _canary_pass(r.canary_percent, event_id):
            continue
        matched_ids.append(r.id)
        if r.action == ACTION_DROP:
            return EventRouting(
                frozenset(),
                dropped=True,
                matched=True,
                matched_route_ids=frozenset(matched_ids),
                drop_route_id=r.id,
            )
        dests.update(r.destination_ids)
        for d in r.destination_ids:
            assignments.append((d, r))
        if r.is_final:
            break
    return EventRouting(
        frozenset(dests),
        dropped=False,
        matched=bool(matched_ids),
        matched_route_ids=frozenset(matched_ids),
        assignments=tuple(assignments),
    )


# ── Batch routing → per-destination sub-batches ────────────────────────


@dataclass
class BatchRouting:
    """Result of routing a whole batch.

    ``sub_batches`` maps destination_id → the events that go to it (the producer
    enqueues one task per destination). The configured ``fallback_destination_id``
    (if any) may appear as a key (explicit route target OR zero-loss fallback)."""

    sub_batches: dict
    routed: int = 0
    dropped: int = 0
    fallback: int = 0
    #: Vendor-neutral zero-loss: eventos sem rota casada E sem fallback
    #: configurado. NÃO inventamos um sink (ex.: wazuh-default hardcoded) — o caller
    #: persiste ``unrouted_events`` na DLQ/quarentena (zero perda, visível, replayável).
    unrouted: int = 0
    #: Eventos correspondentes ao contador ``unrouted`` (para o caller mandar à DLQ).
    unrouted_events: list = field(default_factory=list)
    #: {route_id: number of events that matched + applied this route} — per-route
    #: observability. Fan-out events count toward every route they hit.
    per_route: dict = field(default_factory=dict)
    #: count of (event, destination) pairs excluded by residency enforcement.
    residency_blocked: int = 0
    #: eventos cuja FONTE é uma integração wazuh que seriam
    #: entregues ao catch-all/sink ``wazuh-default`` (syslog → o próprio Wazuh) e
    #: foram SUPRIMIDOS para quebrar o loop fonte↔destino. Wazuh é fonte (pull do
    #: Indexer), não destino.
    loop_blocked: int = 0
    #: pares (evento, destino) amostrados PARA FORA (redução de
    #: volume). Total + por-rota, p/ o pipeline emitir events_dropped{reason=sample}.
    sampled: int = 0
    sampled_per_route: dict = field(default_factory=dict)
    #: bytes LÓGICOS do envelope ORIGINAL (aproximação — ver ``_envelope_bytes``)
    #: evitados por amostragem, por-entrega, agregados por ``organization_id``. O
    #: pipeline converte em ``bytes_saved{reason=sample}`` (Evitado/Redução na
    #: /cost-summary). Só popula com o sampling ativo (o que, por contrato de
    #: ``SamplingConfig.enabled``, implica ``COST_METERING_ENABLED`` também on).
    sampled_bytes_per_org: dict = field(default_factory=dict)
    #: bytes LÓGICOS evitados por rotas ``action=drop``, agregados por
    #: ``organization_id`` — mesma base/serializador de ``sampled_bytes_per_org``. O
    #: pipeline converte em ``bytes_saved{reason=drop}``. Só popula com
    #: ``measure_drop_bytes=True`` (o caller liga com ``COST_METERING_ENABLED``): drop
    #: não tem flag REDUCTION_* própria — é config de rota, sempre ativa.
    dropped_bytes_per_org: dict = field(default_factory=dict)

    # ── Eventos por DESFECHO (tap de captura) ──────────────────────────
    # O engine é PURO (sem I/O): ele ACUMULA os eventos de cada desfecho, o CALLER
    # (``_enqueue_routed``) escreve na captura. Mesmo padrão de ``unrouted_events``.
    # São REFERÊNCIAS aos envelopes do lote (sem cópia) — custo = 1 ponteiro/evento.
    #: (envelope, route_id) descartados por ``action=drop``.
    dropped_events: list = field(default_factory=list)
    #: (envelope, reason) suprimidos pelo anti-loop de fonte Wazuh.
    loop_blocked_events: list = field(default_factory=list)
    #: (envelope, destination_id) excluídos por conflito de residência de dados. O
    #: evento pode AINDA assim ser entregue aos demais destinos — é por-par.
    residency_blocked_events: list = field(default_factory=list)
    #: (envelope, destination_id, route_id) amostrados PARA FORA (redução).
    sampled_events: list = field(default_factory=list)


def _residency_conflict(
    dest_residency: Optional[str],
    event_geography: Optional[str],
) -> bool:
    """True when the destination's residency requirement conflicts with the event's geography.

    Enforcement is CONSERVATIVE (gated): only active when BOTH values are
    known and non-null.  A destination with ``data_residency=None`` accepts
    events from any geography.  An event whose geography is unknown (None)
    always passes (we never block on missing metadata).

    Conflict rule: ``dest_residency`` must match ``event_geography`` OR one of
    them must be ``"global"`` (a global residency zone accepts all geographies;
    a global event origin is accepted by all residency zones).
    """
    if dest_residency is None or not dest_residency:
        return False
    if event_geography is None or not event_geography:
        return False
    if dest_residency.lower() == "global" or event_geography.lower() == "global":
        return False
    return dest_residency.upper() != event_geography.upper()


def _log_loop_blocked(labels: Mapping[str, Any], *, reason: str) -> None:
    """Trilha forense do drop anti-loop: loga o event id como o
    residency_block faz — antes era counter mudo (drop sem rastro)."""
    import logging as _log

    _log.getLogger(__name__).info(
        "routing: loop_blocked evento=%r org=%s reason=%s",
        labels.get("event_id", "?"),
        labels.get("organization_id", "?"),
        reason,
    )


def route_batch(
    batch: Sequence[Mapping[str, Any]],
    routes: Sequence[CompiledRoute],
    *,
    fallback_destination_id: Optional[str] = None,
    destination_residency: Optional[Mapping[str, Optional[str]]] = None,
    wazuh_loop_destination_ids: Optional[frozenset] = None,
    sampling: Optional[SamplingConfig] = None,
    measure_drop_bytes: bool = False,
) -> BatchRouting:
    """Split ``batch`` into per-destination sub-batches by evaluating each event.

    Zero silent loss, VENDOR-NEUTRAL: an event matching no route (or a
    route resolving to no destination) goes to ``fallback_destination_id`` WHEN the
    operator configured one (a Destination flagged default, or a ``condition={}``
    catch-all route — the latter arrives via normal assignments). When NO fallback
    is configured, the event is NOT forced into a hardcoded vendor sink — it is
    collected into ``BatchRouting.unrouted_events`` for the caller to persist to the
    DLQ/quarantine (zero loss, surfaced, replayable). Dropped events go nowhere
    (counted). Routes are ordered internally.

    ``destination_residency`` — optional mapping of ``destination_id →
    data_residency``.  When provided, destinations whose declared
    residency zone CONFLICTS with the event's geography (from
    ``_centralops.data_geography``) are EXCLUDED from the effective fan-out
    for that event.  If exclusion empties the destination set, the event takes the
    fallback/unrouted path above (zero-loss guarantee preserved).  Gate: only
    enforced when the destination has a non-null residency AND the event
    carries a known geography.  Blocked destinations are counted in
    ``BatchRouting.residency_blocked``.

    ``measure_drop_bytes`` — quando True, mede o volume lógico dos eventos descartados
    por ``action=drop`` (mesmo serializador da entrega) em
    ``BatchRouting.dropped_bytes_per_org``. O caller liga junto com
    ``COST_METERING_ENABLED``; off ⇒ zero serialização extra no ramo de drop.

    Além dos CONTADORES, o resultado carrega os EVENTOS de cada desfecho
    (``dropped_events``, ``unrouted_events``, ``loop_blocked_events``,
    ``residency_blocked_events``, ``sampled_events``) para o caller alimentar o tap de
    captura ("como entrou e como saiu"). O engine permanece PURO — nenhuma escrita aqui.
    """
    ordered = order_routes(routes)
    sub: dict = defaultdict(list)
    per_route: dict = defaultdict(int)
    result = BatchRouting(sub_batches=sub)

    # anti-loop fonte↔destino do Wazuh é POR-HOST (vendor-neutro).
    # O caller computa o conjunto de destinos que entregam de volta ao manager Wazuh
    # (qualquer syslog dest cujo host casa o do manager). NÃO há mais um sentinela
    # hardcoded no conjunto — se o fallback configurado for um desses destinos-loop,
    # ele é suprimido p/ fontes wazuh exatamente como qualquer outro loop dest.
    loop_ids = frozenset(wazuh_loop_destination_ids or frozenset())

    def _no_destination(env: Mapping[str, Any], labels: Mapping[str, Any], reason: str) -> None:
        """Resolve o caminho 'sem destino' (sem match / residency / loop-vazio):
        fallback configurado → entrega; senão → bucket unrouted (DLQ). Respeita o
        loop-guard de fonte wazuh (não entrega de volta a um destino-loop)."""
        src_wazuh = labels.get("platform") == "wazuh"
        fb = fallback_destination_id
        if src_wazuh and (fb is None or fb in loop_ids):
            # Fonte wazuh sem fallback não-loop: o evento já está no Wazuh — suprime
            # (não é perda; entregar de volta = loop). Conta + loga (trilha forense).
            result.loop_blocked += 1
            result.loop_blocked_events.append((env, reason))
            _log_loop_blocked(labels, reason=reason)
            return
        if fb is not None:
            sub[fb].append(env)
            result.fallback += 1
            return
        # Vendor-neutro: nenhum fallback configurado → DLQ/quarentena (zero perda).
        result.unrouted += 1
        result.unrouted_events.append(env)

    for env in batch:
        labels = event_labels(env)
        decision = evaluate_event(labels, ordered)
        for rid in decision.matched_route_ids:
            per_route[rid] += 1
        if decision.dropped:
            result.dropped += 1
            # desfecho por-evento p/ o tap de captura (o operador vê QUAL rota matou).
            result.dropped_events.append((env, decision.drop_route_id or ""))
            # volume evitado por drop → bytes_saved{reason=drop} no pipeline. Mesma
            # base do sampling (envelope, serializador da entrega); só neste ramo e só
            # com metering on. Best-effort: _envelope_bytes devolve 0 em falha.
            if measure_drop_bytes:
                _d_org = labels.get("organization_id")
                if _d_org is not None:
                    _d_bytes = _envelope_bytes(env)
                    if _d_bytes:
                        result.dropped_bytes_per_org[_d_org] = (
                            result.dropped_bytes_per_org.get(_d_org, 0.0) + _d_bytes
                        )
            continue
        # Wazuh é FONTE (pull do Indexer); NÃO é tipo de destino — o
        # destino é syslog. Um evento cuja fonte é integração wazuh entregue a um
        # destino-loop (syslog → o próprio manager) seria reindexado → recoletado →
        # loop. O ``_no_destination`` + o pop de ``loop_ids`` abaixo suprimem isso
        # de forma vendor-neutra (por-host, não por sentinela hardcoded).
        source_is_wazuh = labels.get("platform") == "wazuh"
        if not decision.assignments:
            # Sem rota casada / rota sem destino → fallback configurado ou unrouted→DLQ.
            _no_destination(env, labels, reason="no-explicit-destination")
            continue
        # per-(destination, route) assignment so each ROUTE's PII
        # redaction applies ONLY to the copy bound for ITS destination. A
        # redacting route deep-copies+masks (apply_pii_redaction); a non-redacting
        # route appends the ORIGINAL reference — so the SAME source event reaches
        # the lake full and the SIEM masked, and wazuh-default stays byte-identical.
        #
        # DEDUP por destino: ``assignments`` é uma LISTA
        # ordenada — um destino alvo de DUAS rotas (ou duplicado em
        # destination_ids) apareceria 2×. O ``destinations`` antigo era um set
        # (dedup natural). Sem dedup, o destino recebia o evento 2× (infla EPS no
        # wazuh) e, pior, uma cópia CLEARTEXT + uma MASCARADA (a cleartext vence o
        # 409 idempotente → vaza PII). Regra fail-safe: 1 cópia por destino, e a
        # rota que REDIGE SEMPRE vence uma irmã cleartext no mesmo destino.
        chosen: dict = {}  # dest_id -> CompiledRoute (a rota "vencedora" do destino)
        for dest, route in decision.assignments:
            cur = chosen.get(dest)
            if cur is None or (not cur.redaction and route.redaction):
                chosen[dest] = route

        # fonte wazuh nunca entrega a um destino que volte ao
        # manager — nem o sentinela ``wazuh-default`` nem um syslog dest genérico
        # que aponte para o MESMO manager (``loop_ids``, computado pelo caller).
        # Se isto esvaziar o conjunto, o drop é contabilizado em ``loop_blocked``.
        if source_is_wazuh:
            for _lid in loop_ids:
                chosen.pop(_lid, None)

        # residency enforcement (conservative, gated).
        # ``data_geography`` lives in _centralops (already extracted in labels).
        event_geo: Optional[str] = labels.get("data_geography") or None
        if destination_residency and chosen:
            blocked: list[str] = []
            for dest in list(chosen):
                dest_res = destination_residency.get(dest)
                if _residency_conflict(dest_res, event_geo):
                    blocked.append(dest)
                    del chosen[dest]
            if blocked:
                result.residency_blocked = result.residency_blocked + len(blocked)
                for _bd in blocked:
                    result.residency_blocked_events.append((env, _bd))
                import logging as _log
                _log.getLogger(__name__).info(
                    "routing: residency_block evento=%r dests=%s geo=%s",
                    labels.get("event_id", "?"),
                    blocked,
                    event_geo,
                )

        # If all destinations were blocked (residency/loop), take the fallback/
        # unrouted path (zero-loss, vendor-neutro).
        if not chosen:
            _no_destination(env, labels, reason="all-destinations-loop")
            continue

        event_id = str(labels.get("event_id") or "")
        # bytes do envelope medidos NO MÁXIMO 1× por evento (env não muta no laço
        # interno — _with_sample_rate/apply_pii_redaction copiam) e reusados por destino
        # amostrado, evitando re-serializar o mesmo objeto N vezes.
        _sampled_nbytes: Optional[int] = None
        delivered_any = False
        for dest, route in chosen.items():
            # sampling de redução por-rota: só uma fração
            # (consistent-hash por event_id) chega aos destinos DESTA rota; o resto
            # é reduzido. Rotas protect_detection nunca são amostradas (fail-safe).
            if _should_sample_out(route, event_id, sampling):
                result.sampled += 1
                result.sampled_per_route[route.id] = result.sampled_per_route.get(route.id, 0) + 1
                result.sampled_events.append((env, dest, route.id))
                # volume LÓGICO evitado por ESTE par evento×destino (consistente com
                # bytes_out, também por-entrega) → bytes_saved{reason=sample} no pipeline.
                # Serialização só AQUI, no ramo de amostragem (sampling on ⇒ metering on),
                # medida 1× por evento e reusada por destino; best-effort (0 em falha).
                _s_org = labels.get("organization_id")
                if _s_org is not None:
                    if _sampled_nbytes is None:
                        _sampled_nbytes = _envelope_bytes(env)
                    if _sampled_nbytes:
                        result.sampled_bytes_per_org[_s_org] = (
                            result.sampled_bytes_per_org.get(_s_org, 0.0) + _sampled_nbytes
                        )
                continue
            # Evento MANTIDO por uma rota amostrada → decora a cópia com sample_rate
            # (reescala contagens downstream). Rota sem sampling → env full-fidelity.
            env_out = (
                _with_sample_rate(env, route.sample_percent)
                if (sampling and sampling.enabled and route.sample_percent < 100)
                else env
            )
            if route.redaction:
                redacted = apply_pii_redaction(env_out, route.redaction)
                sub[dest].append(redacted if redacted is not None else env_out)
            else:
                sub[dest].append(env_out)
            delivered_any = True
        # Conta como routed só se ALGO foi entregue; um evento 100% amostrado p/ fora
        # (todos os destinos) foi reduzido, não roteado (não infla o contador routed).
        if delivered_any:
            result.routed += 1

    result.sub_batches = dict(sub)
    result.per_route = dict(per_route)
    return result


# ── Validation + UX guards ─────────────────────────────────────────────


def validate_condition(condition: Any) -> None:
    """Raise ``ValueError`` if ``condition`` is not a valid label-driven matcher.

    Used by the routes API to 422 a bad condition at create/update time.
    """
    if not isinstance(condition, Mapping):
        raise ValueError("condition must be a JSON object")
    for field_name, spec in condition.items():
        # Aliases (``org_id``) are accepted and resolve to a canonical field.
        if _canonical_field(field_name) not in ALLOWED_FIELDS:
            raise ValueError(
                f"unknown routing field {field_name!r}; allowed: {sorted(ALLOWED_FIELDS)}"
            )
        if isinstance(spec, Mapping):
            if not spec:
                raise ValueError(f"empty operator map for field {field_name!r}")
            for op, val in spec.items():
                if op not in ALLOWED_OPS:
                    raise ValueError(
                        f"unknown operator {op!r} for {field_name!r}; allowed: {sorted(ALLOWED_OPS)}"
                    )
                if op in ("in", "nin") and not isinstance(val, (list, tuple)):
                    raise ValueError(f"operator {op!r} requires a list value")
                if op == "exists" and not isinstance(val, bool):
                    raise ValueError("operator 'exists' requires a boolean value")
        # scalar spec → eq shorthand, always valid


def _clause_value_set(spec: Any) -> Optional[frozenset]:
    """Return the FINITE set of values a clause admits, or ``None`` if the clause
    is not reducible to a finite enumerable set (e.g. an inequality like ``gte``).

    Only ``eq`` (scalar shorthand or ``{"eq": v}``) and ``in`` are enumerable:
      * scalar ``v``           → {v}
      * ``{"eq": v}``          → {v}
      * ``{"in": [a, b, ...]}`` → {a, b, ...}
    A single-key map of exactly one of those ops qualifies. Anything else (ranges,
    ``ne``/``nin``/``exists``, or a multi-op AND map) returns ``None`` → we cannot
    cheaply enumerate it, so subsumption stays conservative (no false positive).
    """
    if isinstance(spec, Mapping):
        if len(spec) != 1:
            return None
        (op, val), = spec.items()
        if op == "eq":
            return frozenset({_hashable(val)})
        if op == "in" and isinstance(val, (list, tuple)):
            try:
                return frozenset(_hashable(v) for v in val)
            except TypeError:
                return None
        return None
    # scalar shorthand → eq
    try:
        return frozenset({_hashable(spec)})
    except TypeError:
        return None


def _hashable(v: Any) -> Any:
    """Make a value hashable for set membership (lists → tuples). Raises TypeError
    for anything still unhashable, which the caller turns into 'not enumerable'."""
    if isinstance(v, list):
        return tuple(v)
    hash(v)  # probe; raises TypeError if unhashable
    return v


def _subsumes(cond_a: Mapping[str, Any], cond_b: Mapping[str, Any]) -> bool:
    """True if condition A SUBSUMES condition B: every event matching B also
    matches A (A is a superset of B's matched population).

    Decidable over the closed eq/in vocabulary:
      * A empty (catch-all) → subsumes everything.
      * For each field A constrains: B must constrain the SAME (canonical) field
        with a value set that is a SUBSET of A's value set. If B does not constrain
        that field, or either side is non-enumerable (ranges/ne/exists), we cannot
        prove containment → return False (conservative: never a false positive).

    Disjoint predicates ({vendor: sophos} vs {vendor: defender}) are NOT subsumed
    (B's set ⊄ A's set). A weaker A over a stronger B IS subsumed
    ({vendor in [a,b]} subsumes {vendor: a}).
    """
    # Normalize aliases on both sides so org_id and organization_id compare equal.
    a_norm: dict[str, Any] = {_canonical_field(f): s for f, s in cond_a.items()}
    b_norm: dict[str, Any] = {_canonical_field(f): s for f, s in cond_b.items()}
    if not a_norm:
        return True  # catch-all subsumes anything
    for field_name, a_spec in a_norm.items():
        a_set = _clause_value_set(a_spec)
        if a_set is None:
            return False  # A's clause not enumerable → cannot prove subsumption
        if field_name not in b_norm:
            return False  # B unconstrained on a field A restricts → B leaks through
        b_set = _clause_value_set(b_norm[field_name])
        if b_set is None or not b_set:
            return False  # B's clause not enumerable / empty → cannot prove
        if not b_set <= a_set:
            return False  # B admits a value A rejects → not subsumed
    return True


def find_unreachable(ordered_routes: Sequence[CompiledRoute]) -> list[str]:
    """Ids of routes shadowed by an EARLIER enabled, full (non-canary) ``is_final``
    route whose condition SUBSUMES theirs.

    SOUND (no false positives) over the closed eq/in vocabulary:
      * An earlier catch-all (``condition={}``) shadows everything after it.
      * An earlier route with an IDENTICAL or BROADER condition shadows a later
        narrower one (subsumption): ``{vendor: {in: [a, b]}}`` shadows
        ``{vendor: a}``; an exact duplicate shadows its twin.
      * DISJOINT predicates do not shadow each other.
      * A CANARY route (``canary_percent < 100``) never shadows — its non-canary
        fraction falls through to later routes.
      * Non-``is_final`` routes clone+continue, so they never shadow either.

    Ranges/``ne``/``exists`` and multi-op clauses are treated conservatively: when
    containment can't be proven, the later route is NOT flagged (reachable).

    WARNING-level only (non-blocking) — callers surface these as UX hints.
    """
    unreachable: list[str] = []
    seen_final: list[CompiledRoute] = []
    for r in ordered_routes:
        if not r.enabled:
            continue
        shadowed = any(_subsumes(e.condition, r.condition) for e in seen_final)
        if shadowed:
            unreachable.append(r.id)
            continue
        # Only a FULL (non-canary) is_final route shadows what follows. A canary
        # route (percent<100) lets the non-canary fraction fall through, so it
        # never fully shadows a later route.
        if r.is_final and r.canary_percent >= 100:
            seen_final.append(r)
    return unreachable
