"""Testes do export CSV de histórico de busca — GET /api/search/history/result/{id}/csv.

Bug de campo: a busca federada (opensearch_dsl) persiste ``result_json`` como uma
LISTA no topo (array de N itens com dicts aninhados), mas o handler antigo fazia
``json.loads(...).get("items")`` — ``list.get`` lançava ``AttributeError``, engolido
por ``except Exception`` → 404 "Nenhum dado disponível" (o usuário via o erro genérico
de download). Além disso a serialização manual (``",".join(json.dumps(...))``) quebrava
colunas com dicts aninhados e usava só as chaves do 1º item.

Cobre:
- helpers puros ``_extract_items`` / ``_rows_to_csv`` (LISTA aninhada + dict{items|results}
  + união estável de cabeçalho + quoting via ``csv``);
- rota: LISTA aninhada → 200 CSV correto; dict → 200; vazio → 404; sem result_json → 404;
  expirado → 410.
"""

from __future__ import annotations

import csv
import io
import json
import os
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

from backend.app.core import auth as app_auth
from backend.app.core.config import settings
from backend.app.db import models
from backend.app.db.database import Base, get_session
from backend.app.main import app
from backend.app.routers import results as results_router


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def db_and_client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_get_session():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    class _FakeAdmin:
        # Admin/global → get_by_search_id não filtra por user_id nem org.
        id = 1
        role = "admin"
        is_global = True
        organization_id = None
        username = "admin"

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[app_auth.require_authenticated_user] = lambda: _FakeAdmin()
    client = TestClient(app)

    yield client, TestingSessionLocal

    client.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


def _seed_result(
    SessionLocal,
    *,
    search_id: str,
    result_json: str | None,
    created_at: datetime | None = None,
) -> None:
    db = SessionLocal()
    try:
        sr = models.SearchResult(
            search_id=search_id,
            integration_id=None,
            user_id=1,
            platform="wazuh",
            statement='{"query": {"match_all": {}}}',
            table="wazuh-alerts-*",
            from_ts="2026-07-01T00:00:00",
            to_ts="2026-07-02T00:00:00",
            status="finished",
            engine="query",
            language="opensearch_dsl",
            result_json=result_json,
            created_at=created_at or datetime.utcnow(),
        )
        db.add(sr)
        db.commit()
    finally:
        db.close()


# Shape real (reduzido) de um hit de busca federada Wazuh: dicts ANINHADOS.
_FEDERATED_ITEMS = [
    {
        "agent": {"name": "web-01", "id": "001"},
        "rule": {"level": 5, "description": "Login, com vírgula", "id": "5715"},
        "data": {"srcip": "10.0.0.1"},
        "full_log": 'sshd: line with "quotes" and, comma',
        "id": "1",
        "timestamp": "2026-07-01T10:00:00Z",
        "GeoLocation": {"country_name": "Brazil"},
    },
    {
        # Item HETEROGÊNEO: sem 'data'/'GeoLocation', mas com 'manager' e 'decoder'.
        "agent": {"name": "db-02", "id": "002"},
        "rule": {"level": 10, "description": "Multi\nline\ndesc", "id": "5716"},
        "manager": {"name": "mgr-1"},
        "decoder": {"name": "sshd"},
        "id": "2",
        "timestamp": "2026-07-01T11:00:00Z",
    },
]


# ── Helpers puros ─────────────────────────────────────────────────────


def test_extract_items_accepts_top_level_list():
    payload = json.dumps(_FEDERATED_ITEMS)
    items = results_router._extract_items(payload)
    assert isinstance(items, list) and len(items) == 2


def test_extract_items_accepts_dict_items_and_results():
    assert len(results_router._extract_items(json.dumps({"items": [{"a": 1}]}))) == 1
    assert len(results_router._extract_items(json.dumps({"results": [{"a": 1}, {"a": 2}]}))) == 2


def test_extract_items_empty_and_corrupt():
    assert results_router._extract_items("[]") == []
    assert results_router._extract_items(json.dumps({"items": []})) == []
    assert results_router._extract_items("not-json{") == []  # parse falha → sem dados
    assert results_router._extract_items(json.dumps({"other": 1})) == []  # dict sem items/results


def test_rows_to_csv_header_is_stable_union():
    """Cabeçalho = união das chaves NA ORDEM de 1ª aparição, não só do 1º item.
    'manager'/'decoder' (só no item 2) devem entrar; nada some."""
    csv_text = results_router._rows_to_csv(_FEDERATED_ITEMS)
    rows = list(csv.reader(io.StringIO(csv_text)))
    header = rows[0]
    # Chaves do 1º item vêm primeiro, depois as novas do 2º item.
    assert header[: 7] == [
        "agent", "rule", "data", "full_log", "id", "timestamp", "GeoLocation",
    ]
    assert "manager" in header and "decoder" in header
    assert len(rows) == 3  # header + 2 itens


def test_rows_to_csv_quotes_and_nested_json():
    """Dicts aninhados → JSON compacto na célula; vírgulas/aspas/quebras NÃO
    quebram colunas (csv.reader round-trip reconstrói exatamente)."""
    csv_text = results_router._rows_to_csv(_FEDERATED_ITEMS)
    reader = csv.DictReader(io.StringIO(csv_text))
    parsed = list(reader)

    # Célula aninhada = JSON compacto e parseável de volta.
    assert json.loads(parsed[0]["agent"]) == {"name": "web-01", "id": "001"}
    assert json.loads(parsed[0]["GeoLocation"]) == {"country_name": "Brazil"}
    # full_log com vírgula E aspas preservado intacto.
    assert parsed[0]["full_log"] == 'sshd: line with "quotes" and, comma'
    # rule com quebra de linha preservada.
    assert json.loads(parsed[1]["rule"])["description"] == "Multi\nline\ndesc"
    # Coluna ausente no item → célula vazia (não perde alinhamento).
    assert parsed[1]["data"] == ""
    assert parsed[1]["GeoLocation"] == ""
    assert parsed[0]["manager"] == ""


def test_rows_to_csv_handles_scalar_items():
    """Robustez: itens escalares (não-dict) caem na coluna sintética 'value'."""
    csv_text = results_router._rows_to_csv(["alpha", "beta"])
    rows = list(csv.reader(io.StringIO(csv_text)))
    assert rows[0] == ["value"]
    assert rows[1] == ["alpha"] and rows[2] == ["beta"]


# ── Rota ──────────────────────────────────────────────────────────────


def test_csv_route_top_level_list_returns_200(db_and_client):
    """REGRESSÃO do bug: result_json = LISTA no topo (busca federada) → 200 CSV,
    não mais 404 'Nenhum dado disponível'."""
    client, SessionLocal = db_and_client
    _seed_result(SessionLocal, search_id="fed-list", result_json=json.dumps(_FEDERATED_ITEMS))

    r = client.get("/api/search/history/result/fed-list/csv")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert 'attachment; filename="fed-list.csv"' in r.headers.get("content-disposition", "")

    rows = list(csv.reader(io.StringIO(r.text)))
    assert len(rows) == 3  # header + 2 hits
    assert "agent" in rows[0] and "manager" in rows[0]


def test_csv_route_dict_shape_still_works(db_and_client):
    """Compat: result_json = dict {items|results} → continua funcionando."""
    client, SessionLocal = db_and_client
    _seed_result(
        SessionLocal,
        search_id="dict-shape",
        result_json=json.dumps({"items": [{"a": 1, "b": {"n": 2}}]}),
    )

    r = client.get("/api/search/history/result/dict-shape/csv")
    assert r.status_code == 200, r.text
    reader = csv.DictReader(io.StringIO(r.text))
    parsed = list(reader)
    assert parsed[0]["a"] == "1"
    assert json.loads(parsed[0]["b"]) == {"n": 2}


def test_csv_route_empty_list_returns_404(db_and_client):
    """Busca sem hits (lista vazia) → 404 legítimo 'sem dados'."""
    client, SessionLocal = db_and_client
    _seed_result(SessionLocal, search_id="empty", result_json="[]")

    r = client.get("/api/search/history/result/empty/csv")
    assert r.status_code == 404, r.text


def test_csv_route_no_result_json_returns_404(db_and_client):
    """Sem result_json persistido → 404 legítimo."""
    client, SessionLocal = db_and_client
    _seed_result(SessionLocal, search_id="no-json", result_json=None)

    r = client.get("/api/search/history/result/no-json/csv")
    assert r.status_code == 404, r.text


def test_csv_expired_helper():
    """``_csv_expired``: True além da janela; False dentro; False sem created_at."""
    window = settings.SEARCH_HISTORY_CSV_RETENTION_DAYS

    class _R:
        created_at = datetime.utcnow() - timedelta(days=window + 5)

    class _Fresh:
        created_at = datetime.utcnow() - timedelta(days=1)

    class _NoDate:
        created_at = None

    assert results_router._csv_expired(_R()) is True
    assert results_router._csv_expired(_Fresh()) is False
    assert results_router._csv_expired(_NoDate()) is False


def test_csv_route_expired_returns_410(db_and_client):
    """Além da janela de retenção → 410 (expirado), não 404/200.

    A dependência real ``get_results_repo`` PODA registros expirados antes do handler
    (mesma janela de retenção) → viraria 404. Para exercitar o ramo 410 do handler em
    isolamento, injetamos um repo que NÃO poda.
    """
    from backend.app.db import repository

    client, SessionLocal = db_and_client
    stale = datetime.utcnow() - timedelta(days=settings.SEARCH_HISTORY_CSV_RETENTION_DAYS + 5)
    _seed_result(
        SessionLocal,
        search_id="stale",
        result_json=json.dumps(_FEDERATED_ITEMS),
        created_at=stale,
    )

    def _repo_no_prune():
        db = SessionLocal()
        try:
            yield repository.SearchResultRepository(db)
        finally:
            db.close()

    app.dependency_overrides[results_router.get_results_repo] = _repo_no_prune
    try:
        r = client.get("/api/search/history/result/stale/csv")
        assert r.status_code == 410, r.text
    finally:
        app.dependency_overrides.pop(results_router.get_results_repo, None)


def test_csv_route_missing_search_returns_404(db_and_client):
    """search_id inexistente → 404."""
    client, _ = db_and_client
    r = client.get("/api/search/history/result/does-not-exist/csv")
    assert r.status_code == 404, r.text
