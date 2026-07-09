"""Isolamento multi-tenant event-level (critério de aceite).

Este é o teste-portão: o tenant A escreve nas TRÊS superfícies que
vazavam cross-tenant (sample_reservoir, audit_buffer, UnknownField/drift) e o
tenant B lê ZERO de A — com controle positivo (B vê os PRÓPRIOS dados, provando
que o teste não passa vazio).

As três superfícies, no nível onde cada uma é testável de forma limpa:
- sample_reservoir / audit_buffer: Redis (fakeredis) — isolamento por chave.
- UnknownField/drift: DB (SQLite in-memory) — isolamento por organization_id.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.collectors import audit_buffer
from backend.app.collectors.normalize import drift, sample_reservoir
from backend.app.db import database as db_module
from backend.app.db import models
from backend.app.db.database import Base

ORG_A = 1
ORG_B = 2


@pytest.fixture
def isolated_db(monkeypatch):
    """DB SQLite in-memory para a superfície de drift (UnknownField)."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine)
    monkeypatch.setattr(db_module, "SessionLocal", TestingSession)
    yield engine


@pytest.mark.asyncio
async def test_s1_gate_no_cross_tenant_read_across_all_surfaces(redis_client, isolated_db):
    vendor, event_type = "sophos", "sophos.alert"

    # ── Tenant A escreve nas 3 superfícies ────────────────────────────
    # 1. sample_reservoir
    await sample_reservoir.push(
        redis_client, ORG_A, vendor, event_type, {"secret_ip": "10.0.0.1"}
    )
    # 2. audit_buffer
    await audit_buffer.record_batch(
        redis_client,
        [{"id": "a-evt", "_centralops": {"organization_id": ORG_A, "vendor": vendor}}],
        ORG_A,
    )
    # 3. UnknownField/drift
    drift.record_unknown_fields(
        vendor=vendor, event_type=event_type, organization_id=ORG_A,
        raw={"alert": {"newFieldA": "x"}}, consumed_paths=[],
    )

    # ── Tenant B lê ZERO de A (mesmo vendor — o vetor de vazamento) ────
    assert await sample_reservoir.peek(redis_client, ORG_B, vendor, event_type) == []
    assert await sample_reservoir.size(redis_client, ORG_B, vendor, event_type) == 0
    assert await audit_buffer.read_recent(redis_client, ORG_B, limit=100) == []
    with db_module.SessionLocal() as db:
        b_fields = db.scalars(
            select(models.UnknownField).where(
                models.UnknownField.organization_id == ORG_B
            )
        ).all()
        assert b_fields == [], "tenant B NÃO pode ver drift do tenant A"

    # ── Controle positivo: B vê os PRÓPRIOS dados ─────────────────────
    await sample_reservoir.push(
        redis_client, ORG_B, vendor, event_type, {"b_only": "yes"}
    )
    await audit_buffer.record_batch(
        redis_client,
        [{"id": "b-evt", "_centralops": {"organization_id": ORG_B, "vendor": vendor}}],
        ORG_B,
    )
    drift.record_unknown_fields(
        vendor=vendor, event_type=event_type, organization_id=ORG_B,
        raw={"alert": {"newFieldB": "y"}}, consumed_paths=[],
    )

    b_samples = await sample_reservoir.peek(redis_client, ORG_B, vendor, event_type)
    assert [s.get("b_only") for s in b_samples] == ["yes"]
    b_audit = await audit_buffer.read_recent(redis_client, ORG_B, limit=100)
    assert [e["event"]["id"] for e in b_audit] == ["b-evt"]
    with db_module.SessionLocal() as db:
        b_fields = db.scalars(
            select(models.UnknownField).where(
                models.UnknownField.organization_id == ORG_B
            )
        ).all()
        assert {f.field_path for f in b_fields} == {"alert.newFieldB"}

    # ── A continua vendo só o de A (não contaminado por B) ────────────
    a_samples = await sample_reservoir.peek(redis_client, ORG_A, vendor, event_type)
    assert [s.get("secret_ip") for s in a_samples] == ["10.0.0.1"]
    with db_module.SessionLocal() as db:
        a_fields = db.scalars(
            select(models.UnknownField).where(
                models.UnknownField.organization_id == ORG_A
            )
        ).all()
        assert {f.field_path for f in a_fields} == {"alert.newFieldA"}


@pytest.mark.asyncio
async def test_s1_gate_unknownfield_unique_constraint_allows_same_field_per_org(
    redis_client, isolated_db
):
    """O MESMO (vendor, event_type, field_path) pode coexistir em orgs
    diferentes — a unicidade agora inclui organization_id."""
    vendor, event_type = "sophos", "sophos.alert"
    raw = {"alert": {"sharedField": "v"}}
    drift.record_unknown_fields(
        vendor=vendor, event_type=event_type, organization_id=ORG_A,
        raw=raw, consumed_paths=[],
    )
    drift.record_unknown_fields(
        vendor=vendor, event_type=event_type, organization_id=ORG_B,
        raw=raw, consumed_paths=[],
    )
    with db_module.SessionLocal() as db:
        rows = db.scalars(
            select(models.UnknownField).where(
                models.UnknownField.field_path == "alert.sharedField"
            )
        ).all()
        # Duas linhas: uma por org (não colidiram na unique constraint).
        assert {r.organization_id for r in rows} == {ORG_A, ORG_B}
