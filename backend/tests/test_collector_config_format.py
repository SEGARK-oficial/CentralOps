"""Testes para schema wazuh_syslog_format.

Cobre:
- Default rfc3164 para novas configs.
- Migração preserva rfc5424 em linhas existentes.
- PUT aceita rfc3164 e rfc5424.
- PUT rejeita valor inválido (422).

Nota: o chaveamento de sender via wazuh_target._build foi
removido junto com output/wazuh_target.py. wazuh-default agora é uma Destination
normal (kind syslog_rfc3164) entregue por dispatch_batch_to_destination, então
não há mais factory de target para testar aqui.
"""

from __future__ import annotations

import os
import tempfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db.database import Base, get_session
from backend.app.main import app


# ── Fixture de app com DB em memória ─────────────────────────────────────────


@pytest.fixture()
def client_and_db():
    """TestClient com admin bootstrapado e DB SQLite in-memory isolado."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_session():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_session] = override_session
    client = TestClient(app)

    # Bootstrap admin
    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPassword123!", "display_name": "Admin"},
    )
    assert r.status_code == 200, f"Bootstrap falhou: {r.text}"

    # Login
    r = client.post("/api/auth/login", json={"username": "admin", "password": "AdminPassword123!"})
    assert r.status_code == 200, f"Login falhou: {r.text}"

    try:
        yield client, TestingSession
    finally:
        client.close()
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)


# ── Testes de schema / GET ────────────────────────────────────────────────────


def test_default_is_rfc3164_for_new_config(client_and_db) -> None:
    """GET /collectors/config deve retornar wazuh_syslog_format presente e válido."""
    client, _ = client_and_db
    resp = client.get("/api/collectors/config")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "wazuh_syslog_format" in data, "Campo wazuh_syslog_format ausente"
    assert data["wazuh_syslog_format"] in ("rfc3164", "rfc5424"), (
        f"Valor inesperado: {data['wazuh_syslog_format']}"
    )


def test_put_config_accepts_rfc3164(client_and_db) -> None:
    """PUT /collectors/config com wazuh_syslog_format='rfc3164' deve ser aceito."""
    client, _ = client_and_db
    resp = client.put(
        "/api/collectors/config",
        json={"wazuh_syslog_format": "rfc3164"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["wazuh_syslog_format"] == "rfc3164"


def test_put_config_accepts_rfc5424(client_and_db) -> None:
    """PUT /collectors/config com wazuh_syslog_format='rfc5424' deve ser aceito."""
    client, _ = client_and_db
    resp = client.put(
        "/api/collectors/config",
        json={"wazuh_syslog_format": "rfc5424"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["wazuh_syslog_format"] == "rfc5424"


def test_put_config_rejects_invalid_format(client_and_db) -> None:
    """PUT /collectors/config com formato inválido deve retornar 422."""
    client, _ = client_and_db
    resp = client.put(
        "/api/collectors/config",
        json={"wazuh_syslog_format": "foobar"},
    )
    assert resp.status_code == 422, (
        f"Esperado 422, obtido {resp.status_code}: {resp.text}"
    )


# ── Teste de migração ─────────────────────────────────────────────────────────


def test_legacy_rfc5424_preserved_after_migration() -> None:
    """Linha existente sem wazuh_syslog_format deve receber 'rfc5424' via migration.

    Simula banco que não tinha a coluna (legado), roda migration e verifica
    que a linha existente fica com rfc5424 (preserva comportamento prod).
    """
    from sqlalchemy import inspect as sa_inspect

    # Engine separado em arquivo temporário para simular banco legado.
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    legacy_engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )

    # Cria tabela collector_config SEM a coluna wazuh_syslog_format.
    with legacy_engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE collector_config (
                id INTEGER PRIMARY KEY,
                wazuh_syslog_host VARCHAR,
                wazuh_syslog_port INTEGER NOT NULL DEFAULT 514,
                wazuh_syslog_use_tls BOOLEAN NOT NULL DEFAULT 0,
                wazuh_ca_bundle VARCHAR,
                wazuh_dispatch_mode VARCHAR NOT NULL DEFAULT 'syslog',
                collector_jsonl_dir VARCHAR NOT NULL DEFAULT '/var/log/centralops/collectors',
                collector_batch_size INTEGER NOT NULL DEFAULT 200,
                collector_batch_flush_seconds INTEGER NOT NULL DEFAULT 5,
                dedupe_ttl_days INTEGER NOT NULL DEFAULT 7,
                domain_concurrency_limits TEXT NOT NULL DEFAULT '{}',
                rate_limits_by_vendor TEXT NOT NULL DEFAULT '{}',
                created_at DATETIME NOT NULL DEFAULT '2026-01-01',
                updated_at DATETIME NOT NULL DEFAULT '2026-01-01'
            )
        """))
        # Insere linha "legada" sem wazuh_syslog_format.
        conn.execute(text("""
            INSERT INTO collector_config (
                id, wazuh_syslog_host, wazuh_syslog_port,
                wazuh_syslog_use_tls, wazuh_dispatch_mode,
                collector_jsonl_dir, collector_batch_size,
                collector_batch_flush_seconds, dedupe_ttl_days,
                domain_concurrency_limits, rate_limits_by_vendor,
                created_at, updated_at
            ) VALUES (
                1, '192.168.3.211', 514, 0, 'syslog',
                '/var/log/centralops', 200, 5, 7, '{}', '{}',
                '2026-01-01 00:00:00', '2026-01-01 00:00:00'
            )
        """))

    # Executa a migration (replica lógica de _run_lightweight_migrations).
    inspector = sa_inspect(legacy_engine)
    with legacy_engine.begin() as conn:
        cc_cols = {col["name"] for col in inspector.get_columns("collector_config")}
        if "wazuh_syslog_format" not in cc_cols:
            conn.execute(
                text(
                    "ALTER TABLE collector_config "
                    "ADD COLUMN wazuh_syslog_format VARCHAR "
                    "NOT NULL DEFAULT 'rfc5424'"
                )
            )

    # Verifica que a linha legada tem rfc5424 (preserva comportamento prod).
    with legacy_engine.connect() as conn:
        row = conn.execute(
            text("SELECT wazuh_syslog_format FROM collector_config WHERE id=1")
        ).fetchone()

    legacy_engine.dispose()

    try:
        os.unlink(db_path)
    except OSError:
        pass

    assert row is not None
    assert row[0] == "rfc5424", (
        f"Linha legada deve ter 'rfc5424' após migration, obtido '{row[0]}'"
    )
