"""Cursor/checkpoint por (integration, stream) com dois níveis (RF02, RNF01).

- **Hot** — Redis, chave ``collection:cursor:{integration_id}:{stream}``.
  Leitura/escrita de baixa latência dentro do worker.
- **Cold / source of truth** — tabela ``collection_state`` (Postgres/SQLite).
  Usada no ``load`` como fallback se o Redis estiver vazio (cold start
  após flush/restart sem AOF).

Em caso de erro na coleta, gravamos o cursor **anterior** com
``last_error`` setado e ``consecutive_failures += 1`` — não perdemos a
posição original.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

import redis.asyncio as redis_async

from ...db import database
from ...db.repository import CollectionStateRepository

logger = logging.getLogger(__name__)

HOT_KEY = "collection:cursor:{integration_id}:{stream}"


class CursorStore:
    def __init__(self, redis: redis_async.Redis):
        self.redis = redis

    async def load(
        self, integration_id: int, stream: str
    ) -> Optional[Dict[str, Any]]:
        raw = await self.redis.get(
            HOT_KEY.format(integration_id=integration_id, stream=stream)
        )
        if raw:
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                logger.warning(
                    "cursor: hot value corrompido (redis) integration=%s stream=%s",
                    integration_id, stream,
                )

        # Cold fallback — Postgres/SQLite.
        with database.SessionLocal() as db:
            repo = CollectionStateRepository(db)
            row = repo.get(integration_id, stream)
            if not row or not row.cursor:
                return None
            try:
                return json.loads(row.cursor)
            except json.JSONDecodeError:
                logger.error(
                    "cursor: valor corrompido em collection_state id=%s stream=%s",
                    integration_id, stream,
                )
                return None

    async def save(
        self,
        integration_id: int,
        stream: str,
        cursor: Dict[str, Any],
        events_collected: int,
        error: Optional[str] = None,
        watermark_at: Optional["datetime"] = None,
        last_run_capped: bool = False,
    ) -> None:
        """Persiste o cursor e, com ele, o ATRASO REAL da coleta.

        ``watermark_at`` é até onde este cursor consumiu na linha do tempo do
        FORNECEDOR — extraído pelo próprio coletor, já que a semântica do cursor
        é opaca ao core. ``last_run_capped`` diz se o run parou por bater o teto
        de páginas, ou seja, se sobrou trabalho.

        Os dois só fazem sentido juntos: watermark parado com o teto NÃO atingido
        é um stream sem eventos (normal); watermark parado COM o teto atingido é
        backlog que o coletor não está vencendo.
        """
        payload = json.dumps(cursor, separators=(",", ":"), default=str)

        # Hot path primeiro — se Postgres falhar, ainda temos o cursor em Redis.
        #
        # BEST-EFFORT (ago/2026). Antes esta escrita não tinha guarda, e um Redis
        # que RECUSA ESCRITA — MISCONF por disco cheio, réplica READONLY, failover
        # em curso — levantava aqui e o ``upsert`` abaixo NUNCA rodava. O detalhe
        # que transformou isso num apagão de horas: o registro do ERRO de um ciclo
        # também passa por este mesmo método, então a falha apagava o próprio
        # rastro. ``consecutive_failures`` ficava em 0 e ``last_error`` em NULL
        # enquanto a coleta estava parada, e todo painel lia "saudável" — inclusive
        # a regra de ``pipeline_health`` que escala para ``unhealthy`` em 3 falhas
        # consecutivas, que nunca chegou a contar a primeira.
        #
        # O Postgres é a fonte da verdade e agora é gravado SEMPRE. Divergir dele o
        # hot path custa no máximo uma re-coleta da borda no ciclo seguinte (o
        # ``load`` lê o Redis primeiro, e a dedupe absorve a sobreposição) — preço
        # muito menor que perder o sinal de que a coleta morreu.
        try:
            await self.redis.set(
                HOT_KEY.format(integration_id=integration_id, stream=stream),
                payload,
            )
        except Exception:  # noqa: BLE001 — degradar, nunca suprimir o registro
            logger.warning(
                "cursor: falha ao gravar o hot path (integration=%s stream=%s) — "
                "o estado vai para o Postgres mesmo assim; o próximo ciclo relê o "
                "cursor ANTIGO do Redis e a dedupe absorve a sobreposição",
                integration_id,
                stream,
                exc_info=True,
            )

        with database.SessionLocal() as db:
            repo = CollectionStateRepository(db)
            repo.upsert(
                integration_id=integration_id,
                stream=stream,
                cursor=payload,
                events_collected=events_collected,
                error=error,
                watermark_at=watermark_at,
                last_run_capped=last_run_capped,
            )
