"""SEGARK EDR — detecções e telemetria de endpoint pela API de coleta para SIEM da plataforma.

Vendor novo = 1 módulo, ZERO core. A plataforma do SEGARK EDR é instalada pelo
cliente (uma URL por instalação) e expõe uma API só de leitura para SIEM,
autenticada por **conta de serviço** revogável, com escopos e allowlist de IP:

- ``GET {console}/api/integrations/v1/alerts`` (escopo ``alerts:read``): o estado
  ATUAL de cada detecção, em ordem de ``updated_at, id``. Janela até 31 dias,
  página até 200. Uma detecção que muda (status, caso, novas ocorrências) volta
  com ``updated_at`` novo — é uma atualização, não uma duplicata.
- ``GET {console}/api/integrations/v1/events?feed=<feed>`` (escopo
  ``events:read`` + o feed na allowlist da conta): linhas do Data Lake em ordem de
  ``received_at, event_id``. Janela até 24 h, página até 500.

Streams: ``alerts`` (→ Detection Finding) e um por feed de telemetria mapeado
(``process_events`` → Process Activity, ``file_events`` → File System Activity,
``network_connections`` → Network Activity, ``dns_events`` → DNS Activity). Todo
stream registrado roda em toda integração; o campo **Fluxos coletados** (coluna
``tenant_id``) diz quais ficam ligados — os demais saem do ciclo sem tocar a rede.
Vazio = só ``alerts``.

**Janela e cursor.** A API recebe ``from`` (inclusivo) e ``to`` (exclusivo), UTC
com ``Z``, e devolve ``next_cursor`` (keyset ESTRITO, preso àquela janela e feed)
até ``null``. O cursor do coletor é ``{"since": ISO}`` (início da próxima janela =
watermark) e, no meio de uma janela interrompida pelo teto do ciclo ou por
throttling, ``{"window": {"from", "to", "cursor"}}`` — a próxima execução retoma
EXATAMENTE a página seguinte. Dentro da janela não há reemissão (o keyset é
estrito). Entre janelas:

- o ``to`` nunca passa de ``agora − 60 s`` (linha gravada com instante anterior
  mas ainda não visível na hora da leitura — transação longa, ingestão);
- nas detecções, a janela seguinte recomeça ``2 min`` ANTES do ``to`` anterior
  (a doc da plataforma pede sobreposição) e a memória de fronteira
  (``cursor_boundary``) pula as que já saíram, pela chave ``id:updated_at``. Uma
  detecção atualizada tem chave nova e sai de novo, como deve.

**Throttling.** 429 e 503 (o nginx da plataforma responde 429 sem
``Retry-After``) esperam o ``Retry-After`` dentro do ciclo quando ele é curto
(até 30 s, 3 tentativas). Passou disso: se o ciclo já andou, ele termina com o
cursor na última página gravada; se não andou, ``SegarkEdrRateLimitedError``
leva o ``Retry-After`` ao Celery.

**Segredo.** O token (``oedrsvc_…``) vem do store de segredos da integração, vai
só no header ``Authorization`` e nunca em mensagem de erro, log ou resultado do
teste de conexão. Só HTTPS (o token não trafega em claro), sem seguir redirect,
URL validada pela política de URL do core.

**Fuso.** Todo instante da API é UTC com sufixo; um timestamp sem fuso no campo
que posiciona o cursor é recusado com erro que nomeia o campo — assumir UTC em
silêncio é como nasce um erro de horas.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict, Optional, Tuple
from urllib.parse import urlsplit

import aiohttp

from ..base import BaseCollector, CollectorContext, _parse_iso_utc_naive
from ..cursor_boundary import BoundaryKeys
from ..metrics import API_LATENCY
from ._rate_limit import VendorRateLimitedError

logger = logging.getLogger(__name__)

PLATFORM = "segark_edr"
_API_PREFIX = "/api/integrations/v1"

#: Formato do token de conta de serviço da plataforma (``oedrsvc_`` + 43 base64url).
_TOKEN_RE = re.compile(r"^oedrsvc_[A-Za-z0-9_-]{43}$")
_FEED_RE = re.compile(r"^[a-zA-Z0-9_.:/-]{1,128}$")

#: Teto de PÁGINAS por ciclo (por stream). Sem ele um backlog (cold start, coletor
#: parado) seria drenado num run só e estouraria o ``task_soft_time_limit``; o
#: ciclo seguinte recomeçaria do mesmo ponto (poison-loop). No teto o cursor já
#: aponta para a próxima página.
_MAX_PAGES_PER_CYCLE = 20

#: A janela não chega ao "agora": linha gravada com instante anterior pode ainda
#: não estar visível (transação longa no Postgres, lote do gateway no ClickHouse).
_SETTLE_LAG = timedelta(seconds=60)

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=60)
_PROBE_TIMEOUT = aiohttp.ClientTimeout(total=20)
#: Throttling: espera DENTRO do ciclo só quando curta.
_MAX_INLINE_WAIT = 30
_INLINE_RETRIES = 3
_DEFAULT_INLINE_WAIT = 5
_DEFAULT_RETRY_AFTER = 60
_ERROR_TEXT_MAX = 200


# ── Streams ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StreamSpec:
    """Como ler UM stream: rota, feed, campo que posiciona o cursor e limites."""

    path: str
    feed: Optional[str]
    time_field: str
    page_size: int
    max_window: timedelta
    cold_start: timedelta
    overlap: timedelta


ALERTS = StreamSpec(
    path="alerts", feed=None, time_field="updated_at", page_size=200,
    # A API aceita 31 dias; janelas de 1 dia mantêm cada página resumível barata.
    max_window=timedelta(days=1), cold_start=timedelta(hours=24), overlap=timedelta(minutes=2),
)


def _event_spec(feed: str) -> StreamSpec:
    # A API aceita 24 h. Sem sobreposição: o ``received_at`` é carimbado na
    # ingestão e o atraso de 60 s cobre a visibilidade; a sobreposição custaria
    # reler o volume de telemetria a cada ciclo.
    return StreamSpec(
        path="events", feed=feed, time_field="received_at", page_size=500,
        max_window=timedelta(hours=1), cold_start=timedelta(hours=1), overlap=timedelta(0),
    )


#: stream → feed da plataforma (nome exato de "Eventos históricos").
EVENT_FEEDS: Tuple[str, ...] = ("process_events", "file_events", "network_connections", "dns_events")
STREAMS: Tuple[str, ...] = ("alerts", *EVENT_FEEDS)
DEFAULT_STREAMS: Tuple[str, ...] = ("alerts",)


# ── Erros ────────────────────────────────────────────────────────────────


class SegarkEdrRateLimitedError(VendorRateLimitedError):
    def __init__(self, retry_after: int) -> None:
        super().__init__(retry_after, vendor=PLATFORM)


class SegarkEdrConfigError(ValueError):
    """URL, token ou lista de fluxos inválidos (mensagem sem segredo)."""


class SegarkEdrContractError(RuntimeError):
    """A resposta não segue o contrato da API (forma, fuso)."""


class SegarkEdrApiError(aiohttp.ClientResponseError):
    """Erro HTTP da plataforma (401/403 marcam erro de autenticação no pipeline).

    Montado SÓ a partir da resposta: o ``request_info`` de um ``raise_for_status``
    levaria o header ``Authorization`` do pedido para o log de exceção.
    """

    def __init__(self, resp: aiohttp.ClientResponse, message: str, code: str = "") -> None:
        info = aiohttp.RequestInfo(resp.url, resp.method, resp.headers, resp.url)
        super().__init__(
            info, getattr(resp, "history", ()), status=resp.status, message=message, headers=resp.headers,
        )
        self.api_code = code


class _Throttled(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__(f"throttled retry_after={retry_after}")
        self.retry_after = retry_after


class _StaleCursor(Exception):
    pass


# ── Configuração ─────────────────────────────────────────────────────────


def parse_streams(raw: Optional[str]) -> Tuple[str, ...]:
    """``"alerts, dns_events"`` → fluxos ligados, sem repetição. Vazio = só ``alerts``."""
    parts = [p for p in re.split(r"[,;\s]+", (raw or "").strip().lower()) if p]
    bad = [p for p in parts if p not in STREAMS]
    if bad:
        raise SegarkEdrConfigError(
            "fluxo desconhecido: " + ", ".join(p[:40] for p in bad[:5])
            + f" (use {', '.join(STREAMS)})"
        )
    return tuple(dict.fromkeys(parts)) or DEFAULT_STREAMS


def console_url(raw: Optional[str]) -> str:
    """URL do console (``https://host[:porta]``), validada pela política de URL do core.

    HTTPS é obrigatório: o token de conta de serviço não trafega em claro.
    """
    from ...core.url_policy import normalize_service_url

    try:
        base = normalize_service_url(raw or "")
    except ValueError as exc:
        raise SegarkEdrConfigError(f"URL do console inválida: {exc}") from exc
    if not base:
        raise SegarkEdrConfigError("informe a URL do console do SEGARK EDR (ex.: https://edr.example.com)")
    if urlsplit(base).scheme != "https":
        raise SegarkEdrConfigError("use https:// — o token da conta de serviço não pode trafegar em claro")
    return base.rstrip("/")


def check_token(raw: Optional[str]) -> str:
    token = (raw or "").strip()
    if not token:
        raise SegarkEdrConfigError("token da conta de serviço não configurado")
    if not _TOKEN_RE.match(token):
        raise SegarkEdrConfigError(
            "token em formato inesperado — copie o token exibido UMA vez ao criar a conta "
            "de serviço (começa com oedrsvc_)"
        )
    return token


def _load_settings(integration_id: int) -> Dict[str, Any]:
    """URL, token, TLS e fluxos da integração (sync, rodar em thread)."""
    from ...db import database, models
    from ...services import integration_secrets

    with database.SessionLocal() as db:
        integ = db.get(models.Integration, integration_id)
        if integ is None:
            raise RuntimeError(f"segark_edr: integração {integration_id} não encontrada")
        return {
            "base_url": integ.base_url or "",
            "token": integration_secrets.read_secret(integ, "client_secret") or "",
            "verify_ssl": True if integ.verify_ssl is None else bool(integ.verify_ssl),
            "streams": integ.tenant_id or "",
        }


# ── Tempo ────────────────────────────────────────────────────────────────


def _utcnow() -> datetime:
    """Agora em UTC, naive e truncado ao segundo."""
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _has_offset(value: Any) -> bool:
    """``True`` se o ISO-8601 carrega fuso explícito (``Z`` ou ``±hh:mm``/``±hhmm``)."""
    if not isinstance(value, str):
        return False
    text = value.strip()
    return text.endswith(("Z", "z")) or bool(re.search(r"[+-]\d{2}:?\d{2}$", text[10:]))


def _parse_retry_after(value: Optional[str], default: int) -> int:
    try:
        return max(1, int(str(value).strip()))
    except (TypeError, ValueError):
        return default


# ── Transporte ───────────────────────────────────────────────────────────


def _scrub(text: Any, token: str) -> str:
    out = str(text)[:_ERROR_TEXT_MAX]
    return out.replace(token, "***") if token else out


async def _json_or_none(resp: aiohttp.ClientResponse) -> Any:
    try:
        return await resp.json(content_type=None)
    except (ValueError, aiohttp.ClientError):
        return None


def _api_error(body: Any, token: str) -> Tuple[str, str]:
    """``{"error": {"code", "message"}}`` → (code, message), sem token."""
    err = body.get("error") if isinstance(body, dict) else None
    if not isinstance(err, dict):
        return "", ""
    return _scrub(err.get("code") or "", token), _scrub(err.get("message") or "", token)


_HINT_403 = (
    "a conta de serviço precisa do escopo alerts:read (detecções) ou events:read com o feed "
    "na lista de feeds (telemetria), e o IP do CentralOps na allowlist da conta, se houver"
)


async def _request(
    session: aiohttp.ClientSession,
    base: str,
    token: str,
    spec: StreamSpec,
    params: Dict[str, Any],
    *,
    ssl: Any,
    timeout: aiohttp.ClientTimeout,
) -> Dict[str, Any]:
    """Uma página → ``{"items": [...], "next_cursor": str|None}`` ou exceção com mensagem limpa."""
    url = f"{base}{_API_PREFIX}/{spec.path}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    async with session.get(
        url, params=params, headers=headers, allow_redirects=False, timeout=timeout, ssl=ssl,
    ) as resp:
        if resp.status in (429, 503):
            default = _DEFAULT_INLINE_WAIT if resp.status == 429 else _DEFAULT_RETRY_AFTER
            raise _Throttled(_parse_retry_after(resp.headers.get("Retry-After"), default))
        if 300 <= resp.status < 400:
            raise SegarkEdrApiError(resp, f"redirecionamento recusado (HTTP {resp.status}) — confira a URL do console")
        body = await _json_or_none(resp)
        code, message = _api_error(body, token)
        if resp.status == 401:
            raise SegarkEdrApiError(
                resp, "token inválido, expirado ou revogado — crie outra conta de serviço no console", code,
            )
        if resp.status == 403:
            raise SegarkEdrApiError(resp, f"acesso negado ({message or code or 'forbidden'}) — {_HINT_403}", code)
        if resp.status == 400 and code == "invalid_cursor" and params.get("cursor"):
            raise _StaleCursor(message)
        if resp.status == 404:
            raise SegarkEdrApiError(
                resp, "API de integração não encontrada — informe a URL do console sem caminho "
                "(ex.: https://edr.example.com) e confira a versão da plataforma", code,
            )
        if resp.status != 200:
            detail = f"{code}: {message}" if code else (message or f"HTTP {resp.status}")
            raise SegarkEdrApiError(resp, detail, code)
    if not isinstance(body, dict) or not isinstance(body.get("items"), list):
        raise SegarkEdrContractError(f"resposta de /{spec.path} sem a lista items")
    nxt = body.get("next_cursor")
    if nxt is not None and (not isinstance(nxt, str) or not nxt):
        raise SegarkEdrContractError(f"resposta de /{spec.path} com next_cursor inválido")
    return {"items": [i for i in body["items"] if isinstance(i, dict)], "next_cursor": nxt}


# ── Coletor ──────────────────────────────────────────────────────────────


class _SegarkEdrCollector(BaseCollector):
    """Leitura por janelas ``[from, to)`` com o cursor keyset da API."""

    platform = PLATFORM
    spec: StreamSpec

    def __init__(self, ctx: CollectorContext) -> None:
        super().__init__(ctx)
        self._host = ""
        self._base = ""
        self._token = ""
        self._ssl: Any = None
        self._pages = 0
        self._progress = False

    @property
    def domain(self) -> str:
        return self._host or "segark-edr"

    # ── coleta ───────────────────────────────────────────────────────────

    async def collect(self) -> AsyncIterator[Dict[str, Any]]:
        cfg = await asyncio.to_thread(_load_settings, self.ctx.integration_id)
        streams = parse_streams(cfg["streams"])
        if self.stream not in streams:
            # Fluxo desligado nesta integração: nenhum pedido, cursor intocado.
            logger.debug(
                "segark_edr %s: fluxo desligado na integração %s (ligados: %s)",
                self.stream, self.ctx.integration_id, ",".join(streams),
            )
            return
        self._base = console_url(cfg["base_url"])
        self._host = urlsplit(self._base).hostname or "segark-edr"
        self._token = check_token(cfg["token"])
        self._ssl = None if cfg["verify_ssl"] else False

        cursor = self.ctx.cursor or {}
        if "backfill_from_ts" in cursor:
            async for event in self._backfill(cursor):
                yield event
            return
        async for event in self._poll(cursor):
            yield event

    def _params(self, frm: datetime, to: datetime, page: Optional[str]) -> Dict[str, Any]:
        params: Dict[str, Any] = {"from": _iso(frm), "to": _iso(to), "limit": str(self.spec.page_size)}
        if self.spec.feed:
            params["feed"] = self.spec.feed
        if page:
            params["cursor"] = page
        return params

    async def _page(self, frm: datetime, to: datetime, page: Optional[str]) -> Dict[str, Any]:
        """Uma página, com a espera curta de throttling dentro do ciclo."""
        attempt = 0
        while True:
            await self.ctx.rate_limiter.acquire(self.ctx.integration_id, self.platform)
            started = time.monotonic()
            try:
                async with self.ctx.domain_limiter.slot(self.domain):
                    return await _request(
                        self.ctx.session, self._base, self._token, self.spec,
                        self._params(frm, to, page), ssl=self._ssl, timeout=_REQUEST_TIMEOUT,
                    )
            except _Throttled as exc:
                attempt += 1
                if exc.retry_after > _MAX_INLINE_WAIT or attempt > _INLINE_RETRIES:
                    raise
                logger.info(
                    "segark_edr %s: throttling da plataforma — nova tentativa em %ss (%d/%d, integration=%s)",
                    self.stream, exc.retry_after, attempt, _INLINE_RETRIES, self.ctx.integration_id,
                )
                await asyncio.sleep(exc.retry_after)
            finally:
                API_LATENCY.labels(vendor=self.platform, stream=self.stream).observe(time.monotonic() - started)

    def _instant(self, item: Dict[str, Any]) -> Any:
        """O campo que posiciona o cursor; sem fuso explícito é recusado (nunca "assume UTC")."""
        value = item.get(self.spec.time_field)
        if not _has_offset(value):
            raise SegarkEdrContractError(
                f"segark_edr {self.stream}: o campo {self.spec.time_field}={str(value)[:40]!r} veio sem fuso "
                "explícito — a API deve devolver UTC com Z; a coleta para em vez de adivinhar o fuso"
            )
        return value

    def _saved_window(self, cursor: Dict[str, Any]) -> Optional[Tuple[datetime, datetime, str]]:
        win = cursor.get("window")
        if not isinstance(win, dict):
            return None
        frm = _parse_iso_utc_naive(str(win.get("from") or ""))
        to = _parse_iso_utc_naive(str(win.get("to") or ""))
        page = win.get("cursor")
        if frm is None or to is None or to <= frm or not isinstance(page, str) or not page:
            logger.warning(
                "segark_edr %s: janela do cursor ilegível — recomeçando pelo since (integration=%s)",
                self.stream, self.ctx.integration_id,
            )
            return None
        return frm, to, page

    def _cursor_mid_window(self, frm: datetime, to: datetime, page: str, boundary: BoundaryKeys) -> Dict[str, Any]:
        return {
            "since": _iso(frm),
            "window": {"from": _iso(frm), "to": _iso(to), "cursor": page},
            # Mesma janela ⇒ a memória da fronteira dela continua valendo.
            **boundary.carry(),
        }

    async def _poll(self, cursor: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        now = _utcnow()
        ceiling = now - _SETTLE_LAG
        saved = self._saved_window(cursor)
        if saved is not None:
            frm, to, page = saved
            page_opt: Optional[str] = page
        else:
            since = _parse_iso_utc_naive(cursor["since"]) if isinstance(cursor.get("since"), str) else None
            if since is None:
                if cursor.get("since"):
                    logger.warning(
                        "segark_edr %s: since ilegível no cursor — recomeçando a frio (integration=%s)",
                        self.stream, self.ctx.integration_id,
                    )
                since = now - self.spec.cold_start
            if since >= ceiling:
                return
            frm, to, page_opt = since, min(since + self.spec.max_window, ceiling), None

        boundary = BoundaryKeys.load(cursor, window_start=_iso(frm))
        healed = False
        while True:
            # ── páginas de UMA janela ─────────────────────────────────────
            while True:
                try:
                    body = await self._page(frm, to, page_opt)
                except _StaleCursor:
                    if healed:
                        raise SegarkEdrContractError(
                            f"segark_edr {self.stream}: a plataforma recusou o cursor duas vezes no mesmo ciclo"
                        ) from None
                    healed = True
                    logger.warning(
                        "segark_edr %s: a plataforma recusou o cursor da janela %s — relendo a janela do "
                        "início (o dedupe absorve a repetição; integration=%s)",
                        self.stream, _iso(frm), self.ctx.integration_id,
                    )
                    page_opt = None
                    continue
                except _Throttled as exc:
                    if self._progress:
                        # O cursor já aponta para a próxima página: encerra o ciclo
                        # normalmente em vez de reverter o que foi lido.
                        logger.warning(
                            "segark_edr %s: throttling persistente (Retry-After %ss) — ciclo encerrado "
                            "na última página lida (integration=%s)",
                            self.stream, exc.retry_after, self.ctx.integration_id,
                        )
                        return
                    raise SegarkEdrRateLimitedError(exc.retry_after) from None

                for item in body["items"]:
                    if boundary.seen(self._instant(item), self.extract_message_id(item)):
                        continue
                    yield item
                self._pages += 1
                self._progress = True
                page_opt = body["next_cursor"]
                if page_opt is None:
                    break
                self.ctx.cursor = self._cursor_mid_window(frm, to, page_opt, boundary)
                # Teto por ciclo NO MEIO da janela: o cursor acima retoma a próxima página.
                if self.ctx.bounded_per_cycle and self._pages >= _MAX_PAGES_PER_CYCLE:
                    self.mark_cycle_capped()
                    logger.info(
                        "segark_edr %s: teto de %d páginas/ciclo no meio da janela %s — retomando no "
                        "próximo ciclo (integration=%s)",
                        self.stream, _MAX_PAGES_PER_CYCLE, _iso(frm), self.ctx.integration_id,
                    )
                    return

            # ── janela drenada: a próxima começa no ``to`` (menos a sobreposição) ──
            next_since = max(frm, to - self.spec.overlap)
            if boundary.skipped:
                logger.debug(
                    "segark_edr %s: %d item(ns) da sobreposição já emitidos — pulados",
                    self.stream, boundary.skipped,
                )
            self.ctx.cursor = {"since": _iso(next_since), **boundary.dump(_iso(next_since))}
            if to >= ceiling:
                return
            # Teto por ciclo ENTRE janelas: o cursor acima já é o início da próxima.
            if self.ctx.bounded_per_cycle and self._pages >= _MAX_PAGES_PER_CYCLE:
                self.mark_cycle_capped()
                logger.info(
                    "segark_edr %s: teto de %d páginas/ciclo com backlog a partir de %s (integration=%s)",
                    self.stream, _MAX_PAGES_PER_CYCLE, _iso(next_since), self.ctx.integration_id,
                )
                return
            boundary = BoundaryKeys.load(self.ctx.cursor, window_start=_iso(next_since))
            frm, to, page_opt, healed = next_since, min(next_since + self.spec.max_window, ceiling), None, False

    # ── backfill ─────────────────────────────────────────────────────────

    async def _backfill(self, cursor: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        """Fatias ``[lo, hi)`` disjuntas de ``max_window``, drenadas inteiras; o cursor do
        polling não é tocado (o backfill guarda o dele no job)."""
        now = _utcnow()
        ceiling = now - _SETTLE_LAG
        lo = _parse_iso_utc_naive(str(cursor.get("backfill_from_ts") or "")) or (now - self.spec.cold_start)
        hi = min(_parse_iso_utc_naive(str(cursor.get("backfill_to_ts") or "")) or ceiling, ceiling)
        frm = lo.replace(microsecond=0)
        while frm < hi:
            to = min(frm + self.spec.max_window, hi)
            page: Optional[str] = None
            while True:
                try:
                    body = await self._page(frm, to, page)
                except _Throttled as exc:
                    raise SegarkEdrRateLimitedError(exc.retry_after) from None
                for item in body["items"]:
                    self._instant(item)
                    yield item
                page = body["next_cursor"]
                if page is None:
                    break
            frm = to

    # ── identidade / watermark ───────────────────────────────────────────

    @classmethod
    def watermark_at(cls, cursor: Optional[Dict[str, Any]]) -> Optional[datetime]:
        """``since`` — início da janela em curso (no meio dela, o início dela: conservador)."""
        return cls.watermark_from_iso(cursor, "since")


class SegarkEdrAlertsCollector(_SegarkEdrCollector):
    """Detecções (estado atual, por ``updated_at``) → Detection Finding."""

    stream = "alerts"
    event_type = "segark_edr.alert"
    spec = ALERTS

    def extract_message_id(self, event: Dict[str, Any]) -> str:
        """``id:updated_at`` — a mesma versão da detecção sai uma vez; uma atualização sai de novo."""
        return f"{event.get('id') or ''}:{event.get('updated_at') or ''}"


class _SegarkEdrEventsCollector(_SegarkEdrCollector):
    def extract_message_id(self, event: Dict[str, Any]) -> str:
        return str(event.get("event_id") or "")


class SegarkEdrProcessEventsCollector(_SegarkEdrEventsCollector):
    stream = "process_events"
    event_type = "segark_edr.process_event"
    spec = _event_spec("process_events")


class SegarkEdrFileEventsCollector(_SegarkEdrEventsCollector):
    stream = "file_events"
    event_type = "segark_edr.file_event"
    spec = _event_spec("file_events")


class SegarkEdrNetworkConnectionsCollector(_SegarkEdrEventsCollector):
    stream = "network_connections"
    event_type = "segark_edr.network_connection"
    spec = _event_spec("network_connections")


class SegarkEdrDnsEventsCollector(_SegarkEdrEventsCollector):
    stream = "dns_events"
    event_type = "segark_edr.dns_event"
    spec = _event_spec("dns_events")


COLLECTORS = (
    SegarkEdrAlertsCollector,
    SegarkEdrProcessEventsCollector,
    SegarkEdrFileEventsCollector,
    SegarkEdrNetworkConnectionsCollector,
    SegarkEdrDnsEventsCollector,
)


# ── Refresher + teste de conexão ─────────────────────────────────────────


async def _segark_edr_refresher(integration_id: int) -> Dict[str, object]:
    """No-op p/ o framework: o token da conta de serviço é estático (expira e é revogado no console)."""
    return {"access_token": "", "expires_in": 3600}


async def _segark_edr_probe(cfg: Dict[str, Any]) -> Any:
    """Teste STATELESS pré-save: uma página de 1 item, nos últimos 5 min, de CADA fluxo ligado.

    Passa só se todos responderem: um fluxo ligado sem escopo ou sem o feed na
    allowlist falharia em todo ciclo. O token nunca é ecoado.
    """
    from ..output.base import TestResult

    try:
        base = console_url(cfg.get("base_url"))
        token = check_token(cfg.get("client_secret"))
        streams = parse_streams(cfg.get("tenant_id"))
    except SegarkEdrConfigError as exc:
        return TestResult.failed(str(exc))
    verify = cfg.get("verify_ssl")
    ssl: Any = None if (verify is None or bool(verify)) else False
    now = _utcnow()
    t0 = time.perf_counter()
    states = []
    failed = False
    try:
        async with aiohttp.ClientSession(timeout=_PROBE_TIMEOUT) as session:
            for stream in streams:
                spec = ALERTS if stream == "alerts" else _event_spec(stream)
                params: Dict[str, Any] = {"from": _iso(now - timedelta(minutes=5)), "to": _iso(now), "limit": "1"}
                if spec.feed:
                    params["feed"] = spec.feed
                try:
                    await _request(session, base, token, spec, params, ssl=ssl, timeout=_PROBE_TIMEOUT)
                    states.append(f"{stream}: ok")
                except _Throttled as exc:
                    failed = True
                    states.append(f"{stream}: limite de requisições da plataforma (tente em {exc.retry_after}s)")
                except SegarkEdrApiError as exc:
                    failed = True
                    states.append(f"{stream}: {exc.message}")
                except SegarkEdrContractError as exc:
                    failed = True
                    states.append(f"{stream}: {exc}")
    except aiohttp.ClientSSLError:
        return TestResult.failed(
            "Falha de TLS ao falar com o console — confira o certificado ou desligue a verificação "
            "SSL se o console usa certificado próprio"
        )
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        return TestResult.failed(f"Falha ao falar com o console ({urlsplit(base).hostname}): {type(exc).__name__}")
    detail = "; ".join(states)
    if failed:
        return TestResult.failed(f"Nem todo fluxo está acessível — {detail}")
    return TestResult.passed(f"Conexão OK — {detail}", latency_ms=(time.perf_counter() - t0) * 1000.0)


# ── Self-registration ────────────────────────────────────────────────────


def _register() -> None:
    from datetime import timedelta as _td

    from ..queues import Q_BULK, Q_PRIORITY, T_COLLECT_BULK, T_COLLECT_PRIORITY
    from ..registry import (
        AuthField,
        CollectorRegistration,
        PlatformRegistration,
        register,
        register_platform,
    )

    register_platform(
        PlatformRegistration(
            platform=PLATFORM,
            display_name="SEGARK EDR",
            category="EDR / XDR",
            description=(
                "SEGARK EDR — detecções (processo, arquivo, MITRE ATT&CK, veredito e ML) e "
                "telemetria de endpoint (processos, arquivos, conexões e DNS) pela API de coleta "
                "para SIEM da plataforma, com conta de serviço revogável."
            ),
            icon_id="shield",
            docs_url=None,
            order=16,
            test_fn=_segark_edr_probe,
            required_secrets=("client_secret",),
            capabilities=frozenset({"catalog", "auth:test", *(f"collect:{s}" for s in STREAMS)}),
            auth_fields=(
                AuthField(key="base_url", label="URL do console", type="url", required=True,
                          help_text="Endereço do console do SEGARK EDR, sem caminho (ex.: https://edr.example.com)."),
                AuthField(key="client_secret", label="Token da conta de serviço", type="secret", required=True,
                          help_text=(
                              "Criado em Administração → Integrações no console (começa com oedrsvc_). "
                              "Escopo alerts:read para detecções; events:read com os feeds para telemetria."
                          )),
                AuthField(key="tenant_id", label="Fluxos coletados (opcional)", type="string", required=False,
                          help_text=(
                              "Separados por vírgula: " + ", ".join(STREAMS)
                              + ". Vazio = só alerts. Cada feed precisa estar na lista da conta de serviço."
                          )),
                AuthField(key="verify_ssl", label="Verificar SSL", type="bool", required=False,
                          help_text="Desative só se o console usa certificado próprio que o CentralOps não confia."),
            ),
        )
    )

    for cls in COLLECTORS:
        priority = cls.stream == "alerts"
        register(
            CollectorRegistration(
                platform=PLATFORM,
                stream=cls.stream,
                collector_cls=cls,
                refresh_fn=_segark_edr_refresher,
                schedule=_td(minutes=1),
                queue=Q_PRIORITY if priority else Q_BULK,
                task_name=T_COLLECT_PRIORITY if priority else T_COLLECT_BULK,
            )
        )


_register()
