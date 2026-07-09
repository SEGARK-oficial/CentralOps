"""Migração leve: tabelas destinations/routes + seed wazuh-default.

Cenários (espelha test_lightweight_migrations_tenant_selection.py):
  * Tabelas ``destinations`` e ``routes`` criadas (Base.metadata.create_all).
  * Seed materializa ``wazuh-default`` a partir das colunas ``wazuh_*`` do
    ``collector_config`` — caminho Wazuh idêntico.
  * Seed idempotente (re-run não duplica).
  * O ``config`` do destino reflete o ``dispatch_mode``/host do collector_config.
  * Sem rotas → a tabela existe vazia (default = tudo p/ wazuh-default).
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import database as _db_module
from backend.app.db import models  # noqa: F401  — register Base tables


@pytest.fixture
def fresh_engine(monkeypatch, tmp_path):
    db_path = tmp_path / "test.db"
    url = f"sqlite:///{db_path}"
    engine = create_engine(
        url, connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    _db_module.Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(_db_module, "engine", engine)
    monkeypatch.setattr(_db_module, "SessionLocal", Session)
    monkeypatch.setattr(_db_module, "DATABASE_URL", url)
    yield engine
    _db_module.Base.metadata.drop_all(bind=engine)


def _insert_collector_config(engine, *, mode="syslog", host="wazuh.local", fmt="rfc3164"):
    """Pré-insere a linha collector_config id=1 com valores controlados,
    para que o seed do collector_config (que vem de env) seja pulado e o
    seed de destino leia ESTES valores."""
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO collector_config (
                    id, wazuh_syslog_host, wazuh_syslog_port, wazuh_syslog_use_tls,
                    wazuh_ca_bundle, wazuh_dispatch_mode, wazuh_syslog_format,
                    collector_jsonl_dir, collector_batch_size,
                    collector_batch_flush_seconds, dedupe_ttl_days,
                    domain_concurrency_limits, rate_limits_by_vendor,
                    created_at, updated_at
                ) VALUES (
                    1, :host, 514, 0, NULL, :mode, :fmt,
                    '/var/log/centralops/collectors', 200, 5, 7, '{}', '{}',
                    datetime('now'), datetime('now')
                )
                """
            ),
            {"host": host, "mode": mode, "fmt": fmt},
        )


def test_creates_destinations_and_routes_tables(fresh_engine):
    table_names = set(inspect(fresh_engine).get_table_names())
    assert "destinations" in table_names
    assert "routes" in table_names

    dst_cols = {c["name"] for c in inspect(fresh_engine).get_columns("destinations")}
    assert {
        "id", "name", "kind", "enabled", "config", "secret_ref",
        "delivery", "config_version", "organization_id",
        "created_at", "updated_at",
    }.issubset(dst_cols)


def test_seeds_wazuh_default_from_collector_config(fresh_engine):
    # mode="both" com syslog_format=rfc3164 (default) → kind=syslog_rfc3164
    # (fan-out syslog+jsonl é responsabilidade do roteador).
    _insert_collector_config(fresh_engine, mode="both", host="192.168.3.211")
    _db_module._run_lightweight_migrations()

    Session = sessionmaker(bind=fresh_engine)
    with Session() as db:
        rows = db.execute(text("SELECT * FROM destinations")).fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row.id == "wazuh-default"
        assert row.name == "Wazuh (default)"
        assert row.kind == "syslog_rfc3164"
        assert row.enabled in (1, True)
        cfg = json.loads(row.config)
        # Config agora usa campos vendor-neutros do SyslogRfc3164Config.
        assert cfg["host"] == "192.168.3.211"
        assert "dispatch_mode" not in cfg  # campo removido com o split
        assert row.config_version  # não vazio


def test_seed_is_idempotent(fresh_engine):
    _insert_collector_config(fresh_engine)
    _db_module._run_lightweight_migrations()
    _db_module._run_lightweight_migrations()
    with fresh_engine.connect() as conn:
        n = conn.execute(
            text("SELECT COUNT(*) AS n FROM destinations WHERE id='wazuh-default'")
        ).fetchone().n
    assert n == 1


def test_migration_seeds_no_catchall_route(fresh_engine):
    """Vendor-neutro: a migração NÃO seeda mais um catch-all hardcoded
    (``{} → [wazuh-default]``). Um SDPP vendor-neutro não presume um sink — o
    operador configura o roteamento (rota ``condition={}`` ou Destination
    ``is_default``); sem isso, não-roteados vão à DLQ/quarentena."""
    _insert_collector_config(fresh_engine)
    _db_module._run_lightweight_migrations()
    with fresh_engine.connect() as conn:
        rows = conn.execute(text("SELECT id FROM routes")).fetchall()
    assert len(rows) == 0  # nenhuma rota auto-seedada (vendor-neutro)
    # Idempotente: rodar de novo continua sem criar rota.
    _db_module._run_lightweight_migrations()
    with fresh_engine.connect() as conn:
        rows2 = conn.execute(text("SELECT id FROM routes")).fetchall()
    assert len(rows2) == 0


def test_seed_is_noop_without_collector_config_table(fresh_engine):
    """Guarda defensiva: sem a tabela ``collector_config``, o seed de destino
    é um no-op limpo (não explode, não cria wazuh-default órfão). Cenário
    impossível em produção (create_all sempre cria collector_config), mas o
    seed nunca deve assumir sua presença."""
    with fresh_engine.begin() as conn:
        conn.execute(text("DROP TABLE collector_config"))

    # Não deve levantar exceção.
    _db_module._run_lightweight_migrations()

    with fresh_engine.connect() as conn:
        n = conn.execute(
            text("SELECT COUNT(*) AS n FROM destinations WHERE id='wazuh-default'")
        ).fetchone().n
    assert n == 0


def test_seed_skipped_on_fresh_install_when_wazuh_not_configured(fresh_engine):
    """Vendor-neutro: numa instalação NOVA (host syslog NULL + modo default
    'syslog'), o seed NÃO materializa wazuh-default — a lista de destinos vem ZERADA.
    Um SDPP não presume um sink; o operador adiciona destinos explicitamente."""
    _insert_collector_config(fresh_engine, host=None, mode="syslog")
    _db_module._run_lightweight_migrations()

    with fresh_engine.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) AS n FROM destinations")).fetchone().n
    assert n == 0


def test_seed_materializes_when_jsonl_mode_explicit(fresh_engine):
    """Modo 'jsonl' é uma escolha EXPLÍCITA (não-default) → materializa o destino
    jsonl mesmo sem host syslog (preserva quem usa arquivo local)."""
    _insert_collector_config(fresh_engine, host=None, mode="jsonl")
    _db_module._run_lightweight_migrations()

    with fresh_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT id, kind FROM destinations WHERE id='wazuh-default'")
        ).fetchall()
    assert len(rows) == 1
    assert rows[0].kind == "jsonl"
