"""Falha ANTES do loop de coleta re-levanta o erro ORIGINAL (não UnboundLocalError).

Regressão do incidente jul/2026: ``_track_claims``/``claimed_msg_ids`` eram
atribuídos só no passo 4 (dentro do try), mas o ``except`` final os referencia.
Uma exceção nos passos 1–3 (ex.: conexão de DB corrompida por soft-timeout ao
carregar a Integration) virava ``UnboundLocalError: local variable
'_track_claims' referenced before assignment`` — mascarando o erro real no log
do worker e no retry do Celery.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.app.collectors import pipeline


class _Boom(RuntimeError):
    """Erro original que deve sobreviver ao caminho de except."""


class _FakeRedis:
    async def aclose(self) -> None:  # o finally sempre fecha o client efêmero
        return None


def test_early_db_failure_reraises_original_error(monkeypatch):
    # Redis efêmero fake (criado antes do try — não pode explodir).
    monkeypatch.setattr(
        "backend.app.collectors.celery_app.get_worker_redis", lambda: _FakeRedis()
    )

    # Passo 1 (carregar Integration) explode — ANTES de _track_claims existir
    # no código antigo. O except persiste cursor de erro (também falha, é
    # engolido) e deve re-levantar _Boom, não UnboundLocalError.
    def _raise(*args, **kwargs):
        raise _Boom("db down")

    monkeypatch.setattr(pipeline.database, "SessionLocal", _raise)

    with pytest.raises(_Boom):
        asyncio.run(pipeline._run_collection_once(integration_id=999, stream="alerts"))
