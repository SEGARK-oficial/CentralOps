"""Sample reservoir Redis — ring buffer para dry-run, escopado por tenant (S1a)."""

from __future__ import annotations

import pytest

from backend.app.collectors.normalize import sample_reservoir

_ORG = 1  # organization_id usado nos testes single-tenant


@pytest.mark.asyncio
async def test_push_and_peek_basic(redis_client) -> None:
    await sample_reservoir.push(
        redis_client, _ORG, "sophos", "sophos.alert", {"id": "a", "severity": "high"}
    )
    items = await sample_reservoir.peek(redis_client, _ORG, "sophos", "sophos.alert")
    assert len(items) == 1
    assert items[0] == {"id": "a", "severity": "high"}


@pytest.mark.asyncio
async def test_peek_returns_most_recent_first(redis_client) -> None:
    for i in range(5):
        await sample_reservoir.push(
            redis_client, _ORG, "sophos", "sophos.alert", {"id": f"alert-{i}"}
        )
    items = await sample_reservoir.peek(redis_client, _ORG, "sophos", "sophos.alert")
    # LPUSH inverte: item 4 foi o último push, vem primeiro.
    assert items[0]["id"] == "alert-4"
    assert items[-1]["id"] == "alert-0"


@pytest.mark.asyncio
async def test_capacity_bounds_buffer(redis_client) -> None:
    # 10 pushes, capacidade 3 → só 3 mais recentes ficam.
    for i in range(10):
        await sample_reservoir.push(
            redis_client, _ORG, "sophos", "sophos.alert", {"id": f"alert-{i}"}, capacity=3
        )
    items = await sample_reservoir.peek(redis_client, _ORG, "sophos", "sophos.alert")
    assert len(items) == 3
    assert [it["id"] for it in items] == ["alert-9", "alert-8", "alert-7"]


@pytest.mark.asyncio
async def test_peek_with_limit(redis_client) -> None:
    for i in range(10):
        await sample_reservoir.push(
            redis_client, _ORG, "sophos", "sophos.alert", {"id": f"alert-{i}"}
        )
    items = await sample_reservoir.peek(
        redis_client, _ORG, "sophos", "sophos.alert", limit=3
    )
    assert len(items) == 3
    assert [it["id"] for it in items] == ["alert-9", "alert-8", "alert-7"]


@pytest.mark.asyncio
async def test_size_reflects_buffer_count(redis_client) -> None:
    assert await sample_reservoir.size(redis_client, _ORG, "sophos", "sophos.alert") == 0
    for i in range(3):
        await sample_reservoir.push(
            redis_client, _ORG, "sophos", "sophos.alert", {"id": str(i)}
        )
    assert await sample_reservoir.size(redis_client, _ORG, "sophos", "sophos.alert") == 3


@pytest.mark.asyncio
async def test_reservoirs_isolated_per_event_type(redis_client) -> None:
    await sample_reservoir.push(redis_client, _ORG, "sophos", "sophos.alert", {"id": "a"})
    await sample_reservoir.push(redis_client, _ORG, "sophos", "sophos.case", {"id": "c"})
    alerts = await sample_reservoir.peek(redis_client, _ORG, "sophos", "sophos.alert")
    cases = await sample_reservoir.peek(redis_client, _ORG, "sophos", "sophos.case")
    assert len(alerts) == 1 and alerts[0]["id"] == "a"
    assert len(cases) == 1 and cases[0]["id"] == "c"


@pytest.mark.asyncio
async def test_zero_capacity_is_noop(redis_client) -> None:
    await sample_reservoir.push(
        redis_client, _ORG, "sophos", "sophos.alert", {"id": "x"}, capacity=0
    )
    assert await sample_reservoir.size(redis_client, _ORG, "sophos", "sophos.alert") == 0


@pytest.mark.asyncio
async def test_reservoirs_isolated_per_organization(redis_client) -> None:
    """S1a — vazamento cross-tenant fechado: org 1 escreve amostras do MESMO
    vendor/event_type; org 2 lê ZERO delas."""
    await sample_reservoir.push(
        redis_client, 1, "sophos", "sophos.alert", {"id": "tenant-1-secret"}
    )
    # Mesmo vendor + event_type, tenant diferente → reservoir separado.
    org2_items = await sample_reservoir.peek(redis_client, 2, "sophos", "sophos.alert")
    assert org2_items == [], "tenant 2 NÃO pode ver amostras do tenant 1"
    assert await sample_reservoir.size(redis_client, 2, "sophos", "sophos.alert") == 0
    # Controle positivo: org 1 vê a própria amostra.
    org1_items = await sample_reservoir.peek(redis_client, 1, "sophos", "sophos.alert")
    assert len(org1_items) == 1 and org1_items[0]["id"] == "tenant-1-secret"
