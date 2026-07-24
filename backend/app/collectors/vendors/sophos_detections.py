"""Sophos Central — Detections (XDR async run query).

**Paradigma async 2-step** confirmado na Postman collection oficial
(docs/Sophos Central APIs.postman_collection.json):

1. ``POST /detections/v1/queries/detections``
   Body: ``{"from": "<iso>", "to": "<iso>"}``
   Resposta: ``{"id": "<run_id>", "status": "finished"|"running"|...,
                "resultCount": N, ...}``
   Nota: a API pode retornar ``status: "finished"`` imediatamente no POST
   se a query foi processada antes de devolver a resposta.

2. ``GET /detections/v1/queries/detections/:runId``
   Poll de status. Quando ``status == "finished"``, passa ao step 3.

3. ``GET /detections/v1/queries/detections/:runId/results``
   Paginação offset-based (``pages.current``, ``pages.total``, ``pages.size``).
   Retorna ``{"items": [...], "pages": {...}}``
   Fim: ``pages.current >= pages.total``.

**Cursor** persistido entre ciclos:

    {
      "run_id": "<uuid>|null",
      "from_ts": "<iso>",   # próximo ciclo começa daqui
      "page": 1             # página a retomar se worker morrer mid-loop
    }

Se ``cursor.run_id`` está presente → retomar run existente (poll/results).
Caso contrário → criar novo run cobrindo ``from_ts → now``.

**Dedupe**: campo ``id`` do detection. Sem ``id`` cai para hash do evento.

Docs:
https://developer.sophos.com/docs/detections-v1/1/routes/queries/detections/post
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict, Optional

from ..base import BaseCollector
from ..metrics import API_LATENCY
from ._sophos_common import resolve_sophos_domain
from .sophos import _normalize_ts  # normalização de timestamp compartilhada

logger = logging.getLogger(__name__)

# Bug A fix: pageSize real da Sophos é 50 (não 100).
# A Postman collection oficial mostra ``pages.maxSize: 50`` em vários
# endpoints do vendor. Se enviarmos 100 e a API silenciosamente retornar
# 50 (o máximo dela), ``len(items) < _RESULTS_PAGE_SIZE`` seria True na
# primeira página → break prematuro, sem ler os itens restantes.
# Fixado para 50 para alinhar com o cap real do endpoint.
_RESULTS_PAGE_SIZE = 50

# Bug B fix: aumentado de 3 para 6 tentativas (30s total @ 5s/poll).
# Queries XDR da Sophos podem levar 30–60s. Com 3 tentativas (15s) o
# collector esgotava os polls antes da query terminar, persistia run_id
# expirado no cursor e falhava no próximo ciclo (run já expirou após ~24h
# sem paginação). Com 6 tentativas chegamos a 30s, cobrindo a maioria dos
# casos documentados.
_MAX_POLL_ATTEMPTS = 6

# Intervalo entre polls (segundos). A API é async — queries longas
# podem levar alguns segundos. 5s é razoável sem sobresolicitar.
_POLL_INTERVAL_SECONDS = 5.0

# Bug C fix: limite de páginas por ciclo Celery.
# Sem este guard, um backfill grande poderia consumir centenas de páginas
# num único ciclo, ocupando o slot do worker por minutos. Com 50 páginas
# × 50 eventos = 2.500 eventos por ciclo no máximo.
# Quando o limite é atingido, o cursor preserva run_id + page para o
# próximo ciclo continuar de onde parou (sem criar novo run).
_MAX_PAGES_PER_CYCLE = 50

# Timeout duro por requisição de poll (segundos). Previne que uma
# resposta travada do endpoint de status prenda o loop indefinidamente.
_POLL_REQUEST_TIMEOUT_SECONDS = 30.0


class SophosDetectionsCollector(BaseCollector):
    """Collector de Detections XDR da Sophos Central.

    Implementa paradigma async 2-step: cria run → poll status → resultados.
    Cursor persiste run_id em andamento para retomada segura se o worker
    morrer entre os steps.
    """

    platform = "sophos"
    stream = "detections"
    event_type = "sophos.detection"

    @property
    def domain(self) -> str:
        # Preferimos ``X-Api-Host``; fallback estrito de ``X-Region`` (só aceita
        # slug de datacenter — ver ``_sophos_common.resolve_sophos_domain``).
        return resolve_sophos_domain(
            self.ctx.headers, integration_id=getattr(self.ctx, "integration_id", None)
        )

    async def collect(self) -> AsyncIterator[Dict[str, Any]]:
        """Coleta detections via paradigma async run → poll → results.

        Yields:
            Dicts crus do vendor (item do endpoint de results).
        """
        cursor = self.ctx.cursor or {}
        run_id: Optional[str] = cursor.get("run_id")
        from_ts: str = _normalize_ts(
            cursor.get("from_ts") or _default_lookback_iso()
        )
        page: int = int(cursor.get("page") or 1)

        base_url = f"https://{self.domain}/detections/v1/queries/detections"

        # ── Step 1: criar run se não houver run_id pendente ────────────
        if not run_id:
            run_id = await self._create_run(base_url, from_ts)
            if run_id is None:
                # Falha ao criar run — cursor inalterado, retry no próximo ciclo.
                return
            # Persiste run_id para que, se o worker morrer agora, retomamos
            # o poll no próximo ciclo (sem recriar o run).
            self.ctx.cursor = {"run_id": run_id, "from_ts": from_ts, "page": 1}
            page = 1

        # ── Step 2: poll status ────────────────────────────────────────
        finished = await self._poll_until_finished(base_url, run_id)
        if not finished:
            # Run ainda em andamento (ou excedeu _MAX_POLL_ATTEMPTS).
            # Mantém cursor com run_id para retomar no próximo ciclo.
            logger.info(
                "sophos detections: run_id=%s ainda em andamento — retomando no próximo ciclo",
                run_id,
            )
            return

        # ── Step 3: paginar resultados e yield ─────────────────────────
        latest_ts = from_ts
        results_url = f"{base_url}/{run_id}/results"
        page_count = 0

        while True:
            # Bug C: limita páginas por ciclo para não bloquear o slot Celery.
            page_count += 1
            if self.ctx.bounded_per_cycle and page_count > _MAX_PAGES_PER_CYCLE:
                # Sobrou backlog: o ``from_ts`` do cursor não avança enquanto o run
                # não é drenado, e sem este sinal esse "parado" é lido como tenant
                # sem detecções pela Saúde do Pipeline.
                self.mark_cycle_capped()
                logger.info(
                    "sophos_detections: max pages reached, persistindo cursor para próximo ciclo",
                    extra={"run_id": run_id, "page_count": page_count},
                )
                # Mantém run_id no cursor — próximo ciclo retoma paginação sem
                # criar novo run, continuando na página atual.
                self.ctx.cursor = {"run_id": run_id, "from_ts": from_ts, "page": page}
                return

            await self.ctx.rate_limiter.acquire(
                self.ctx.integration_id, self.platform
            )
            params: Dict[str, Any] = {
                "pageSize": _RESULTS_PAGE_SIZE,
                "page": page,
            }

            started = time.monotonic()
            async with self.ctx.domain_limiter.slot(self.domain):
                async with self.ctx.session.get(
                    results_url, headers=self.ctx.headers, params=params
                ) as resp:
                    if 400 <= resp.status < 500 and resp.status != 401:
                        body_preview = (await resp.text())[:500]
                        logger.warning(
                            "sophos detections: results HTTP %s run_id=%s page=%s body=%s",
                            resp.status, run_id, page, body_preview,
                        )
                    resp.raise_for_status()
                    payload = await resp.json()

            API_LATENCY.labels(vendor=self.platform, stream=self.stream).observe(
                time.monotonic() - started
            )

            items = payload.get("items") or []
            pages_meta = payload.get("pages") or {}

            for ev in items:
                raw_ts = ev.get("time") or latest_ts
                ts = _normalize_ts(raw_ts) if isinstance(raw_ts, str) else latest_ts
                if ts > latest_ts:
                    latest_ts = ts
                yield ev

            # Fim de paginação: página atual >= total de páginas, ou menos
            # itens que o tamanho de página solicitado (Bug A: usa 50 como cap).
            total_pages = int(pages_meta.get("total") or 1)
            if page >= total_pages or len(items) < _RESULTS_PAGE_SIZE:
                break

            page += 1
            # Cursor intermediário — retomada segura se worker morrer mid-loop.
            self.ctx.cursor = {"run_id": run_id, "from_ts": from_ts, "page": page}

        # Cursor final: próximo ciclo cria run novo cobrindo latest_ts → now.
        self.ctx.cursor = {"run_id": None, "from_ts": latest_ts, "page": 1}

    async def _create_run(self, base_url: str, from_ts: str) -> Optional[str]:
        """Cria um novo run de detections e retorna o run_id.

        Retorna None em caso de erro (log já registrado).
        """
        to_ts = _now_iso()
        body: Dict[str, Any] = {
            "from": from_ts,
            "to": to_ts,
        }

        await self.ctx.rate_limiter.acquire(
            self.ctx.integration_id, self.platform
        )
        started = time.monotonic()
        async with self.ctx.domain_limiter.slot(self.domain):
            async with self.ctx.session.post(
                base_url,
                headers=self.ctx.headers,
                json=body,
            ) as resp:
                if 400 <= resp.status < 500 and resp.status != 401:
                    body_preview = (await resp.text())[:500]
                    logger.warning(
                        "sophos detections: create run HTTP %s from=%s body=%s",
                        resp.status, from_ts, body_preview,
                    )
                resp.raise_for_status()
                payload = await resp.json()

        API_LATENCY.labels(vendor=self.platform, stream=self.stream).observe(
            time.monotonic() - started
        )

        run_id: Optional[str] = payload.get("id")
        if not run_id:
            logger.error(
                "sophos detections: create run não retornou id; payload=%s",
                str(payload)[:200],
            )
            return None

        logger.info(
            "sophos detections: run criado run_id=%s status=%s",
            run_id, payload.get("status"),
        )
        return run_id

    async def _poll_until_finished(
        self, base_url: str, run_id: str
    ) -> bool:
        """Poll do status do run até finished ou esgotamento de tentativas.

        Bug B fix: aumentado para _MAX_POLL_ATTEMPTS=6 (30s total @ 5s/poll)
        para cobrir queries XDR que levam 30–60s.

        Cada requisição de poll é envolta em ``asyncio.wait_for`` com timeout
        de _POLL_REQUEST_TIMEOUT_SECONDS para não pendurar em caso de
        Sophos travar a resposta.

        Retorna True se o run está finished; False se ainda em andamento
        após _MAX_POLL_ATTEMPTS tentativas (ou timeout de requisição).
        """
        poll_url = f"{base_url}/{run_id}"

        for attempt in range(_MAX_POLL_ATTEMPTS):
            if attempt > 0:
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)

            await self.ctx.rate_limiter.acquire(
                self.ctx.integration_id, self.platform
            )
            started = time.monotonic()

            try:
                payload = await asyncio.wait_for(
                    self._do_poll_request(poll_url, run_id, attempt),
                    timeout=_POLL_REQUEST_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "sophos detections: poll timeout run_id=%s tentativa=%d/%d "
                    "(timeout=%ss) — mantendo cursor para próximo ciclo",
                    run_id, attempt + 1, _MAX_POLL_ATTEMPTS,
                    _POLL_REQUEST_TIMEOUT_SECONDS,
                )
                return False

            if payload is None:
                # 404 ou reset já tratado em _do_poll_request.
                return False

            API_LATENCY.labels(vendor=self.platform, stream=self.stream).observe(
                time.monotonic() - started
            )

            status = payload.get("status", "")
            if status == "finished":
                return True

            logger.debug(
                "sophos detections: run_id=%s status=%s (tentativa %d/%d)",
                run_id, status, attempt + 1, _MAX_POLL_ATTEMPTS,
            )

        return False

    async def _do_poll_request(
        self, poll_url: str, run_id: str, attempt: int
    ) -> Optional[Dict[str, Any]]:
        """Executa uma única requisição de poll e retorna o payload JSON.

        Retorna None se o run expirou (404) — cursor já atualizado.
        Levanta exceções HTTP para outros erros.
        """
        async with self.ctx.domain_limiter.slot(self.domain):
            async with self.ctx.session.get(
                poll_url, headers=self.ctx.headers
            ) as resp:
                if resp.status == 404:
                    # Run expirou (Sophos mantém runs por ~24h).
                    logger.warning(
                        "sophos detections: run_id=%s não encontrado (expirado?) — "
                        "cursor será resetado no próximo ciclo",
                        run_id,
                    )
                    # Reseta run_id no cursor para criar run novo no próximo ciclo.
                    cursor = self.ctx.cursor or {}
                    self.ctx.cursor = {
                        "run_id": None,
                        "from_ts": cursor.get("from_ts") or _default_lookback_iso(),
                        "page": 1,
                    }
                    return None
                if 400 <= resp.status < 500 and resp.status != 401:
                    body_preview = (await resp.text())[:500]
                    logger.warning(
                        "sophos detections: poll HTTP %s run_id=%s body=%s",
                        resp.status, run_id, body_preview,
                    )
                resp.raise_for_status()
                return await resp.json()  # type: ignore[return-value]

    def extract_message_id(self, event: Dict[str, Any]) -> str:
        """ID de dedupe: campo ``id`` do detection, ou fallback hash."""
        return str(event.get("id") or "")

    @classmethod
    def watermark_at(cls, cursor: Optional[Dict[str, Any]]) -> Optional[datetime]:
        """``from_ts`` — o ``from`` do run assíncrono já criado (ou do próximo).

        Aqui o watermark cobre um atraso que os outros streams não têm: enquanto
        um ``run_id`` fica pendente ciclo após ciclo (run que a Sophos não termina),
        ``from_ts`` não avança e o indicador mostra a distância crescendo, mesmo
        sem nenhum teto de página ter sido atingido.
        """
        return cls.watermark_from_iso(cursor, "from_ts")


def _default_lookback_iso() -> str:
    """Lookback padrão: 1 hora atrás em ISO-8601 sem microsegundos."""
    dt = datetime.now(timezone.utc) - timedelta(hours=1)
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _now_iso() -> str:
    """Timestamp atual em ISO-8601 sem microsegundos."""
    dt = datetime.now(timezone.utc)
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


# ── Self-registration ────────────────────────────────────────────────


def _register() -> None:
    from datetime import timedelta as _td

    from ..auth.refreshers import sophos_refresher
    from ..queues import Q_PRIORITY, T_COLLECT_PRIORITY
    from ..registry import CollectorRegistration, register

    register(
        CollectorRegistration(
            platform=SophosDetectionsCollector.platform,
            stream=SophosDetectionsCollector.stream,
            collector_cls=SophosDetectionsCollector,
            refresh_fn=sophos_refresher,
            # Detections são menos urgentes que alerts (XDR queries têm
            # latência inerente no pipeline async da Sophos). 5min é
            # balanço entre frescor e carga no endpoint.
            schedule=_td(minutes=5),
            queue=Q_PRIORITY,
            task_name=T_COLLECT_PRIORITY,
        )
    )


_register()
