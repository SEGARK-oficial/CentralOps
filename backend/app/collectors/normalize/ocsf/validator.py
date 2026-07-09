"""OCSF structural validator (tier-1).

Two-tier design. This module is **tier-1**: a pure-Python *structural gate*
(~µs, no ``jsonschema``, always safe to run) that checks the arithmetic + enum
invariants which catch the gross defects a normalization mapping can produce:

    GATE-1  class_uid ∈ vendored manifest        (else unknown_class / out_of_scope)
    GATE-2  category_uid == class_uid // 1000     (else category_mismatch)
    GATE-3  severity_id ∈ universal enum          (else bad_severity_id)
    GATE-4  activity_id ∈ class enum              (else bad_activity_id)
            type_uid == class_uid*100 + activity  (else type_uid_mismatch)
    GATE-5  status_id ∈ class enum   (only if present; else bad_status_id)
    GATE-6  content-required presence             (ADVISORY — does NOT fail the event)

Source of truth is the **vendored, versioned** manifest
(``ocsf/schemas/<version>/manifest.json``) — never a live fetch, never the schema
server at runtime. Loaded once at boot into an **immutable** registry
(thread-safe, no lock; O(1) lookup by ``class_uid``). Tier-2 (full JSON Schema:
required objects/observables/profiles) is deferred.

Runtime posture is **tag-and-pass** behind ``OCSF_VALIDATION_ENABLED``
(see ``pipeline.py``): the hook tags the envelope + emits metrics but never drops.
Enforcement (quarantine, per-org policy) is deferred to a later stage.

``reason`` is a **closed enum** (:data:`OCSF_REASONS`) — it is used as a metric
label and a wire tag, so it must never interpolate a value from the event
(anti-PII + bounded cardinality). A test enforces this.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any, Mapping, Optional

# ── reason: closed enum (never interpolate event values) ──────────
REASON_OK = "ok"
REASON_OUT_OF_SCOPE = "out_of_scope"          # valid-looking class we do not vendor
REASON_UNKNOWN_CLASS = "unknown_class"        # class_uid missing / not a positive int
REASON_CATEGORY_MISMATCH = "category_mismatch"
REASON_BAD_SEVERITY_ID = "bad_severity_id"
REASON_BAD_ACTIVITY_ID = "bad_activity_id"
REASON_TYPE_UID_MISMATCH = "type_uid_mismatch"
REASON_BAD_STATUS_ID = "bad_status_id"
REASON_MISSING_REQUIRED = "missing_required"  # advisory (GATE-6), not an enforced failure

OCSF_REASONS = frozenset(
    {
        REASON_OK,
        REASON_OUT_OF_SCOPE,
        REASON_UNKNOWN_CLASS,
        REASON_CATEGORY_MISMATCH,
        REASON_BAD_SEVERITY_ID,
        REASON_BAD_ACTIVITY_ID,
        REASON_TYPE_UID_MISMATCH,
        REASON_BAD_STATUS_ID,
        REASON_MISSING_REQUIRED,
    }
)

# severity_id universal enum (OCSF 1.x). Kept local to avoid an import cycle with
# ``ocsf.classes``; ``test_ocsf_validator`` asserts it equals ``set(SEVERITY_ID.values())``.
_SEVERITY_IDS = frozenset({0, 1, 2, 3, 4, 5, 6, 99})

# Identity/envelope fields the engine always emits (const rules) or the envelope
# injects — excluded from the ADVISORY content-required coverage so GATE-6 measures
# real source-content gaps, not universals already enforced by GATES 1-4.
_ENGINE_PROVIDED = frozenset(
    {"class_uid", "category_uid", "type_uid", "activity_id", "severity_id", "time", "metadata"}
)

DEFAULT_OCSF_VERSION = "1.8.0"


@dataclass(frozen=True)
class OcsfClassSpec:
    """Immutable per-class validation spec, derived from the vendored manifest."""

    class_uid: int
    name: str
    category_uid: int
    activity_ids: frozenset[int]
    status_ids: frozenset[int]
    required: tuple[str, ...]


@dataclass(frozen=True)
class OcsfValidationResult:
    """Outcome of the structural gate for one normalized event.

    ``valid`` is the authoritative structural verdict (GATES 1-5, identity + enums).
    ``missing_required`` (GATE-6) is advisory and does NOT flip ``valid``.
    ``in_scope`` is False for a positive ``class_uid`` we simply do not vendor — such
    events are neither counted valid nor invalid for the conformance rate.
    """

    valid: bool
    reason: str
    in_scope: bool
    type_uid: Optional[int]
    class_uid: Optional[int]
    class_name: str
    missing_required: tuple[str, ...] = ()


def _is_ocsf_int(value: Any) -> bool:
    """True for a real OCSF enum int. Excludes ``bool`` (a subclass of ``int`` that
    would spuriously match 0/1) and, crucially, any non-int/unhashable value — so
    ``value in frozenset`` never raises ``TypeError`` on a malformed mapping output."""
    return isinstance(value, int) and not isinstance(value, bool)


def derive_type_uid(class_uid: int, activity_id: int) -> int:
    """``type_uid = class_uid * 100 + activity_id`` (OCSF identity)."""
    return class_uid * 100 + activity_id


class OcsfValidatorRegistry:
    """Vendored OCSF class specs for one version. Loaded once, then immutable.

    Immutable after construction → safe to share across threads/tasks without a
    lock, and lookups are O(1) dict access on the hot path.
    """

    __slots__ = ("version", "_by_class_uid")

    def __init__(self, version: str, specs: Mapping[int, OcsfClassSpec]) -> None:
        self.version = version
        self._by_class_uid: dict[int, OcsfClassSpec] = dict(specs)

    @classmethod
    def load(cls, version: str = DEFAULT_OCSF_VERSION) -> "OcsfValidatorRegistry":
        """Load ``ocsf/schemas/<version>/manifest.json`` into an immutable registry."""
        anchor = resources.files("backend.app.collectors.normalize.ocsf")
        text = anchor.joinpath("schemas", version, "manifest.json").read_text("utf-8")
        raw = json.loads(text)
        specs: dict[int, OcsfClassSpec] = {}
        for uid_str, entry in raw.get("classes", {}).items():
            class_uid = int(uid_str)
            specs[class_uid] = OcsfClassSpec(
                class_uid=class_uid,
                name=str(entry["name"]),
                category_uid=int(entry["category_uid"]),
                activity_ids=frozenset(int(k) for k in entry.get("activity_ids", {})),
                status_ids=frozenset(int(k) for k in entry.get("status_ids", {})),
                required=tuple(entry.get("required", ())),
            )
        return cls(version=str(raw.get("ocsf_version", version)), specs=specs)

    def spec_for(self, class_uid: int) -> Optional[OcsfClassSpec]:
        return self._by_class_uid.get(class_uid)

    @property
    def class_uids(self) -> frozenset[int]:
        return frozenset(self._by_class_uid)

    def __len__(self) -> int:  # pragma: no cover - trivial
        return len(self._by_class_uid)


@lru_cache(maxsize=4)
def get_registry(version: str = DEFAULT_OCSF_VERSION) -> OcsfValidatorRegistry:
    """Process-wide cached registry for ``version`` (compiled once, then reused)."""
    return OcsfValidatorRegistry.load(version)


def _result(
    *,
    valid: bool,
    reason: str,
    in_scope: bool,
    class_uid: Optional[int],
    spec: Optional[OcsfClassSpec],
    type_uid: Optional[int] = None,
    missing_required: tuple[str, ...] = (),
) -> OcsfValidationResult:
    return OcsfValidationResult(
        valid=valid,
        reason=reason,
        in_scope=in_scope,
        type_uid=type_uid,
        class_uid=class_uid,
        class_name=spec.name if spec else "",
        missing_required=missing_required,
    )


def structural_gate(
    normalized: Mapping[str, Any],
    registry: OcsfValidatorRegistry,
) -> OcsfValidationResult:
    """Tier-1 structural validation of a ``normalized`` OCSF object.

    Pure arithmetic + frozenset membership — no allocation beyond the small result
    dataclass, no jsonschema, no I/O. Returns on the first identity/enum failure.
    """
    class_uid = normalized.get("class_uid")

    # ── GATE-1: class_uid present, positive int, and a class we vendor ──────────
    if not _is_ocsf_int(class_uid) or class_uid <= 0:
        return _result(valid=False, reason=REASON_UNKNOWN_CLASS, in_scope=True,
                       class_uid=class_uid if _is_ocsf_int(class_uid) else None, spec=None)
    spec = registry.spec_for(class_uid)
    if spec is None:
        # A positive class_uid we don't vendor (e.g. a valid-but-unmapped OCSF class).
        # Graceful: not rejected, but not vouched for either.
        return _result(valid=False, reason=REASON_OUT_OF_SCOPE, in_scope=False,
                       class_uid=class_uid, spec=None)

    # ── GATE-2: category_uid == class_uid // 1000 (and matches the manifest) ────
    category_uid = normalized.get("category_uid")
    if category_uid != spec.category_uid:
        return _result(valid=False, reason=REASON_CATEGORY_MISMATCH, in_scope=True,
                       class_uid=class_uid, spec=spec)

    # ── GATE-3: severity_id ∈ universal enum ────────────────────────────────────
    # Type-safe membership: these enum fields are ints in OCSF; a non-int (str,
    # list, dict, bool) is INVALID, never a crash — ``x in frozenset`` raises
    # TypeError on an unhashable value, and this gate also runs on the hot path.
    severity_id = normalized.get("severity_id")
    if not _is_ocsf_int(severity_id) or severity_id not in _SEVERITY_IDS:
        return _result(valid=False, reason=REASON_BAD_SEVERITY_ID, in_scope=True,
                       class_uid=class_uid, spec=spec)

    # ── GATE-4: activity_id ∈ class enum, and type_uid consistent ───────────────
    activity_id = normalized.get("activity_id")
    if not _is_ocsf_int(activity_id) or activity_id not in spec.activity_ids:
        return _result(valid=False, reason=REASON_BAD_ACTIVITY_ID, in_scope=True,
                       class_uid=class_uid, spec=spec)
    expected_type_uid = derive_type_uid(class_uid, activity_id)
    type_uid = normalized.get("type_uid")
    if type_uid != expected_type_uid:
        return _result(valid=False, reason=REASON_TYPE_UID_MISMATCH, in_scope=True,
                       class_uid=class_uid, spec=spec, type_uid=expected_type_uid)

    # ── GATE-5: status_id ∈ class enum — CONDITIONAL (only when present) ─────────
    status_id = normalized.get("status_id")
    if status_id is not None and (
        not _is_ocsf_int(status_id) or status_id not in spec.status_ids
    ):
        return _result(valid=False, reason=REASON_BAD_STATUS_ID, in_scope=True,
                       class_uid=class_uid, spec=spec, type_uid=expected_type_uid)

    # ── GATE-6: content-required presence — ADVISORY (never flips ``valid``) ─────
    missing = tuple(
        attr
        for attr in spec.required
        if attr not in _ENGINE_PROVIDED and normalized.get(attr) is None
    )

    return _result(valid=True, reason=REASON_OK, in_scope=True,
                   class_uid=class_uid, spec=spec, type_uid=expected_type_uid,
                   missing_required=missing)


def validate_normalized(
    normalized: Mapping[str, Any],
    version: str = DEFAULT_OCSF_VERSION,
) -> OcsfValidationResult:
    """Convenience: run the structural gate against the cached registry for ``version``."""
    return structural_gate(normalized, get_registry(version))
