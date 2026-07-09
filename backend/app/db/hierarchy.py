"""Manutenção da hierarquia de tenants — árvore + dispatch de closure.

FONTE ÚNICA da materialização de ``parent_organization_id`` / ``root_id`` / ``depth``.
Após o carve-out open-core a **closure table**
(``org_closure``) e a lógica de subárvore paga (reseller/quota/seed) vivem no
pacote Enterprise — este módulo Core mantém só o esqueleto FLAT + o ponto de dispatch.

:func:`materialize_node` delega ao materializador registrado via
``ee_hooks.register_hierarchy_materializer`` (Enterprise: escreve a closure + deriva
root/depth do pai); sem materializador (Community) é **FLAT**: ``root_id=self``,
``depth=0``, ``parent=None``, sem closure (single-tenant). Usado por:
  - criação de Organization (``repository``, router ``organizations``) via
    :func:`assign_on_create` — mantém a árvore consistente em inserts novos;
  - boot (``database._run_schema_init``) via :func:`backfill_hierarchy`.

Como ``assign_on_create`` e ``backfill_hierarchy`` passam ambos por
:func:`materialize_node`, a edição correta (FLAT vs subtree) é aplicada uniformemente
sem que os call-sites saibam da edição.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import ee_hooks
from . import models

logger = logging.getLogger(__name__)


def resolve_parent_id(session: Session, org: models.Organization) -> Optional[int]:
    """Deriva o pai org→org a partir de ``partner_integration_id`` (o 2-hop legado):
    ``org.partner_integration_id → Integration.organization_id`` (a org do reseller).
    Nunca retorna o próprio id (anti self-loop)."""
    pii = org.partner_integration_id
    if not pii:
        return None
    integ = session.get(models.Integration, pii)
    if integ is None or not integ.organization_id:
        return None
    if integ.organization_id == org.id:
        return None
    return integ.organization_id


def materialize_node(session: Session, org_id: int, parent_id: Optional[int]) -> None:
    """Seta parent/root/depth de ``org_id``. Idempotente.

    Dispatch: se um materializador Enterprise está
    registrado (``ee_hooks.register_hierarchy_materializer``), delega a ele — ele escreve
    a closure e deriva ``root_id``/``depth`` do pai. Sem materializador (Community) é
    **FLAT**: ``root_id=self``, ``depth=0``, ``parent=None``, sem closure (single-tenant).
    """
    org = session.get(models.Organization, org_id)
    if org is None:
        return
    materializer = ee_hooks.get_hierarchy_materializer()
    if materializer is not None:
        materializer(session, org, parent_id)
        return
    # Community FLAT — as colunas de árvore ficam no Core (inertes), sem closure.
    org.parent_organization_id = None
    org.root_id = org_id
    org.depth = 0


def assign_on_create(session: Session, org: models.Organization) -> None:
    """Materializa a hierarquia de uma org recém-criada (idempotente).

    Resolve o pai pelo ``partner_integration_id`` e, se houver pai (Enterprise), marca-o
    como ``reseller``. Requer ``org.id`` atribuído — dá ``flush`` se necessário. NÃO
    commita: o caller controla a transação. Em Community ``resolve_parent_id`` é ``None``
    (sem integração partner) → ``materialize_node`` é FLAT.
    """
    if org.id is None:
        session.flush()
    parent_id = resolve_parent_id(session, org)
    materialize_node(session, org.id, parent_id)
    if parent_id is not None:
        parent = session.get(models.Organization, parent_id)
        if parent is not None and parent.kind != "reseller":
            parent.kind = "reseller"
    session.flush()


def needs_backfill(session: Session) -> bool:
    """True se existe alguma org sem ``root_id`` (não materializada)."""
    return (
        session.execute(
            select(models.Organization.id)
            .where(models.Organization.root_id.is_(None))
            .limit(1)
        ).first()
        is not None
    )


def backfill_hierarchy(session: Session) -> int:
    """Materializa a árvore para TODAS as orgs a partir do estado legado.

    Idempotente: pode rodar a cada boot (guardado por :func:`needs_backfill`). Deriva o
    pai do ``partner_integration_id``, processa em ordem topológica (raízes primeiro) e
    delega cada nó a :func:`materialize_node` (FLAT em Community; subtree+closure quando o
    materializador Enterprise está registrado). Marca ``kind='reseller'`` nas orgs que
    possuem uma ``Integration(kind='partner')``. Devolve nº de orgs.
    """
    orgs = list(session.execute(select(models.Organization)).scalars().all())
    if not orgs:
        return 0

    parent_of = {o.id: resolve_parent_id(session, o) for o in orgs}
    reseller_ids = set(
        session.execute(
            select(models.Integration.organization_id).where(
                models.Integration.kind == "partner",
                models.Integration.organization_id.isnot(None),
            )
        ).scalars().all()
    )

    done: set[int] = set()
    progress = True
    while progress:
        progress = False
        for o in orgs:
            if o.id in done:
                continue
            pid = parent_of[o.id]
            if pid is None or pid in done:
                materialize_node(session, o.id, pid)
                done.add(o.id)
                progress = True

    # Sobras (ciclo ou pai dangling) → raiz, defensivo.
    for o in orgs:
        if o.id not in done:
            logger.warning("hierarchy: org %s não resolvida (ciclo/dangling) — raiz", o.id)
            materialize_node(session, o.id, None)
            done.add(o.id)

    for o in orgs:
        o.kind = "reseller" if o.id in reseller_ids else (o.kind or "customer")

    session.flush()
    return len(orgs)
