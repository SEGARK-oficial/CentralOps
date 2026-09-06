"""W4.3: o subsistema legado de Threat Intel foi removido — e as tabelas
órfãs de instâncias antigas são derrubadas na migração leve.

O que existia: ``services/threat_intel`` (AbuseIPDB/OTX, cache que colapsava
MISS e UNKNOWN), 4 modelos, um seed singleton e uma entrada no
``reencrypt_secrets``. Tudo isso foi substituído por ``collectors/enrich``.
Teste de ausência sozinho passa por vacuidade; por isso o de migração
CRIA as tabelas legadas e prova que o boot as derruba — uma vez, com log.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.core.config import settings
from backend.app.db import database as _db_module
from backend.app.db import models

LEGACY_TABLES = (
    "threat_intel_config",
    "threat_intel_api_keys",
    "threat_intel_tokens",
    "threat_intel_queries",
)


@pytest.fixture
def fresh_engine(monkeypatch, tmp_path):
    db_path = tmp_path / "test.db"
    url = f"sqlite:///{db_path}"
    engine = create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    _db_module.Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(_db_module, "engine", engine)
    monkeypatch.setattr(_db_module, "SessionLocal", Session)
    monkeypatch.setattr(_db_module, "DATABASE_URL", url)
    yield engine
    _db_module.Base.metadata.drop_all(bind=engine)


def _create_legacy_tables(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE threat_intel_config (id INTEGER PRIMARY KEY, enabled BOOLEAN)"))
        conn.execute(text("CREATE TABLE threat_intel_api_keys (id INTEGER PRIMARY KEY, provider VARCHAR, api_key TEXT)"))
        conn.execute(text(
            "CREATE TABLE threat_intel_tokens (id INTEGER PRIMARY KEY, token_hash VARCHAR, "
            "created_by INTEGER REFERENCES app_users(id) ON DELETE SET NULL)"
        ))
        conn.execute(text(
            "CREATE TABLE threat_intel_queries (id INTEGER PRIMARY KEY, "
            "token_id INTEGER REFERENCES threat_intel_tokens(id) ON DELETE SET NULL)"
        ))
        conn.execute(text("INSERT INTO threat_intel_api_keys (provider, api_key) VALUES ('abuseipdb', 'enc::x')"))


def test_modelos_pacote_e_config_nao_existem_mais():
    for name in ("ThreatIntelConfig", "ThreatIntelApiKey", "ThreatIntelToken", "ThreatIntelQuery"):
        assert not hasattr(models, name), name
    assert not (set(LEGACY_TABLES) & set(_db_module.Base.metadata.tables))
    assert importlib.util.find_spec("backend.app.services.threat_intel") is None
    assert not hasattr(settings, "THREAT_INTEL_QUERY_RETENTION_DAYS")
    assert not any(t.startswith("threat_intel") for t, *_ in _db_module._EXPECTED_FK_ONDELETE_RULES)


def test_reencrypt_nao_conhece_mais_a_api_key_legada():
    scripts_dir = Path(__file__).resolve().parents[2] / "scripts"
    sys.path.insert(0, str(scripts_dir))
    try:
        mod = pytest.importorskip("reencrypt_secrets")
    finally:
        sys.path.remove(str(scripts_dir))
    for value in vars(mod).values():
        if isinstance(value, dict):
            assert "ThreatIntelApiKey" not in value


def test_migracao_derruba_tabelas_orfas_uma_vez_com_log(fresh_engine, caplog):
    _create_legacy_tables(fresh_engine)
    assert set(LEGACY_TABLES) <= set(inspect(fresh_engine).get_table_names())

    with caplog.at_level(logging.WARNING, logger="backend.app.db.database"):
        _db_module._run_lightweight_migrations()

    assert not (set(LEGACY_TABLES) & set(inspect(fresh_engine).get_table_names()))
    dropped = [
        getattr(r, "table", None) for r in caplog.records
        if getattr(r, "event", "") == "migration.legacy_threat_intel_dropped"
    ]
    # Ordem das FKs: queries antes de tokens.
    assert dropped == ["threat_intel_queries", "threat_intel_tokens", "threat_intel_api_keys", "threat_intel_config"]

    # Idempotente: 2º boot não tem o que derrubar e não loga.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="backend.app.db.database"):
        _db_module._run_lightweight_migrations()
    assert not any(getattr(r, "event", "") == "migration.legacy_threat_intel_dropped" for r in caplog.records)


def test_instancia_nova_nao_ganha_as_tabelas(fresh_engine, caplog):
    with caplog.at_level(logging.WARNING, logger="backend.app.db.database"):
        _db_module._run_lightweight_migrations()
    assert not (set(LEGACY_TABLES) & set(inspect(fresh_engine).get_table_names()))
    assert not any(getattr(r, "event", "") == "migration.legacy_threat_intel_dropped" for r in caplog.records)
