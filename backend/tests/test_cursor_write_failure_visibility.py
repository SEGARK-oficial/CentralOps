"""Um Redis que recusa ESCRITA não pode apagar o registro de que a coleta caiu.

Incidente ago/2026: o disco do host encheu, o Redis entrou em MISCONF
(``stop-writes-on-bgsave-error``) e passou a recusar toda escrita — aceitando
leitura normalmente. ``CursorStore.save`` gravava o hot path ANTES do Postgres e
sem guarda, então a exceção subia e o ``upsert`` nunca rodava.

O detalhe que transformou isso num apagão de ~6h: o registro do ERRO de um ciclo
também passa por ``save``. A falha apagava o próprio rastro —
``consecutive_failures`` ficou em 0 e ``last_error`` em NULL durante toda a
queda, e a regra de ``pipeline_health`` que escala para ``unhealthy`` em 3 falhas
consecutivas nunca chegou a contar a primeira. 87 streams parados, todos
``healthy``.

Aqui o contrato é: **o Postgres é gravado SEMPRE**, e a falha do hot path degrada
para re-coleta da borda (a dedupe absorve) em vez de silêncio.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

from typing import Any, Dict, Optional

import pytest


class _FakeRedis:
    """Aceita escrita — o caminho feliz."""

    def __init__(self) -> None:
        self.store: Dict[str, str] = {}

    async def get(self, key: str) -> Optional[str]:
        return self.store.get(key)

    async def set(self, key: str, value: str) -> None:
        self.store[key] = value


class _MisconfRedis(_FakeRedis):
    """Recusa escrita e aceita leitura — a forma EXATA do MISCONF do Redis."""

    async def set(self, key: str, value: str) -> None:
        raise RuntimeError(
            "MISCONF Redis is configured to save RDB snapshots, but it's "
            "currently unable to persist to disk."
        )


def _spy_repo(monkeypatch) -> Dict[str, Any]:
    """Substitui repositório e sessão por espiões; devolve o dict capturado."""
    from backend.app.collectors.state import cursor as cursor_module

    capturado: Dict[str, Any] = {}

    class _SpyRepo:
        def __init__(self, db) -> None:
            pass

        def upsert(self, **kw):
            capturado.update(kw)
            capturado["_chamado"] = capturado.get("_chamado", 0) + 1

    class _SpySession:
        def __enter__(self):
            return None

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(cursor_module, "CollectionStateRepository", _SpyRepo)
    monkeypatch.setattr(cursor_module.database, "SessionLocal", lambda: _SpySession())
    return capturado


async def test_erro_do_ciclo_chega_ao_postgres_mesmo_com_redis_recusando_escrita(
    monkeypatch,
) -> None:
    """O caso do incidente: ciclo falhou E o Redis não aceita gravar o registro.

    Antes da correção o ``upsert`` nunca era alcançado e a linha de estado ficava
    congelada com ``consecutive_failures = 0`` — a queda ficava invisível.
    """
    from backend.app.collectors.state import cursor as cursor_module

    capturado = _spy_repo(monkeypatch)
    store = cursor_module.CursorStore(_MisconfRedis())

    await store.save(
        7,
        "siem_events",
        {"from_ts": "2026-08-25T06:28:00Z"},
        events_collected=0,
        error="MISCONF Errors writing to the AOF file: No space left on device",
    )

    # CONTAGEM, não "não levantou": um ``except`` largo a jusante engoliria a
    # sentinela e este teste passaria sem o upsert ter acontecido.
    assert capturado.get("_chamado") == 1, (
        "o upsert no Postgres NÃO foi chamado — a falha do hot path voltou a "
        "suprimir o registro do ciclo, que é exatamente o bug de ago/2026"
    )
    assert capturado["error"], "o erro do ciclo tem de chegar à fonte da verdade"
    assert capturado["integration_id"] == 7
    assert capturado["stream"] == "siem_events"


async def test_falha_do_hot_path_nao_propaga_e_nao_derruba_o_ciclo(monkeypatch) -> None:
    """Degradar, nunca explodir: quem chama ``save`` não trata esta exceção."""
    from backend.app.collectors.state import cursor as cursor_module

    _spy_repo(monkeypatch)
    store = cursor_module.CursorStore(_MisconfRedis())

    await store.save(1, "alerts", {"from_ts": "x"}, events_collected=3)  # não levanta


async def test_caminho_feliz_continua_gravando_nos_dois_lugares(monkeypatch) -> None:
    """Controle POSITIVO do par acima.

    Sem ele, um ``save`` que parasse de gravar em qualquer um dos lados deixaria
    os testes anteriores verdes por vacuidade.
    """
    from backend.app.collectors.state import cursor as cursor_module

    capturado = _spy_repo(monkeypatch)
    redis = _FakeRedis()
    store = cursor_module.CursorStore(redis)

    await store.save(1, "alerts", {"from_ts": "2026-08-25T12:00:00Z"}, events_collected=5)

    assert capturado.get("_chamado") == 1
    assert capturado["events_collected"] == 5
    assert redis.store, "o hot path continua sendo gravado quando o Redis aceita"
    assert "collection:cursor:1:alerts" in redis.store
