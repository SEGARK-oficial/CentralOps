"""Microsoft 365 — auditoria unificada pela Office 365 Management Activity API.

Vendor novo = 1 módulo, ZERO core. Um stream por *content type* da API, cada um
com a própria assinatura do lado da Microsoft:

- ``audit_aad``        — ``Audit.AzureActiveDirectory``: logon no Entra
  (``UserLoggedIn``/``UserLoginFailed``) e operações administrativas do diretório
  (``Add user.``, ``Add member to role.``, ``Reset user password.``…).
- ``audit_exchange``   — ``Audit.Exchange``: admin do Exchange (``New-InboxRule``,
  ``Set-Mailbox`` com encaminhamento, ``Add-MailboxPermission``) e auditoria de
  caixa (``MailItemsAccessed`` com Audit Premium, ``HardDelete``, ``Send``).
- ``audit_sharepoint`` — ``Audit.SharePoint``: SharePoint e OneDrive (download,
  compartilhamento anônimo, sincronização em massa).
- ``audit_general``    — ``Audit.General``: tudo o que não é das três acima
  (Teams, Power Platform, Defender for Office, Purview…).
- ``dlp``              — ``DLP.All``: casamentos de política de DLP de todos os
  workloads. Exige a permissão de leitura de dados sensíveis de DLP; sem ela o
  stream fica desligado (no-op com aviso), sem derrubar os demais.

**Como a API funciona.** Não é um feed por timestamp de evento: a Microsoft
agrupa eventos em *blobs* de conteúdo e a listagem (``subscriptions/content``) é
filtrada pela hora em que o blob ficou DISPONÍVEL (``contentCreated``), em
janelas de até 24 h, no máximo 7 dias para trás. Cada blob é um array JSON de
eventos, e nem os blobs nem os eventos vêm em ordem. Por isso o cursor anda
sobre a disponibilidade, não sobre ``CreationTime``: evento atrasado chega num
blob novo e é lido, sem janela de "atraso tolerado" para adivinhar.

**Entrega sem perda e sem repetição gratuita.** A janela seguinte recomeça
``_OVERLAP`` antes do fim da anterior; os blobs já lidos na sobreposição ficam na
memória da fronteira (``cursor_boundary``, chave = ``contentId``) e não são
baixados de novo. O que escapar disso cai no dedupe por ``Id`` do evento.

**Assinatura.** Cada content type precisa de ``subscriptions/start`` uma vez por
tenant. O coletor inicia no primeiro ciclo (``AF20024`` = já ativa = sucesso) e
guarda ``subscribed`` no cursor — chamar ``start`` todo ciclo esbarraria na regra
de 15 minutos da API. Se a listagem responder ``AF20022`` (sem assinatura — o
admin parou, por exemplo), o coletor reinicia e tenta de novo. Uma assinatura
recém-criada pode levar até 12 h para produzir o primeiro blob.

**Uma credencial, a mesma do coletor Azure.** ``tenant_id`` + ``client_id`` +
``client_secret`` de um app registration com a permissão de aplicativo
``ActivityFeed.Read`` (Office 365 Management APIs) e consentimento do admin. O
token é client credentials v2 com escopo ``https://manage.office.com/.default``.

**Superfície de rede fechada.** O bearer vale como a identidade do cliente: o
``NextPageUri`` e o ``contentUri`` que a API devolve só são seguidos em
``https://manage.office.com``, e nenhuma chamada segue redirect. Só a nuvem
comercial; GCC/GCC High/DoD usam outros hosts de login e de API (follow-up).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict, List, Optional, Set, Tuple

import aiohttp
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from ..base import BaseCollector
from ..cursor_boundary import BoundaryKeys
from ..metrics import API_LATENCY
from ._cursor_selfheal import is_stale_cursor_response, log_stale_cursor
from ._rate_limit import VendorRateLimitedError
from .azure import (
    _GUID_RE,
    AzureTokenError,
    _client_credentials_token,
    _iso,
    _parse_iso,
    _parse_retry_after,
    validated_client_id,
)

logger = logging.getLogger(__name__)

_MANAGE_HOST = "manage.office.com"
_SCOPE = f"https://{_MANAGE_HOST}/.default"

#: Uma listagem cobre no máximo 24 h e começa no máximo 7 dias atrás. A margem
#: de 5 min evita pedir um início que vence entre o cálculo e a chamada.
_WINDOW = timedelta(hours=24)
_RETENTION = timedelta(days=7) - timedelta(minutes=5)
#: Cold start: a primeira janela. Backfill alcança os 7 dias inteiros.
_LOOKBACK = timedelta(hours=1)
#: Sobreposição entre janelas. A listagem é por disponibilidade do blob, então
#: não precisa cobrir atraso de evento — só a borda (``endTime`` é exclusivo e o
#: relógio do worker não é o da Microsoft).
_OVERLAP = timedelta(minutes=10)
#: Teto de requisições por CICLO (páginas de listagem + blobs baixados). Sem ele
#: o catch-up de dias de backlog seria drenado num run só e estouraria o
#: ``task_soft_time_limit`` (720 s): o pipeline reverte o cursor e o ciclo
#: seguinte recomeça do mesmo ponto — poison-loop. No teto o cursor guarda a
#: janela, a página da listagem e os blobs dela já lidos.
_MAX_REQUESTS_PER_CYCLE = 300

#: Códigos de erro da Management Activity API (``{"error": {"code": "AF…"}}``).
_AF_NO_SUBSCRIPTION = "AF20022"
_AF_ALREADY_ENABLED = "AF20024"
_AF_CONTENT_EXPIRED = "AF20051"
_AF_THROTTLED = "AF429"


class Microsoft365RateLimitedError(VendorRateLimitedError):
    def __init__(self, retry_after: int) -> None:
        super().__init__(retry_after, vendor="microsoft_365")


class _StreamDenied(Exception):
    """O tenant não concede este content type (ex.: DLP sem a permissão)."""


# ── Credencial e alvo ────────────────────────────────────────────────────


def validated_tenant_guid(tenant: str) -> str:
    """O tenant vira PATH da URL e ``PublisherIdentifier``: só o GUID serve.

    A API documenta o GUID do diretório nos dois lugares; um domínio
    (``contoso.onmicrosoft.com``) é recusado aqui em vez de virar um 404 opaco.
    """
    tenant = (tenant or "").strip()
    if _GUID_RE.match(tenant):
        return tenant.lower()
    raise ValueError(
        f"Tenant ID inválido: {tenant!r} — use o Directory (tenant) ID, um GUID"
    )


def _load_settings(integration_id: int) -> Dict[str, str]:
    """Tenant, client e segredo da integração (sync, rodar em thread)."""
    from ...db import database, models
    from ...services import integration_secrets

    with database.SessionLocal() as db:
        integ = db.get(models.Integration, integration_id)
        if integ is None:
            raise RuntimeError(f"microsoft_365: integração {integration_id} não encontrada")
        return {
            "tenant_id": integ.tenant_id or "",
            "client_id": integ.client_id or "",
            "client_secret": integration_secrets.read_secret(integ, "client_secret") or "",
        }


async def _m365_refresher(integration_id: int) -> Dict[str, object]:
    """Token da Management API; o framework o guarda no cache da integração."""
    cfg = await asyncio.to_thread(_load_settings, integration_id)
    async with aiohttp.ClientSession() as session:
        token, expires_in = await _client_credentials_token(
            session,
            validated_tenant_guid(cfg["tenant_id"]),
            cfg["client_id"],
            cfg["client_secret"],
            _SCOPE,
        )
    return {"access_token": token, "expires_in": expires_in}


def _feed_base(tenant: str) -> str:
    return f"https://{_MANAGE_HOST}/api/v1.0/{tenant}/activity/feed"


def _feed_url_allowed(url: str) -> bool:
    """Link devolvido pela API (``NextPageUri``, ``contentUri``) só no próprio host."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and (parts.hostname or "").lower() == _MANAGE_HOST
        and parts.port in (None, 443)
    )


def _with_publisher(url: str, tenant: str) -> Optional[Dict[str, str]]:
    """``PublisherIdentifier`` em toda chamada (cota dedicada), sem duplicar."""
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    if any(k.lower() == "publisheridentifier" for k in query):
        return None
    return {"PublisherIdentifier": tenant}


async def _af_code(resp: aiohttp.ClientResponse) -> str:
    """Só o código ``AF…`` do corpo de erro: a mensagem não volta para a UI."""
    try:
        body = await resp.json(content_type=None)
    except Exception:  # noqa: BLE001 — corpo ilegível não muda o erro
        return ""
    if not isinstance(body, dict):
        return ""
    err = body.get("error")
    if isinstance(err, dict):
        return str(err.get("code") or "")[:32]
    return ""


async def _raise_for_feed_status(resp: aiohttp.ClientResponse, op: str, code: str = "") -> None:
    if resp.status < 400:
        return
    code = code or await _af_code(resp)
    if resp.status == 429 or code == _AF_THROTTLED:
        raise Microsoft365RateLimitedError(_parse_retry_after(resp.headers.get("Retry-After")))
    logger.warning("microsoft_365: %s devolveu HTTP %s %s", op, resp.status, code)
    # 401 sai como ``ClientResponseError``: é o que faz o pipeline invalidar o
    # token em cache e marcar a integração com erro de autenticação.
    resp.raise_for_status()


def _fmt(dt: datetime) -> str:
    """Formato que a listagem aceita: ``YYYY-MM-DDTHH:MM:SS`` em UTC."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def clean_client_ip(raw: Any) -> Optional[str]:
    """``ClientIP`` como a API manda → IP puro, ou ``None``.

    O campo vem como ``203.0.113.5``, ``203.0.113.5:51234``,
    ``[2001:db8::1]:443``, ``2001:db8::1`` ou vazio/``<null>``; JMESPath não
    separa porta de IPv6, então o coletor acrescenta ``ClientIPNormalized``
    já limpo (nome sem ``_``: esse prefixo é reservado ao preprocess do motor).
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if text.startswith("["):
        text = text[1:].split("]", 1)[0]
    elif text.count(":") == 1:
        text = text.split(":", 1)[0]
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return None


def _redact_detected_values(node: Any) -> int:
    """Tira os VALORES detectados pelo DLP (número de cartão, CPF…) do evento.

    Com a permissão de DLP a API devolve o trecho sensível que casou a política;
    o bruto do vendor viaja para os destinos, então ele não sai do coletor. Fica
    a contagem (``DetectedValuesRedacted``) — o tipo e a quantidade continuam lá.
    """
    removed = 0
    if isinstance(node, dict):
        values = node.pop("DetectedValues", None)
        if values is not None:
            count = len(values) if isinstance(values, list) else 1
            node["DetectedValuesRedacted"] = count
            removed += count
        for child in node.values():
            removed += _redact_detected_values(child)
    elif isinstance(node, list):
        for child in node:
            removed += _redact_detected_values(child)
    return removed


# ── Coletor ──────────────────────────────────────────────────────────────


class Microsoft365AuditAadCollector(BaseCollector):
    """Um content type da Management Activity API (polling por disponibilidade)."""

    platform = "microsoft_365"
    stream = "audit_aad"
    event_type = "microsoft_365.audit"
    domain = _MANAGE_HOST
    _CONTENT_TYPE = "Audit.AzureActiveDirectory"
    #: Sem permissão = stream desligado em vez de erro da integração inteira.
    _OPTIONAL = False

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._tenant = ""
        #: Cursor resumível montado ao bater o teto (``None`` = não bateu).
        self._resume: Optional[Dict[str, Any]] = None
        #: Cursor da próxima janela quando a leitura alcançou o relógio.
        self._final: Optional[Dict[str, Any]] = None

    async def collect(self) -> AsyncIterator[Dict[str, Any]]:
        cursor = dict(self.ctx.cursor or {})
        cfg = await asyncio.to_thread(_load_settings, self.ctx.integration_id)
        self._tenant = validated_tenant_guid(cfg["tenant_id"])
        now = datetime.now(timezone.utc)

        try:
            if not cursor.get("subscribed"):
                await self._start_subscription()

            if "backfill_from_ts" in cursor:
                # A listagem é endereçável por janela: o backfill lê o pedido,
                # dentro dos 7 dias, e não mexe no cursor do polling.
                start = max(_parse_iso(cursor["backfill_from_ts"]) or now, now - _RETENTION)
                stop = min(_parse_iso(cursor.get("backfill_to_ts")) or now, now)
                walker = self._walk(start, stop, BoundaryKeys.load(None, window_start=None))
                async for event in walker:
                    yield event
                return

            start = _parse_iso(cursor.get("startTime")) or (now - _LOOKBACK)
            if start < now - _RETENTION:
                floor = now - _RETENTION
                logger.warning(
                    "microsoft_365 %s: cursor %s além dos 7 dias da API — %s de conteúdo "
                    "não existe mais e foi perdido; retomando no limite (integration=%s)",
                    self.stream, _iso(start), floor - start, self.ctx.integration_id,
                )
                start = floor
                cursor = {}
            start_s = _iso(start)
            boundary = BoundaryKeys.load(cursor, window_start=start_s)
            resuming = cursor.get("startTime") == start_s and bool(cursor.get("window_end"))
            page = cursor.get("page") if resuming else None
            if page and not _feed_url_allowed(page):
                page = None  # cursor adulterado/antigo: refaz a listagem da janela
            walker = self._walk(
                start,
                now,
                boundary,
                end=_parse_iso(cursor.get("window_end")) if resuming else None,
                page=page,
                done=set(cursor.get("done") or []) if resuming else set(),
            )
            async for event in walker:
                yield event
        except _StreamDenied:
            logger.warning(
                "microsoft_365 %s: o tenant não concede %s a este app — stream desligado "
                "(para DLP: conceda a permissão de leitura de dados sensíveis de DLP e o "
                "consentimento do admin) (integration=%s)",
                self.stream, self._CONTENT_TYPE, self.ctx.integration_id,
            )
            return

        if self._resume is not None:
            self.ctx.cursor = {**self._resume, "subscribed": True}
        elif self._final is not None:
            self.ctx.cursor = {**self._final, "subscribed": True}

    async def _walk(
        self,
        start: datetime,
        stop: datetime,
        boundary: BoundaryKeys,
        *,
        end: Optional[datetime] = None,
        page: Optional[str] = None,
        done: Optional[Set[str]] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Lê janelas de até 24 h de ``start`` até ``stop``.

        Preenche ``_resume`` ao bater o teto ou ``_final`` ao alcançar ``stop``.
        """
        done = set(done or ())
        work = 0
        while True:
            end = end or min(start + _WINDOW, stop)
            start_s, end_s = _iso(start), _iso(end)
            while True:  # páginas da listagem desta janela
                if self.ctx.bounded_per_cycle and work >= _MAX_REQUESTS_PER_CYCLE:
                    self._resume = self._resume_cursor(start_s, end_s, page, done, boundary)
                    self.mark_cycle_capped()
                    logger.info(
                        "microsoft_365 %s: teto de %d requisições/ciclo — retomando a "
                        "janela %s no próximo ciclo (integration=%s)",
                        self.stream, _MAX_REQUESTS_PER_CYCLE, start_s, self.ctx.integration_id,
                    )
                    return
                work += 1
                listed = await self._list(page, start, end)
                if listed is None:
                    # NextPageUri recusado: refaz a janela pela listagem; o que
                    # voltar repetido a fronteira e o dedupe seguram.
                    page, done = None, set()
                    continue
                blobs, next_page = listed
                for blob in blobs:
                    content_id = str(blob.get("contentId") or "")
                    if not content_id or content_id in done:
                        continue
                    if boundary.seen(blob.get("contentCreated"), content_id):
                        continue
                    if self.ctx.bounded_per_cycle and work >= _MAX_REQUESTS_PER_CYCLE:
                        self._resume = self._resume_cursor(start_s, end_s, page, done, boundary)
                        self.mark_cycle_capped()
                        logger.info(
                            "microsoft_365 %s: teto de %d requisições/ciclo no meio de uma "
                            "página — %d blob(s) dela já lidos (integration=%s)",
                            self.stream, _MAX_REQUESTS_PER_CYCLE, len(done),
                            self.ctx.integration_id,
                        )
                        return
                    work += 1
                    async for event in self._read_blob(blob.get("contentUri")):
                        yield event
                    done.add(content_id)
                if not next_page:
                    break
                if not _feed_url_allowed(next_page):
                    raise RuntimeError(
                        f"microsoft_365 {self.stream}: NextPageUri fora de {_MANAGE_HOST}"
                    )
                page, done = next_page, set()

            next_start = max(start, end - _OVERLAP)
            next_s = _iso(next_start)
            if end >= stop:
                if boundary.skipped:
                    logger.debug(
                        "microsoft_365 %s: %d blob(s) da sobreposição já lidos — pulados",
                        self.stream, boundary.skipped,
                    )
                self._final = {"startTime": next_s, **boundary.dump(next_s)}
                return
            # Catch-up: a próxima janela no mesmo ciclo, com a memória da borda.
            boundary = BoundaryKeys.load(boundary.dump(next_s), window_start=next_s)
            start, end, page, done = next_start, None, None, set()

    def _resume_cursor(
        self,
        start_s: str,
        end_s: str,
        page: Optional[str],
        done: Set[str],
        boundary: BoundaryKeys,
    ) -> Dict[str, Any]:
        cursor: Dict[str, Any] = {"startTime": start_s, "window_end": end_s}
        if page:
            cursor["page"] = page
        if done:
            cursor["done"] = sorted(done)
        # Mesma janela ⇒ a memória da fronteira dela continua valendo.
        cursor.update(boundary.carry())
        return cursor

    # ── HTTP ─────────────────────────────────────────────────────────────

    async def _request(
        self, method: str, url: str, params: Optional[Dict[str, str]]
    ) -> Tuple[int, Any, Any, str]:
        """``(status, json, headers, af_code)`` de uma chamada à feed API."""
        await self.ctx.rate_limiter.acquire(self.ctx.integration_id, self.platform)
        started = time.monotonic()
        try:
            async with self.ctx.domain_limiter.slot(self.domain):
                async with self.ctx.session.request(
                    method, url, params=params, headers=self.ctx.headers, allow_redirects=False
                ) as resp:
                    if resp.status >= 400:
                        code = await _af_code(resp)
                        if resp.status == 429 or code == _AF_THROTTLED:
                            await _raise_for_feed_status(resp, method, code)
                        return resp.status, None, resp.headers, code
                    payload = await resp.json(content_type=None)
                    return resp.status, payload, resp.headers, ""
        finally:
            API_LATENCY.labels(vendor=self.platform, stream=self.stream).observe(
                time.monotonic() - started
            )

    async def _fail(self, status: int, code: str, op: str, url: str) -> None:
        """Erro definitivo: ``_StreamDenied`` (stream opcional) ou exceção HTTP.

        A exceção carrega só o código ``AF…``: o corpo da resposta não vai para
        o log de erro da integração.
        """
        if self._OPTIONAL and status in (401, 403):
            raise _StreamDenied()
        logger.warning("microsoft_365 %s: %s devolveu HTTP %s %s", self.stream, op, status, code)
        target = URL(url)
        raise aiohttp.ClientResponseError(
            aiohttp.RequestInfo(target, "GET", CIMultiDictProxy(CIMultiDict()), target),
            (),
            status=status,
            message=f"{op}: {code or 'HTTP ' + str(status)}",
        )

    async def _start_subscription(self) -> None:
        url = f"{_feed_base(self._tenant)}/subscriptions/start"
        params = {"contentType": self._CONTENT_TYPE, "PublisherIdentifier": self._tenant}
        status, _payload, _headers, code = await self._request("POST", url, params)
        if status < 400 or code == _AF_ALREADY_ENABLED:
            if status < 400:
                logger.info(
                    "microsoft_365 %s: assinatura de %s iniciada — o primeiro conteúdo "
                    "pode levar até 12 h para aparecer (integration=%s)",
                    self.stream, self._CONTENT_TYPE, self.ctx.integration_id,
                )
            return
        await self._fail(status, code, "subscriptions/start", url)

    async def _list(
        self, page: Optional[str], start: datetime, end: datetime
    ) -> Optional[Tuple[List[Dict[str, Any]], Optional[str]]]:
        """Uma página da listagem. ``None`` = ``NextPageUri`` recusado (refazer)."""
        restarted = False
        while True:
            if page:
                url, params = page, _with_publisher(page, self._tenant)
            else:
                url = f"{_feed_base(self._tenant)}/subscriptions/content"
                params = {
                    "contentType": self._CONTENT_TYPE,
                    "startTime": _fmt(start),
                    "endTime": _fmt(end),
                    "PublisherIdentifier": self._tenant,
                }
            status, payload, headers, code = await self._request("GET", url, params)
            if status < 400:
                blobs = [b for b in (payload or []) if isinstance(b, dict)]
                return blobs, headers.get("NextPageUri")
            if code == _AF_NO_SUBSCRIPTION and not restarted:
                logger.warning(
                    "microsoft_365 %s: assinatura de %s não está ativa (parada no tenant?) "
                    "— reiniciando (integration=%s)",
                    self.stream, self._CONTENT_TYPE, self.ctx.integration_id,
                )
                await self._start_subscription()
                restarted = True
                continue
            if page and is_stale_cursor_response(status, has_opaque_token=True):
                log_stale_cursor(
                    f"microsoft_365 {self.stream}",
                    status=status,
                    integration_id=self.ctx.integration_id,
                    token_field="NextPageUri",
                    resume_from="início da janela",
                    body_preview=code,
                )
                return None
            await self._fail(status, code, "subscriptions/content", url)

    async def _read_blob(self, uri: Any) -> AsyncIterator[Dict[str, Any]]:
        """Eventos de um blob; blob expirado (> 7 dias) é pulado com aviso."""
        if not isinstance(uri, str) or not _feed_url_allowed(uri):
            # Nunca mandar o bearer do cliente para um host que a listagem citou.
            logger.warning(
                "microsoft_365 %s: contentUri fora de %s recusado (integration=%s)",
                self.stream, _MANAGE_HOST, self.ctx.integration_id,
            )
            return
        status, payload, _headers, code = await self._request(
            "GET", uri, _with_publisher(uri, self._tenant)
        )
        if status == 404 or code == _AF_CONTENT_EXPIRED:
            logger.warning(
                "microsoft_365 %s: blob expirado/inexistente (%s) — eventos dele "
                "não podem mais ser lidos (integration=%s)",
                self.stream, code or f"HTTP {status}", self.ctx.integration_id,
            )
            return
        if status >= 400:
            await self._fail(status, code, "contentUri", uri)
        for event in payload or []:
            if isinstance(event, dict):
                yield self._prepare(event)

    def _prepare(self, event: Dict[str, Any]) -> Dict[str, Any]:
        ip = clean_client_ip(event.get("ClientIP") or event.get("ActorIpAddress"))
        if ip:
            event["ClientIPNormalized"] = ip
        return event

    def extract_message_id(self, event: Dict[str, Any]) -> str:
        return str(event.get("Id") or "")

    @classmethod
    def watermark_at(cls, cursor: Optional[Dict[str, Any]]) -> Optional[datetime]:
        """``startTime``: início da janela atual (disponibilidade do conteúdo).

        Fica até ``_OVERLAP`` atrás do relógio por construção — é a sobreposição,
        não atraso. Não inclui a latência da própria Microsoft (60–90 min típicos
        entre o evento e o blob), que nenhum cursor do lado de cá enxerga.
        """
        return cls.watermark_from_iso(cursor, "startTime")


class Microsoft365AuditExchangeCollector(Microsoft365AuditAadCollector):
    stream = "audit_exchange"
    _CONTENT_TYPE = "Audit.Exchange"


class Microsoft365AuditSharePointCollector(Microsoft365AuditAadCollector):
    stream = "audit_sharepoint"
    _CONTENT_TYPE = "Audit.SharePoint"


class Microsoft365AuditGeneralCollector(Microsoft365AuditAadCollector):
    stream = "audit_general"
    _CONTENT_TYPE = "Audit.General"


class Microsoft365DlpCollector(Microsoft365AuditAadCollector):
    stream = "dlp"
    event_type = "microsoft_365.dlp"
    _CONTENT_TYPE = "DLP.All"
    _OPTIONAL = True

    def _prepare(self, event: Dict[str, Any]) -> Dict[str, Any]:
        _redact_detected_values(event)
        return super()._prepare(event)


_COLLECTORS = (
    Microsoft365AuditAadCollector,
    Microsoft365AuditExchangeCollector,
    Microsoft365AuditSharePointCollector,
    Microsoft365AuditGeneralCollector,
    Microsoft365DlpCollector,
)


# ── Teste de conexão (pré-save) ──────────────────────────────────────────


async def _m365_probe(cfg: Dict[str, Any]):
    """Teste STATELESS pré-save: token + quais content types estão assinados."""
    from ..output.base import TestResult

    raw_tenant = (cfg.get("tenant_id") or "").strip()
    client_id = (cfg.get("client_id") or "").strip()
    secret = cfg.get("client_secret") or ""
    if not raw_tenant or not client_id or not secret:
        return TestResult.failed("Informe o Tenant ID, o Client ID e o Client Secret.")
    try:
        tenant = validated_tenant_guid(raw_tenant)
        validated_client_id(client_id)
    except ValueError as exc:
        return TestResult.failed(str(exc))

    t0 = time.perf_counter()
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            token, _ = await _client_credentials_token(session, tenant, client_id, secret, _SCOPE)
            async with session.get(
                f"{_feed_base(tenant)}/subscriptions/list",
                params={"PublisherIdentifier": tenant},
                headers={"Authorization": f"Bearer {token}"},
                allow_redirects=False,
            ) as resp:
                if resp.status in (401, 403):
                    code = await _af_code(resp)
                    return TestResult.failed(
                        f"A Management API recusou o app (HTTP {resp.status}"
                        f"{' ' + code if code else ''}). Conceda a permissão de aplicativo "
                        "ActivityFeed.Read (Office 365 Management APIs) com consentimento do admin."
                    )
                if resp.status >= 400:
                    code = await _af_code(resp)
                    return TestResult.failed(
                        f"Falha ao listar as assinaturas (HTTP {resp.status}"
                        f"{' ' + code if code else ''}). Confira se a auditoria unificada "
                        "está ligada no Microsoft Purview."
                    )
                payload = await resp.json(content_type=None)
    except AzureTokenError as exc:
        return TestResult.failed(
            f"O Entra recusou a credencial: {exc.reason_code}. Confira o Tenant ID, o "
            "Client ID e o segredo (e se ele não expirou)."
        )
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        return TestResult.failed(f"Falha ao falar com o Microsoft 365: {type(exc).__name__}")

    enabled = {
        str((sub or {}).get("contentType") or ""): str((sub or {}).get("status") or "").lower()
        for sub in (payload if isinstance(payload, list) else [])
    }
    states: List[str] = []
    for cls in _COLLECTORS:
        status = enabled.get(cls._CONTENT_TYPE)
        if status == "enabled":
            states.append(f"{cls.stream}: assinatura ativa")
        elif status:
            states.append(f"{cls.stream}: assinatura {status} (será reiniciada no 1º ciclo)")
        else:
            states.append(f"{cls.stream}: sem assinatura (iniciada no 1º ciclo)")
    ms = (time.perf_counter() - t0) * 1000.0
    return TestResult.passed(f"Conexão OK — {'; '.join(states)}", latency_ms=ms)


# ── Self-registration ────────────────────────────────────────────────────


def _register() -> None:
    from datetime import timedelta as _td

    from ..queues import Q_BULK, T_COLLECT_BULK
    from ..registry import (
        AuthField,
        CollectorRegistration,
        PlatformRegistration,
        register,
        register_platform,
    )

    register_platform(
        PlatformRegistration(
            platform="microsoft_365",
            display_name="Microsoft 365",
            category="Cloud-audit",
            description=(
                "Microsoft 365 — auditoria unificada pela Management Activity API: logons e "
                "administração do Entra, regras e acesso a caixas do Exchange, arquivos e "
                "compartilhamento no SharePoint/OneDrive, Teams e casamentos de DLP."
            ),
            icon_id="microsoft",
            docs_url=(
                "https://learn.microsoft.com/office/office-365-management-api/"
                "office-365-management-activity-api-reference"
            ),
            order=53,
            test_fn=_m365_probe,
            required_secrets=("client_secret",),
            capabilities=frozenset(
                {"catalog", "auth:test"} | {f"collect:{cls.stream}" for cls in _COLLECTORS}
            ),
            auth_fields=(
                AuthField(key="tenant_id", label="Tenant ID", type="string", required=True,
                          help_text="Directory (tenant) ID do Microsoft Entra ID (GUID)"),
                AuthField(key="client_id", label="Client ID", type="string", required=True,
                          help_text=(
                              "Application (client) ID do app registration com a permissão de "
                              "aplicativo ActivityFeed.Read (Office 365 Management APIs) e "
                              "consentimento do admin"
                          )),
                AuthField(key="client_secret", label="Client Secret", type="secret", required=True),
            ),
        )
    )

    for collector_cls in _COLLECTORS:
        register(
            CollectorRegistration(
                platform=collector_cls.platform,
                stream=collector_cls.stream,
                collector_cls=collector_cls,
                refresh_fn=_m365_refresher,
                schedule=_td(minutes=5),
                queue=Q_BULK,
                task_name=T_COLLECT_BULK,
            )
        )


_register()
