"""_load_wazuh_loop_destination_ids casa o host do
MANAGER (não só indexer) — o syslog dest faz loopback para o manager, e na
topologia canônica manager ≠ indexer. Também trava _bare_host (normalização)."""

from __future__ import annotations

import json
import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.collectors import pipeline
from backend.app.collectors.pipeline import _bare_host, _load_wazuh_loop_destination_ids
from backend.app.db import models
from backend.app.db.database import Base


# ── _bare_host ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://wazuh-mgr:1514/path", "wazuh-mgr"),
        ("wazuh-mgr:514", "wazuh-mgr"),
        ("WAZUH-MGR", "wazuh-mgr"),
        ("user@wazuh-mgr:514", "wazuh-mgr"),
        ("[2001:db8::1]:514", "2001:db8::1"),   # IPv6 bracketed → tira porta
        ("2001:db8::1", "2001:db8::1"),         # IPv6 nu (2+ colons) → mantém
        ("", None),
        (None, None),
    ],
)
def test_bare_host(raw, expected):
    assert _bare_host(raw) == expected


# ── _load_wazuh_loop_destination_ids ─────────────────────────────────────


@pytest.fixture()
def seeded_sessionlocal(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(pipeline.database, "SessionLocal", Session)
    return Session


def test_matches_syslog_dest_pointing_at_manager(seeded_sessionlocal):
    """Topologia canônica manager≠indexer: um syslog dest cujo host == manager_url
    da integração Wazuh entra no loop-set (antes era omitido — só indexer)."""
    Session = seeded_sessionlocal
    with Session() as db:
        org = models.Organization(name="o", slug="o", is_active=True)
        db.add(org); db.flush()
        db.add(models.Integration(
            organization_id=org.id, name="wz", platform="wazuh",
            manager_url="https://wazuh-manager:55000",
            indexer_url="https://wazuh-indexer:9200",  # host DIFERENTE
        ))
        db.add(models.Destination(
            id="dest-syslog-1", name="to-manager", kind="syslog_rfc5424",
            config=json.dumps({"host": "wazuh-manager", "port": 514}),
        ))
        db.add(models.Destination(
            id="dest-syslog-2", name="to-splunk", kind="syslog_rfc5424",
            config=json.dumps({"host": "splunk.corp", "port": 514}),
        ))
        db.commit()

    loop_ids = _load_wazuh_loop_destination_ids({"dest-syslog-1", "dest-syslog-2"})
    assert "dest-syslog-1" in loop_ids   # casa o manager → suprimido
    assert "dest-syslog-2" not in loop_ids  # outro host → roteado normal


def test_empty_when_no_wazuh_integration(seeded_sessionlocal):
    Session = seeded_sessionlocal
    with Session() as db:
        db.add(models.Destination(id="d1", name="x", kind="syslog_rfc5424",
                                  config=json.dumps({"host": "anything"})))
        db.commit()
    assert _load_wazuh_loop_destination_ids({"d1"}) == frozenset()
