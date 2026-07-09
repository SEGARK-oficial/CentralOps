"""Service Accounts (Fase 2 — credencial machine-to-machine).

CRUD + helpers de conversão. Audit é responsabilidade do router (segue o
padrão de api_tokens.py).
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from sqlalchemy.orm import Session

from ..db import models


class ServiceAccountService:
    """Camada de aplicação: CRUD de Service Accounts + utilitários.

    Permissão de chamada (USER_MANAGE) é validada no router via
    ``require_permission(Permission.USER_MANAGE)``. Aqui só validamos
    invariantes de negócio (nome único, role válida, etc).
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    # -- Leitura ---------------------------------------------------------

    def get(self, service_account_id: int) -> models.ServiceAccount | None:
        return (
            self.db.query(models.ServiceAccount)
            .filter(models.ServiceAccount.id == service_account_id)
            .first()
        )

    def get_by_name(self, name: str) -> models.ServiceAccount | None:
        return (
            self.db.query(models.ServiceAccount)
            .filter(models.ServiceAccount.name == name)
            .first()
        )

    def list_all(self, *, include_inactive: bool = True) -> list[models.ServiceAccount]:
        query = self.db.query(models.ServiceAccount).order_by(
            models.ServiceAccount.created_at.desc()
        )
        if not include_inactive:
            query = query.filter(models.ServiceAccount.is_active.is_(True))
        return query.all()

    # -- Mutação ---------------------------------------------------------

    def create(
        self,
        *,
        name: str,
        description: str | None,
        role: str,
        organization_id: int | None,
        created_by_user_id: int | None,
    ) -> models.ServiceAccount:
        """Cria um novo SA. Lança ``ValueError`` em conflito de nome.

        Caller (router) já validou ``role`` via Pydantic schema.
        """
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("name must not be empty")

        if self.get_by_name(normalized_name):
            raise ValueError("service account name already in use")

        sa = models.ServiceAccount(
            name=normalized_name,
            description=description,
            role=role,
            organization_id=organization_id,
            is_active=True,
            created_by_user_id=created_by_user_id,
        )
        self.db.add(sa)
        self.db.commit()
        self.db.refresh(sa)
        return sa

    def update(
        self,
        sa: models.ServiceAccount,
        *,
        description: str | None = None,
        role: str | None = None,
        organization_id: int | None = None,
        is_active: bool | None = None,
        _description_set: bool = False,
        _organization_id_set: bool = False,
    ) -> models.ServiceAccount:
        """Aplica patch parcial. Use os flags ``_*_set`` pra distinguir
        "campo ausente" de "campo enviado como None".

        Mudar ``role`` muda o teto de permissões dos tokens vinculados —
        o router escreve audit explícito quando role muda (sensitive op).
        """
        if _description_set:
            sa.description = description
        if role is not None:
            sa.role = role
        if _organization_id_set:
            sa.organization_id = organization_id
        if is_active is not None:
            sa.is_active = is_active
        sa.updated_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(sa)
        return sa

    def delete(self, sa: models.ServiceAccount) -> None:
        """Hard delete. Cascade revoga ApiTokens vinculados (ondelete=CASCADE).

        Router faz audit antes de chamar (precisa do ID e tokens count).
        Considere ``update(is_active=False)`` se quiser preservar trail
        histórico — delete é destructive permanente.
        """
        self.db.delete(sa)
        self.db.commit()

    # -- Conveniência pra schemas ---------------------------------------

    def count_active_tokens(self, service_account_id: int) -> int:
        """Conta PATs ativos (não-revogados) ligados a este SA.

        Útil pra UI: "5 tokens ativos — confirma exclusão?".
        """
        return (
            self.db.query(models.ApiToken)
            .filter(models.ApiToken.service_account_id == service_account_id)
            .filter(models.ApiToken.revoked_at.is_(None))
            .count()
        )


__all__: Iterable[str] = ("ServiceAccountService",)
