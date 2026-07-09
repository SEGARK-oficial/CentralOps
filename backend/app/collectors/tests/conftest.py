"""Fixtures compartilhadas dos testes de ``collectors``.

Usa ``fakeredis.aioredis`` para substituir o Redis real — permite que
scripts Lua (sliding window, domain semaphore, unlock CAS) rodem
localmente sem dependência externa.
"""

from __future__ import annotations

import asyncio
import os
from typing import AsyncIterator

# Settings é resolvido no import de muitos módulos do app — precisa
# existir antes da fase de coleta do pytest. Idêntico ao que
# ``backend/tests/conftest.py`` já faz para os tests de router.
os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def redis_client() -> AsyncIterator:
    """Fake Redis assíncrono compatível com ``redis.asyncio``."""
    try:
        import fakeredis.aioredis as fakeredis_aio
    except ImportError:
        pytest.skip("fakeredis não disponível; instale fakeredis[lua]")

    client = fakeredis_aio.FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def sample_event() -> dict:
    return {
        "id": "alert-abc-123",
        "createdAt": "2026-04-23T14:22:10Z",
        "severity": "high",
        "type": "malware",
    }
