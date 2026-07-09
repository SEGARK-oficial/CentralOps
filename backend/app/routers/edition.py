"""``GET /api/edition`` — observable edition + licensed features (open-core).

Read-only view of the running edition, derived offline from the (optional) license
token. Excludes the commercial customer id (``sub``) — only non-identifying gating
info is exposed. Defaults to Community when no valid license is present.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from ..core import edition as edition_core

router = APIRouter(prefix="/edition", tags=["edition"])


class EditionStatus(BaseModel):
    edition: str
    features: List[str]
    plan: Optional[str] = None
    seats: Optional[int] = None
    # Teto de orgs do tier (None = ilimitado). Permite ao front mostrar "1/1
    # organizações" e desabilitar o "Nova organização" ANTES do 403 do tier.
    max_organizations: Optional[int] = None
    expires_at: Optional[str] = None  # ISO-8601, or null
    # True = licença já venceu mas está na JANELA DE CARÊNCIA (renove!). A UI mostra
    # o alerta "expirada, em carência" em vez de derrubar as features na hora.
    expired_in_grace: bool = False


@router.get("", response_model=EditionStatus)
def get_edition() -> EditionStatus:
    fs = edition_core.current()
    return EditionStatus(
        edition=fs.edition,
        features=sorted(fs.features),
        plan=fs.plan,
        seats=fs.seats,
        max_organizations=fs.max_organizations,
        expires_at=fs.expires_at.isoformat() if fs.expires_at else None,
        expired_in_grace=fs.expired_in_grace,
    )
