"""Testes do CLI bulk-approve / list-selections.

Cobre:
  * dry-run não grava nada e imprime resumo
  * apply muda state e materialização é tentada (mockada)
  * list-selections respeita filtro de state
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.collectors import cli as cli_module
from backend.app.db import database as _db_module
from backend.app.db import models  # noqa: F401  — register tables
from backend.app.db.database import Base
from backend.app.db.repository import IntegrationTenantSelectionRepository


@pytest.fixture()
def db_session(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(_db_module, "SessionLocal", TestingSession)
    db = TestingSession()
    yield db
    db.close()


@pytest.fixture()
def partner_with_pending(db_session):
    """Partner integration + 3 pending selections + 1 approved."""
    org = models.Organization(name="Test MSP", slug="test-msp")
    db_session.add(org)
    db_session.commit()
    db_session.refresh(org)

    partner = models.Integration(
        organization_id=org.id,
        platform="sophos",
        kind="partner",
        name="Sophos Partner",
        is_active=True,
        auto_managed=False,
        auto_approve_new_tenants=False,
    )
    db_session.add(partner)
    db_session.commit()
    db_session.refresh(partner)

    sel_repo = IntegrationTenantSelectionRepository(db_session)
    for ext_id, name in [
        ("tenant-aaa", "Tenant A"),
        ("tenant-bbb", "Tenant B"),
        ("tenant-ccc", "Tenant C"),
    ]:
        sel_repo.upsert_snapshot(
            parent_id=partner.id,
            external_id=ext_id,
            name_snapshot=name,
            region_snapshot="EU",
            api_host_snapshot="api-eu03.central.sophos.com",
            last_seen_at=datetime.utcnow(),
            default_state="pending",
        )
    sel_repo.upsert_snapshot(
        parent_id=partner.id,
        external_id="tenant-zzz",
        name_snapshot="Already approved",
        region_snapshot="US",
        api_host_snapshot="api-us03.central.sophos.com",
        last_seen_at=datetime.utcnow(),
        default_state="approved",
    )
    return partner


def test_bulk_approve_dry_run_does_not_write(
    db_session, partner_with_pending, capsys, tmp_path
):
    """Dry-run (sem --apply) NÃO modifica state no DB."""
    csv_path = tmp_path / "ids.csv"
    csv_path.write_text("external_id\ntenant-aaa\ntenant-bbb\n")

    rc = cli_module.main(
        [
            "bulk-approve",
            "--partner-id",
            str(partner_with_pending.id),
            "--csv",
            str(csv_path),
        ]
    )
    assert rc == 0

    sel_repo = IntegrationTenantSelectionRepository(db_session)
    rows = {
        r.external_id: r.state
        for r in sel_repo.list(partner_with_pending.id, state="pending")
    }
    # Continuam pending — dry-run não escreveu.
    assert rows.get("tenant-aaa") == "pending"
    assert rows.get("tenant-bbb") == "pending"

    captured = capsys.readouterr()
    assert "DRY-RUN" in captured.err
    assert "mudará de estado" in captured.err


def _stub_applier(captured):
    """An Enterprise tenant-selection applier double — records calls and returns
    truthful counts by state, without touching RedBeat/Iris."""

    def _applier(db, integration, selections, state):
        captured["calls"].append((state, len(selections)))
        n = len(selections)
        return {
            "materialized": n if state == "approved" else 0,
            "deactivated": n if state == "excluded" else 0,
            "pending": 0,
            "errors": [],
        }

    return _applier


def test_bulk_approve_apply_without_applier_is_enterprise_required(
    db_session, partner_with_pending, capsys, tmp_path
):
    """Open-core: in Community there is no Enterprise applier, so --apply
    exits 1 and — being atomic — does NOT change selection state."""
    csv_path = tmp_path / "ids.csv"
    csv_path.write_text("tenant-aaa\ntenant-bbb\n")

    rc = cli_module.main(
        [
            "bulk-approve",
            "--partner-id",
            str(partner_with_pending.id),
            "--csv",
            str(csv_path),
            "--apply",
        ]
    )
    assert rc == 1
    assert "Enterprise" in capsys.readouterr().err

    sel_repo = IntegrationTenantSelectionRepository(db_session)
    rows = {r.external_id: r.state for r in sel_repo.list(partner_with_pending.id)}
    assert rows["tenant-aaa"] == "pending"  # unchanged (atomic)
    assert rows["tenant-bbb"] == "pending"


def test_bulk_approve_apply_delegates_to_applier(
    db_session, partner_with_pending, capsys, tmp_path
):
    """--apply muda state pending -> approved e delega a materialização ao applier EE."""
    from backend.app.core import ee_hooks

    captured = {"calls": []}
    ee_hooks.register_tenant_selection_applier(_stub_applier(captured))  # conftest resets

    csv_path = tmp_path / "ids.csv"
    csv_path.write_text("tenant-aaa\ntenant-bbb\n")

    rc = cli_module.main(
        [
            "bulk-approve",
            "--partner-id",
            str(partner_with_pending.id),
            "--csv",
            str(csv_path),
            "--apply",
        ]
    )
    assert rc == 0
    assert captured["calls"] == [("approved", 2)]

    sel_repo = IntegrationTenantSelectionRepository(db_session)
    rows = {r.external_id: r.state for r in sel_repo.list(partner_with_pending.id)}
    assert rows["tenant-aaa"] == "approved"
    assert rows["tenant-bbb"] == "approved"
    assert rows["tenant-ccc"] == "pending"  # não estava no CSV
    assert rows["tenant-zzz"] == "approved"  # já estava

    out = capsys.readouterr().out
    assert '"processed": 2' in out
    assert '"materialized": 2' in out


def test_bulk_approve_all_pending_delegates(db_session, partner_with_pending, capsys):
    """--all-pending pega todos pending e delega ao applier EE."""
    from backend.app.core import ee_hooks

    captured = {"calls": []}
    ee_hooks.register_tenant_selection_applier(_stub_applier(captured))

    rc = cli_module.main(
        [
            "bulk-approve",
            "--partner-id",
            str(partner_with_pending.id),
            "--all-pending",
            "--apply",
        ]
    )
    assert rc == 0
    assert captured["calls"] == [("approved", 3)]  # 3 pending originais

    sel_repo = IntegrationTenantSelectionRepository(db_session)
    assert sel_repo.list(partner_with_pending.id, state="pending") == []


def test_bulk_approve_excluded_state_delegates(
    db_session, partner_with_pending, capsys, tmp_path
):
    """--state excluded delega ao applier com state='excluded'."""
    from backend.app.core import ee_hooks

    captured = {"calls": []}
    ee_hooks.register_tenant_selection_applier(_stub_applier(captured))

    csv_path = tmp_path / "ids.csv"
    csv_path.write_text("tenant-zzz\n")

    rc = cli_module.main(
        [
            "bulk-approve",
            "--partner-id",
            str(partner_with_pending.id),
            "--csv",
            str(csv_path),
            "--state",
            "excluded",
            "--apply",
        ]
    )
    assert rc == 0
    assert captured["calls"] == [("excluded", 1)]


def test_bulk_approve_rejects_non_partner(db_session, capsys):
    """Integração que não é partner aborta com erro."""
    org = models.Organization(name="Org", slug="org-x")
    db_session.add(org)
    db_session.commit()
    db_session.refresh(org)

    tenant_int = models.Integration(
        organization_id=org.id,
        platform="sophos",
        kind="tenant",
        name="single tenant",
        is_active=True,
    )
    db_session.add(tenant_int)
    db_session.commit()
    db_session.refresh(tenant_int)

    with pytest.raises(SystemExit) as exc_info:
        cli_module.main(
            [
                "bulk-approve",
                "--partner-id",
                str(tenant_int.id),
                "--all-pending",
            ]
        )
    assert exc_info.value.code == 2

    captured = capsys.readouterr()
    assert "expected partner|organization" in captured.err


def test_list_selections_filters_by_state(
    db_session, partner_with_pending, capsys
):
    rc = cli_module.main(
        [
            "list-selections",
            "--partner-id",
            str(partner_with_pending.id),
            "--state",
            "approved",
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "tenant-zzz" in captured.out
    assert "tenant-aaa" not in captured.out  # é pending


def test_csv_dedup_and_strip(tmp_path):
    p = tmp_path / "ids.csv"
    p.write_text(
        "external_id\n"
        "tenant-aaa\n"
        "  tenant-bbb  \n"
        "tenant-aaa\n"  # dup
        "\n"  # vazia
        "tenant-ccc\n"
    )
    ids = cli_module._read_csv_external_ids(str(p))
    assert ids == ["tenant-aaa", "tenant-bbb", "tenant-ccc"]
