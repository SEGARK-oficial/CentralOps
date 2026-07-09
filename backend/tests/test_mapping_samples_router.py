"""Testes do endpoint GET /api/mappings/samples.

Cobre:
- Reservoir vazio → lista vazia + total 0.
- Eventos populados → retorna os N mais recentes primeiro.
- Sem autenticação → 401.
- MAPPING_READ obrigatório (todos os papéis do Sprint 1 têm a permissão).
- Params inválidos (limit=0, vendor vazio) → 422.

Estratégia de isolamento Redis:
    O router faz ``redis_async.from_url(...)`` internamente.
    Patchamos ``backend.app.routers.mappings.redis_async`` para que
    ``from_url`` devolva um ``FakeRedis`` em vez de tentar conectar
    num Redis real. Isso cobre o caminho de produção sem precisar de
    container externo.
"""

from __future__ import annotations

from typing import Any, Generator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import models
from backend.app.db.database import Base, get_session
from backend.app.main import app
from backend.app.collectors.normalize import sample_reservoir

# Importado apenas pra garantir que fakeredis está disponível;
# o skip é acionado pelo conftest dos collectors, mas duplicamos
# aqui por clareza.
try:
    import fakeredis.aioredis as _fakeredis_aio  # noqa: F401
    _FAKEREDIS_AVAILABLE = True
except ImportError:
    _FAKEREDIS_AVAILABLE = False


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def client_factory() -> Generator[Any, None, None]:
    """Cria engine SQLite em memória + override de get_session no app."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_get_session():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_session] = override_get_session
    clients: list[TestClient] = []

    def factory() -> TestClient:
        c = TestClient(app)
        clients.append(c)
        return c

    yield factory, TestingSession

    for c in clients:
        c.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


def _bootstrap_admin(client: TestClient) -> None:
    """Cria admin e mantém sessão ativa no client."""
    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPassword123!", "display_name": "Admin"},
    )
    assert r.status_code == 200, r.text


def _make_fake_redis_patcher():
    """Retorna context manager que substitui redis_async.from_url por FakeRedis."""
    if not _FAKEREDIS_AVAILABLE:
        pytest.skip("fakeredis[lua] não instalado")

    import fakeredis.aioredis as fakeredis_aio

    fake_instance = fakeredis_aio.FakeRedis(decode_responses=True)

    # from_url precisa ser síncrono (retorna o client; aclose é async).
    mock_from_url = MagicMock(return_value=fake_instance)

    return patch(
        "backend.app.routers.mappings.redis_async.from_url",
        mock_from_url,
    ), fake_instance


# ── Testes ────────────────────────────────────────────────────────────


def test_samples_requires_auth(client_factory: Any) -> None:
    """Sem cookie de sessão, endpoint deve retornar 401."""
    factory, _ = client_factory
    # Cria client mas NÃO faz bootstrap/login
    client = TestClient(app)
    r = client.get("/api/mappings/samples?vendor=sophos&event_type=sophos.alert")
    assert r.status_code == 401


def test_samples_returns_empty_when_no_reservoir(client_factory: Any) -> None:
    """Reservoir vazio deve retornar total_in_reservoir=0 e items=[]."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    patcher, fake_redis = _make_fake_redis_patcher()
    with patcher:
        r = client.get("/api/mappings/samples?vendor=sophos&event_type=sophos.alert")

    assert r.status_code == 200, r.text
    data = r.json()
    assert data["vendor"] == "sophos"
    assert data["event_type"] == "sophos.alert"
    assert data["total_in_reservoir"] == 0
    assert data["items"] == []


@pytest.mark.asyncio
async def test_samples_returns_recent_first_with_limit(client_factory: Any) -> None:
    """Popula 5 eventos no reservoir; GET com limit=3 deve retornar os 3 mais recentes."""
    if not _FAKEREDIS_AVAILABLE:
        pytest.skip("fakeredis[lua] não instalado")

    import fakeredis.aioredis as fakeredis_aio

    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    # Reservoir escopado por org. Popula sob org 7 e lê como
    # admin nomeando o tenant via ?org_id=7.
    org = 7
    fake_instance = fakeredis_aio.FakeRedis(decode_responses=True)
    eventos = [{"seq": i, "msg": f"evento-{i}"} for i in range(5)]
    for ev in eventos:
        await sample_reservoir.push(fake_instance, org, "sophos", "sophos.alert", ev)

    # A lista no Redis é LIFO: o último push fica no índice 0.
    # peek(limit=3) devolve os 3 primeiros da lista, ou seja, os 3 mais recentes.
    mock_from_url = MagicMock(return_value=fake_instance)
    with patch("backend.app.routers.mappings.redis_async.from_url", mock_from_url):
        r = client.get(
            f"/api/mappings/samples?vendor=sophos&event_type=sophos.alert&limit=3&org_id={org}"
        )

    assert r.status_code == 200, r.text
    data = r.json()
    assert data["total_in_reservoir"] == 5
    assert len(data["items"]) == 3
    # O mais recente (seq=4) deve ser o primeiro item retornado.
    assert data["items"][0]["seq"] == 4


def test_samples_invalid_limit_zero_returns_422(client_factory: Any) -> None:
    """limit=0 viola ge=1 → FastAPI deve retornar 422."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.get("/api/mappings/samples?vendor=sophos&event_type=sophos.alert&limit=0")
    assert r.status_code == 422


def test_samples_invalid_limit_over_max_returns_422(client_factory: Any) -> None:
    """limit=101 viola le=100 → FastAPI deve retornar 422."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.get("/api/mappings/samples?vendor=sophos&event_type=sophos.alert&limit=101")
    assert r.status_code == 422


def test_samples_missing_vendor_returns_422(client_factory: Any) -> None:
    """vendor ausente → FastAPI deve retornar 422 (campo obrigatório)."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.get("/api/mappings/samples?event_type=sophos.alert")
    assert r.status_code == 422


def test_samples_missing_event_type_returns_422(client_factory: Any) -> None:
    """event_type ausente → FastAPI deve retornar 422 (campo obrigatório)."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.get("/api/mappings/samples?vendor=sophos")
    assert r.status_code == 422


@pytest.mark.parametrize("role,expected_status", [
    ("viewer", 200),
    ("operator", 200),
    ("engineer", 200),
    ("admin", 200),
])
def test_samples_requires_mapping_read_permission(
    client_factory: Any,
    role: str,
    expected_status: int,
) -> None:
    """Todos os papéis têm mapping.read; todos devem receber 200.

    Non-admin precisam ter uma Integration com platform=sophos na sua org
    para que o filtro multi-tenant permita acesso ao vendor.
    """
    factory, TestingSession = client_factory

    admin_client = factory()
    _bootstrap_admin(admin_client)

    # Para non-admin: cria org + integration com platform=sophos
    org_id: int | None = None
    if role != "admin":
        with TestingSession() as db:
            org = models.Organization(
                name=f"Samples Test Org {role}",
                slug=f"samples-test-{role}",
                is_active=True,
            )
            db.add(org)
            db.flush()
            integration = models.Integration(
                organization_id=org.id,
                name="Sophos Integration",
                platform="sophos",
            )
            db.add(integration)
            db.commit()
            db.refresh(org)
            org_id = org.id

    # Cria usuário com o papel desejado (e org para non-admin).
    r = admin_client.post(
        "/api/auth/users",
        json={
            "username": f"user_{role}",
            "password": "Password123!X",
            "display_name": role.title(),
            "role": role,
            "organization_id": org_id,
        },
    )
    assert r.status_code == 200, r.text

    user_client = factory()
    login_r = user_client.post(
        "/api/auth/login",
        json={"username": f"user_{role}", "password": "Password123!X"},
    )
    assert login_r.status_code == 200, login_r.text

    patcher, fake_redis = _make_fake_redis_patcher()
    with patcher:
        r = user_client.get(
            "/api/mappings/samples?vendor=sophos&event_type=sophos.alert"
        )

    assert r.status_code == expected_status, f"role={role} esperava {expected_status}, got {r.status_code}: {r.text}"


@pytest.mark.asyncio
async def test_samples_non_admin_org_id_param_is_ignored(client_factory: Any) -> None:
    """Um non-admin que passa ?org_id=<outra_org> é TRAVADO na
    própria org — o param só vale para global scope (admin/SOC interno)."""
    if not _FAKEREDIS_AVAILABLE:
        pytest.skip("fakeredis[lua] não instalado")

    import fakeredis.aioredis as fakeredis_aio

    factory, TestingSession = client_factory
    admin_client = factory()
    _bootstrap_admin(admin_client)

    with TestingSession() as db:
        org = models.Organization(name="Own Org", slug="own-org", is_active=True)
        db.add(org)
        db.flush()
        db.add(models.Integration(
            organization_id=org.id, name="i", platform="sophos",
            is_active=True, kind="tenant", auth_status="unknown",
        ))
        db.commit()
        db.refresh(org)
        own_org = org.id
    other_org = own_org + 9999

    r = admin_client.post("/api/auth/users", json={
        "username": "viewer_own", "password": "Password123!X",
        "display_name": "V", "role": "viewer", "organization_id": own_org,
    })
    assert r.status_code == 200, r.text
    vclient = factory()
    assert vclient.post(
        "/api/auth/login", json={"username": "viewer_own", "password": "Password123!X"}
    ).status_code == 200

    fake = fakeredis_aio.FakeRedis(decode_responses=True)
    await sample_reservoir.push(fake, own_org, "sophos", "sophos.alert", {"who": "own"})
    await sample_reservoir.push(fake, other_org, "sophos", "sophos.alert", {"who": "other"})

    with patch("backend.app.routers.mappings.redis_async.from_url", MagicMock(return_value=fake)):
        # Tenta escapar para other_org via ?org_id — deve ser IGNORADO.
        r = vclient.get(
            f"/api/mappings/samples?vendor=sophos&event_type=sophos.alert&org_id={other_org}"
        )
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [it.get("who") for it in items] == ["own"], (
        "non-admin não pode ler samples de outra org via ?org_id"
    )


@pytest.mark.asyncio
async def test_dry_run_global_admin_reads_org_reservoir(client_factory: Any) -> None:
    """POST /dry-run: admin GLOBAL (org=None) consegue dry-run nomeando o tenant
    via ``organization_id`` — espelha o ``?org_id`` do GET reservoir.

    Regressão do leak-fix: o dry-run lia ``user.organization_id`` (None p/
    admin) → reservoir vazio → editor sem amostra (o e2e 02-dry-run-live pegou).
    """
    if not _FAKEREDIS_AVAILABLE:
        pytest.skip("fakeredis[lua] não instalado")

    import fakeredis.aioredis as fakeredis_aio

    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    org = 7
    fake_instance = fakeredis_aio.FakeRedis(decode_responses=True)
    sample = {
        "id": "e2e-001",
        "createdAt": "2026-01-15T10:00:00.000Z",
        "severity": "high",
        "type": "X",
        "description": "Y",
    }
    await sample_reservoir.push(fake_instance, org, "sophos", "sophos.alert", sample)

    rules = {
        "rules": [
            {"target": "normalized.class_uid", "const": 2004, "required": True},
            {"target": "normalized.time", "source": "createdAt", "required": True},
        ]
    }
    mock_from_url = MagicMock(return_value=fake_instance)

    # COM organization_id → admin global lê a amostra da org 7 (sample_size >= 1).
    with patch("backend.app.routers.mappings.redis_async.from_url", mock_from_url):
        r = client.post(
            "/api/mappings/dry-run",
            json={"rules": rules, "vendor": "sophos", "event_type": "sophos.alert", "organization_id": org},
        )
    assert r.status_code == 200, r.text
    assert r.json()["sample_size"] >= 1, r.json()  # era 0 antes do fix

    # SEM organization_id → admin global sem org → fail-closed (reservoir vazio).
    with patch("backend.app.routers.mappings.redis_async.from_url", mock_from_url):
        r2 = client.post(
            "/api/mappings/dry-run",
            json={"rules": rules, "vendor": "sophos", "event_type": "sophos.alert"},
        )
    assert r2.status_code == 200, r2.text
    assert r2.json()["sample_size"] == 0, r2.json()
