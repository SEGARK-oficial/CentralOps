"""O esqueleto FLAT do Core (pós open-core carve-out).

A closure table (``org_closure``) + a materialização de subárvore são Enterprise: foram
para o ``centralops_ee``. O Core agora só mantém o shim FLAT +
o dispatch: ``hierarchy.materialize_node`` seta ``root_id=self``, ``depth=0`` e NÃO escreve
closure quando NENHUM materializador está registrado (Community); quando o materializador
Enterprise está registrado, delega a ele. Este arquivo cobre o lado Community + o dispatch;
a materialização real de subárvore/closure é testada na suíte do ``centralops_ee``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.core import ee_hooks
from backend.app.db import hierarchy, models
from backend.app.db.database import Base


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with Session() as s:
        yield s


@pytest.fixture(autouse=True)
def _reset_materializer():
    ee_hooks.reset_hierarchy_materializer()
    yield
    ee_hooks.reset_hierarchy_materializer()


def _org(session, name, **kw):
    o = models.Organization(name=name, slug=name.lower().replace(" ", "-"), **kw)
    session.add(o)
    session.flush()
    return o


# ── Community FLAT (sem materializador registrado) ────────────────────────────

def test_root_org_is_its_own_root(session):
    o = _org(session, "Acme")
    hierarchy.materialize_node(session, o.id, None)
    session.flush()
    assert o.parent_organization_id is None
    assert o.root_id == o.id
    assert o.depth == 0


def test_community_child_is_flat_not_subtree(session):
    """Sem materializador EE, um "filho" NÃO herda root/depth do pai — é FLAT (root=self,
    depth=0). O subtree é feature Enterprise (closure vive no EE)."""
    root = _org(session, "MSP")
    hierarchy.materialize_node(session, root.id, None)
    child = _org(session, "Client A")
    hierarchy.materialize_node(session, child.id, root.id)  # parent_id ignorado em FLAT
    session.flush()
    assert child.parent_organization_id is None
    assert child.root_id == child.id
    assert child.depth == 0


def test_materialize_node_dispatches_to_registered_materializer(session):
    """Com um materializador registrado (o EE faz isso em activate/worker),
    ``materialize_node`` delega a ele — prova o seam."""
    seen: list[tuple[int, int | None]] = []

    def _fake_materializer(sess, org, parent_id):
        seen.append((org.id, parent_id))
        org.root_id = parent_id or org.id
        org.depth = 0 if parent_id is None else 1

    ee_hooks.register_hierarchy_materializer(_fake_materializer)
    root = _org(session, "R")
    hierarchy.materialize_node(session, root.id, None)
    child = _org(session, "C")
    hierarchy.materialize_node(session, child.id, root.id)
    session.flush()
    assert seen == [(root.id, None), (child.id, root.id)]
    assert child.root_id == root.id and child.depth == 1  # o materializador decidiu


# ── Backfill FLAT + marcação de reseller (colunas Core, sem closure) ───────────

def test_backfill_flat_and_marks_reseller_kind(session):
    reseller = _org(session, "Reseller")
    partner_integ = models.Integration(
        name="partner", organization_id=reseller.id, platform="sophos", kind="partner"
    )
    session.add(partner_integ)
    session.flush()
    child = _org(
        session,
        "Child",
        partner_integration_id=partner_integ.id,
        external_provider="sophos",
        external_id="t1",
        auto_managed=True,
    )

    assert reseller.root_id is None and child.root_id is None
    assert hierarchy.needs_backfill(session) is True

    n = hierarchy.backfill_hierarchy(session)
    session.flush()

    assert n == 2
    # FLAT: cada org é sua própria raiz (a subárvore é EE).
    assert reseller.root_id == reseller.id and reseller.depth == 0
    assert child.root_id == child.id and child.depth == 0
    # a marcação de kind (coluna Core) permanece: quem tem Integration(kind=partner) é reseller.
    assert reseller.kind == "reseller"
    assert child.kind == "customer"
    assert hierarchy.needs_backfill(session) is False


def test_assign_on_create_marks_parent_reseller_flat(session):
    reseller = _org(session, "R")
    hierarchy.materialize_node(session, reseller.id, None)
    partner_integ = models.Integration(
        name="p", organization_id=reseller.id, platform="sophos", kind="partner"
    )
    session.add(partner_integ)
    session.flush()
    child = _org(session, "C", partner_integration_id=partner_integ.id)

    hierarchy.assign_on_create(session, child)
    session.flush()

    # o pai é marcado reseller (kind é coluna Core), mas o filho é FLAT (root=self, depth=0).
    assert reseller.kind == "reseller"
    assert child.root_id == child.id and child.depth == 0


def test_is_global_migration_semantics_unaffected(session):
    o = _org(session, "Solo")
    hierarchy.assign_on_create(session, o)
    session.flush()
    assert o.root_id == o.id and o.kind == "customer"
