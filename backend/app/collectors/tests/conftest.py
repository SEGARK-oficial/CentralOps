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


def make_threadsafe_sqlite_engine(tmp_dir: str):
    """Engine SQLite seguro para leituras CONCORRENTES vindas de threads.

    Substitui o par ``sqlite:///:memory:`` + ``StaticPool`` nos testes que
    exercitam ``dispatch_batch_to_destination``. O motivo é concreto e foi medido,
    não preventivo:

    ``StaticPool`` mantém **UMA única conexão** compartilhada por todas as sessões.
    Isso é seguro enquanto o teste é single-threaded — e deixa de ser no instante em
    que o código sob teste lê o DB de dentro de um worker thread. E ele lê:
    ``_load_destination_config`` roda em ``asyncio.to_thread`` (pipeline.py), e um
    lote com N destinos dispara N leituras simultâneas na MESMA conexão sqlite3.

    Medido neste repo — 2.400 leituras concorrentes (6 threads × 400 rodadas) de uma
    linha recém-atualizada, com ``StaticPool``::

        MISSING: 2      linha existente lida como inexistente
        None: 2         coluna lida vazia
        InterfaceError / IndexError / ValueError: 3

    Esses cinco resultados são exatamente os dois flakes intermitentes que a suíte
    exibia (~1 em 4 execuções completas):

    * ``test_dispatch_chaos_isolation_e5`` — *"destino dest-fast-b ausente/
      desabilitado — persistindo lote na DLQ"* (o ``MISSING`` acima);
    * ``test_semaphore_cap_respected_via_dispatcher`` — *"concorrência máxima
      observada=3 excede o cap"*, porque a task que leu ``delivery`` vazio caiu no
      ``concurrency`` default do kind (8) em vez do 2 que o teste gravou.

    Com arquivo temporário + WAL, cada thread recebe a **própria conexão** e as
    mesmas 2.400 leituras dão ZERO anomalias.

    Por que não ``sqlite:///file:...?mode=memory&cache=shared``: testado, e o
    interpretador **segfaultou** (exit 139) sob a mesma carga. O custo de I/O do
    arquivo é irrelevante na escala destes testes e não vale trocar um flake por um
    crash.

    ``check_same_thread=False`` continua necessário: o pool devolve/fecha conexões
    numa thread diferente da que as criou.
    """
    import pathlib

    from sqlalchemy import create_engine, event

    db_path = pathlib.Path(tmp_dir) / "test.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
        cur = dbapi_conn.cursor()
        # WAL: leitores concorrentes não bloqueiam o escritor (e vice-versa).
        cur.execute("PRAGMA journal_mode=WAL")
        # Sem timeout, um lock momentâneo vira ``database is locked`` — trocaria
        # um flake por outro.
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    return engine


@pytest.fixture()
def threadsafe_sqlite_engine(tmp_path):
    """Engine do :func:`make_threadsafe_sqlite_engine`, com descarte no teardown.

    Exposto como fixture (e não importado dos arquivos de teste) porque o pytest
    injeta conftest sem exigir import relativo dentro do pacote de testes.
    """
    engine = make_threadsafe_sqlite_engine(str(tmp_path))
    try:
        yield engine
    finally:
        engine.dispose()


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
