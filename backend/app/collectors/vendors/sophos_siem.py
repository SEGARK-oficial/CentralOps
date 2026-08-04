"""Sophos Central — SIEM v1 events (telemetria de endpoint).

Endpoint: ``GET /siem/v1/events`` em ``api-{region}.central.sophos.com``.

**Por que este stream existe.** Os outros três coletores Sophos consomem feeds de
*finding*: ``/common/v1/alerts`` (alertas), ``/detections/v1`` (XDR Data Lake) e
``/cases/v1/cases`` (casos MDR). Nenhum deles carrega telemetria de endpoint
não-detecção — web control, status de atualização, conformidade. A própria
Sophos documenta que a SIEM API não coleta dados de XDR e que não é possível
replicar tudo do Data Lake, ou seja, os conjuntos são disjuntos nas duas
direções. Sem este stream, esses eventos simplesmente não existem no pipeline.

**Auth e modos.** Idêntica aos demais: OAuth2 client_credentials + ``X-Tenant-ID``,
resolvida pelo mesmo ``sophos_refresher``. Isso faz o stream funcionar nos três
modos sem código extra — tenant individual coleta a si próprio, e em
partner/organization cada filho descoberto é um ``kind=tenant`` que roda este
coletor com o seu próprio ``X-Tenant-ID`` (parents não coletam, são
guarda-chuvas).

**Armadilha operacional — retenção de ~24h.** Este feed é uma janela curta: o
que não for coletado dentro dela é perdido de forma irrecuperável, e não há
backfill possível. Duas consequências de projeto:

  1. O lookback de cold start é MAIOR que o dos outros streams (12h, não 1h):
     numa integração nova, começar 1h atrás jogaria fora quase toda a janela
     disponível. 12h é o mesmo default que o cliente de referência da Sophos usa.
  2. O teto por ciclo é generoso, e o cursor é sempre resumível. Um coletor que
     trava por mais de 24h aqui não perde "um pedaço" — perde tudo o que passou.

**Paginação.** Cursor opaco (``cursor`` + ``has_more``), diferente do
``pageFromKey``/``pages.nextKey`` dos alerts e do ``page``/``pageSize`` dos
cases. Enquanto ``has_more`` for verdadeiro seguimos paginando; o cursor
devolvido é o ponto de retomada.

**Contrato.** Verificado contra a especificação OAS 3.0 oficial em
``developer.sophos.com/docs/siem-v1/1`` (rota ``/events`` e tipo
``LegacyEventEntity``). Pontos que divergem dos outros coletores Sophos e que
custaram um bug antes da verificação:

- ``from_date`` é ``integer (int64)`` — **Unix timestamp em segundos**, não uma
  string ISO como o ``from`` de ``/common/v1/alerts``. É ignorado quando
  ``cursor`` está presente.
- ``limit`` aceita ``200..1000`` (default 200): valores abaixo de 200 são
  rejeitados.
- Um ``cursor`` fora das últimas 24h NÃO devolve erro — a resposta silenciosamente
  volta para a janela de 24h ("Response will default to last 24 hours if cursor
  is not within last 24 hours").
- ``severity`` é um enum fechado: ``NONE, LOW, MEDIUM, HIGH, CRITICAL``.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict, Optional

from ..base import BaseCollector
from ..metrics import API_LATENCY
from ._rate_limit import VendorRateLimitedError
from ._sophos_common import resolve_sophos_domain
from .sophos import _parse_retry_after, _safe_cursor_ts

logger = logging.getLogger(__name__)

#: ``limit``: a doc oficial define ``200 <= value <= 1000`` (default 200). Valor
#: abaixo de 200 é rejeitado — não reduza isto pensando em "páginas menores".
_PAGE_SIZE = 1000

#: Teto de páginas por CICLO Celery. Mesmo motivo dos demais coletores: sem ele,
#: um backlog grande é drenado num único run, estoura o ``task_soft_time_limit``
#: (720s), o pipeline reverte o cursor e a coleta trava sem progresso — o
#: poison-loop de jul/2026. Aqui o teto é mais alto que o de cases porque a
#: janela de retenção é de apenas 24h: ser conservador demais custaria dados.
_MAX_PAGES_PER_CYCLE = 30

#: Cold start. Deliberadamente maior que a 1h dos outros streams — ver docstring.
_COLD_START_LOOKBACK = timedelta(hours=12)


class SophosSiemRateLimitedError(VendorRateLimitedError):
    def __init__(self, retry_after: int) -> None:
        super().__init__(retry_after, vendor="sophos-siem")


def _default_lookback_iso() -> str:
    dt = datetime.now(timezone.utc) - _COLD_START_LOOKBACK
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _iso_to_epoch_seconds(value: str) -> int:
    """ISO-8601 canônico -> epoch em SEGUNDOS.

    ``from_date`` deste endpoint é ``integer (int64)``, "Unix timestamp in UTC" —
    NÃO uma string ISO como em ``/common/v1/alerts`` (``from``) e
    ``/cases/v1/cases`` (``createdAfter``). Mandar ISO aqui é 400 garantido.
    Mantemos o cursor interno em ISO (é o formato que o watermark, o backfill e
    o resto do pipeline entendem) e convertemos só na hora do request.
    """
    dt = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _extract_items(payload: Dict[str, Any]) -> list:
    """Itens da resposta, tolerando o nome da chave.

    O contrato não foi verificado contra doc oficial; aceitar ``items`` e
    ``events`` evita que uma diferença de nomenclatura vire "coletou zero" em
    silêncio — que, num feed de retenção 24h, significa perda definitiva.
    """
    for key in ("items", "events", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def _extract_cursor(payload: Dict[str, Any]) -> tuple[Optional[str], bool]:
    """``(cursor, has_more)`` da resposta, tolerando variações de nome.

    ``has_more`` só é considerado verdadeiro quando explicitamente verdadeiro:
    na ausência do campo, paramos e deixamos o próximo ciclo retomar pela
    janela. Preferimos re-ler (o dedupe absorve) a paginar indefinidamente
    contra um contrato que não confirmamos.
    """
    cursor = None
    for key in ("next_cursor", "nextCursor", "cursor"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            cursor = value
            break
    has_more = payload.get("has_more")
    if has_more is None:
        has_more = payload.get("hasMore")
    return cursor, bool(has_more) and cursor is not None


class SophosSiemEventsCollector(BaseCollector):
    """Coleta ``/siem/v1/events`` de um tenant Sophos Central."""

    platform = "sophos"
    stream = "siem_events"
    event_type = "sophos.siem_event"

    @property
    def domain(self) -> str:
        return resolve_sophos_domain(
            self.ctx.headers, integration_id=getattr(self.ctx, "integration_id", None)
        )

    async def collect(self) -> AsyncIterator[Dict[str, Any]]:
        cursor = self.ctx.cursor or {}
        # Guard fail-safe do cursor (ver ``sophos._safe_cursor_ts``): um ``from``
        # fora de UTC/segundos/``Z`` faz a Sophos responder 400 em todo ciclo
        # seguinte, e o caminho de erro do pipeline regrava o cursor anterior —
        # o feed trava até um reset manual. Descartar e recomeçar pela janela
        # custa uma re-leitura que o dedupe absorve.
        fallback = _default_lookback_iso()
        from_ts: str = _safe_cursor_ts(
            cursor.get("from_ts") or cursor.get("backfill_from_ts") or fallback,
            fallback,
        )
        page_cursor: Optional[str] = cursor.get("page_cursor")
        latest_ts = from_ts
        base_url = f"https://{self.domain}/siem/v1/events"
        page_count = 0

        while True:
            await self.ctx.rate_limiter.acquire(self.ctx.integration_id, self.platform)

            params: Dict[str, Any] = {"limit": _PAGE_SIZE}
            if page_cursor:
                # A doc é explícita: "from_date ... Ignored if cursor is set".
                # Mandar os dois só adicionaria ruído.
                params["cursor"] = page_cursor
            else:
                # int, não ISO — ver ``_iso_to_epoch_seconds``.
                params["from_date"] = _iso_to_epoch_seconds(from_ts)

            started = time.monotonic()
            stale_cursor = False
            payload: Dict[str, Any] = {}
            async with self.ctx.domain_limiter.slot(self.domain):
                async with self.ctx.session.get(
                    base_url, headers=self.ctx.headers, params=params
                ) as resp:
                    if resp.status == 429:
                        retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                        await self.ctx.rate_limiter.backoff(self.platform, retry_after)
                        raise SophosSiemRateLimitedError(retry_after)
                    if 400 <= resp.status < 500 and resp.status != 401:
                        body_preview = (await resp.text())[:500]
                        logger.warning(
                            "sophos siem events: HTTP %s params=%s body=%s",
                            resp.status, params, body_preview,
                        )
                        # AUTO-CURA do cursor de paginação. A doc diz que um
                        # cursor fora das 24h faz a resposta voltar
                        # silenciosamente para a janela padrão (não é erro), mas
                        # um cursor MALFORMADO ainda cai em 400 — e nesse caso
                        # encerramos o ciclo SEM levantar, para que a escrita
                        # final persista ``page_cursor=None``. Levantar faria o
                        # pipeline regravar ``cursor_before`` e a chave morta
                        # voltaria a cada ciclo, exigindo reset manual.
                        if resp.status == 400 and page_cursor:
                            stale_cursor = True
                            logger.warning(
                                "sophos siem events: 400 com cursor de paginação — "
                                "descartado, próximo ciclo reinicia de from=%s "
                                "(integration=%s)",
                                from_ts, self.ctx.integration_id,
                            )
                    if not stale_cursor:
                        resp.raise_for_status()
                        payload = await resp.json()
            if stale_cursor:
                page_cursor = None
                break

            API_LATENCY.labels(vendor=self.platform, stream=self.stream).observe(
                time.monotonic() - started
            )

            items = _extract_items(payload)
            for ev in items:
                # ``when`` é o instante do evento no endpoint; ``created_at`` é
                # quando a Sophos o registrou. Preferimos ``created_at`` para o
                # watermark porque é ele que ordena a entrega do feed — usar
                # ``when`` perderia eventos que chegam fora de ordem.
                raw_ts = ev.get("created_at") or ev.get("when") or latest_ts
                ts = (
                    _safe_cursor_ts(raw_ts, latest_ts)
                    if isinstance(raw_ts, str)
                    else latest_ts
                )
                if ts > latest_ts:
                    latest_ts = ts
                yield ev

            page_cursor, has_more = _extract_cursor(payload)

            page_count += 1
            if self.ctx.bounded_per_cycle and has_more and page_count >= _MAX_PAGES_PER_CYCLE:
                # Cursor RESUMÍVEL: mantém a janela original de propósito. Mover
                # ``from_ts`` para ``latest_ts`` aqui pularia as páginas ainda
                # não lidas (o feed não garante ordenação por data).
                self.ctx.cursor = {"from_ts": from_ts, "page_cursor": page_cursor}
                self.mark_cycle_capped()
                logger.info(
                    "sophos siem events: teto de %d páginas/ciclo atingido — cursor "
                    "RESUMÍVEL (from_ts=%s) p/ próximo ciclo (integration=%s)",
                    _MAX_PAGES_PER_CYCLE, from_ts, self.ctx.integration_id,
                )
                return
            if not has_more:
                break

            # Cursor intermediário: se o worker morrer mid-loop, retomamos da
            # próxima página em vez de re-paginar tudo.
            self.ctx.cursor = {"from_ts": from_ts, "page_cursor": page_cursor}

        # Cursor final: janela avança, paginação zera.
        self.ctx.cursor = {"from_ts": latest_ts, "page_cursor": None}

    def extract_message_id(self, event: Dict[str, Any]) -> str:
        """Dedupe pelo ``id`` do evento, que a SIEM v1 garante único.

        Diferente de cases, aqui NÃO compomos com um timestamp de atualização:
        eventos deste feed são imutáveis (não há ciclo de vida), então o ``id``
        sozinho é a identidade correta — compor com data reintroduziria o mesmo
        evento a cada re-leitura de janela.
        """
        # ``id`` é o único identificador do LegacyEventEntity — não existe
        # ``event_id`` no schema oficial.
        return str(event.get("id") or "")

    @classmethod
    def watermark_at(cls, cursor: Optional[Dict[str, Any]]) -> Optional[datetime]:
        """``from_ts`` — a janela inferior enviada à Sophos.

        Não lê ``backfill_from_ts``: durante um backfill o cursor carrega uma
        janela histórica escolhida a dedo, e reportá-la como watermark pintaria
        a integração de atrasada enquanto ela recupera passado de propósito.
        """
        return cls.watermark_from_iso(cursor, "from_ts")


# ── Self-registration ────────────────────────────────────────────────

def _register() -> None:
    from datetime import timedelta as _td
    from ..auth.refreshers import sophos_refresher
    from ..queues import Q_PRIORITY, T_COLLECT_PRIORITY
    from ..registry import CollectorRegistration, register

    register(
        CollectorRegistration(
            platform=SophosSiemEventsCollector.platform,
            stream=SophosSiemEventsCollector.stream,
            collector_cls=SophosSiemEventsCollector,
            refresh_fn=sophos_refresher,
            # 1 min, como alerts. A retenção de ~24h deste feed não perdoa
            # cadência folgada: cada minuto sem coletar é janela que encolhe, e
            # o que sai dela não volta por backfill.
            schedule=_td(minutes=1),
            queue=Q_PRIORITY,
            task_name=T_COLLECT_PRIORITY,
        )
    )


_register()
