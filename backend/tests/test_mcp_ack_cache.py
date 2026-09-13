"""Ack tokens do MCP: uso único, expiração, ligação ao analista e teto por analista."""

from __future__ import annotations

import pytest

from backend.app.mcp import ack_cache as ack_mod
from backend.app.mcp.ack_cache import AckCache, AckTokenError
from backend.app.mcp.context import McpPrincipal, current_principal


class _MemoryStore:
    """Mesma superfície que o gateway usa no Redis: get/set(ex)/delete."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self.data[key] = value
        return True

    async def delete(self, *keys):
        removed = 0
        for key in keys:
            if self.data.pop(key, None) is not None:
                removed += 1
        return removed


def _principal(user_id: int) -> McpPrincipal:
    return McpPrincipal(
        user_id=user_id, username=f"u{user_id}", role="engineer", token_id=1,
        organization_id=None, is_global=True, client_ip="127.0.0.1",
        user_agent="t", correlation_id="cid",
    )


@pytest.fixture
def clock():
    state = {"now": 1_000.0}

    def _now():
        return state["now"]

    _now.state = state  # type: ignore[attr-defined]
    return _now


@pytest.fixture
def cache(clock):
    return AckCache(store=_MemoryStore(), clock=clock)


async def _as(user_id: int, coro):
    token = current_principal.set(_principal(user_id))
    try:
        return await coro
    finally:
        current_principal.reset(token)


async def test_issue_then_consume_is_single_use(cache):
    rules = {"rules": [{"target": "a"}]}
    token = await _as(1, cache.issue("def-1", rules))
    await _as(1, cache.consume(token, "def-1", rules))
    with pytest.raises(AckTokenError, match="unknown or already consumed"):
        await _as(1, cache.consume(token, "def-1", rules))


async def test_consume_rejects_other_rules_or_definition(cache):
    rules = {"rules": [{"target": "a"}]}
    token = await _as(1, cache.issue("def-1", rules))
    with pytest.raises(AckTokenError, match="different definition_id"):
        await _as(1, cache.consume(token, "def-2", rules))
    with pytest.raises(AckTokenError, match="rules differ"):
        await _as(1, cache.consume(token, "def-1", {"rules": []}))


async def test_token_belongs_to_the_analyst_who_minted_it(cache):
    """O ack de um analista não confirma o commit de outro."""
    rules = {"rules": []}
    token = await _as(1, cache.issue("def-1", rules))
    with pytest.raises(AckTokenError, match="unknown or already consumed"):
        await _as(2, cache.consume(token, "def-1", rules))
    # ...e o dono ainda consegue usar (a tentativa alheia não consumiu).
    await _as(1, cache.consume(token, "def-1", rules))


async def test_expired_token_is_rejected(cache, clock):
    rules = {"rules": []}
    token = await _as(1, cache.issue("def-1", rules))
    clock.state["now"] += ack_mod.ACK_TTL_SECONDS + 1
    with pytest.raises(AckTokenError, match="expired"):
        await _as(1, cache.consume(token, "def-1", rules))


async def test_patch_and_plain_tokens_do_not_cross_flows(cache):
    plain = await _as(1, cache.issue("def-1", {"rules": []}))
    patch = await _as(1, cache.issue_patch(
        "def-1", {"rules": [1]}, base_version_id="v9", patch_digest="d1",
    ))
    with pytest.raises(AckTokenError, match="came from dry_run_mapping"):
        await _as(1, cache.consume_patch(plain, "def-1", "d1"))
    with pytest.raises(AckTokenError, match="came from patch_mapping_rules"):
        await _as(1, cache.consume(patch, "def-1", {"rules": []}))
    assert await _as(1, cache.peek_base_version(patch)) == "v9"
    entry = await _as(1, cache.consume_patch(patch, "def-1", "d1"))
    assert entry.staged_rules == {"rules": [1]}
    assert entry.base_version_id == "v9"


async def test_per_analyst_cap_evicts_oldest(cache, clock, monkeypatch):
    monkeypatch.setattr(ack_mod, "MAX_ENTRIES", 3)
    tokens = []
    for i in range(4):
        clock.state["now"] += 1  # expiries ordenadas
        tokens.append(await _as(1, cache.issue("def-1", {"i": i})))
    # O mais antigo foi despejado; os outros três seguem válidos.
    with pytest.raises(AckTokenError):
        await _as(1, cache.consume(tokens[0], "def-1", {"i": 0}))
    for i in (1, 2, 3):
        await _as(1, cache.consume(tokens[i], "def-1", {"i": i}))


async def test_cap_is_per_analyst_not_global(cache, monkeypatch):
    monkeypatch.setattr(ack_mod, "MAX_ENTRIES", 1)
    t1 = await _as(1, cache.issue("def-1", {"a": 1}))
    t2 = await _as(2, cache.issue("def-1", {"a": 2}))
    await _as(1, cache.consume(t1, "def-1", {"a": 1}))
    await _as(2, cache.consume(t2, "def-1", {"a": 2}))


async def test_requires_principal():
    cache = AckCache(store=_MemoryStore())
    with pytest.raises(RuntimeError, match="outside of an authenticated call"):
        await cache.issue("def-1", {})
