"""Synthetic load generator for the SLO harness.

Pure, dependency-light helpers shared by ``test_load_slo.py``. NO production
code lives here — this is a test-only fixture factory.

It produces **canonical envelopes** (the same ``_centralops`` shape the
normalize engine emits, see ``normalize/envelope.py``) at controllable
byte sizes and across N tenants, including one disproportionately loud
("noisy") tenant. The harness feeds these envelopes into the real in-process
delivery path (``pipeline._send_chunk_with_retry``) with mocked sinks.

Design intent:
  mix de tamanho de evento (200 B firewall / 500 B / 2 KB Windows), N tenants,
  um tenant barulhento, para provar que o back-pressure de um NÃO contamina os
  outros. Este gerador é o lado de *produção de carga*; as
  asserções vivem no teste.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Mapping, Sequence


# ── Event-size profiles ─────────────────────────────────
# Target *approximate* serialized byte sizes for the ``raw`` payload. We pad a
# filler string to hit the target so the envelope's wire size is realistic; the
# delivery path never inspects the filler, so its content is irrelevant.
SIZE_FIREWALL_B = 200      # 200 B — firewall/syslog-class event
SIZE_MEDIUM_B = 500        # 500 B — the SLO reference event size
SIZE_WINDOWS_B = 2048      # 2 KB — Windows/EVTX-class event

# Fixed overhead (in bytes) the canonical ``_centralops`` block + JSON scaffolding
# adds before the filler. Used only to size the filler so the *total* lands near
# the target; an approximation is fine — the SLO is about distribution, not exact
# bytes.
_ENVELOPE_OVERHEAD_B = 320


@dataclass(frozen=True)
class SizeMix:
    """Relative weights of the three event-size classes in a generated stream.

    The weights are integers (parts), not percentages — a tenant generated with
    ``SizeMix(firewall=6, medium=3, windows=1)`` emits a 6:3:1 ratio.
    """

    firewall: int = 6
    medium: int = 3
    windows: int = 1

    def as_sequence(self) -> List[int]:
        """Expand the weights into a flat list of target byte sizes (one cycle)."""
        out: List[int] = []
        out += [SIZE_FIREWALL_B] * self.firewall
        out += [SIZE_MEDIUM_B] * self.medium
        out += [SIZE_WINDOWS_B] * self.windows
        if not out:  # defensive: never yield an empty cycle
            out = [SIZE_MEDIUM_B]
        return out


@dataclass
class TenantSpec:
    """One tenant in the synthetic fleet.

    ``organization_id`` is the internal tenant id. ``events`` is how
    many envelopes this tenant contributes. ``noisy`` flags the disproportionate
    producer whose isolation we assert.
    """

    organization_id: int
    events: int
    vendor: str = "fortinet"
    size_mix: SizeMix = field(default_factory=SizeMix)
    noisy: bool = False


# Module-level monotonic counter guaranteeing GLOBALLY-unique event_ids across
# every tenant and every call within a process — the zero-duplication assertion
# depends on uniqueness at the source, so a generated id is never reused.
_event_seq = itertools.count(1)


def reset_event_seq() -> None:
    """Reset the global event_id counter — test seam for deterministic ids."""
    global _event_seq
    _event_seq = itertools.count(1)


def _filler_for(target_bytes: int) -> str:
    """Return a filler string sized so the serialized envelope ~= target_bytes."""
    pad = target_bytes - _ENVELOPE_OVERHEAD_B
    if pad < 0:
        pad = 0
    return "x" * pad


def make_envelope(
    *,
    organization_id: int,
    vendor: str,
    severity_id: int,
    target_bytes: int,
    event_id: str | None = None,
) -> Dict[str, Any]:
    """Build ONE canonical envelope with a valid ``_centralops`` block.

    Mirrors the contract of ``normalize/envelope.build_envelope`` for the fields
    the delivery path and routing engine read: ``organization_id``, ``event_id``
    (globally unique), ``severity_id``, ``vendor``. ``raw`` carries a filler to
    hit ``target_bytes``; ``normalized`` carries the routing label.
    """
    eid = event_id if event_id is not None else f"evt-{next(_event_seq):010d}"
    return {
        "_centralops": {
            "schema_version": "1.0",
            "organization_id": organization_id,
            "event_id": eid,
            "severity_id": severity_id,
            "vendor": vendor,
            "event_type": f"{vendor}.event",
            "stream": "events",
        },
        "raw": {"vendor_event": _filler_for(target_bytes), "_size_class": target_bytes},
        "normalized": {"severity_id": severity_id},
    }


def generate_tenant_events(spec: TenantSpec) -> List[Dict[str, Any]]:
    """Generate ``spec.events`` envelopes for one tenant, cycling its size mix."""
    sizes = spec.size_mix.as_sequence()
    out: List[Dict[str, Any]] = []
    for i in range(spec.events):
        target = sizes[i % len(sizes)]
        # severity cycles 1..5 so routing/label assertions have spread.
        severity_id = (i % 5) + 1
        out.append(
            make_envelope(
                organization_id=spec.organization_id,
                vendor=spec.vendor,
                severity_id=severity_id,
                target_bytes=target,
            )
        )
    return out


def build_fleet(specs: Sequence[TenantSpec]) -> Dict[int, List[Dict[str, Any]]]:
    """Generate envelopes for every tenant spec, keyed by organization_id."""
    return {spec.organization_id: generate_tenant_events(spec) for spec in specs}


def chunked(
    batch: Sequence[Mapping[str, Any]], max_items: int
) -> Iterator[List[Dict[str, Any]]]:
    """Yield successive ``max_items``-sized chunks of ``batch`` (B2 chunking)."""
    for i in range(0, len(batch), max_items):
        yield list(batch[i : i + max_items])


def event_id_of(envelope: Mapping[str, Any]) -> str:
    """Extract the canonical event_id from an envelope."""
    return envelope["_centralops"]["event_id"]


def org_id_of(envelope: Mapping[str, Any]) -> int:
    """Extract the organization_id (tenant) from an envelope."""
    return envelope["_centralops"]["organization_id"]
