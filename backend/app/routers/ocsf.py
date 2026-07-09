"""OCSF governance — per-org enforcement policy + compliance report.

- ``GET  /api/ocsf/policies``            — list every org + its effective enforcement
- ``PUT  /api/ocsf/policies/{org_id}``   — set an org's enforcement mode
- ``GET  /api/ocsf/compliance``          — per-integration OCSF-quarantine counts (24h)

Admin-only (admin is always global — see rbac). Enforcement modes come from
``collectors.ocsf_policy.ENFORCEMENT_MODES``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..collectors import ocsf_policy
from ..collectors import quarantine
from ..core import auth as app_auth
from ..core.config import settings
from ..core.errors import ApiError
from ..db import database, models

router = APIRouter(prefix="/ocsf", tags=["ocsf"])

_MODES = sorted(ocsf_policy.ENFORCEMENT_MODES)


# ── Schemas ───────────────────────────────────────────────────────────────────


class OcsfPolicyRead(BaseModel):
    organization_id: int
    organization_name: Optional[str]
    enforcement_mode: str
    # True when the org has no explicit row and inherits the global default.
    is_default: bool


class OcsfPolicyUpdate(BaseModel):
    enforcement_mode: str = Field(pattern="^(tag_and_pass|quarantine|fail_closed)$")


class OcsfComplianceItem(BaseModel):
    integration_id: int
    integration_name: Optional[str]
    organization_id: Optional[int]
    enforcement_mode: str
    invalid_quarantined_24h: int


class OcsfComplianceResponse(BaseModel):
    validation_enabled: bool
    global_default: str
    ocsf_version: str
    items: List[OcsfComplianceItem]


# ── Policy CRUD ─────────────────────────────────────────────────────────────


@router.get("/policies", response_model=List[OcsfPolicyRead])
def list_policies(
    _: models.AppUser = Depends(app_auth.require_admin_user),
    db: Session = Depends(database.get_session),
) -> List[OcsfPolicyRead]:
    """Every org with its effective OCSF enforcement mode (explicit row or global)."""
    global_default = settings.OCSF_DEFAULT_ENFORCEMENT
    policies = {
        p.organization_id: p.enforcement_mode
        for p in db.execute(select(models.OrganizationOcsfPolicy)).scalars()
    }
    orgs = db.execute(
        select(models.Organization.id, models.Organization.name).order_by(
            models.Organization.name
        )
    ).all()
    out: List[OcsfPolicyRead] = []
    for org_id, name in orgs:
        explicit = policies.get(org_id)
        out.append(
            OcsfPolicyRead(
                organization_id=org_id,
                organization_name=name,
                enforcement_mode=explicit or global_default,
                is_default=explicit is None,
            )
        )
    return out


@router.put("/policies/{org_id}", response_model=OcsfPolicyRead)
def set_policy(
    org_id: int,
    body: OcsfPolicyUpdate,
    _: models.AppUser = Depends(app_auth.require_admin_user),
    db: Session = Depends(database.get_session),
) -> OcsfPolicyRead:
    """Upsert an org's OCSF enforcement mode."""
    if body.enforcement_mode not in ocsf_policy.ENFORCEMENT_MODES:
        raise ApiError(
            "ocsf.invalid_enforcement_mode",
            422,
            messages={
                "pt": f"Modo de enforcement inválido (use {_MODES}).",
                "en": f"Invalid enforcement mode (use one of {_MODES}).",
                "es": f"Modo de enforcement inválido (use {_MODES}).",
            },
        )
    org = db.get(models.Organization, org_id)
    if org is None:
        raise ApiError(
            "org.not_found",
            404,
            messages={
                "pt": "Organização não encontrada.",
                "en": "Organization not found.",
                "es": "Organización no encontrada.",
            },
        )
    row = db.get(models.OrganizationOcsfPolicy, org_id)
    if row is None:
        row = models.OrganizationOcsfPolicy(
            organization_id=org_id, enforcement_mode=body.enforcement_mode
        )
        db.add(row)
    else:
        row.enforcement_mode = body.enforcement_mode
    db.commit()
    return OcsfPolicyRead(
        organization_id=org_id,
        organization_name=org.name,
        enforcement_mode=body.enforcement_mode,
        is_default=False,
    )


# ── Compliance report ────────────────────────────────────────────────


@router.get("/compliance", response_model=OcsfComplianceResponse)
def compliance_report(
    _: models.AppUser = Depends(app_auth.require_admin_user),
    db: Session = Depends(database.get_session),
) -> OcsfComplianceResponse:
    """Per-integration OCSF conformance signal from the DB: how many events each
    integration had QUARANTINED as OCSF-invalid in the last 24h, plus the org's
    enforcement mode. (In ``tag_and_pass`` invalid events are not quarantined —
    that conformance signal lives in the ``collector_ocsf_*`` metrics; this report
    covers the enforced modes.)"""
    cutoff = datetime.utcnow() - timedelta(hours=24)
    global_default = settings.OCSF_DEFAULT_ENFORCEMENT
    policies = {
        p.organization_id: p.enforcement_mode
        for p in db.execute(select(models.OrganizationOcsfPolicy)).scalars()
    }

    # invalid-quarantined per integration in the window
    counts = dict(
        db.execute(
            select(
                models.QuarantineEvent.integration_id,
                func.count(models.QuarantineEvent.id),
            )
            .where(
                models.QuarantineEvent.error_kind == quarantine.ERROR_KIND_VALIDATE,
                models.QuarantineEvent.created_at > cutoff,
            )
            .group_by(models.QuarantineEvent.integration_id)
        ).all()
    )

    integrations = db.execute(
        select(
            models.Integration.id,
            models.Integration.name,
            models.Integration.organization_id,
        ).where(models.Integration.kind.notin_(("partner", "organization")))
    ).all()

    items = [
        OcsfComplianceItem(
            integration_id=iid,
            integration_name=name,
            organization_id=org_id,
            enforcement_mode=policies.get(org_id, global_default),
            invalid_quarantined_24h=int(counts.get(iid, 0)),
        )
        for iid, name, org_id in integrations
    ]
    return OcsfComplianceResponse(
        validation_enabled=settings.OCSF_VALIDATION_ENABLED,
        global_default=global_default,
        ocsf_version=settings.OCSF_VALIDATION_VERSION,
        items=items,
    )
