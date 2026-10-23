"""Google Cloud — Cloud Audit Logs, GKE e Cloud SQL (MySQL) via Pub/Sub (pull).

Vendor novo = 1 módulo, ZERO core. Um coletor cobre o que um sink do Cloud
Logging exporta, um stream por subscription:

- ``audit``          — Cloud Audit Logs de qualquer serviço: Admin Activity, Data
  Access, System Event e Policy Denied. É onde caem IAM (``SetIamPolicy``,
  ``CreateServiceAccountKey``), as mudanças de instância do Cloud SQL
  (``cloudsql.instances.update``) e o login do Workspace/Cloud Identity
  (``login.googleapis.com``, exige sink da ORGANIZAÇÃO).
- ``gke_audit``      — audit do Kubernetes do GKE (``serviceName=k8s.io``):
  ``kubectl exec``, RBAC, leitura de secret.
- ``cloudsql_mysql`` — auditoria DENTRO do banco (plugin ``cloudsql_mysql_audit``):
  conexão, login falho, GRANT, CREATE USER, DDL, queries auditadas.

**Por que Pub/Sub e não a Logging API.** ``entries.list`` tem cota de 60
req/min por projeto, sem aumento, e nem ack nem cursor: serve para investigar,
não para coletar. Sink agregado → tópico → subscription pull é o padrão de
mercado (Splunk, Elastic, Sentinel, Panther, Cribl).

**Creds (reuso GENÉRICO das colunas, editáveis pelo PUT de hoje):**
``tenant_id`` = Project ID das subscriptions; ``base_url`` = prefixo das
subscriptions (default ``centralops`` → ``centralops-audit``,
``centralops-gke-audit``, ``centralops-cloudsql-mysql``); ``client_secret`` = JSON
da chave de service account OU da configuração Workload Identity Federation
(``external_account``). Subscription que não existe = stream desligado (no-op):
o cliente cria só as que quer.

**Entrega at-least-once.** Pull → ``yield`` → o pipeline despacha e grava o
cursor → ``on_cycle_committed`` dá o ack. Ciclo com erro → ``on_cycle_aborted``
devolve as mensagens (prazo 0) e o Pub/Sub reentrega. Ack DENTRO de
``collect()`` apagaria da fila um evento que o ciclo ainda podia perder.
Reentregas (o Pub/Sub reentrega até depois de um ack) caem no dedupe por
``logName|timestamp|insertId``.

**A credencial é um JSON do cliente, e ``external_account`` aponta URLs.** O
token é obtido por um transporte próprio (``_guarded_request``) que só fala com
os hosts de token do Google e com o endpoint de metadata de nuvem (WIF a partir
de AWS/Azure), sem seguir redirect e sem ler arquivo local. Sem isso, um JSON
forjado faria o worker buscar URL interna ou arquivo do próprio servidor.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, Iterator, List, Optional, Tuple

import aiohttp

from ..base import BaseCollector
from ..cursor_boundary import is_later
from ..metrics import API_LATENCY
from ._rate_limit import VendorRateLimitedError

logger = logging.getLogger(__name__)

_PUBSUB_HOST = "pubsub.googleapis.com"
_PUBSUB_API = f"https://{_PUBSUB_HOST}/v1"
_SCOPE = "https://www.googleapis.com/auth/pubsub"
_DEFAULT_PREFIX = "centralops"
_NO_SDK_MSG = "google-auth não instalado — instale: pip install -r requirements-sinks.txt"

# Teto do ciclo: 20 pulls × 1000 mensagens = 20.000 eventos, e no máximo
# ``_PULL_BUDGET_SECONDS`` puxando. Sem ele, um backlog (sink novo, worker parado
# no fim de semana) seria drenado num run só e estouraria o
# ``task_soft_time_limit`` (720 s) → o pipeline reverte o cursor, nada recebe ack
# e o ciclo seguinte recomeça do mesmo ponto: poison-loop. O orçamento de tempo
# fica bem abaixo do prazo das mensagens (``_LEASE_SECONDS``) para o ack do fim
# do ciclo chegar antes de o Pub/Sub dar a mensagem por perdida.
_MAX_MESSAGES_PER_PULL = 1000
_MAX_PULLS_PER_CYCLE = 20
_PULL_BUDGET_SECONDS = 300
#: Uma pull sem mensagem disponível segura a conexão aberta esperando chegar
#: alguma. Esgotar o tempo de LEITURA é a resposta "fila vazia agora".
_PULL_WAIT_SECONDS = 10
_PULL_TIMEOUT = aiohttp.ClientTimeout(
    total=_PULL_WAIT_SECONDS + 30, sock_connect=10, sock_read=_PULL_WAIT_SECONDS
)
#: Prazo pedido para as mensagens recém-lidas. A subscription nasce com 10 s se
#: ninguém configurar; sem esticar, um ciclo mais lento que isso receberia as
#: próprias mensagens de volta no meio do caminho.
_LEASE_SECONDS = 600
_ACK_BATCH = 500
_SETTLE_TIMEOUT = aiohttp.ClientTimeout(total=60)

# Hosts que o transporte de autenticação aceita. HTTPS para o Google; HTTP só
# para o endpoint de metadata (é como AWS e Azure entregam a identidade da
# máquina para o WIF).
_AUTH_HOSTS = frozenset(
    {
        "oauth2.googleapis.com",
        "accounts.google.com",
        "sts.googleapis.com",
        "iamcredentials.googleapis.com",
    }
)
_METADATA_HOSTS = frozenset({"169.254.169.254", "fd00:ec2::254"})
_SA_TOKEN_URIS = frozenset(
    {"https://oauth2.googleapis.com/token", "https://accounts.google.com/o/oauth2/token"}
)
_STS_TOKEN_URL = "https://sts.googleapis.com/v1/token"

# Nomes de projeto e de subscription viram PATH da URL: validar evita que um
# valor com ``/`` ou ``..`` aponte para outro recurso da API.
_PROJECT_RE = re.compile(r"^(?:[a-z0-9.-]+:)?[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_PREFIX_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._~+-]{0,200}$")


class GcpPubSubRateLimitedError(VendorRateLimitedError):
    def __init__(self, retry_after: int) -> None:
        super().__init__(retry_after, vendor="gcp_pubsub")


class GcpCredentialError(ValueError):
    """Credencial JSON recusada ANTES de qualquer chamada de rede."""


class GcpPubSubAuditCollector(BaseCollector):
    """Cloud Audit Logs (todas as categorias) de uma subscription Pub/Sub."""

    platform = "gcp_pubsub"
    stream = "audit"
    event_type = "gcp.audit"
    _SUBSCRIPTION_SUFFIX = "audit"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._project: Optional[str] = None
        self._subscription: Optional[str] = None
        # ackIds lidos neste ciclo — confirmados (ou devolvidos) só no fim.
        self._ack_ids: List[str] = []

    @property
    def domain(self) -> str:
        # Semáforo POR PROJETO: a cota do Pub/Sub é por projeto, e uma pull de
        # fila vazia segura a vaga por até ``_PULL_WAIT_SECONDS``. Com o host
        # inteiro num semáforo só, os projetos ociosos de uns clientes
        # atrasariam a coleta dos outros.
        return f"{_PUBSUB_HOST}/{self._project}" if self._project else _PUBSUB_HOST

    async def collect(self) -> AsyncIterator[Dict[str, Any]]:
        cursor = self.ctx.cursor or {}
        if "backfill_from_ts" in cursor:
            # Fila não é endereçável por janela de tempo (replay exige ``seek``
            # com retenção de mensagens confirmadas). Puxar aqui consumiria a
            # fila do ciclo normal sem dar ack — então não puxa.
            logger.info(
                "gcp_pubsub %s: backfill por janela não se aplica a fila — ignorado "
                "(integration=%s)",
                self.stream, self.ctx.integration_id,
            )
            return

        self._project, prefix = await asyncio.to_thread(self._load_target)
        self._subscription = _subscription_path(self._project, prefix, self._SUBSCRIPTION_SUFFIX)
        watermark: Optional[str] = cursor.get("watermark")
        started = time.monotonic()
        pulls = 0
        while True:
            if self.ctx.bounded_per_cycle and (
                pulls >= _MAX_PULLS_PER_CYCLE
                or time.monotonic() - started >= _PULL_BUDGET_SECONDS
            ):
                # Teto do ciclo depois de uma pull NÃO vazia: sobrou fila. O
                # watermark fica no ``publishTime`` mais novo já lido, e não em
                # "agora" — há mensagens mais antigas esperando.
                self.ctx.cursor = {"watermark": watermark}
                self.mark_cycle_capped()
                return
            pulls += 1
            received = await self._pull()
            if received is None:
                return  # subscription não existe: stream desligado
            if not received:
                break
            ack_ids = [str(item["ackId"]) for item in received if item.get("ackId")]
            self._ack_ids.extend(ack_ids)
            await self._extend_lease(ack_ids)
            for item in received:
                message = item.get("message") or {}
                published = message.get("publishTime")
                if isinstance(published, str) and (
                    watermark is None or is_later(published, watermark)
                ):
                    watermark = published
                event = _decode_log_entry(message)
                if event is not None:
                    yield event

        # Fila drenada: tudo o que foi publicado até agora já foi lido. Sem isto
        # um stream quieto mostraria o atraso crescendo para sempre, desde a
        # última mensagem — que é justamente quando ele está em dia.
        self.ctx.cursor = {"watermark": _utc_now_iso()}

    def _load_target(self) -> Tuple[str, str]:
        """Project ID + prefixo das subscriptions (sync, em thread)."""
        from ...db import database, models

        with database.SessionLocal() as db:
            integ = db.get(models.Integration, self.ctx.integration_id)
            if integ is None:
                raise RuntimeError(f"gcp_pubsub: integração {self.ctx.integration_id} não encontrada")
            project = (integ.tenant_id or "").strip()
            prefix = (integ.base_url or "").strip() or _DEFAULT_PREFIX
        return _validated_target(project, prefix)

    async def _pull(self) -> Optional[List[Dict[str, Any]]]:
        """Uma pull. ``None`` = a subscription não existe; ``[]`` = nada agora."""
        url = f"{_PUBSUB_API}/{self._subscription}:pull"
        await self.ctx.rate_limiter.acquire(self.ctx.integration_id, self.platform)
        started = time.monotonic()
        try:
            async with self.ctx.domain_limiter.slot(self.domain):
                async with self.ctx.session.post(
                    url,
                    json={"maxMessages": _MAX_MESSAGES_PER_PULL},
                    headers=self.ctx.headers,
                    timeout=_PULL_TIMEOUT,
                ) as resp:
                    if resp.status == 404:
                        logger.info(
                            "gcp_pubsub %s: subscription %s não existe — stream desligado "
                            "(integration=%s)",
                            self.stream, self._subscription, self.ctx.integration_id,
                        )
                        return None
                    await _raise_for_pubsub_status(resp, "pull")
                    payload = await resp.json()
        except aiohttp.SocketTimeoutError:
            # Só o tempo de LEITURA cai aqui. Timeout de CONEXÃO é outra classe
            # (``ConnectionTimeoutError``) e continua sendo erro: Google
            # inalcançável não pode virar um ciclo vazio "verde".
            return []
        finally:
            API_LATENCY.labels(vendor=self.platform, stream=self.stream).observe(
                time.monotonic() - started
            )
        return list((payload or {}).get("receivedMessages") or [])

    async def _extend_lease(self, ack_ids: List[str]) -> None:
        """Estica o prazo das mensagens recém-lidas para ``_LEASE_SECONDS``.

        Falhar aqui não derruba o ciclo: no pior caso o Pub/Sub reentrega no
        prazo da própria subscription e o dedupe absorve.
        """
        for chunk in _chunks(ack_ids, _ACK_BATCH):
            try:
                await self._post(
                    self.ctx.session,
                    ":modifyAckDeadline",
                    {"ackIds": chunk, "ackDeadlineSeconds": _LEASE_SECONDS},
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "gcp_pubsub %s: não consegui esticar o prazo de %d mensagem(ns) — "
                    "podem ser reentregues (integration=%s)",
                    self.stream, len(chunk), self.ctx.integration_id, exc_info=True,
                )

    async def on_cycle_committed(self) -> None:
        """Cursor gravado: confirma (ack) tudo o que este ciclo leu."""
        await self._settle(":acknowledge", {})

    async def on_cycle_aborted(self) -> None:
        """Ciclo com erro: devolve as mensagens (prazo 0) para reentrega imediata."""
        await self._settle(":modifyAckDeadline", {"ackDeadlineSeconds": 0})

    async def _settle(self, verb: str, extra: Dict[str, Any]) -> None:
        if not self._ack_ids or not self._subscription:
            return
        ack_ids, self._ack_ids = self._ack_ids, []
        failed = 0
        # A sessão do ciclo já fechou quando o pipeline chama os ganchos de fim
        # de ciclo — por isso uma sessão própria.
        async with aiohttp.ClientSession(timeout=_SETTLE_TIMEOUT) as session:
            for chunk in _chunks(ack_ids, _ACK_BATCH):
                try:
                    await self._post(session, verb, {"ackIds": chunk, **extra})
                except Exception:  # noqa: BLE001
                    failed += len(chunk)
                    logger.debug("gcp_pubsub %s falhou", verb, exc_info=True)
        if failed:
            raise RuntimeError(
                f"gcp_pubsub {self.stream}: {verb.lstrip(':')} falhou para {failed} de "
                f"{len(ack_ids)} mensagem(ns) — o Pub/Sub vai reentregá-las"
            )

    async def _post(self, session: aiohttp.ClientSession, verb: str, body: Dict[str, Any]) -> None:
        url = f"{_PUBSUB_API}/{self._subscription}{verb}"
        async with session.post(url, json=body, headers=self.ctx.headers) as resp:
            await _raise_for_pubsub_status(resp, verb.lstrip(":"))

    def extract_message_id(self, event: Dict[str, Any]) -> str:
        insert_id = event.get("insertId")
        if not insert_id:
            return ""  # o pipeline cai no hash do evento
        # ``insertId`` só é único dentro do mesmo log e instante.
        return f"{event.get('logName') or ''}|{event.get('timestamp') or ''}|{insert_id}"

    @classmethod
    def watermark_at(cls, cursor: Optional[Dict[str, Any]]) -> Optional[datetime]:
        """``watermark``: ``publishTime`` mais novo lido, ou o instante em que a
        fila foi drenada (ver o fim de ``collect``)."""
        return cls.watermark_from_iso(cursor, "watermark")


class GcpPubSubGkeAuditCollector(GcpPubSubAuditCollector):
    """Audit do Kubernetes do GKE (``serviceName=k8s.io``)."""

    stream = "gke_audit"
    event_type = "gcp.gke_audit"
    _SUBSCRIPTION_SUFFIX = "gke-audit"


class GcpPubSubCloudSqlMysqlCollector(GcpPubSubAuditCollector):
    """Auditoria do banco Cloud SQL for MySQL (``MysqlAuditEntry``)."""

    stream = "cloudsql_mysql"
    event_type = "gcp.cloudsql_mysql"
    _SUBSCRIPTION_SUFFIX = "cloudsql-mysql"


_COLLECTORS = (GcpPubSubAuditCollector, GcpPubSubGkeAuditCollector, GcpPubSubCloudSqlMysqlCollector)


# ── Mensagem → evento ──────────────────────────────────────────────────


def _decode_log_entry(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """``message.data`` (base64) → o ``LogEntry`` que o sink publicou.

    Texto que não é JSON (sink com formato inesperado) sai como
    ``{"textPayload": ...}`` em vez de sumir — e a mensagem é confirmada do mesmo
    jeito, porque reentregar o mesmo lixo não muda nada.
    """
    data = message.get("data")
    if not data:
        return None
    try:
        text = base64.b64decode(data, validate=True).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError):
        logger.warning(
            "gcp_pubsub: mensagem %s com data fora de base64 — descartada",
            message.get("messageId"),
        )
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return {"textPayload": text}
    return parsed if isinstance(parsed, dict) else {"jsonPayload": parsed}


# ── Pub/Sub HTTP ───────────────────────────────────────────────────────


async def _raise_for_pubsub_status(resp: aiohttp.ClientResponse, op: str) -> None:
    if resp.status < 400:
        return
    if resp.status == 429:
        raise GcpPubSubRateLimitedError(_parse_retry_after(resp.headers.get("Retry-After")))
    detail = ""
    try:
        body = await resp.json()
        detail = str(((body or {}).get("error") or {}).get("message") or "")[:300]
    except Exception:  # noqa: BLE001 — corpo ilegível não muda o erro
        pass
    logger.warning("gcp_pubsub: %s devolveu HTTP %s: %s", op, resp.status, detail)
    # 401 sai como ``ClientResponseError`` — é o que faz o pipeline invalidar o
    # token em cache e marcar a integração com erro de autenticação.
    resp.raise_for_status()


def _parse_retry_after(value: Optional[str]) -> int:
    try:
        return max(1, int(value or ""))
    except (TypeError, ValueError):
        return 30


def _chunks(items: List[str], size: int) -> Iterator[List[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _subscription_path(project: str, prefix: str, suffix: str) -> str:
    return f"projects/{project}/subscriptions/{prefix}-{suffix}"


def _validated_target(project: str, prefix: str) -> Tuple[str, str]:
    if not _PROJECT_RE.match(project or ""):
        raise ValueError(f"Project ID inválido: {project!r}")
    if not _PREFIX_RE.match(prefix or ""):
        raise ValueError(f"prefixo de subscription inválido: {prefix!r}")
    return project, prefix


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ── Credencial → token ─────────────────────────────────────────────────


def parse_credential_info(text: str) -> Dict[str, Any]:
    """Valida o JSON da credencial; levanta ``GcpCredentialError`` com o motivo.

    Aceita só chave de service account e WIF (``external_account``), com os
    endpoints de token fixados no Google. ``credential_source`` de arquivo ou
    executável é recusado: o arquivo seria lido do disco DO WORKER, e o
    executável rodaria nele.
    """
    try:
        info = json.loads(text)
    except (TypeError, ValueError):
        raise GcpCredentialError("a credencial não é um JSON válido") from None
    if not isinstance(info, dict):
        raise GcpCredentialError("a credencial não é um objeto JSON")
    kind = info.get("type")
    if kind == "service_account":
        if not info.get("client_email") or not info.get("private_key"):
            raise GcpCredentialError("chave de service account sem client_email/private_key")
        token_uri = info.get("token_uri") or "https://oauth2.googleapis.com/token"
        if token_uri not in _SA_TOKEN_URIS:
            raise GcpCredentialError("token_uri fora do Google")
        return info
    if kind == "external_account":
        if info.get("token_url") != _STS_TOKEN_URL:
            raise GcpCredentialError(f"token_url do WIF deve ser {_STS_TOKEN_URL}")
        for key in ("service_account_impersonation_url", "token_info_url"):
            value = info.get(key)
            if value and not _auth_url_allowed(value, allow_metadata=False):
                raise GcpCredentialError(f"{key} fora do Google")
        source = info.get("credential_source")
        if not isinstance(source, dict):
            raise GcpCredentialError("configuração WIF sem credential_source")
        if "file" in source or "executable" in source:
            raise GcpCredentialError(
                "credential_source de arquivo/executável não é aceito — use a "
                "identidade da nuvem (url de metadata) ou uma chave de service account"
            )
        for key in ("url", "region_url", "imdsv2_session_token_url"):
            value = source.get(key)
            if value and not _auth_url_allowed(value, allow_metadata=True):
                raise GcpCredentialError(f"credential_source.{key} fora do endpoint de metadata")
        return info
    raise GcpCredentialError(
        "tipo de credencial não suportado — use a chave JSON de uma service account "
        "ou a configuração WIF (external_account)"
    )


def _auth_url_allowed(url: str, *, allow_metadata: bool) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme == "https" and host in _AUTH_HOSTS:
        return True
    return allow_metadata and parts.scheme == "http" and host in _METADATA_HOSTS


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirect vira resposta de erro: um 3xx não pode levar o token para outro host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _guarded_request():
    """Transporte do google-auth sobre ``urllib``, restrito aos hosts de auth.

    Dispensa o pacote ``requests`` (que não está no lock da imagem) e é a
    barreira contra um JSON de ``external_account`` forjado: qualquer URL fora
    de ``_AUTH_HOSTS``/``_METADATA_HOSTS`` é recusada antes da conexão.
    """
    from google.auth import exceptions as ga_exceptions  # noqa: PLC0415
    from google.auth import transport as ga_transport  # noqa: PLC0415

    class _Response(ga_transport.Response):
        def __init__(self, status: int, headers: Dict[str, str], data: bytes) -> None:
            self._status, self._headers, self._data = status, headers, data

        @property
        def status(self) -> int:
            return self._status

        @property
        def headers(self) -> Dict[str, str]:
            return self._headers

        @property
        def data(self) -> bytes:
            return self._data

    class _Request(ga_transport.Request):
        def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kwargs):
            if not _auth_url_allowed(url, allow_metadata=True):
                raise ga_exceptions.TransportError(
                    f"destino de autenticação não permitido: {urllib.parse.urlsplit(url).hostname}"
                )
            data = body.encode("utf-8") if isinstance(body, str) else body
            request = urllib.request.Request(url, data=data, headers=dict(headers or {}), method=method)
            opener = urllib.request.build_opener(_NoRedirect())
            try:
                with opener.open(request, timeout=timeout or 30) as resp:
                    return _Response(resp.status, dict(resp.headers.items()), resp.read())
            except urllib.error.HTTPError as exc:
                return _Response(exc.code, dict(exc.headers.items()) if exc.headers else {}, exc.read())
            except (urllib.error.URLError, OSError) as exc:
                raise ga_exceptions.TransportError(str(exc)) from exc

    return _Request()


def _mint_token(info: Dict[str, Any]) -> Tuple[str, int]:
    """Credencial validada → ``(access_token, expires_in)``. Síncrono: rodar em thread."""
    try:
        import google.auth  # noqa: PLC0415 — lazy: google-auth vem de requirements-sinks
    except ImportError as exc:
        raise RuntimeError(_NO_SDK_MSG) from exc
    creds, _ = google.auth.load_credentials_from_dict(info, scopes=[_SCOPE])
    creds.refresh(_guarded_request())
    expires_in = 3600
    if creds.expiry is not None:  # naive UTC, convenção do google-auth
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        expires_in = max(60, int((creds.expiry - now).total_seconds()))
    return str(creds.token), expires_in


def _load_credential_text(integration_id: int) -> str:
    from ...db import database, models
    from ...services import integration_secrets

    with database.SessionLocal() as db:
        integ = db.get(models.Integration, integration_id)
        if integ is None:
            raise RuntimeError(f"gcp_pubsub: integração {integration_id} não encontrada")
        return integration_secrets.read_secret(integ, "client_secret") or ""


async def _gcp_refresher(integration_id: int) -> Dict[str, object]:
    """Token OAuth do Google para o Pub/Sub; o framework o guarda no cache Redis."""
    info = parse_credential_info(await asyncio.to_thread(_load_credential_text, integration_id))
    token, expires_in = await asyncio.to_thread(_mint_token, info)
    return {"access_token": token, "expires_in": expires_in}


# ── Teste de conexão (pré-save) ────────────────────────────────────────


async def _probe_subscription(
    session: aiohttp.ClientSession, headers: Dict[str, str], subscription: str
) -> str:
    """Estado de UMA subscription, sem consumir mensagem.

    ``testIamPermissions`` em vez de ``subscriptions.get``: o papel mínimo
    (Pub/Sub Subscriber) não inclui ``subscriptions.get``, e o teste não pode
    exigir mais permissão do que a coleta usa.
    """
    url = f"{_PUBSUB_API}/{subscription}:testIamPermissions"
    async with session.post(
        url, json={"permissions": ["pubsub.subscriptions.consume"]}, headers=headers
    ) as resp:
        if resp.status == 404:
            return "não existe (stream desligado)"
        if resp.status == 403:
            return "sem permissão"
        if resp.status >= 400:
            return f"erro HTTP {resp.status}"
        payload = await resp.json()
    granted = (payload or {}).get("permissions") or []
    return "ok" if "pubsub.subscriptions.consume" in granted else "sem pubsub.subscriptions.consume"


async def _gcp_pubsub_probe(cfg: Dict[str, Any]):
    """Teste STATELESS pré-save: token + permissão de consumo em cada subscription."""
    from ..output.base import TestResult

    project = (cfg.get("tenant_id") or "").strip()
    prefix = (cfg.get("base_url") or "").strip() or _DEFAULT_PREFIX
    raw = (cfg.get("client_secret") or "").strip()
    if not project or not raw:
        return TestResult.failed("Informe o Project ID e a credencial JSON.")
    try:
        project, prefix = _validated_target(project, prefix)
        info = parse_credential_info(raw)
    except ValueError as exc:  # GcpCredentialError é ValueError
        return TestResult.failed(str(exc))

    t0 = time.perf_counter()
    try:
        token, _ = await asyncio.to_thread(_mint_token, info)
    except RuntimeError as exc:
        return TestResult.failed(str(exc))
    except Exception as exc:  # noqa: BLE001
        # Só a classe do erro: a mensagem do google-auth pode trazer resposta de
        # endpoint, e o teste de conexão devolve texto para quem digitou o JSON.
        logger.info("gcp_pubsub probe: credencial recusada", exc_info=True)
        return TestResult.failed(
            f"O Google recusou a credencial ({type(exc).__name__}). Confira a chave "
            "ou a configuração WIF."
        )

    headers = {"Authorization": f"Bearer {token}"}
    states: List[str] = []
    ready = 0
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            for collector_cls in _COLLECTORS:
                sub = _subscription_path(project, prefix, collector_cls._SUBSCRIPTION_SUFFIX)
                state = await _probe_subscription(session, headers, sub)
                if state == "ok":
                    ready += 1
                states.append(f"{collector_cls.stream} ({prefix}-{collector_cls._SUBSCRIPTION_SUFFIX}): {state}")
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        return TestResult.failed(f"Falha ao falar com o Pub/Sub: {type(exc).__name__}")
    detail = "; ".join(states)
    if not ready:
        return TestResult.failed(f"Nenhuma subscription utilizável em {project} — {detail}")
    ms = (time.perf_counter() - t0) * 1000.0
    return TestResult.passed(f"Conexão OK — {detail}", latency_ms=ms)


# ── Self-registration ──────────────────────────────────────────────────


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
            platform="gcp_pubsub",
            display_name="Google Cloud (Pub/Sub)",
            category="Cloud-audit",
            description=(
                "Google Cloud — Cloud Audit Logs (IAM, Admin Activity, login), audit do "
                "GKE e auditoria do Cloud SQL MySQL, via sink do Cloud Logging → Pub/Sub."
            ),
            icon_id="gcp",
            docs_url="https://docs.cloud.google.com/logging/docs/export/pubsub",
            order=51,
            test_fn=_gcp_pubsub_probe,
            required_secrets=("client_secret",),
            capabilities=frozenset(
                {"catalog", "auth:test"} | {f"collect:{cls.stream}" for cls in _COLLECTORS}
            ),
            auth_fields=(
                AuthField(key="tenant_id", label="Project ID", type="string", required=True,
                          help_text="Projeto GCP onde ficam as subscriptions do Pub/Sub"),
                AuthField(key="base_url", label="Prefixo das subscriptions", type="string",
                          required=False,
                          help_text=(
                              "Padrão: centralops → centralops-audit, centralops-gke-audit e "
                              "centralops-cloudsql-mysql. Subscription que não existe = stream desligado."
                          )),
                AuthField(key="client_secret", label="Credencial (JSON)", type="secret", required=True,
                          help_text=(
                              "Chave JSON de service account ou configuração WIF (external_account), "
                              "com o papel Pub/Sub Subscriber nas subscriptions"
                          )),
            ),
        )
    )

    for collector_cls in _COLLECTORS:
        register(
            CollectorRegistration(
                platform=collector_cls.platform,
                stream=collector_cls.stream,
                collector_cls=collector_cls,
                refresh_fn=_gcp_refresher,
                schedule=_td(minutes=2),
                queue=Q_BULK,
                task_name=T_COLLECT_BULK,
            )
        )


_register()
