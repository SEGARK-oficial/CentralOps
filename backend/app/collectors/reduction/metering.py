"""Cost/volume metering — pure measurement, no reduction lever.

Measures the LOGICAL (pre-compression) volume of events flowing through the pipeline:
how much ENTERS (per source/integration and per org) vs how much is DELIVERED (per
destination and per org). That ratio is the foundation of the "cut my SIEM bill"
value proposition — but this module only MEASURES it; no event is dropped here.

Design contract (mirrors ``observability_store`` / ``sample_reservoir``):
  * **Flag-gated, fail-fast**: every entry point returns IMMEDIATELY when
    ``settings.COST_METERING_ENABLED`` is False — so flag-off adds at most one
    attribute read and a function call (no serialization), keeping the hot path
    byte-identical.
  * **Best-effort / fire-and-forget**: a metering failure (Redis down, OTel hiccup)
    is swallowed at debug level and NEVER affects collection or delivery.
  * **No re-serialization on the OUT path**: :func:`record_out` takes the byte/event
    totals the dispatch path ALREADY computed (``_record_dest_observability`` sums
    ``len(dumps_bytes(e))`` for the wire-proxy bytes) — we only add the per-org rollup.

Open-core: this whole module is Community. Turning bytes into MONEY (US$/GB, savings,
per-org cost policy) is Enterprise, reached via the ``ee_hooks.cost_pricer`` seam read
at the ``/collectors/cost-summary`` endpoint — never here.

Storage surfaces (both already exist; we only add series):
  * OTel catalog — ``collector_events_in_total`` / ``collector_bytes_in_total``
    (labels org_id, integration_id; the OUT side reuses ``collector_*_sent_total``).
  * observability_store (Redis, TTL 3h) — kinds ``source``/``org`` join the existing
    ``dest``/``route``; metrics ``events_in``/``bytes_in``/``events_out``/``bytes_out``.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, Optional, Tuple

from ...core.config import settings

logger = logging.getLogger(__name__)

# observability_store metric names (free-form strings; no schema change).
_M_EVENTS_IN = "events_in"
_M_BYTES_IN = "bytes_in"
_M_EVENTS_OUT = "events_out"
_M_BYTES_OUT = "bytes_out"
_KIND_SOURCE = "source"
_KIND_ORG = "org"
_KIND_DEST = "dest"
_M_BYTES_SAVED = "bytes_saved"


def enabled() -> bool:
    """True when cost/volume metering is active (single source of truth for the flag)."""
    return bool(getattr(settings, "COST_METERING_ENABLED", False))


def record_saving(
    organization_id: Optional[int],
    destination_id: Optional[str],
    reason: str,
    *,
    bytes_: float,
) -> None:
    """Contabiliza o VOLUME LÓGICO evitado por uma alavanca de
    redução, atribuído à sua causa (``reason`` ∈ {trim, sample, suppress, drop,
    aggregate, redaction}) para auditabilidade.

    Open-core: só bytes (Community). A tradução em US$/ROI é Enterprise (o endpoint
    ``/collectors/cost-summary`` lê ``bytes_saved`` daqui e chama o seam
    ``ee_hooks.cost_pricer``). No-op flag-off; fail-closed em org ausente (nunca escreve
    num bucket compartilhado/nulo — anti cross-tenant). ``destination_id`` é ``None`` no
    trim pré-fan-out (normalize): entra só no rollup por-org + label "-" no OTel."""
    if not enabled() or organization_id is None or not bytes_:
        return
    try:
        from .. import metrics

        dest_label = str(destination_id) if destination_id is not None else "-"
        metrics.BYTES_SAVED.labels(destination_id=dest_label, reason=reason).inc(float(bytes_))

        from .. import observability_store as obs

        if destination_id is not None:
            obs.record_counter(_KIND_DEST, str(destination_id), _M_BYTES_SAVED, float(bytes_))
        obs.record_counter(_KIND_ORG, str(organization_id), _M_BYTES_SAVED, float(bytes_))
    except Exception:  # noqa: BLE001 — metering é best-effort; nunca quebra a coleta/entrega
        logger.debug(
            "metering.record_saving falhou (org=%s reason=%s)", organization_id, reason, exc_info=True
        )


def record_trim_saving(organization_id: Optional[int], raw: Any, reduced: Any) -> None:
    """Mede o delta ``bytes(raw) - bytes(reduced)`` do trimming
    (raw_reduction) e o contabiliza como ``bytes_saved{reason=trim}``.

    Gated por ``REDUCTION_TRIM_ENABLED`` **E** ``COST_METERING_ENABLED``: a serialização
    extra (2 dumps) só ocorre quando as DUAS flags estão on — flag-off ⇒ zero overhead
    (early-return antes de qualquer serialização). ``reduced is None`` (o engine não
    trimou nada) ⇒ no-op."""
    if not enabled() or not bool(getattr(settings, "REDUCTION_TRIM_ENABLED", False)):
        return
    if reduced is None or organization_id is None:
        return
    try:
        saved = _event_bytes(raw) - _event_bytes(reduced)
        if saved > 0:
            record_saving(organization_id, None, "trim", bytes_=float(saved))
    except Exception:  # noqa: BLE001 — best-effort
        logger.debug("metering.record_trim_saving falhou (org=%s)", organization_id, exc_info=True)


def record_sample_saving(organization_id: Optional[int], bytes_: float) -> None:
    """Contabiliza o volume lógico evitado por AMOSTRAGEM (redução) como
    ``bytes_saved{reason=sample}``. Os bytes são MEDIDOS no engine de roteamento (o
    mesmo serializador da entrega, por par evento x destino amostrado) e AGREGADOS por
    org; aqui só gravamos o rollup. Gated por ``REDUCTION_SAMPLE_ENABLED`` **E**
    ``COST_METERING_ENABLED`` (espelha :func:`record_trim_saving`); no-op/fail-closed
    em org ausente ou ``bytes_`` zero."""
    if not enabled() or not bool(getattr(settings, "REDUCTION_SAMPLE_ENABLED", False)):
        return
    if organization_id is None or not bytes_:
        return
    try:
        record_saving(organization_id, None, "sample", bytes_=float(bytes_))
    except Exception:  # noqa: BLE001 — best-effort
        logger.debug("metering.record_sample_saving falhou (org=%s)", organization_id, exc_info=True)


def record_suppress_saving(organization_id: Optional[int], envelope: Any) -> None:
    """Mede o volume lógico evitado por SUPRESSÃO: o envelope inteiro é
    descartado (não entra no batch) → ``bytes_saved{reason=suppress}``.

    BASE per-EVENTO (não per-entrega): a supressão é um rate-limiter que dispara
    PRÉ-roteamento, então mede o envelope UMA vez — diferente de ``bytes_out`` e
    ``bytes_saved{sample}`` (base per-ENTREGA). Logo o termo suppress na razão de
    Redução pode SUB-contar (fan-out>1: o evento alcançaria N destinos) OU SOBRE-contar
    (o evento poderia ser drop/unrouted/loop-blocked = 0 entregas faturáveis, ou
    entregue a um destino que redige = menos bytes). Recalcular o contrafactual exigiria
    rotear um evento já descartado (caro/estranho no ponto do rate-limiter) — aceitamos
    a estimativa per-evento.

    Gated por ``REDUCTION_SUPPRESS_ENABLED`` **E** ``COST_METERING_ENABLED`` — a
    serialização (1 dumps) só ocorre com as duas on; fail-closed em org ausente."""
    if not enabled() or not bool(getattr(settings, "REDUCTION_SUPPRESS_ENABLED", False)):
        return
    if organization_id is None:
        return
    try:
        nbytes = _event_bytes(envelope)  # serializa o ENVELOPE (base = bytes_out)
        if nbytes > 0:
            record_saving(organization_id, None, "suppress", bytes_=float(nbytes))
    except Exception:  # noqa: BLE001 — best-effort
        logger.debug("metering.record_suppress_saving falhou (org=%s)", organization_id, exc_info=True)


def _event_bytes(raw_event: Any) -> int:
    """Logical (pre-compression) JSON size of the RAW event, via the same serializer
    (``dumps_bytes``) the dispatch path uses.

    IMPORTANT — IN and OUT are NOT the same unit, by design:
      * IN (here) = the bare RAW event (volume COLLECTED);
      * OUT (``_record_dest_observability``) = the full delivered ENVELOPE
        ``{_centralops, normalized, raw}`` (volume DELIVERED — the SIEM's billable basis).
    The envelope wraps the raw + normalized + metadata, so bytes_out is normally LARGER
    than bytes_in even with zero reduction. The out/in ratio is therefore an
    envelope+fan-out OVERHEAD factor, NOT a "savings" — this module has no reduction lever.
    The cost-summary endpoint and the EE pricer treat bytes_out (delivered) as the cost
    basis; bytes_in is the collected-volume reference."""
    from ..output._fastjson import dumps_bytes

    return len(dumps_bytes(raw_event))


def record_in(
    organization_id: Optional[int],
    integration_id: Optional[int],
    raw_event: Any,
) -> None:
    """Meter ONE ingested (post-dedupe) raw event: +1 event_in and +bytes_in of its
    logical size, under the source (integration) and org series + the OTel counters.

    NOTE — the collection loop no longer calls this per event: it feeds an
    :class:`InVolumeAccumulator` and flushes via :func:`record_in_batch` (the 4 sync
    Redis pipelines per event blocked the event loop ~0.8ms/event). This per-event
    form is kept for compat/tests and one-off callers; it is exactly equivalent to
    ``record_in_batch(org, integ, 1, bytes(raw_event))``.

    Counts events AFTER the dedupe claim succeeds (so it counts events that actually
    entered — note: this is BEFORE quarantine, so bytes_in includes events that may
    later be quarantined; that is intentional [they DID ingest] and documented so the
    UI does not read quarantine loss as "savings"). No-op when the flag is off
    (returns before any serialization). org_id is fail-closed: a missing org is
    skipped (never written to a shared/null bucket — anti cross-tenant)."""
    if not enabled():
        return
    try:
        record_in_batch(organization_id, integration_id, 1, float(_event_bytes(raw_event)))
    except Exception:  # noqa: BLE001 — metering is best-effort; never breaks collection
        logger.debug("metering.record_in falhou (integ=%s)", integration_id, exc_info=True)


def record_in_batch(
    organization_id: Optional[int],
    integration_id: Optional[int],
    events: int,
    nbytes: float,
) -> None:
    """Meter an AGGREGATED slice of ingested events: +``events`` events_in and
    +``nbytes`` bytes_in under the source (integration) and org series + the OTel
    counters. Semantically equivalent to ``events`` calls to :func:`record_in`
    (counter ``inc(N)`` ≡ N × ``inc(1)``; the per-minute Redis buckets accumulate
    floats), but costs ONE set of writes instead of N — this is the flush target of
    :class:`InVolumeAccumulator`.

    No-op when the flag is off or ``events`` is zero. org_id/integration_id are
    fail-closed individually: a missing org skips the org series and the OTel
    counters (which require both labels — anti cross-tenant), a missing integration
    skips the source series."""
    if not enabled() or not events:
        return
    try:
        from .. import metrics

        if organization_id is not None and integration_id is not None:
            labels = {"org_id": str(organization_id), "integration_id": str(integration_id)}
            metrics.EVENTS_IN.labels(**labels).inc(float(events))
            metrics.BYTES_IN.labels(**labels).inc(float(nbytes))

        from .. import observability_store as obs

        if integration_id is not None:
            obs.record_counter(_KIND_SOURCE, str(integration_id), _M_EVENTS_IN, float(events))
            obs.record_counter(_KIND_SOURCE, str(integration_id), _M_BYTES_IN, float(nbytes))
        if organization_id is not None:
            obs.record_counter(_KIND_ORG, str(organization_id), _M_EVENTS_IN, float(events))
            obs.record_counter(_KIND_ORG, str(organization_id), _M_BYTES_IN, float(nbytes))
    except Exception:  # noqa: BLE001 — metering is best-effort; never breaks collection
        logger.debug("metering.record_in_batch falhou (integ=%s)", integration_id, exc_info=True)


class InVolumeAccumulator:
    """Batcher do metering IN para o loop async de coleta.

    O ``record_in`` por-evento fazia 4 pipelines Redis SÍNCRONOS
    (hincrbyfloat+expire) POR EVENTO no event loop (~0,8ms/evento; ciclo de 10k
    ≈ 8s bloqueados). Este acumulador soma (events, bytes) por
    ``(org_id, integration_id)`` — na prática 1 par por ciclo (mono-tenant) — e
    faz flush via :func:`record_in_batch` a cada ``flush_events`` eventos OU
    ``flush_seconds`` segundos, o que vier primeiro. O limite de 15s preserva a
    granularidade de MINUTO dos buckets do observability_store (um ciclo bulk de
    12min não pode atribuir tudo ao minuto final).

    Contrato (espelha o módulo): o ``_event_bytes`` (1 dumps) continua por evento
    — só o Redis é batched; ``add`` é no-op imediato flag-off (zero serialização);
    tudo é best-effort/fail-open (NUNCA levanta no hot path). O dono do ciclo DEVE
    chamar :meth:`flush` num ``finally`` (inclusive exceção/soft-timeout: grava o
    parcial sem mascarar o erro original — padrão ``_track_claims``: instancie
    ANTES do try). ``clock`` é injetável para testes (default ``time.monotonic``).
    """

    def __init__(
        self,
        *,
        flush_events: int = 500,
        flush_seconds: float = 15.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._flush_events = int(flush_events)
        self._flush_seconds = float(flush_seconds)
        self._clock = clock
        # (org_id, integration_id) -> [events, bytes]
        self._pending: Dict[Tuple[Optional[int], Optional[int]], list] = {}
        self._pending_events = 0
        self._window_start: Optional[float] = None

    def add(
        self,
        organization_id: Optional[int],
        integration_id: Optional[int],
        raw_event: Any,
    ) -> None:
        """Acumula UM evento ingerido (pós-dedupe). Flag-off = no-op imediato
        (sem serialização). Best-effort: qualquer falha é engolida em debug."""
        if not enabled():
            return
        try:
            nbytes = _event_bytes(raw_event)
            if self._window_start is None:
                # a janela de tempo começa no 1º evento pendente (não na
                # construção) — garante "dado parado no buffer ≤ flush_seconds".
                self._window_start = self._clock()
            slot = self._pending.setdefault((organization_id, integration_id), [0, 0.0])
            slot[0] += 1
            slot[1] += float(nbytes)
            self._pending_events += 1
            if self._pending_events >= self._flush_events or (
                self._clock() - self._window_start
            ) >= self._flush_seconds:
                self.flush()
        except Exception:  # noqa: BLE001 — best-effort; nunca quebra a coleta
            logger.debug(
                "metering.InVolumeAccumulator.add falhou (integ=%s)",
                integration_id,
                exc_info=True,
            )

    def flush(self) -> None:
        """Grava os agregados pendentes (1 ``record_in_batch`` por par) e zera o
        buffer. Best-effort: NUNCA levanta (seguro num ``finally`` — não mascara
        a exceção original do ciclo). Sem pendências = no-op."""
        try:
            pending, self._pending = self._pending, {}
            self._pending_events = 0
            self._window_start = None
            for (org_id, integ_id), (events, nbytes) in pending.items():
                record_in_batch(org_id, integ_id, events, nbytes)
        except Exception:  # noqa: BLE001 — best-effort; nunca mascara o erro do ciclo
            logger.debug("metering.InVolumeAccumulator.flush falhou", exc_info=True)


def record_out(
    organization_id: Optional[int],
    events: int,
    nbytes: float,
) -> None:
    """Meter a delivered sub-batch's volume into the per-ORG rollup. The per-DESTINATION
    series (``dest``/sent/bytes) is already written by ``_record_dest_observability``;
    this adds ONLY the org-level out totals the cost-summary reads, REUSING the byte sum
    the dispatch path already computed (no second serialization). No-op when the flag is
    off; fail-closed on a missing org."""
    if not enabled() or organization_id is None or not events:
        return
    try:
        from .. import observability_store as obs

        obs.record_counter(_KIND_ORG, str(organization_id), _M_EVENTS_OUT, float(events))
        obs.record_counter(_KIND_ORG, str(organization_id), _M_BYTES_OUT, float(nbytes))
    except Exception:  # noqa: BLE001 — best-effort; never affects delivery
        logger.debug("metering.record_out falhou (org=%s)", organization_id, exc_info=True)
