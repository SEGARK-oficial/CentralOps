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

**Janela dimensionada** (nov/2026). ``from_ts`` só avança quando o run é lido
até o fim. Um run que não cabe na vida útil dele nunca termina: expira, recomeça
da página 1 com janela ainda maior, e o stream atrasa sem erro nenhum. Por isso o
``resultCount`` do run decide antes da primeira página: acima de
``_MAX_RESULTS_PER_RUN`` o run é abandonado e refeito com janela
proporcionalmente menor (``span_s`` no cursor guarda a largura aprendida;
``to_ts`` guarda o fim da janela truncada). Janela truncada lida até o fim avança
``from_ts`` até ``to_ts`` — com ou sem eventos — e o ciclo sinaliza backlog. Ao
alcançar o presente, o cursor volta ao formato de três campos.

**Teto do ciclo**: páginas e tempo (``_CYCLE_TIME_BUDGET_SECONDS``, abaixo da
cadência — ciclo mais longo que ela faz o agendamento seguinte ser pulado).
**Tamanho de página**: o ``pages.maxSize`` que a API informa vale para os runs
seguintes (``max_page_size``), limitado a ``_PAGE_SIZE_CEILING``.

**Dedupe**: campo ``id`` do detection. Sem ``id`` cai para hash do evento.

**Fronteira da janela**: ``from`` só aceita segundos inteiros, então a detection
mais recente (``17:06:39.978``) volta em todo run que começa em ``17:06:39``. Num
tenant quieto a janela não anda e ela reaparecia a cada ciclo (e era reenviada a
cada vencimento do TTL do dedupe). O cursor guarda ``boundary`` — as chaves já
emitidas no instante onde a próxima janela começa — e o ciclo seguinte as pula
(ver ``cursor_boundary``). O run continua começando no mesmo ``from_ts``.

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
from ..cursor_boundary import BoundaryKeys
from ..metrics import API_LATENCY
from ._cursor_selfheal import is_stale_cursor_response, log_stale_cursor
from ._sophos_common import resolve_sophos_domain
from .sophos import _normalize_ts, _safe_cursor_ts  # timestamp compartilhado

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

# ── Dimensionamento da janela e do ciclo (nov/2026) ──────────────────
#
# Maior ``pageSize`` que pedimos, mesmo que a API aceite mais (ela informa o
# máximo em ``pages.maxSize``). Limita a resposta a poucos MB.
_PAGE_SIZE_CEILING = 500

# Orçamento de TEMPO do ciclo. Menor que a cadência (5 min) de propósito: ciclo
# que passa da cadência faz o agendamento seguinte ser pulado pela trava do
# stream, e o teto por ciclo vira metade da vazão que parece ter.
_CYCLE_TIME_BUDGET_SECONDS = 180.0

# Teto de resultados por run. Run maior que isso é abandonado ANTES da primeira
# página e refeito com janela menor: o watermark só anda quando o run termina,
# então run que não termina é atraso que só cresce.
_MAX_RESULTS_PER_RUN = 20_000

# Menor janela que ainda encolhe. Abaixo disso o run é lido do tamanho que for.
_MIN_WINDOW_SECONDS = 60

# Runs abandonados por ciclo. A redução é proporcional ao excesso e costuma
# acertar na primeira; o limite existe para rajada não virar rajada de POSTs.
_MAX_RESIZES_PER_CYCLE = 3

# Folga da redução proporcional (densidade nunca é uniforme).
_SHRINK_SAFETY = 0.8

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
        ctx = self.ctx
        # ``polling``: ciclo agendado, que o beat retoma. O backfill chama
        # ``collect()`` UMA vez e precisa drenar a janela inteira, então nele a
        # janela não é dimensionada. (Alias de propósito: o guard de teto ancora
        # no nome completo da flag e trata cada menção como uma saída de teto.)
        polling = ctx.bounded_per_cycle
        started = time.monotonic()

        cursor = ctx.cursor or {}
        run_id: Optional[str] = cursor.get("run_id")
        # Guard fail-safe do cursor: um ``from`` fora de UTC/segundos/``Z`` gera
        # 400 permanente (ver sophos.py). Descarta em favor do lookback.
        _fallback = _default_lookback_iso()
        from_ts: str = _safe_cursor_ts(cursor.get("from_ts") or _fallback, _fallback)
        page: int = int(cursor.get("page") or 1)
        # Fim da janela do run pendente — só existe quando ela foi truncada.
        to_ts: Optional[str] = _canonical_or_none(cursor.get("to_ts")) if run_id else None
        # Largura (s) que a janela deste tenant aguenta; ausente = "até agora".
        span_s: Optional[int] = _positive_int(cursor.get("span_s"))
        # Tamanho de página: o do run pendente (a numeração depende dele) e o que
        # a API disse aceitar, para os runs seguintes.
        size_hint: Optional[int] = _positive_int(cursor.get("max_page_size"))
        page_size: int = (
            _positive_int(cursor.get("page_size")) if run_id else None
        ) or _RESULTS_PAGE_SIZE
        # Memória da fronteira — só vale para o MESMO ``from_ts`` gravado.
        boundary = BoundaryKeys.load(cursor, window_start=from_ts)

        base_url = f"https://{self.domain}/detections/v1/queries/detections"

        # ── Steps 1 e 2: criar o run, esperar terminar e DIMENSIONAR ───
        #
        # O run cobre ``from_ts → to``; o watermark só anda quando ele é lido até
        # o fim. Sem limite de tamanho, um run que não cabe na vida útil dele (a
        # Sophos o expira) nunca termina: recomeça da página 1 com janela ainda
        # maior e ``from_ts`` fica parado para sempre (produção, nov/2026: 47 h de
        # atraso sem um único erro). Por isso, antes de ler a primeira página,
        # o ``resultCount`` decide: grande demais ⇒ o run é abandonado e refeito
        # com janela proporcionalmente menor.
        resizes = 0
        result_count: Optional[int] = None
        while True:
            if not run_id:
                to_ts = _window_end(from_ts, span_s) if polling else None
                page_size = min(size_hint or _RESULTS_PAGE_SIZE, _PAGE_SIZE_CEILING)
                run_id = await self._create_run(base_url, from_ts, to_ts)
                if run_id is None:
                    # Falha ao criar run — cursor inalterado, retry no próximo ciclo.
                    return
                page = 1
                # Persiste run_id para que, se o worker morrer agora, retomamos
                # o poll no próximo ciclo (sem recriar o run).
                ctx.cursor = _build_cursor(
                    run_id, from_ts, 1,
                    to_ts=to_ts, span_s=span_s, page_size=page_size,
                    size_hint=size_hint, memory=boundary.carry(),
                )

            run = await self._poll_until_finished(base_url, run_id)
            if run is None:
                # Run ainda em andamento (ou excedeu _MAX_POLL_ATTEMPTS).
                # Mantém cursor com run_id para retomar no próximo ciclo.
                logger.info(
                    "sophos detections: run_id=%s ainda em andamento — retomando no próximo ciclo",
                    run_id,
                )
                return

            result_count = _positive_int(run.get("resultCount"))
            oversized = (
                polling
                and page == 1
                and resizes < _MAX_RESIZES_PER_CYCLE
                and result_count is not None
                and result_count > _MAX_RESULTS_PER_RUN
            )
            smaller = _shrunk_span(from_ts, to_ts, result_count) if oversized else None
            if smaller is None:
                break
            resizes += 1
            logger.info(
                "sophos detections: run_id=%s tem %d resultados (teto %d) — janela "
                "reduzida para %ds a partir de %s (integration=%s)",
                run_id, result_count, _MAX_RESULTS_PER_RUN, smaller, from_ts,
                ctx.integration_id,
            )
            span_s, run_id, to_ts = smaller, None, None
            # Grava a largura aprendida: se o ciclo acabar aqui, o próximo já
            # começa pela janela menor em vez de refazer o run gigante.
            ctx.cursor = _build_cursor(
                None, from_ts, 1,
                span_s=span_s, size_hint=size_hint, memory=boundary.carry(),
            )

        # ── Step 3: paginar resultados e yield ─────────────────────────
        #
        # ``latest_ts`` atravessa os ciclos do MESMO run. Sem isso, o watermark
        # final seria o máximo só das páginas do último ciclo — e, como a ordem
        # dos resultados não é garantida, ele podia ficar no evento mais ANTIGO
        # do run, fazendo o run seguinte reler quase tudo.
        latest_ts = from_ts
        seen_ts = _canonical_or_none(cursor.get("latest_ts")) if cursor.get("run_id") else None
        if seen_ts and seen_ts > latest_ts:
            latest_ts = seen_ts
        results_url = f"{base_url}/{run_id}/results"
        page_count = 0

        while True:
            # Teto por ciclo: páginas (não bloquear o slot Celery) e TEMPO. O teto
            # de tempo fica abaixo da cadência de propósito: um ciclo mais longo
            # que ela faz a trava do stream pular o agendamento seguinte, e a
            # vazão real cai pela metade sem nada na tela. A 1ª página sempre é
            # lida — ciclo sem progresso nenhum seria poison-loop.
            page_count += 1
            out_of_time = (
                page_count > 1
                and time.monotonic() - started >= _CYCLE_TIME_BUDGET_SECONDS
            )
            if self.ctx.bounded_per_cycle and (
                page_count > _MAX_PAGES_PER_CYCLE or out_of_time
            ):
                # Sobrou backlog: o ``from_ts`` do cursor não avança enquanto o run
                # não é drenado, e sem este sinal esse "parado" é lido como tenant
                # sem detecções pela Saúde do Pipeline.
                self.mark_cycle_capped()
                logger.info(
                    "sophos_detections: teto do ciclo atingido (%s), persistindo "
                    "cursor para o próximo ciclo",
                    "tempo" if out_of_time else "páginas",
                    extra={"run_id": run_id, "page_count": page_count},
                )
                # Mantém run_id no cursor — próximo ciclo retoma paginação sem
                # criar novo run, continuando na página atual.
                ctx.cursor = _build_cursor(
                    run_id, from_ts, page,
                    to_ts=to_ts, span_s=span_s, page_size=page_size,
                    size_hint=size_hint, latest_ts=latest_ts,
                    memory=boundary.carry(),
                )
                return

            await self.ctx.rate_limiter.acquire(
                self.ctx.integration_id, self.platform
            )
            params: Dict[str, Any] = {
                "pageSize": page_size,
                "page": page,
            }

            request_started = time.monotonic()
            stale_run = False
            payload: Dict[str, Any] = {}
            async with self.ctx.domain_limiter.slot(self.domain):
                async with self.ctx.session.get(
                    results_url, headers=self.ctx.headers, params=params
                ) as resp:
                    if is_stale_cursor_response(resp.status, has_opaque_token=bool(run_id)):
                        # O run do Data Lake expirou ou foi invalidado. Encerramos
                        # o ciclo SEM levantar: no caminho de exceção o pipeline
                        # regrava ``cursor_before``, o ``run_id`` morto voltaria e
                        # cada ciclo recriaria o run só para tomar o mesmo 4xx na
                        # mesma página — o feed ficaria parado até um reset manual
                        # (observado em produção com 400 em .../results?page=6).
                        stale_run = True
                        log_stale_cursor(
                            "sophos detections",
                            status=resp.status,
                            integration_id=self.ctx.integration_id,
                            token_field=f"run_id={run_id} page={page}",
                            resume_from=from_ts,
                            body_preview=(await resp.text())[:200],
                        )
                    else:
                        resp.raise_for_status()
                        payload = await resp.json()
            if stale_run:
                # Recomeça a MESMA janela com run novo. O watermark NÃO anda: as
                # páginas que faltavam deste run não foram lidas, e avançar para
                # o máximo visto as pularia (a ordem dos resultados não é
                # garantida). Reler custa duplicata; pular custa detecção.
                if page == 1 and page_size != _RESULTS_PAGE_SIZE:
                    # 4xx logo na 1ª página de um run com página maior que o
                    # padrão: a suspeita é o tamanho. Volta ao padrão e FIXA —
                    # senão o próximo run reaprenderia o máximo e bateria no
                    # mesmo 4xx, ciclo sim, ciclo não.
                    size_hint = _RESULTS_PAGE_SIZE
                ctx.cursor = _build_cursor(
                    None, from_ts, 1,
                    span_s=span_s, size_hint=size_hint, memory=boundary.carry(),
                )
                return

            API_LATENCY.labels(vendor=self.platform, stream=self.stream).observe(
                time.monotonic() - request_started
            )

            items = payload.get("items") or []
            pages_meta = payload.get("pages") or {}
            # A API informa em toda resposta o maior ``pageSize`` que aceita. Vale
            # para os PRÓXIMOS runs (trocar no meio de um run mudaria a numeração
            # das páginas) e é aprendido uma vez só.
            if size_hint is None:
                size_hint = _positive_int(pages_meta.get("maxSize"))

            for ev in items:
                raw_ts = ev.get("time") or latest_ts
                # Canonicaliza antes de comparar (comparação lexicográfica só é
                # monotônica com os dois lados em UTC/``Z``).
                ts = (
                    _safe_cursor_ts(raw_ts, latest_ts)
                    if isinstance(raw_ts, str)
                    else latest_ts
                )
                if ts > latest_ts:
                    latest_ts = ts
                # Detection da fronteira que um ciclo anterior já emitiu: volta
                # porque ``from`` é truncado em segundos — pula (o máximo acima
                # já a contou).
                if boundary.seen(ev.get("time"), self.extract_message_id(ev)):
                    continue
                yield ev

            # Fim de paginação: página atual >= total de páginas, ou menos itens
            # que o tamanho de página em vigor. ``pages.total`` é opcional na
            # especificação: na falta dele o total sai do ``resultCount`` do run,
            # e sem nenhum dos dois vale o comportamento antigo (uma página).
            effective_size = min(
                page_size, _positive_int(pages_meta.get("maxSize")) or page_size
            )
            total_pages = _positive_int(pages_meta.get("total"))
            if total_pages is None and result_count is not None:
                total_pages = -(-result_count // effective_size)
            if page >= (total_pages or 1) or len(items) < effective_size:
                break

            page += 1
            # Cursor intermediário — retomada segura se worker morrer mid-loop.
            ctx.cursor = _build_cursor(
                run_id, from_ts, page,
                to_ts=to_ts, span_s=span_s, page_size=page_size,
                size_hint=size_hint, latest_ts=latest_ts,
                memory=boundary.carry(),
            )

        if boundary.skipped:
            logger.debug(
                "sophos detections: %d detection(s) da fronteira %s já emitidas — puladas",
                boundary.skipped, from_ts,
            )

        if self.ctx.bounded_per_cycle and to_ts:
            # Janela TRUNCADA lida até o fim: o watermark anda até o fim dela —
            # tudo até ``to_ts`` foi entregue — mas ainda há passado por ler, e é
            # isso que ``mark_cycle_capped`` diz à Saúde do Pipeline. Janela com
            # folga dobra a largura da próxima; no limite, fica como está.
            self.mark_cycle_capped()
            next_from = max(latest_ts, to_ts)
            if result_count is None or result_count * 2 <= _MAX_RESULTS_PER_RUN:
                span_s = (span_s or _MIN_WINDOW_SECONDS) * 2
            ctx.cursor = _build_cursor(
                None, next_from, 1,
                span_s=span_s, size_hint=size_hint,
                memory=boundary.dump(next_from),
            )
            return

        # Cursor final: próximo ciclo cria run novo cobrindo latest_ts → now. A
        # memória guarda as detections que esse ``from`` vai devolver de novo. A
        # largura aprendida sai: a janela alcançou o presente.
        ctx.cursor = _build_cursor(
            None, latest_ts, 1,
            size_hint=size_hint, memory=boundary.dump(latest_ts),
        )

    async def _create_run(
        self, base_url: str, from_ts: str, to_ts: Optional[str] = None
    ) -> Optional[str]:
        """Cria um novo run de detections e retorna o run_id.

        ``to_ts`` ausente = até agora. Retorna None em caso de erro (log já
        registrado).
        """
        body: Dict[str, Any] = {
            "from": from_ts,
            "to": to_ts or _now_iso(),
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
    ) -> Optional[Dict[str, Any]]:
        """Poll do status do run até finished ou esgotamento de tentativas.

        Bug B fix: aumentado para _MAX_POLL_ATTEMPTS=6 (30s total @ 5s/poll)
        para cobrir queries XDR que levam 30–60s.

        Cada requisição de poll é envolta em ``asyncio.wait_for`` com timeout
        de _POLL_REQUEST_TIMEOUT_SECONDS para não pendurar em caso de
        Sophos travar a resposta.

        Retorna o payload do run quando ele está finished (é dele que sai o
        ``resultCount`` que dimensiona a janela); ``None`` se ainda em andamento
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
                return None

            if payload is None:
                # 404 ou reset já tratado em _do_poll_request.
                return None

            API_LATENCY.labels(vendor=self.platform, stream=self.stream).observe(
                time.monotonic() - started
            )

            status = payload.get("status", "")
            if status == "finished":
                return payload

            logger.debug(
                "sophos detections: run_id=%s status=%s (tentativa %d/%d)",
                run_id, status, attempt + 1, _MAX_POLL_ATTEMPTS,
            )

        return None

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
                        # Mesmo ``from_ts`` ⇒ a memória da fronteira continua valendo
                        # (``load`` a descarta sozinho se a janela for outra). O que
                        # foi aprendido sobre este tenant (largura da janela, página
                        # máxima) também fica: é o que impede o run novo de nascer
                        # do mesmo tamanho do que acabou de expirar.
                        **{
                            key: cursor[key]
                            for key in ("boundary", "span_s", "max_page_size")
                            if key in cursor
                        },
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


def _build_cursor(
    run_id: Optional[str],
    from_ts: str,
    page: int,
    *,
    to_ts: Optional[str] = None,
    span_s: Optional[int] = None,
    page_size: Optional[int] = None,
    size_hint: Optional[int] = None,
    latest_ts: Optional[str] = None,
    memory: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Cursor do stream. Os campos além de ``run_id``/``from_ts``/``page`` só
    entram quando dizem algo — tenant em dia grava o mesmo cursor de sempre.

    - ``to_ts``: fim da janela do run pendente, só quando ela foi truncada;
    - ``span_s``: largura (s) aprendida para as janelas deste tenant;
    - ``page_size``: tamanho de página do run pendente, se diferente do padrão;
    - ``max_page_size``: o maior tamanho que a API disse aceitar;
    - ``latest_ts``: máximo já visto no run pendente, se passou de ``from_ts``.
    """
    cursor: Dict[str, Any] = {"run_id": run_id, "from_ts": from_ts, "page": page}
    if to_ts:
        cursor["to_ts"] = to_ts
    if span_s:
        cursor["span_s"] = span_s
    if page_size and page_size != _RESULTS_PAGE_SIZE:
        cursor["page_size"] = page_size
    if size_hint is not None:
        cursor["max_page_size"] = size_hint
    if latest_ts and latest_ts != from_ts:
        cursor["latest_ts"] = latest_ts
    cursor.update(memory or {})
    return cursor


def _positive_int(value: Any) -> Optional[int]:
    """``value`` como inteiro > 0, ou ``None`` (ausente, zero, lixo, ``bool``)."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _canonical_or_none(value: Any) -> Optional[str]:
    """Timestamp do cursor no formato que a Sophos aceita, ou ``None``."""
    if not isinstance(value, str) or not value:
        return None
    return _safe_cursor_ts(value, "") or None


def _parse_canonical(ts: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _window_end(from_ts: str, span_s: Optional[int]) -> Optional[str]:
    """Fim da janela que começa em ``from_ts`` com a largura aprendida.

    ``None`` = "até agora": não há largura aprendida, ou ela já alcança o
    presente (e então a janela volta a ser a de sempre).
    """
    start = _parse_canonical(from_ts)
    if not span_s or start is None:
        return None
    end = start + timedelta(seconds=span_s)
    if end >= datetime.now(timezone.utc):
        return None
    return end.isoformat(timespec="seconds").replace("+00:00", "Z")


def _shrunk_span(
    from_ts: str, to_ts: Optional[str], result_count: Optional[int]
) -> Optional[int]:
    """Largura (s) com que a janela ``from_ts → to_ts`` caberia no teto por run.

    Proporcional ao excesso, com folga — supõe densidade uniforme, e quando ela
    não é (rajada) o run seguinte volta a passar do teto e encolhe de novo.
    ``None`` quando não dá para encolher: a janela já está no mínimo.
    """
    start = _parse_canonical(from_ts)
    end = _parse_canonical(to_ts) if to_ts else datetime.now(timezone.utc)
    if start is None or end is None or not result_count:
        return None
    span = int((end - start).total_seconds())
    if span <= _MIN_WINDOW_SECONDS:
        return None
    target = int(span * _MAX_RESULTS_PER_RUN / result_count * _SHRINK_SAFETY)
    target = max(_MIN_WINDOW_SECONDS, target)
    return target if target < span else None


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
