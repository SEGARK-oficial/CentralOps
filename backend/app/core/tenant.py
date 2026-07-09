"""Helpers for organization-scoped authorization.

Duas dimensões independentes de acesso:

  * **Quais organizations o usuário enxerga** — controlado por
    ``has_global_scope``: ``admin`` e usuários com ``is_global=True`` veem
    todas; os demais ficam restritos à própria ``organization_id``. É o que
    habilita o "analista de SOC interno" (vê todos os clientes sem ser admin).
  * **Se enxerga recursos inativos/soft-deleted** — controlado por
    ``is_admin`` (capability administrativa, deliberadamente NÃO concedida
    pelo escopo global). Os routers usam ``is_admin`` direto para isso.
"""

from __future__ import annotations

from typing import Iterable, TypeVar

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from ..db import models
from . import ee_hooks

T = TypeVar("T")


def is_admin(user: models.AppUser | None) -> bool:
    return bool(user and user.role == "admin")


def has_global_scope(user: models.AppUser | None) -> bool:
    """True se o usuário enxerga dados de TODAS as organizations.

    Modelo ÚNICO, sem kill-switch (app pré-lançamento): admin é global
    se ``is_global=True`` OU ``organization_id is None``; admin + org +
    ``is_global=False`` = ADMIN-DE-ORG escopado à própria subárvore (persona MSSP/
    self-service). Outros papéis (viewer/operator/engineer) ganham escopo global só
    com ``is_global=True`` — o analista de SOC interno que monitora todos os
    clientes com as permissões do próprio papel, sem privilégio administrativo.
    """
    if user is None:
        return False
    if user.role == "admin":
        return bool(getattr(user, "is_global", False)) or user.organization_id is None
    # ``getattr`` defensivo: o shim de Service Account é transiente e pode não
    # ter o atributo materializado em cenários de teste antigos.
    return bool(getattr(user, "is_global", False))


def can_access_organization(user: models.AppUser, organization_id: int) -> bool:
    if has_global_scope(user):
        return True
    if user.organization_id is None:
        return False
    return user.organization_id == organization_id


def require_organization_access(user: models.AppUser, organization_id: int) -> None:
    if can_access_organization(user, organization_id):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="You do not have access to this organization",
    )


def filter_organizations_for_user(
    user: models.AppUser,
    organizations: Iterable[T],
    *,
    get_org_id,
) -> list[T]:
    if has_global_scope(user):
        return list(organizations)
    if user.organization_id is None:
        return []
    return [item for item in organizations if get_org_id(item) == user.organization_id]


def resolve_owner_org_id(
    user: models.AppUser,
    *,
    explicit_org_id: int | None = None,
    candidate_org_ids: Iterable[int | None] = (),
) -> int | None:
    """Resolve a org dona de um recurso recém-criado (query, agendamento…).

    - Usuário **escopado** (não-global): sempre a própria ``organization_id``
      — ignora ``explicit_org_id`` (não pode carimbar recurso em outra org).
    - Usuário **global** (admin/is_global): usa ``explicit_org_id`` se informado
      (admin direcionando a um tenant); senão deriva de ``candidate_org_ids``
      (ex.: orgs das integrações referenciadas) quando há UMA única org não-nula;
      caso contrário ``None`` (recurso global, visível só a global-scope).
    """
    if not has_global_scope(user):
        return user.organization_id
    if explicit_org_id is not None:
        return explicit_org_id
    distinct = {oid for oid in candidate_org_ids if oid is not None}
    return next(iter(distinct)) if len(distinct) == 1 else None


def scoped_organization_ids(user: models.AppUser) -> list[int] | None:
    """IDs de org visíveis ao usuário, ou ``None`` quando vê todas.

    ``None`` = sem filtro (escopo global). ``[]`` = não vê nenhuma org
    (usuário escopado mas sem ``organization_id`` atribuída).

    FLAT (1 org). Mantido como a fonte do fallback e do contrato histórico; a
    versão subtree-aware é :func:`accessible_org_ids`.
    """
    if has_global_scope(user):
        return None
    if user.organization_id is None:
        return []
    return [user.organization_id]


# ── Community default scope resolver — FLAT (single-org) ──
# The subtree-aware resolver (OrgClosure/OrgRoleBinding) is an Enterprise
# feature: it is provided by the Enterprise package and registered via
# ``ee_hooks.register_scope_resolver``. ``accessible_org_ids`` dispatches to that
# registered resolver when present (Enterprise) and to this FLAT default otherwise
# (Community), so a non-global Community user is scoped to ONLY its own organization.

def _default_scope_resolver(
    user: models.AppUser, session: Session
) -> set[int] | None:
    """FLAT Community scope: a non-global user sees only its own organization.

    Contract (mirrors :func:`accessible_org_ids`): ``set()`` when the user has no org,
    else ``{user.organization_id}``. The global short-circuit (``None``) is handled by
    ``accessible_org_ids`` BEFORE this is consulted. ``session`` is unused here but kept
    to satisfy the ``ee_hooks.ScopeResolver`` signature the Enterprise override shares.
    """
    if user.organization_id is None:
        return set()
    return {user.organization_id}


def accessible_org_ids(
    user: models.AppUser, session: Session
) -> set[int] | None:
    """IDs de org acessíveis ao usuário, ou ``None`` quando vê todas. Contrato:
      - ``None``    = sem filtro (escopo global — admin/is_global);
      - ``set()``   = não vê nenhuma org (escopado sem ``organization_id``);
      - ``set(...)``= as orgs visíveis.

    Seam de edição: o escopo do usuário NÃO-global é computado pelo
    resolver registrado via :func:`ee_hooks.register_scope_resolver` (o
    pacote Enterprise injeta a versão avançada), com fallback para
    :func:`_default_scope_resolver` (Core). O curto-circuito global (``None``) é
    independente de edição — ``is_global``/admin é higiene de identidade, não trava
    paga — e fica SEMPRE no Core. Sem resolver registrado (Community),
    o default é **FLAT**: o usuário não-global vê apenas a própria org — o *subtree*
    scope (ver a subárvore de orgs) é feature Enterprise, injetada via
    ``ee_hooks``. Isolamento fail-safe: na ausência do resolver, sub-expõe (flat),
    nunca vaza cross-org; a regressão é funcional (EE pago), capturada no ``/readyz``.
    """
    if has_global_scope(user):
        return None

    resolver = ee_hooks.get_scope_resolver()
    return (resolver or _default_scope_resolver)(user, session)


def can_access_subtree(
    user: models.AppUser,
    organization_id: int,
    session: Session | None = None,
) -> bool:
    """Versão subtree-aware de :func:`can_access_organization` — o
    gate ÚNICO de write-path (sem fallback FLAT). ``organization_id`` precisa estar
    na subárvore acessível do usuário; admin global bypassa.

    ``session`` é opcional: muitos call-sites de write-path estão em helpers sem
    sessão no escopo (``_ensure_integration_access`` etc.). Quando não informada,
    abre uma sessão curta SÓ para o lookup de closure (read-only, barato) — evita
    threadar sessão por toda a cadeia de helpers. Informe a sessão do request
    quando trivial para poupar o round-trip.
    """
    if has_global_scope(user):
        return True

    if session is not None:
        acc = accessible_org_ids(user, session)
    else:
        from ..db import database

        with database.SessionLocal() as _s:
            acc = accessible_org_ids(user, _s)
    # acc é None só para escopo global (já retornado acima); aqui é sempre um set.
    return acc is not None and organization_id in acc


def require_subtree_access(
    user: models.AppUser,
    organization_id: int,
    session: Session | None = None,
) -> None:
    """403 se ``organization_id`` não está na subárvore acessível do usuário.

    Substituta subtree-aware de :func:`require_organization_access`.
    Admin global mantém bypass. Flag OFF ⇒ FLAT (kill-switch reversível).
    """
    if can_access_subtree(user, organization_id, session):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="You do not have access to this organization",
    )


def require_global_scope(user: models.AppUser) -> None:
    """403 se não for admin GLOBAL. Para ações de PLATAFORMA que um admin-de-org
    NÃO pode executar (criar/deletar Organization).

    Flag OFF ⇒ todo admin é global ⇒ no-op (comportamento legado intacto).
    """
    if has_global_scope(user):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="This action requires a platform (global) administrator",
    )


def enforce_admin_delegation_scope(
    actor: models.AppUser,
    *,
    target_org_id: int | None,
    target_is_global: bool | None,
    session: Session | None = None,
) -> None:
    """Anti-escalonamento na criação/edição de usuário.

    No-op para actor GLOBAL (poder pleno; flag OFF ⇒ todo admin é global ⇒ sem
    efeito, suíte intacta). Para actor ESCOPADO (admin-de-org/MSP sob flag ON):
      1. **não pode conceder ``is_global=True``** (escalar para escopo global);
      2. **não pode criar/mover usuário para ``org=None``** (usuário de plataforma);
      3. a **org alvo deve estar na sua subárvore** (``require_subtree_access``).
    Nunca acima do próprio teto, nunca fora da própria subárvore.
    """
    if has_global_scope(actor):
        return
    if target_is_global:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A scoped administrator cannot grant global scope",
        )
    if target_org_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A scoped administrator must assign the user to an organization",
        )
    require_subtree_access(actor, target_org_id, session)
