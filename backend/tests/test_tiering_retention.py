"""enforce_destination_retention.

Prova o enforcement de retenção por destino de armazenamento: só destinos
habilitados, com a capability ``retention`` (S3) e ``retention_days > 0``, são
podados; os demais (sem a cap, desabilitados, ou retention_days=0) são pulados.
``registry.build`` é stubado por um conector fake — o teste não toca aioboto3.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.collectors import retention_tasks
from backend.app.collectors.output.destinations import registry as dest_registry
from backend.app.db import database, models
from backend.app.db.database import Base


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    original = database.SessionLocal
    database.SessionLocal = Session  # type: ignore[assignment]
    yield Session
    database.SessionLocal = original  # type: ignore[assignment]
    Base.metadata.drop_all(bind=engine)


class _FakeConn:
    def __init__(self) -> None:
        self.pruned_with: int | None = None
        self.closed = False

    async def prune_expired(self, days: int) -> int:
        self.pruned_with = days
        return 7

    async def close(self) -> None:
        self.closed = True


def _seed_dest(session, *, kind: str, delivery: str, enabled: bool = True) -> str:
    dest_id = str(uuid4())
    session.add(
        models.Destination(
            id=dest_id,
            name=f"dest-{dest_id[:6]}",
            kind=kind,
            enabled=enabled,
            config="{}",
            secret_ref=None,
            delivery=delivery,
            config_version="v1",
            organization_id=None,
        )
    )
    session.commit()
    return dest_id


def test_prunes_only_retention_capable_with_positive_days(db_session, monkeypatch):
    session = db_session()
    # (a) S3 com retention → DEVE podar.
    s3_id = _seed_dest(session, kind="s3", delivery='{"retention_days": 30}')
    # (b) S3 com retention_days=0 → pula (retenção infinita).
    _seed_dest(session, kind="s3", delivery='{"retention_days": 0}')
    # (c) Sentinel (sem capability "retention") → pula (retenção é do lado dele).
    _seed_dest(session, kind="sentinel", delivery='{"retention_days": 30}')
    # (d) S3 desabilitado → pula.
    _seed_dest(session, kind="s3", delivery='{"retention_days": 30}', enabled=False)
    session.close()

    conns: list[_FakeConn] = []

    def _fake_build(cfg, secrets):
        c = _FakeConn()
        conns.append(c)
        return c

    monkeypatch.setattr(dest_registry, "build", _fake_build)

    result = retention_tasks.enforce_destination_retention.run()

    # Só o destino (a) foi podado.
    assert result == {s3_id: 7}
    assert len(conns) == 1
    assert conns[0].pruned_with == 30
    assert conns[0].closed is True


def test_no_candidates_is_noop(db_session, monkeypatch):
    session = db_session()
    _seed_dest(session, kind="sentinel", delivery='{"retention_days": 30}')
    session.close()

    called = False

    def _fake_build(cfg, secrets):  # pragma: no cover — não deve ser chamado
        nonlocal called
        called = True
        return _FakeConn()

    monkeypatch.setattr(dest_registry, "build", _fake_build)
    assert retention_tasks.enforce_destination_retention.run() == {}
    assert called is False


def test_one_destination_failure_does_not_break_others(db_session, monkeypatch):
    session = db_session()
    ok_id = _seed_dest(session, kind="s3", delivery='{"retention_days": 10}')
    bad_id = _seed_dest(session, kind="s3", delivery='{"retention_days": 20}')
    session.close()

    def _fake_build(cfg, secrets):
        if cfg.destination_id == bad_id:
            raise RuntimeError("aioboto3 ausente")
        return _FakeConn()

    monkeypatch.setattr(dest_registry, "build", _fake_build)
    result = retention_tasks.enforce_destination_retention.run()
    assert result[ok_id] == 7
    assert result[bad_id] == -1  # falha isolada, marcada
