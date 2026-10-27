"""Microsoft Azure — Activity Log (ARM) e logs de diagnóstico via Event Hubs.

Vendor novo = 1 módulo, ZERO core. Três streams, cada um com a fonte certa para
o que carrega:

- ``activity`` — **Azure Activity Log** do plano de controle, lido da API REST
  do Azure Monitor (``Microsoft.Insights/eventtypes/management/values``) em TODA
  assinatura que o service principal enxerga. É onde caem atribuição de papel
  (``Microsoft.Authorization/roleAssignments/write``), criação/remoção de
  recurso, ``listKeys`` de storage, mudança de NSG, política e os alertas do
  Defender for Cloud (categoria ``Security``). Não exige infraestrutura: basta o
  papel **Reader** (ou **Monitoring Reader**) no escopo a coletar.
- ``entra``    — logs do **Microsoft Entra ID** exportados por diagnostic setting
  para o Event Hub ``centralops-entra``: sign-ins interativos, NÃO interativos,
  de service principal e de managed identity, mais o AuditLogs. O Graph v1.0
  (coletor ``entra_id``) só entrega o sign-in interativo.
- ``resource`` — **resource logs** (diagnostic settings dos recursos) no Event
  Hub ``centralops-resource``: Key Vault (``AuditEvent``), audit do AKS
  (``kube-audit``), auditoria do Azure SQL (``SQLSecurityAuditEvents``),
  Storage e o que mais o cliente mandar.

**Por que Event Hubs e por que via Kafka.** É o único destino de diagnostic
setting que se lê como fila, com retenção, sem Log Analytics no meio (o padrão
de Splunk, Elastic, QRadar e Sentinel para "Azure fora do Azure"). O endpoint
Kafka do Event Hubs (tier Standard ou acima) fala o protocolo que o Core já
carrega (``aiokafka``) — nenhum SDK novo.

**Uma credencial só, sem chave compartilhada.** O mesmo app registration
(``tenant_id`` + ``client_id`` + ``client_secret``) autentica no ARM (escopo
``management.azure.com``) E no Event Hubs (SASL ``OAUTHBEARER`` com token do
Entra, papel **Azure Event Hubs Data Receiver**). Nenhuma connection string com
``SharedAccessKey`` é guardada, e revogar o acesso é tirar um papel no RBAC.
``base_url`` = namespace do Event Hubs; vazio desliga os dois streams de fila.
Event Hub que não existe = stream desligado (no-op): o cliente cria só os que
quer.

**Entrega at-least-once, posição no cursor.** Os streams de fila não usam
consumer group nem commit de offset no Kafka: a posição de cada partição vai no
cursor do CentralOps, que o pipeline só grava DEPOIS de despachar. Ciclo com erro
regrava o cursor anterior e a leitura recomeça do mesmo offset; a repetição cai
no dedupe. Nada é confirmado antes de ser entregue, e não há ack para perder.

**Superfície de rede fechada.** O token do Entra é um bearer: entregue ao host
errado, vale como a identidade do cliente. Então o namespace só é aceito sob
``servicebus.windows.net``, o ``nextLink`` do ARM só é seguido em
``management.azure.com`` e nenhuma chamada segue redirect. Tenant, client e
nomes viram PATH de URL só depois de validados.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import aiohttp

from ..base import BaseCollector
from ..cursor_boundary import BoundaryKeys, is_later
from ..metrics import API_LATENCY
from ._cursor_selfheal import is_stale_cursor_response, log_stale_cursor
from ._rate_limit import VendorRateLimitedError

logger = logging.getLogger(__name__)

_LOGIN_HOST = "login.microsoftonline.com"
_ARM_HOST = "management.azure.com"
_ARM = f"https://{_ARM_HOST}"
_ARM_SCOPE = f"{_ARM}/.default"
_SUBSCRIPTIONS_API = "2022-12-01"
_ACTIVITY_API = "2015-04-01"
_TOKEN_TIMEOUT = aiohttp.ClientTimeout(total=30)

#: Sufixo do namespace do Event Hubs na nuvem pública. Nuvens soberanas
#: (``servicebus.usgovcloudapi.net``, ``servicebus.chinacloudapi.cn``) têm login e
#: ARM em outros hosts — suportá-las é mudar os três juntos, não só este.
_EVENTHUB_SUFFIX = ".servicebus.windows.net"
_EVENTHUB_KAFKA_PORT = 9093

# ── Activity Log ─────────────────────────────────────────────────────────

#: Janela inicial (cold start) e sobreposição entre janelas. O Activity Log
#: publica o evento minutos depois do ``eventTimestamp`` — a ``submissionTimestamp``
#: costuma vir de 1 a 5 min depois, às vezes mais. A próxima janela recomeça
#: ``_LATE_ARRIVAL`` antes do fim da anterior para ainda achar o atrasado; o que
#: volta repetido é pulado pela memória da fronteira (``cursor_boundary``) e,
#: acima dela, pelo dedupe por ``eventDataId``.
_ACTIVITY_LOOKBACK = timedelta(hours=1)
_LATE_ARRIVAL = timedelta(minutes=20)
#: Limite da API: o Activity Log guarda 90 dias.
_ACTIVITY_RETENTION = timedelta(days=89)
#: Teto de páginas por CICLO, somando todas as assinaturas. Sem ele, um backlog
#: (cold start numa organização com dezenas de assinaturas, worker parado no fim
#: de semana) seria drenado num run só e estouraria o ``task_soft_time_limit``
#: (720 s) — o pipeline reverte o cursor e o ciclo seguinte recomeça do mesmo
#: ponto: poison-loop. No teto o cursor guarda a janela, as assinaturas que faltam
#: e o ``nextLink`` da atual.
_MAX_PAGES_PER_CYCLE = 50

# ── Event Hubs (Kafka) ───────────────────────────────────────────────────

#: Teto do ciclo nos streams de fila: mensagens Kafka (cada uma carrega um
#: ``records[]`` com dezenas de registros) e tempo puxando.
_MAX_MESSAGES_PER_CYCLE = 5000
_READ_BUDGET_SECONDS = 300
_FETCH_TIMEOUT_MS = 2000
_FETCH_MAX_RECORDS = 500
#: Buscas seguidas sem nada novo, com partição ainda atrás do fim, antes de
#: desistir do ciclo. Não deveria acontecer (o fim foi medido no início do
#: ciclo), mas um broker lento não pode prender o worker até o soft-timeout.
_MAX_IDLE_FETCHES = 5
_KAFKA_REQUEST_TIMEOUT_MS = 30_000

# Nomes que viram PATH de URL ou host: validar evita que um valor com ``/``,
# ``..`` ou ``@`` aponte para outro recurso ou outro servidor.
_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
_TENANT_DOMAIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9-]{1,63})+$")
_NAMESPACE_RE = re.compile(r"^[a-z][a-z0-9-]{4,48}[a-z0-9]$")
_AAD_CODE_RE = re.compile(r"AADSTS\d+")


class AzureRateLimitedError(VendorRateLimitedError):
    def __init__(self, retry_after: int) -> None:
        super().__init__(retry_after, vendor="azure")


class AzureTokenError(aiohttp.ClientResponseError):
    """O Entra recusou o client credentials.

    É um ``ClientResponseError`` com o status do Entra (400/401) para o pipeline
    tratar como qualquer falha de autenticação, mas a mensagem carrega só o
    código (``invalid_client (AADSTS7000215)``), nunca o corpo da resposta.
    """

    def __init__(self, resp: aiohttp.ClientResponse, reason: str) -> None:
        # Sem os headers do PEDIDO: o corpo do POST carrega o client_secret e
        # ``request_info`` acaba em log de exceção.
        info = aiohttp.RequestInfo(resp.url, resp.method, resp.headers, resp.url)
        super().__init__(
            info, getattr(resp, "history", ()), status=resp.status, message=reason,
            headers=resp.headers,
        )
        self.reason_code = reason


# ── Credencial e alvo ────────────────────────────────────────────────────


def validated_tenant(tenant: str) -> str:
    tenant = (tenant or "").strip()
    if _GUID_RE.match(tenant) or _TENANT_DOMAIN_RE.match(tenant):
        return tenant
    raise ValueError(f"Tenant ID inválido: {tenant!r} (use o GUID do diretório)")


def validated_client_id(client_id: str) -> str:
    client_id = (client_id or "").strip()
    if _GUID_RE.match(client_id):
        return client_id
    raise ValueError("Client ID inválido: use o Application (client) ID, um GUID")


def eventhub_fqdn(raw: Optional[str]) -> Optional[str]:
    """``base_url`` → FQDN do namespace do Event Hubs, ou ``None`` (desligado).

    Aceita o nome curto (``meu-ns``), o FQDN e a forma ``sb://…/`` da connection
    string, porque é o que o cliente copia do portal. Recusa qualquer host fora
    de ``servicebus.windows.net``: o worker manda o token do Entra para este
    host, e um ``base_url`` apontado para outro servidor o entregaria.
    """
    text = (raw or "").strip().lower()
    if not text:
        return None
    for scheme in ("sb://", "https://", "amqps://"):
        if text.startswith(scheme):
            text = text[len(scheme):]
            break
    text = text.rstrip("/")
    if text.endswith(f":{_EVENTHUB_KAFKA_PORT}"):
        text = text[: -len(f":{_EVENTHUB_KAFKA_PORT}")]
    name = text[: -len(_EVENTHUB_SUFFIX)] if text.endswith(_EVENTHUB_SUFFIX) else text
    if not _NAMESPACE_RE.match(name):
        raise ValueError(
            f"namespace do Event Hubs inválido: {raw!r} — use o nome do namespace "
            f"(ex.: meu-namespace) ou <nome>{_EVENTHUB_SUFFIX}"
        )
    return f"{name}{_EVENTHUB_SUFFIX}"


def _load_settings(integration_id: int) -> Dict[str, Any]:
    """Tenant, client, segredo e namespace da integração (sync, rodar em thread)."""
    from ...db import database, models
    from ...services import integration_secrets

    with database.SessionLocal() as db:
        integ = db.get(models.Integration, integration_id)
        if integ is None:
            raise RuntimeError(f"azure: integração {integration_id} não encontrada")
        return {
            "tenant_id": integ.tenant_id or "",
            "client_id": integ.client_id or "",
            "client_secret": integration_secrets.read_secret(integ, "client_secret") or "",
            "base_url": integ.base_url or "",
        }


async def _client_credentials_token(
    session: aiohttp.ClientSession, tenant: str, client_id: str, secret: str, scope: str
) -> Tuple[str, int]:
    """Token do Entra (client credentials) → ``(access_token, expires_in)``."""
    url = f"https://{_LOGIN_HOST}/{validated_tenant(tenant)}/oauth2/v2.0/token"
    data = {
        "grant_type": "client_credentials",
        "client_id": validated_client_id(client_id),
        "client_secret": secret,
        "scope": scope,
    }
    async with session.post(url, data=data, timeout=_TOKEN_TIMEOUT, allow_redirects=False) as resp:
        if resp.status >= 300:
            reason = await _aad_reason(resp)
            logger.warning("azure: o Entra recusou o token (%s) — HTTP %s", reason, resp.status)
            raise AzureTokenError(resp, reason)
        payload = await resp.json()
    return str(payload["access_token"]), int(payload.get("expires_in") or 3600)


async def _aad_reason(resp: aiohttp.ClientResponse) -> str:
    """Só ``error`` + código ``AADSTS``: o resto do corpo não volta para a UI."""
    try:
        body = await resp.json(content_type=None)
    except Exception:  # noqa: BLE001 — corpo ilegível não muda o erro
        return f"HTTP {resp.status}"
    if not isinstance(body, dict):
        return f"HTTP {resp.status}"
    error = str(body.get("error") or f"HTTP {resp.status}")[:64]
    code = _AAD_CODE_RE.search(str(body.get("error_description") or ""))
    return f"{error} ({code.group(0)})" if code else error


async def _arm_refresher(integration_id: int) -> Dict[str, object]:
    """Token do ARM para o stream ``activity``; o framework o guarda no cache.

    Registrado nos três streams: o cache de token é POR INTEGRAÇÃO, então os
    streams de fila não podem usar ``ctx.headers`` (o escopo é outro) e pegam o
    token do Event Hubs eles mesmos (``_EventHubTokenProvider``).
    """
    cfg = await asyncio.to_thread(_load_settings, integration_id)
    async with aiohttp.ClientSession() as session:
        token, expires_in = await _client_credentials_token(
            session, cfg["tenant_id"], cfg["client_id"], cfg["client_secret"], _ARM_SCOPE
        )
    return {"access_token": token, "expires_in": expires_in}


# ── ARM HTTP ─────────────────────────────────────────────────────────────


def _arm_url_allowed(url: str) -> bool:
    """Link que o ARM devolveu (``nextLink``) só é seguido no próprio ARM.

    O pedido leva o bearer do cliente: seguir um link para outro host o
    entregaria a quem quer que responda lá.
    """
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return parts.scheme == "https" and (parts.hostname or "").lower() == _ARM_HOST and parts.port in (None, 443)


async def _raise_for_arm_status(resp: aiohttp.ClientResponse, op: str) -> None:
    if resp.status < 400:
        return
    if resp.status == 429:
        raise AzureRateLimitedError(_parse_retry_after(resp.headers.get("Retry-After")))
    code = ""
    try:
        body = await resp.json(content_type=None)
        code = str(((body or {}).get("error") or {}).get("code") or "")[:80]
    except Exception:  # noqa: BLE001
        pass
    logger.warning("azure: %s devolveu HTTP %s %s", op, resp.status, code)
    # 401 sai como ``ClientResponseError`` — é o que faz o pipeline invalidar o
    # token em cache e marcar a integração com erro de autenticação.
    resp.raise_for_status()


def _parse_retry_after(value: Optional[str]) -> int:
    try:
        return max(1, int(value or ""))
    except (TypeError, ValueError):
        return 30


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


async def _list_subscriptions(
    session: aiohttp.ClientSession, headers: Dict[str, str]
) -> List[str]:
    """Assinaturas que o service principal enxerga — o escopo da coleta é o RBAC."""
    url: Optional[str] = f"{_ARM}/subscriptions?api-version={_SUBSCRIPTIONS_API}"
    found: List[str] = []
    while url:
        async with session.get(url, headers=headers, allow_redirects=False) as resp:
            await _raise_for_arm_status(resp, "subscriptions")
            payload = await resp.json()
        for sub in (payload or {}).get("value") or []:
            sub_id = str((sub or {}).get("subscriptionId") or "")
            if _GUID_RE.match(sub_id):
                found.append(sub_id.lower())
        url = (payload or {}).get("nextLink")
        if url and not _arm_url_allowed(url):
            raise RuntimeError("azure: nextLink de subscriptions fora de management.azure.com")
    return sorted(set(found))


class AzureActivityLogCollector(BaseCollector):
    """Azure Activity Log de todas as assinaturas visíveis (polling no ARM)."""

    platform = "azure"
    stream = "activity"
    event_type = "azure.activity"
    domain = _ARM_HOST

    async def collect(self) -> AsyncIterator[Dict[str, Any]]:
        cursor = self.ctx.cursor or {}
        now = datetime.now(timezone.utc)
        if "backfill_from_ts" in cursor:
            # A API é endereçável por janela: o backfill lê exatamente o pedido,
            # dentro dos 90 dias de retenção, e não mexe no cursor do polling.
            start = max(_parse_iso(cursor["backfill_from_ts"]) or now, now - _ACTIVITY_RETENTION)
            end = _parse_iso(cursor.get("backfill_to_ts")) or now
            subs = await self._subscriptions()
            async for event in self._read_window(subs, _iso(start), _iso(min(end, now)), None, None):
                yield event
            return

        window_start: str = cursor.get("eventTimestamp") or _iso(now - _ACTIVITY_LOOKBACK)
        start_dt = _parse_iso(window_start) or (now - _ACTIVITY_LOOKBACK)
        if start_dt < now - _ACTIVITY_RETENTION:
            # Worker parado por meses: o que passou de 90 dias já não existe na API.
            logger.warning(
                "azure activity: cursor %s além da retenção de 90 dias — retomando no limite "
                "(integration=%s)", window_start, self.ctx.integration_id,
            )
            window_start = _iso(now - _ACTIVITY_RETENTION)
        boundary = BoundaryKeys.load(cursor, window_start=window_start)

        resuming = bool(cursor.get("window_end"))
        window_end = cursor["window_end"] if resuming else _iso(now)
        pending: List[str] = list(cursor.get("pending") or []) if resuming else await self._subscriptions()
        next_link: Optional[str] = cursor.get("nextLink") if resuming else None
        if next_link and not _arm_url_allowed(next_link):
            next_link = None  # cursor adulterado/antigo: refaz a assinatura pelo filtro

        reader = self._read_window(pending, window_start, window_end, next_link, boundary)
        async for event in reader:
            yield event
        if self._capped is not None:
            subs_left, link = self._capped
            self.ctx.cursor = {
                "eventTimestamp": window_start,
                "window_end": window_end,
                "pending": subs_left,
                "nextLink": link,
                # Mesma janela ⇒ a memória da fronteira dela continua valendo.
                **boundary.carry(),
            }
            logger.info(
                "azure activity: teto de %d páginas/ciclo — %d assinatura(s) para o "
                "próximo ciclo (integration=%s)",
                _MAX_PAGES_PER_CYCLE, len(subs_left), self.ctx.integration_id,
            )
            return

        # Janela inteira lida. A próxima recomeça ``_LATE_ARRIVAL`` antes do fim,
        # nunca antes do início desta (janela curta não anda para trás).
        end_dt = _parse_iso(window_end) or now
        next_start = _iso(max(start_dt, end_dt - _LATE_ARRIVAL))
        if boundary.skipped:
            logger.debug(
                "azure activity: %d evento(s) da sobreposição já emitidos — pulados",
                boundary.skipped,
            )
        self.ctx.cursor = {"eventTimestamp": next_start, **boundary.dump(next_start)}

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        #: Preenchido por ``_read_window`` ao bater o teto: (assinaturas que faltam,
        #: ``nextLink`` da primeira delas).
        self._capped: Optional[Tuple[List[str], Optional[str]]] = None

    async def _subscriptions(self) -> List[str]:
        await self.ctx.rate_limiter.acquire(self.ctx.integration_id, self.platform)
        async with self.ctx.domain_limiter.slot(self.domain):
            subs = await _list_subscriptions(self.ctx.session, self.ctx.headers)
        if not subs:
            logger.warning(
                "azure activity: o service principal não enxerga nenhuma assinatura — "
                "atribua Reader/Monitoring Reader (integration=%s)", self.ctx.integration_id,
            )
        return subs

    async def _read_window(
        self,
        subs: List[str],
        start: str,
        end: str,
        next_link: Optional[str],
        boundary: Optional[BoundaryKeys],
    ) -> AsyncIterator[Dict[str, Any]]:
        """Lê ``[start, end]`` de cada assinatura; no teto, preenche ``_capped``."""
        self._capped = None
        flt = f"eventTimestamp ge '{start}' and eventTimestamp le '{end}'"
        by_filter = {"api-version": _ACTIVITY_API, "$filter": flt}
        pages = 0
        for index, sub in enumerate(subs):
            if not _GUID_RE.match(sub or ""):
                continue
            base = (
                f"{_ARM}/subscriptions/{sub}/providers/Microsoft.Insights/eventtypes/management/values"
            )
            is_link = bool(index == 0 and next_link)
            url: str = next_link if is_link else base  # type: ignore[assignment]
            params: Optional[Dict[str, str]] = None if is_link else by_filter
            while True:
                pages += 1
                if self.ctx.bounded_per_cycle and pages > _MAX_PAGES_PER_CYCLE:
                    # Retoma por esta assinatura: no ``nextLink`` se já paginava,
                    # senão pelo filtro.
                    self._capped = (subs[index:], url if is_link else None)
                    self.mark_cycle_capped()
                    return
                payload = await self._get_page(url, params, is_link=is_link)
                if payload is None:
                    if is_link:
                        # Link expirado: refaz a assinatura pela janela; o que
                        # voltar repetido a fronteira e o dedupe seguram.
                        url, params, is_link = base, by_filter, False
                        continue
                    break  # assinatura sem acesso/inexistente: segue para a próxima
                for event in payload.get("value") or []:
                    if not isinstance(event, dict):
                        continue
                    _warn_if_late(event)
                    if boundary is not None and boundary.seen(
                        event.get("eventTimestamp"), self.extract_message_id(event)
                    ):
                        continue
                    yield event
                link = payload.get("nextLink")
                if not link:
                    break
                if not _arm_url_allowed(link):
                    raise RuntimeError("azure activity: nextLink fora de management.azure.com")
                url, params, is_link = link, None, True

    async def _get_page(
        self, url: str, params: Optional[Dict[str, str]], *, is_link: bool
    ) -> Optional[Dict[str, Any]]:
        """Uma página. ``None`` = link expirado (``is_link``) ou assinatura inacessível."""
        await self.ctx.rate_limiter.acquire(self.ctx.integration_id, self.platform)
        started = time.monotonic()
        try:
            async with self.ctx.domain_limiter.slot(self.domain):
                async with self.ctx.session.get(
                    url, params=params, headers=self.ctx.headers, allow_redirects=False
                ) as resp:
                    if is_stale_cursor_response(resp.status, has_opaque_token=is_link):
                        log_stale_cursor(
                            "azure activity",
                            status=resp.status,
                            integration_id=self.ctx.integration_id,
                            token_field="nextLink",
                            resume_from="início da janela",
                            body_preview=(await resp.text())[:200],
                        )
                        return None
                    if resp.status in (403, 404) and not is_link:
                        # Papel removido de UMA assinatura (ou assinatura apagada)
                        # entre a listagem e a leitura: as outras seguem.
                        logger.warning(
                            "azure activity: HTTP %s ao ler %s — assinatura pulada "
                            "(integration=%s)",
                            resp.status, urllib.parse.urlsplit(url).path, self.ctx.integration_id,
                        )
                        return None
                    await _raise_for_arm_status(resp, "activity log")
                    return await resp.json()
        finally:
            API_LATENCY.labels(vendor=self.platform, stream=self.stream).observe(
                time.monotonic() - started
            )

    def extract_message_id(self, event: Dict[str, Any]) -> str:
        return str(event.get("eventDataId") or "")

    @classmethod
    def watermark_at(cls, cursor: Optional[Dict[str, Any]]) -> Optional[datetime]:
        """``eventTimestamp``: início da janela atual. Fica até ``_LATE_ARRIVAL``
        atrás do relógio por construção — é a sobreposição, não atraso."""
        return cls.watermark_from_iso(cursor, "eventTimestamp")


def _warn_if_late(event: Dict[str, Any]) -> None:
    """Evento publicado mais tarde que a sobreposição: os iguais a ele se perdem.

    Não muda a coleta (este chegou), mas é o único sinal de que
    ``_LATE_ARRIVAL`` ficou curto para aquele tenant.
    """
    ts = _parse_iso(event.get("eventTimestamp"))
    sub = _parse_iso(event.get("submissionTimestamp"))
    if ts and sub and sub - ts > _LATE_ARRIVAL:
        logger.warning(
            "azure activity: evento publicado %s depois do eventTimestamp (> sobreposição "
            "de %s) — eventos com esse atraso podem não ser lidos",
            sub - ts, _LATE_ARRIVAL,
        )


# ── Event Hubs via Kafka ─────────────────────────────────────────────────


def _kafka_token_provider_base():
    from aiokafka.abc import AbstractTokenProvider  # noqa: PLC0415

    return AbstractTokenProvider


class _EventHubTokenProvider:
    """Token do Entra para o SASL ``OAUTHBEARER`` do Event Hubs.

    O audience é o PRÓPRIO namespace (``https://<ns>.servicebus.windows.net``):
    o token não serve para nenhum outro recurso. Guardado só durante o ciclo.
    """

    def __init__(self, tenant: str, client_id: str, secret: str, fqdn: str) -> None:
        self._tenant, self._client_id, self._secret = tenant, client_id, secret
        self._scope = f"https://{fqdn}/.default"
        self._token: Optional[str] = None
        self._expires_at = 0.0

    async def token(self) -> str:
        if self._token and time.monotonic() < self._expires_at:
            return self._token
        async with aiohttp.ClientSession() as session:
            token, expires_in = await _client_credentials_token(
                session, self._tenant, self._client_id, self._secret, self._scope
            )
        self._token, self._expires_at = token, time.monotonic() + max(60, expires_in - 120)
        return token

    def extensions(self) -> Dict[str, str]:
        return {}


def _as_aiokafka_provider(provider: _EventHubTokenProvider):
    """Adapta para a ``AbstractTokenProvider`` do aiokafka (import tardio)."""
    base = _kafka_token_provider_base()

    class _Adapter(base):  # type: ignore[misc, valid-type]
        async def token(self) -> str:
            return await provider.token()

    return _Adapter()


def _make_consumer(fqdn: str, provider: _EventHubTokenProvider):
    """Consumer SEM consumer group: a posição vive no cursor do CentralOps.

    Ponto único de construção — os testes trocam esta função por um dublê.
    """
    from aiokafka import AIOKafkaConsumer  # noqa: PLC0415
    from aiokafka.helpers import create_ssl_context  # noqa: PLC0415

    return AIOKafkaConsumer(
        bootstrap_servers=f"{fqdn}:{_EVENTHUB_KAFKA_PORT}",
        security_protocol="SASL_SSL",
        sasl_mechanism="OAUTHBEARER",
        sasl_oauth_token_provider=_as_aiokafka_provider(provider),
        ssl_context=create_ssl_context(),
        group_id=None,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        client_id="centralops",
        request_timeout_ms=_KAFKA_REQUEST_TIMEOUT_MS,
        # Partição vazia é a regra num hub de log de segurança: não segurar
        # cada fetch esperando chegar mensagem.
        fetch_max_wait_ms=500,
    )


def _topic_partition(topic: str, partition: int):
    from aiokafka.structs import TopicPartition  # noqa: PLC0415

    return TopicPartition(topic, partition)


class AzureEventHubEntraCollector(BaseCollector):
    """Logs do Entra ID exportados para o Event Hub ``centralops-entra``."""

    platform = "azure"
    stream = "entra"
    event_type = "azure.entra"
    _HUB = "centralops-entra"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._fqdn: Optional[str] = None
        #: Instante até onde o hub foi lido neste ciclo (``_drain``).
        self._watermark: Optional[str] = None

    @property
    def domain(self) -> str:
        # Semáforo POR NAMESPACE: um namespace de um cliente lento não segura a
        # coleta dos outros.
        return self._fqdn or "eventhubs.azure"

    async def collect(self) -> AsyncIterator[Dict[str, Any]]:
        cursor = self.ctx.cursor or {}
        if "backfill_from_ts" in cursor:
            # A posição da fila vive no cursor do polling; um backfill que
            # lesse daqui andaria com ela sem gravá-la. O replay de um hub é
            # o próprio cursor: zerar ``offsets`` relê toda a retenção.
            logger.info(
                "azure %s: backfill por janela não se aplica ao Event Hub — ignorado "
                "(integration=%s)", self.stream, self.ctx.integration_id,
            )
            return
        cfg = await asyncio.to_thread(_load_settings, self.ctx.integration_id)
        self._fqdn = eventhub_fqdn(cfg["base_url"])
        if not self._fqdn:
            return  # sem namespace: só o stream ``activity`` está em uso
        provider = _EventHubTokenProvider(
            cfg["tenant_id"], cfg["client_id"], cfg["client_secret"], self._fqdn
        )
        offsets: Dict[str, int] = {
            str(k): int(v) for k, v in (cursor.get("offsets") or {}).items()
            if isinstance(v, int) and not isinstance(v, bool) and v >= 0
        }
        watermark: Optional[str] = cursor.get("watermark")

        await self.ctx.rate_limiter.acquire(self.ctx.integration_id, self.platform)
        async with self.ctx.domain_limiter.slot(self.domain):
            consumer = _make_consumer(self._fqdn, provider)
            try:
                # ``start`` dentro do ``try``: falhar no meio do handshake (token
                # recusado, porta fechada) não pode deixar conexão aberta.
                await consumer.start()
                topics = await consumer.topics()
                if self._HUB not in topics:
                    logger.info(
                        "azure %s: Event Hub %s não existe em %s — stream desligado "
                        "(integration=%s)", self.stream, self._HUB, self._fqdn,
                        self.ctx.integration_id,
                    )
                    return
                async for event in self._drain(consumer, offsets):
                    yield event
                watermark = self._watermark or watermark
            finally:
                await consumer.stop()

        self.ctx.cursor = {"offsets": dict(sorted(offsets.items())), "watermark": watermark}

    async def _drain(self, consumer, offsets: Dict[str, int]) -> AsyncIterator[Dict[str, Any]]:
        """Lê cada partição do offset gravado até o fim medido AGORA.

        Atualiza ``offsets`` (próximo offset a ler) mensagem a mensagem e deixa em
        ``_watermark`` o instante até onde o hub foi lido.
        """
        snapshot = _iso(datetime.now(timezone.utc))
        partitions = sorted(consumer.partitions_for_topic(self._HUB) or ())
        tps = [_topic_partition(self._HUB, p) for p in partitions]
        if not tps:
            return
        consumer.assign(tps)
        first = await consumer.beginning_offsets(tps)
        last = await consumer.end_offsets(tps)
        for tp in tps:
            key = str(tp.partition)
            saved = offsets.get(key)
            if saved is None:
                # Partição nova (ou primeiro ciclo): toda a retenção do hub.
                saved = first[tp]
            elif saved < first[tp]:
                # A retenção do hub passou do ponto onde paramos: o que ficou no
                # meio JÁ se perdeu. Não há como recuperar — só não pode ser mudo.
                logger.error(
                    "azure %s: partição %s do hub %s perdeu %d mensagem(ns) por retenção "
                    "(offset gravado %d < primeiro disponível %d) — worker parado por mais "
                    "tempo que a retenção do Event Hub (integration=%s)",
                    self.stream, key, self._HUB, first[tp] - saved, saved, first[tp],
                    self.ctx.integration_id,
                )
                saved = first[tp]
            elif saved > last[tp]:
                # Hub recriado com o mesmo nome: os offsets recomeçaram do zero.
                logger.warning(
                    "azure %s: offset gravado %d além do fim %d na partição %s — hub "
                    "recriado? relendo do início (integration=%s)",
                    self.stream, saved, last[tp], key, self.ctx.integration_id,
                )
                saved = first[tp]
            offsets[key] = saved
            consumer.seek(tp, saved)

        behind = [tp for tp in tps if offsets[str(tp.partition)] < last[tp]]
        newest: Optional[str] = None
        started = time.monotonic()
        messages = idle = 0
        while behind:
            if self.ctx.bounded_per_cycle and (
                messages >= _MAX_MESSAGES_PER_CYCLE
                or time.monotonic() - started >= _READ_BUDGET_SECONDS
            ):
                # Teto com partição ainda atrás do fim: sobrou fila. O watermark
                # fica no enqueue mais novo já lido, não em "agora".
                self.mark_cycle_capped()
                self._watermark = newest
                return
            batch = await consumer.getmany(
                *behind, timeout_ms=_FETCH_TIMEOUT_MS, max_records=_FETCH_MAX_RECORDS
            )
            if not any(batch.values()):
                idle += 1
                if idle >= _MAX_IDLE_FETCHES:
                    logger.warning(
                        "azure %s: Event Hub sem resposta com partições atrás do fim — "
                        "ciclo encerrado no que foi lido (integration=%s)",
                        self.stream, self.ctx.integration_id,
                    )
                    self._watermark = newest
                    return
                continue
            idle = 0
            for tp, records in batch.items():
                for msg in records:
                    messages += 1
                    offsets[str(tp.partition)] = msg.offset + 1
                    enqueued = _epoch_ms_iso(msg.timestamp)
                    if enqueued and (newest is None or is_later(enqueued, newest)):
                        newest = enqueued
                    for event in _decode_records(msg.value):
                        yield event
            behind = [tp for tp in behind if offsets[str(tp.partition)] < last[tp]]
        # Tudo o que estava no hub no início do ciclo foi lido.
        self._watermark = snapshot

    def extract_message_id(self, event: Dict[str, Any]) -> str:
        props = event.get("properties") if isinstance(event.get("properties"), dict) else {}
        uid = props.get("id") or props.get("correlationId")
        if not uid:
            return ""  # o pipeline cai no hash do evento
        return f"{event.get('category') or ''}|{uid}"

    @classmethod
    def watermark_at(cls, cursor: Optional[Dict[str, Any]]) -> Optional[datetime]:
        """``watermark``: enqueue mais novo lido, ou o instante em que o hub foi
        lido até o fim."""
        return cls.watermark_from_iso(cursor, "watermark")


class AzureEventHubResourceCollector(AzureEventHubEntraCollector):
    """Resource logs (diagnostic settings) no Event Hub ``centralops-resource``."""

    stream = "resource"
    event_type = "azure.resource"
    _HUB = "centralops-resource"

    def extract_message_id(self, event: Dict[str, Any]) -> str:
        # Resource log não tem id único comum a todas as categorias: o hash do
        # evento é a chave certa para a releitura at-least-once.
        return ""


_HUB_COLLECTORS = (AzureEventHubEntraCollector, AzureEventHubResourceCollector)


def _epoch_ms_iso(value: Any) -> Optional[str]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return _iso(datetime.fromtimestamp(value / 1000.0, tz=timezone.utc))
    except (OverflowError, OSError, ValueError):
        return None


def _decode_records(value: Any) -> List[Dict[str, Any]]:
    """Corpo da mensagem → os registros que o diagnostic setting publicou.

    O Azure Monitor publica ``{"records": [...]}``; outra forma (um objeto só,
    texto) sai como evento em vez de sumir. O ``log`` do audit do AKS vem como
    TEXTO JSON dentro do registro — é decodificado aqui, como o base64 do
    Pub/Sub, para o mapping enxergar ``verb``/``user``/``objectRef``.
    """
    if value is None:
        return []
    text = value.decode("utf-8", errors="replace") if isinstance(value, (bytes, bytearray)) else str(value)
    try:
        parsed = json.loads(text)
    except ValueError:
        return [{"message": text}]
    if isinstance(parsed, dict) and isinstance(parsed.get("records"), list):
        records = [r for r in parsed["records"] if isinstance(r, dict)]
    elif isinstance(parsed, dict):
        records = [parsed]
    else:
        return [{"message": text}]
    for record in records:
        props = record.get("properties")
        if (
            isinstance(props, dict)
            and isinstance(props.get("log"), str)
            and str(record.get("category") or "").startswith("kube-audit")
        ):
            try:
                inner = json.loads(props["log"])
            except ValueError:
                continue
            if isinstance(inner, dict):
                props["log"] = inner
    return records


# ── Teste de conexão (pré-save) ──────────────────────────────────────────


async def _probe_hubs(fqdn: str, provider: _EventHubTokenProvider) -> Dict[str, str]:
    """Estado de cada hub no namespace, sem ler mensagem."""
    consumer = _make_consumer(fqdn, provider)
    try:
        await consumer.start()
        topics = await consumer.topics()
    finally:
        await consumer.stop()
    return {
        cls.stream: ("ok" if cls._HUB in topics else "não existe (stream desligado)")
        for cls in _HUB_COLLECTORS
    }


async def _azure_probe(cfg: Dict[str, Any]):
    """Teste STATELESS pré-save: token, assinaturas visíveis e hubs do namespace."""
    from ..output.base import TestResult

    tenant = (cfg.get("tenant_id") or "").strip()
    client_id = (cfg.get("client_id") or "").strip()
    secret = (cfg.get("client_secret") or "").strip()
    if not tenant or not client_id or not secret:
        return TestResult.failed("Informe Tenant ID, Client ID e Client Secret.")
    try:
        validated_tenant(tenant)
        validated_client_id(client_id)
        fqdn = eventhub_fqdn(cfg.get("base_url"))
    except ValueError as exc:
        return TestResult.failed(str(exc))

    t0 = time.perf_counter()
    states: List[str] = []
    ready = 0
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            token, _ = await _client_credentials_token(session, tenant, client_id, secret, _ARM_SCOPE)
            headers = {"Authorization": f"Bearer {token}"}
            try:
                subs = await _list_subscriptions(session, headers)
            except aiohttp.ClientResponseError as exc:
                states.append(f"activity: erro HTTP {exc.status} ao listar assinaturas")
            else:
                if subs:
                    ready += 1
                    states.append(f"activity: ok ({len(subs)} assinatura(s))")
                else:
                    states.append(
                        "activity: nenhuma assinatura visível (atribua Reader ou Monitoring Reader)"
                    )
    except AzureTokenError as exc:
        return TestResult.failed(
            f"O Entra recusou a credencial: {exc.reason_code}. Confira o Tenant ID, o "
            "Client ID e o segredo (e se ele não expirou)."
        )
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        return TestResult.failed(f"Falha ao falar com o Azure: {type(exc).__name__}")

    if fqdn:
        provider = _EventHubTokenProvider(tenant, client_id, secret, fqdn)
        try:
            hubs = await asyncio.wait_for(_probe_hubs(fqdn, provider), timeout=30)
        except AzureTokenError as exc:
            states.append(f"Event Hubs: o Entra recusou o token ({exc.reason_code})")
        except Exception as exc:  # noqa: BLE001 — erro do broker vira estado, não 500
            logger.info("azure probe: Event Hubs inacessível", exc_info=True)
            states.append(
                f"Event Hubs ({fqdn}): inacessível ({type(exc).__name__}) — confira o tier "
                "Standard+, a porta 9093 e o papel Azure Event Hubs Data Receiver"
            )
        else:
            for stream, state in hubs.items():
                if state == "ok":
                    ready += 1
                states.append(f"{stream}: {state}")
    else:
        states.append("entra/resource: sem namespace do Event Hubs (streams desligados)")

    detail = "; ".join(states)
    if not ready:
        return TestResult.failed(f"Nenhum stream utilizável — {detail}")
    ms = (time.perf_counter() - t0) * 1000.0
    return TestResult.passed(f"Conexão OK — {detail}", latency_ms=ms)


# ── Self-registration ────────────────────────────────────────────────────

_COLLECTORS = (AzureActivityLogCollector, *_HUB_COLLECTORS)


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
            platform="azure",
            display_name="Microsoft Azure",
            category="Cloud-audit",
            description=(
                "Microsoft Azure — Activity Log de todas as assinaturas (RBAC, recursos, "
                "Defender for Cloud) e, via Event Hubs, sign-ins do Entra ID (inclusive não "
                "interativos) e resource logs (Key Vault, AKS, Azure SQL, Storage)."
            ),
            icon_id="azure",
            docs_url="https://learn.microsoft.com/azure/azure-monitor/essentials/activity-log",
            order=52,
            test_fn=_azure_probe,
            required_secrets=("client_secret",),
            capabilities=frozenset(
                {"catalog", "auth:test"} | {f"collect:{cls.stream}" for cls in _COLLECTORS}
            ),
            auth_fields=(
                AuthField(key="tenant_id", label="Tenant ID", type="string", required=True,
                          help_text="Directory (tenant) ID do Microsoft Entra ID"),
                AuthField(key="client_id", label="Client ID", type="string", required=True,
                          help_text=(
                              "Application (client) ID do app registration — papel Reader ou "
                              "Monitoring Reader nas assinaturas/management group"
                          )),
                AuthField(key="client_secret", label="Client Secret", type="secret", required=True),
                AuthField(key="base_url", label="Namespace do Event Hubs", type="string",
                          required=False,
                          help_text=(
                              "Opcional. Ex.: meu-namespace (tier Standard+). Lê os hubs "
                              "centralops-entra e centralops-resource com o papel Azure Event "
                              "Hubs Data Receiver. Vazio = só o Activity Log."
                          )),
            ),
        )
    )

    for collector_cls, schedule in (
        (AzureActivityLogCollector, _td(minutes=5)),
        (AzureEventHubEntraCollector, _td(minutes=2)),
        (AzureEventHubResourceCollector, _td(minutes=2)),
    ):
        register(
            CollectorRegistration(
                platform=collector_cls.platform,
                stream=collector_cls.stream,
                collector_cls=collector_cls,
                refresh_fn=_arm_refresher,
                schedule=schedule,
                queue=Q_BULK,
                task_name=T_COLLECT_BULK,
            )
        )


_register()
