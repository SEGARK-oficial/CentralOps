"""OCSF enforcement policy per organization.

The structural validator tags + measures; the *policy* decides what to do
with a structurally-invalid event:

- ``tag_and_pass`` — only tag ``_centralops.ocsf_valid`` + metric; dispatch anyway
  (safe rollout default; existing orgs are backfilled here so a future GA flip of the
  GLOBAL default to ``quarantine`` never retroactively quarantines them).
- ``quarantine``   — send to quarantine (``ERROR_KIND_VALIDATE``), do NOT dispatch;
  recoverable via reprocess. Enterprise-safe GA default.
- ``fail_closed``  — drop without quarantine (only for orgs that explicitly ask).

Resolution is **once per collection run** (the run is mono-tenant → the org is
fixed), mirroring ``_load_routes_for_org``. Fail-safe: any error resolving the row
falls back to the global default, never breaking ingestion.
"""

from __future__ import annotations

import logging
from typing import Optional

from ..core.config import settings

logger = logging.getLogger(__name__)

MODE_TAG_AND_PASS = "tag_and_pass"
MODE_QUARANTINE = "quarantine"
MODE_FAIL_CLOSED = "fail_closed"

#: The full set of valid enforcement modes. ``config.py`` validates
#: ``OCSF_DEFAULT_ENFORCEMENT`` against the same literal set; a test keeps them in sync.
ENFORCEMENT_MODES = frozenset({MODE_TAG_AND_PASS, MODE_QUARANTINE, MODE_FAIL_CLOSED})

# Hot-path actions the pipeline applies to an event after validation.
ACTION_PASS = "pass"              # dispatch (valid, out_of_scope, or tag_and_pass-invalid)
ACTION_QUARANTINE = "quarantine"  # write to quarantine (recoverable), do NOT dispatch
ACTION_DROP = "drop"              # discard without quarantine (fail_closed), do NOT dispatch


def decide(*, valid: bool, in_scope: bool, mode: str) -> str:
    """Map a structural-gate outcome + enforcement mode to a hot-path action.

    Invariants (unit-tested exhaustively):
    - a VALID event always PASSes (dispatched) — regardless of mode.
    - an OUT-OF-SCOPE event (valid OCSF class we don't vendor) always PASSes —
      graceful, we never reject what we can't judge.
    - an INVALID in-scope event follows the mode: ``quarantine`` → QUARANTINE,
      ``fail_closed`` → DROP, ``tag_and_pass`` (or anything else) → PASS. So
      ``tag_and_pass`` NEVER drops security data — only tags it.
    """
    if valid or not in_scope:
        return ACTION_PASS
    if mode == MODE_QUARANTINE:
        return ACTION_QUARANTINE
    if mode == MODE_FAIL_CLOSED:
        return ACTION_DROP
    return ACTION_PASS


def _global_default() -> str:
    default = settings.OCSF_DEFAULT_ENFORCEMENT
    return default if default in ENFORCEMENT_MODES else MODE_TAG_AND_PASS


def resolve_enforcement_mode(organization_id: Optional[int]) -> str:
    """Per-org enforcement mode, or the global default when no policy row exists.

    Synchronous (opens its own session) so it can be a clean ``asyncio.to_thread``
    target from the pipeline. Fail-safe: on any DB error → global default.
    """
    if organization_id is not None:
        try:
            # Local import avoids importing the DB layer at module import time
            # (keeps this importable in lightweight contexts / mirrors quarantine.py).
            from ..db import database, models

            with database.SessionLocal() as db:
                row = db.get(models.OrganizationOcsfPolicy, organization_id)
            if row is not None and row.enforcement_mode in ENFORCEMENT_MODES:
                return row.enforcement_mode
        except Exception:  # pragma: no cover - defensive
            logger.warning(
                "falha ao resolver política OCSF; usando default global",
                extra={"event": "ocsf.policy_resolve_failed", "organization_id": organization_id},
                exc_info=True,
            )
    return _global_default()
