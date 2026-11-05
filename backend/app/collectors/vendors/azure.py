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

**Duas fontes para o mesmo conteúdo, escolhidas pelo ``base_url``:**

- namespace do **Event Hubs** → ``entra``/``resource`` leem os hubs e o
  ``activity`` lê o ARM;
- **Workspace ID do Log Analytics** (um GUID) → os TRÊS streams leem as tabelas
  do workspace pela API de consulta (``AzureActivity``; ``SigninLogs`` e
  irmãs; ``AzureDiagnostics`` e as tabelas por recurso). É o caminho de quem já
  manda os diagnostic settings para um workspace: nenhum Event Hub, e o papel é
  **Log Analytics Reader** no workspace. Cada linha é convertida para a forma do
  registro que o diagnostic setting publicaria no Event Hub (ou que o ARM
  devolveria), então os mappings são os mesmos nas duas fontes.

**Por que Event Hubs e por que via Kafka.** É o único destino de diagnostic
setting que se lê como fila, com retenção, sem Log Analytics no meio (o padrão
de Splunk, Elastic, QRadar e Sentinel para "Azure fora do Azure"). O endpoint
Kafka do Event Hubs (tier Standard ou acima) fala o protocolo que o Core já
carrega (``aiokafka``) — nenhum SDK novo.

**Stream sem fonte não é stream saudável.** ``entra``/``resource`` sem
``base_url``, ou um workspace cujas tabelas daquele stream não receberam nada
em 7 dias, falham com a instrução do que configurar. Antes saíam como sucesso
com zero eventos, e a Saúde do Pipeline mostrava "saudável" para uma coleta que
nunca teve de onde ler.

**Uma credencial só, sem chave compartilhada.** O mesmo app registration
(``tenant_id`` + ``client_id`` + ``client_secret``) autentica no ARM (escopo
``management.azure.com``) E no Event Hubs (SASL ``OAUTHBEARER`` com token do
Entra, papel **Azure Event Hubs Data Receiver**). Nenhuma connection string com
``SharedAccessKey`` é guardada, e revogar o acesso é tirar um papel no RBAC.
Event Hub que não existe = stream desligado (no-op): com namespace, o cliente
cria só os hubs que quer.

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

_LA_HOST = "api.loganalytics.io"
_LA_SCOPE = f"https://{_LA_HOST}/.default"

#: Ação que dá leitura do Activity Log. Papel atribuído só num RECURSO (o caso
#: do app com Log Analytics Reader no workspace) deixa a assinatura visível na
#: listagem, mas a API filtra os eventos para os recursos que o app lê: a
#: coleta sai quase vazia e sem erro nenhum.
_ACTIVITY_READ_ACTION = "microsoft.insights/eventtypes/values/read"
_PERMISSIONS_API = "2022-04-01"

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

# ── Log Analytics ────────────────────────────────────────────────────────

#: Linhas por consulta e consultas por ciclo. A API devolve até 500 mil linhas,
#: mas uma página de 5 mil cabe folgada na resposta (~100 MB) e no despacho.
_LA_PAGE_ROWS = 5000
_MAX_LA_PAGES_PER_CYCLE = 10
#: Primeira coleta: o que entrou no workspace nas últimas 24 h.
_LA_COLD_START = timedelta(hours=24)
#: O cursor anda pelo ``ingestion_time()`` (o atraso de ingestão varia de
#: segundos a horas por tabela; pelo ``TimeGenerated`` o evento atrasado ficaria
#: para trás da janela). O ``TimeGenerated`` só delimita a VARREDURA: sem ele a
#: consulta percorre a retenção inteira a cada ciclo.
_LA_SCAN_BACK = timedelta(days=2)
#: Teto da janela: a linha ingerida há instantes pode ainda não estar visível
#: em todas as réplicas. Lê-se até ``agora - _LA_SETTLE``.
_LA_SETTLE = timedelta(minutes=5)
#: "Esta fonte já recebeu algo?" — janela da checagem que separa tenant calado
#: de fonte nunca configurada.
_LA_PRESENCE_WINDOW = timedelta(days=7)
_LA_TIMEOUT = aiohttp.ClientTimeout(total=200)
_LA_WAIT_SECONDS = 180

#: Tabelas de cada stream → ``category`` do registro equivalente no Event Hub
#: (``None`` = a própria coluna ``Category``). As de sign-in não têm valor de
#: ``Category`` documentado; o nome vem da tabela, igual ao do diagnostic setting.
_LA_TABLES: Dict[str, Dict[str, Optional[str]]] = {
    "activity": {"AzureActivity": None},
    "entra": {
        "SigninLogs": "SignInLogs",
        "AADNonInteractiveUserSignInLogs": "NonInteractiveUserSignInLogs",
        "AADServicePrincipalSignInLogs": "ServicePrincipalSignInLogs",
        "AADManagedIdentitySignInLogs": "ManagedIdentitySignInLogs",
        "ADFSSignInLogs": "ADFSSignInLogs",
        "AuditLogs": "AuditLogs",
        "AADProvisioningLogs": "ProvisioningLogs",
        "AADRiskyUsers": "RiskyUsers",
        "AADUserRiskEvents": "UserRiskEvents",
        "AADRiskyServicePrincipals": "RiskyServicePrincipals",
        "AADServicePrincipalRiskEvents": "ServicePrincipalRiskEvents",
    },
    "resource": {
        "AzureDiagnostics": None,
        "AZKVAuditLogs": "AuditEvent",
        "AKSAudit": "kube-audit",
        "AKSAuditAdmin": "kube-audit-admin",
        "StorageBlobLogs": None,
        "StorageFileLogs": None,
        "StorageQueueLogs": None,
        "StorageTableLogs": None,
        "SQLSecurityAuditEvents": "SQLSecurityAuditEvents",
    },
}

# Valores do cursor viram literal de KQL: só passam no formato exato que a
# própria API devolve.
_KQL_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,7})?Z$")
_ROW_KEY_RE = re.compile(r"^[0-9a-f]{64}$")

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


class AzureSourceError(RuntimeError):
    """O stream não tem de onde ler: sem fonte configurada, ou a fonte nunca
    recebeu os logs dele. A mensagem é a instrução, e vai para a Saúde do
    Pipeline como ``last_error``."""


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


def resolve_source(raw: Optional[str]) -> Tuple[str, Optional[str]]:
    """``base_url`` → ``("workspace", guid)``, ``("eventhub", fqdn)`` ou ``("none", None)``.

    O Workspace ID do Log Analytics é um GUID; nome de namespace do Event Hubs
    começa por letra e não tem o formato 8-4-4-4-12, então os dois não se
    confundem. O GUID vira PATH da URL da API (``/v1/workspaces/<id>``).
    """
    text = (raw or "").strip().lower()
    if _GUID_RE.match(text):
        return "workspace", text
    fqdn = eventhub_fqdn(raw)
    return ("eventhub", fqdn) if fqdn else ("none", None)


def _no_source_error(stream: str) -> AzureSourceError:
    return AzureSourceError(
        f"azure {stream}: nenhuma fonte configurada. Informe no campo Fonte o Workspace ID "
        "do Log Analytics (o workspace que recebe os diagnostic settings) ou o namespace "
        "do Event Hubs"
    )


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


async def _activity_read_scopes(
    session: aiohttp.ClientSession, headers: Dict[str, str], subs: List[str]
) -> List[str]:
    """Assinaturas em que o app LÊ o Activity Log no escopo da assinatura.

    ``Microsoft.Authorization/permissions`` no escopo da assinatura devolve só o
    que vem de atribuição nela ou acima (management group); papel num recurso
    não aparece. É a diferença entre "a assinatura é visível" e "os eventos dela
    vêm todos".
    """
    readable: List[str] = []
    for sub in subs:
        if not _GUID_RE.match(sub):
            continue
        url = (
            f"{_ARM}/subscriptions/{sub}/providers/Microsoft.Authorization/permissions"
            f"?api-version={_PERMISSIONS_API}"
        )
        async with session.get(url, headers=headers, allow_redirects=False) as resp:
            await _raise_for_arm_status(resp, "permissions")
            payload = await resp.json()
        if _grants(payload, _ACTIVITY_READ_ACTION):
            readable.append(sub)
    return readable


def _grants(payload: Any, action: str) -> bool:
    """Alguma entrada de ``permissions`` concede ``action`` (com curinga, sem
    ``notActions`` que a tire)?"""
    for perm in (payload or {}).get("value") or []:
        if not isinstance(perm, dict):
            continue
        allowed = any(_action_matches(p, action) for p in perm.get("actions") or [])
        denied = any(_action_matches(p, action) for p in perm.get("notActions") or [])
        if allowed and not denied:
            return True
    return False


def _action_matches(pattern: Any, action: str) -> bool:
    if not isinstance(pattern, str):
        return False
    regex = "^" + ".*".join(re.escape(part) for part in pattern.lower().split("*")) + "$"
    return re.match(regex, action) is not None


# ── Log Analytics: consulta e conversão de linha ─────────────────────────


def _la_page_query(tables: Tuple[str, ...], order: str, lo: str, key: str, hi: str) -> str:
    """Uma página em ordem ``(_ord, _key)``, a partir de ``(lo, key)`` exclusive.

    ``_ord`` é o ``ingestion_time()`` (polling) ou o ``TimeGenerated``
    (backfill). Linhas ingeridas no mesmo lote têm o MESMO instante — no tenant
    real, dois eventos do Activity Log com o mesmo ``ingestion_time()`` —, então
    só o instante não pagina: a ``_key`` (hash da linha inteira, sem as colunas
    nulas, para não mudar quando a união ganha uma tabela) desempata. ``lo``,
    ``key`` e ``hi`` chegam validados (``_KQL_DATETIME_RE``/``_ROW_KEY_RE``).
    """
    ord_expr = "ingestion_time()" if order == "ingested" else "TimeGenerated"
    return (
        f"union isfuzzy=true withsource=_table {', '.join(tables)}\n"
        f"| extend _ord = {ord_expr}\n"
        f"| where _ord >= datetime({lo}) and _ord < datetime({hi})\n"
        "| extend _key = hash_sha256(tostring(pack_all(true)))\n"
        # KQL não compara texto com ``>`` (SEM0064): ``strcmp``.
        f"| where _ord > datetime({lo}) or strcmp(_key, '{key}') > 0\n"
        "| order by _ord asc, _key asc\n"
        f"| take {_LA_PAGE_ROWS}"
    )


def _la_presence_query(tables: Tuple[str, ...]) -> str:
    return (
        f"union isfuzzy=true withsource=_table {', '.join(tables)}\n"
        "| summarize n = count() by _table"
    )


async def _la_query(
    session: aiohttp.ClientSession, token: str, workspace: str, query: str, timespan: str
) -> List[Dict[str, Any]]:
    """Executa a consulta e devolve as linhas como dicts (sem valores nulos)."""
    url = f"https://{_LA_HOST}/v1/workspaces/{workspace}/query"
    headers = {"Authorization": f"Bearer {token}", "Prefer": f"wait={_LA_WAIT_SECONDS}"}
    body = {"query": query, "timespan": timespan}
    async with session.post(
        url, json=body, headers=headers, timeout=_LA_TIMEOUT, allow_redirects=False
    ) as resp:
        if resp.status == 429:
            raise AzureRateLimitedError(_parse_retry_after(resp.headers.get("Retry-After")))
        if resp.status >= 400:
            code = await _la_error_code(resp)
            logger.warning("azure: Log Analytics devolveu HTTP %s %s", resp.status, code)
            if resp.status == 403:
                raise AzureSourceError(
                    f"azure: sem leitura no workspace {workspace} ({code or 'HTTP 403'}). "
                    "Dê ao app o papel Log Analytics Reader no workspace"
                )
            if resp.status == 404:
                raise AzureSourceError(
                    f"azure: workspace {workspace} não encontrado ({code or 'HTTP 404'}). "
                    "Use o Workspace ID (GUID) da página Overview do workspace"
                )
            if resp.status == 400:
                # Consulta recusada: o código (``SEM0064``…) é o que diz o porquê.
                raise RuntimeError(f"azure: Log Analytics recusou a consulta ({code or 'HTTP 400'})")
            # 401 sai como ``ClientResponseError``, como no ARM.
            resp.raise_for_status()
        payload = await resp.json()
    if isinstance(payload, dict) and payload.get("error"):
        # 200 com ``error`` = resultado PARCIAL. Avançar o cursor nele pularia as
        # linhas que faltaram; o ciclo falha e repete a página.
        err = payload["error"] if isinstance(payload["error"], dict) else {}
        raise RuntimeError(
            f"azure: Log Analytics devolveu resultado parcial ({str(err.get('code') or '')[:80]})"
        )
    return _la_rows(payload)


async def _la_error_code(resp: aiohttp.ClientResponse) -> str:
    try:
        body = await resp.json(content_type=None)
    except Exception:  # noqa: BLE001
        return ""
    err = (body or {}).get("error") if isinstance(body, dict) else None
    if not isinstance(err, dict):
        return ""
    code = str(err.get("code") or "")
    inner = err.get("innererror")
    while isinstance(inner, dict):  # o mais interno é o específico (SEM0064)
        code = str(inner.get("code") or code)
        inner = inner.get("innererror")
    return code[:80]


#: Sufixo que o ``union`` põe numa coluna cujo TIPO difere entre as tabelas:
#: ``Status`` é ``dynamic`` em ``SigninLogs`` e ``string`` na irmã não
#: interativa, e a união devolve ``Status_dynamic`` e ``Status_string`` (visto no
#: tenant real, também em ``DurationMs_long``/``_int``/``_real`` e
#: ``Properties_string``/``_dynamic``). Cada linha preenche só a da tabela dela.
_UNION_TYPE_SUFFIX_RE = re.compile(
    r"_(?:dynamic|string|long|int|real|bool|datetime|guid|timespan|decimal)$"
)


def _la_rows(payload: Any) -> List[Dict[str, Any]]:
    tables = (payload or {}).get("tables") if isinstance(payload, dict) else None
    if not tables:
        return []
    table = tables[0]
    columns = [
        (_UNION_TYPE_SUFFIX_RE.sub("", str(c.get("name") or "")), c.get("type"))
        for c in table.get("columns") or []
    ]
    rows: List[Dict[str, Any]] = []
    for raw in table.get("rows") or []:
        row: Dict[str, Any] = {}
        for (name, ctype), value in zip(columns, raw):
            if value is None or value == "" or not name or name in row:
                continue
            # A API serializa ``dynamic`` como texto JSON.
            row[name] = _maybe_json(value) if ctype == "dynamic" else value
        rows.append(row)
    return rows


def _maybe_json(value: Any) -> Any:
    """Texto JSON de objeto/lista → o objeto. Colunas que são ``dynamic`` numa
    tabela de sign-in vêm como ``string`` na irmã não interativa."""
    if isinstance(value, str) and value[:1] in ("{", "["):
        try:
            parsed = json.loads(value)
        except ValueError:
            return value
        if isinstance(parsed, (dict, list)):
            return parsed
    return value


def _camel(name: str) -> str:
    """``UserPrincipalName`` → ``userPrincipalName``; ``IPAddress`` → ``ipAddress``."""
    if name.isupper():
        return name.lower()
    run = 0
    while run < len(name) and name[run].isupper():
        run += 1
    if run <= 1:
        return name[:1].lower() + name[1:]
    return name[: run - 1].lower() + name[run - 1:]


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _compact(value: Any) -> Any:
    """Tira nulos e vazios de dicts aninhados (a forma do ARM não os traz)."""
    if isinstance(value, dict):
        out = {k: _compact(v) for k, v in value.items()}
        return {k: v for k, v in out.items() if v not in (None, "", {}, [])}
    return value


#: Colunas do Log Analytics que não existem no registro original: o
#: ``TenantId`` delas é o id do WORKSPACE, não o do tenant.
_LA_META = frozenset({
    "TenantId", "SourceSystem", "Type", "_table", "_ord", "_key", "_BilledSize",
    "_IsBillable", "_SubscriptionId", "_ResourceId",
})

#: Coluna → chave do topo do registro que o diagnostic setting publica.
_LA_ENVELOPE = {
    "TimeGenerated": "time",
    "OperationName": "operationName",
    "OperationVersion": "operationVersion",
    "Category": "category",
    "ResultType": "resultType",
    "ResultSignature": "resultSignature",
    "ResultDescription": "resultDescription",
    "DurationMs": "durationMs",
    "CallerIpAddress": "callerIpAddress",
    "CallerIPAddress": "callerIpAddress",
    "CorrelationId": "correlationId",
    "Identity": "identity",
    "Level": "level",
    "Location": "location",
    "ResourceId": "resourceId",
}

_ACTIVITY_STATUS = {"Start": "Started", "Success": "Succeeded", "Failure": "Failed", "Accept": "Accepted"}
_ACTIVITY_LEVEL = {"Information": "Informational"}


def _resource_type(resource_id: Any) -> Optional[str]:
    """``/subscriptions/…/providers/Microsoft.Storage/storageAccounts/x`` →
    ``Microsoft.Storage/storageAccounts`` (como o ``resourceType`` do ARM)."""
    parts = [p for p in str(resource_id or "").split("/") if p]
    lowered = [p.lower() for p in parts]
    if "providers" not in lowered:
        return None
    rest = parts[len(lowered) - lowered[::-1].index("providers"):]
    if not rest:
        return None
    types = rest[1:][0::2]
    return "/".join([rest[0], *types])


def _activity_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Linha do ``AzureActivity`` → o evento que a API REST do Activity Log devolve."""
    claims = _maybe_json(row.get("Claims_d") or row.get("Claims")) or {}
    auth = _maybe_json(row.get("Authorization_d") or row.get("Authorization")) or {}
    props = _maybe_json(row.get("Properties_d") or row.get("Properties")) or {}
    http = _maybe_json(row.get("HTTPRequest")) or {}
    claims = claims if isinstance(claims, dict) else {}
    auth = auth if isinstance(auth, dict) else {}
    props = props if isinstance(props, dict) else {}
    http = dict(http) if isinstance(http, dict) else {}
    if row.get("CallerIpAddress") and not http.get("clientIpAddress"):
        http["clientIpAddress"] = row["CallerIpAddress"]

    # ``OperationNameValue`` vem em MAIÚSCULAS no workspace; a REST devolve o
    # nome com a caixa original, e detecção que compara texto (``/listKeys/action``)
    # depende dela. A ``authorization.action`` carrega o mesmo nome na caixa certa.
    op = str(row.get("OperationNameValue") or "")
    for candidate in (auth.get("action"), props.get("message")):
        if isinstance(candidate, str) and candidate.lower() == op.lower():
            op = candidate
            break

    resource_id = row.get("ResourceId") or props.get("entity") or row.get("_ResourceId")
    # ``ResourceGroup`` vem em MAIÚSCULAS no workspace; o id carrega a caixa original.
    group = re.search(r"/resourcegroups/([^/]+)", str(resource_id or ""), re.IGNORECASE)
    hierarchy = str(row.get("Hierarchy") or "")
    tenant = claims.get("tid") or (hierarchy.split("/")[0] if hierarchy else None)
    status = row.get("ActivityStatusValue")
    provider = str(row.get("ResourceProviderValue") or "")
    if op.split("/")[0].lower() == provider.lower():
        provider = op.split("/")[0]
    event = {
        "eventDataId": row.get("EventDataId"),
        "eventTimestamp": row.get("TimeGenerated"),
        "submissionTimestamp": row.get("EventSubmissionTimestamp"),
        "operationName": {"value": op, "localizedValue": row.get("OperationName") or op},
        "category": {"value": row.get("CategoryValue")},
        "status": {"value": _ACTIVITY_STATUS.get(status, status)},
        "subStatus": {"value": row.get("ActivitySubstatusValue")},
        "caller": row.get("Caller"),
        "claims": claims,
        "httpRequest": http,
        "authorization": auth,
        "correlationId": row.get("CorrelationId"),
        "level": _ACTIVITY_LEVEL.get(row.get("Level"), row.get("Level")),
        "subscriptionId": row.get("SubscriptionId"),
        "resourceGroupName": group.group(1) if group else row.get("ResourceGroup"),
        "resourceId": resource_id,
        "resourceType": {"value": _resource_type(resource_id)},
        "resourceProviderName": {"value": provider},
        "properties": props,
        "tenantId": tenant,
    }
    return _compact(event)


def _envelope_record(row: Dict[str, Any], category: Optional[str]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Topo do registro + ``properties`` (colunas restantes em camelCase)."""
    record: Dict[str, Any] = {}
    props: Dict[str, Any] = {}
    for col, value in row.items():
        if col in _LA_META:
            continue
        if col in _LA_ENVELOPE:
            record[_LA_ENVELOPE[col]] = _maybe_json(value)
        else:
            props[_camel(col)] = _maybe_json(value)
    if "resourceId" not in record and row.get("_ResourceId"):
        record["resourceId"] = row["_ResourceId"]
    if category:
        # A coluna ``Category`` dessas tabelas é a categoria DENTRO do log (no
        # AuditLogs, ``RoleManagement``); no registro do hub ela é
        # ``properties.category`` e o topo leva o nome do fluxo.
        if record.get("category") and record["category"] != category:
            props.setdefault("category", record["category"])
        record["category"] = category
    return record, props


def _entra_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Linha de ``SigninLogs``/``AuditLogs``/… → registro do diagnostic setting do Entra."""
    table = str(row.get("_table") or "")
    record, props = _envelope_record(row, _LA_TABLES["entra"].get(table))
    tenant = props.pop("aadTenantId", None)
    if tenant:
        record["tenantId"] = tenant
    # No registro do Event Hub o objeto de localização é ``properties.location``;
    # a coluna ``Location`` do workspace é a região, e fica no topo.
    if "locationDetails" in props:
        props["location"] = props.pop("locationDetails")
    if "aadOperationType" in props:
        props["operationType"] = props.pop("aadOperationType")
    if props.get("ipAddress") and "callerIpAddress" not in record:
        record["callerIpAddress"] = props["ipAddress"]
    record["properties"] = props
    record["logAnalyticsTable"] = table
    return _compact(record)


_AD_SUFFIX_RE = re.compile(r"_(?:s|d|b|g|t)$")
_AD_CLAIM_PREFIXES = (
    "http_schemas_xmlsoap_org_ws_2005_05_identity_claims_",
    "http_schemas_microsoft_com_identity_claims_",
)


def _resource_from_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Linha de resource log → registro do diagnostic setting do recurso."""
    table = str(row.get("_table") or "")
    if table == "AzureDiagnostics":
        record = _azure_diagnostics_record(row)
    else:
        record, props = _envelope_record(row, _LA_TABLES["resource"].get(table))
        nested = props.pop("properties", None)
        if isinstance(nested, dict):
            for k, v in nested.items():
                props.setdefault(k, v)
        if table in ("AKSAudit", "AKSAuditAdmin"):
            props = {"log": _kube_audit_log(props), "pod": props.get("podName")}
        elif table.startswith("Storage"):
            _lift_storage(record, props)
        elif table == "SQLSecurityAuditEvents":
            props = {
                _snake(k[:1].upper() + k[1:]): (str(v).lower() if isinstance(v, bool) else v)
                for k, v in props.items()
            }
        record["properties"] = props
    record["logAnalyticsTable"] = table
    return _compact(record)


def _azure_diagnostics_record(row: Dict[str, Any]) -> Dict[str, Any]:
    """Tabela legada: as propriedades viram colunas com sufixo de tipo
    (``requestUri_s``, ``httpStatusCode_d``) e o ``identity`` vira
    ``identity_claim_<nome>_<tipo>``."""
    record: Dict[str, Any] = {}
    props: Dict[str, Any] = {}
    claims: Dict[str, Any] = {}
    for col, value in row.items():
        if col in _LA_META:
            continue
        if col in _LA_ENVELOPE:
            record[_LA_ENVELOPE[col]] = value
        elif col == "AdditionalFields" and isinstance(value, dict):
            for k, v in value.items():
                props.setdefault(_AD_SUFFIX_RE.sub("", k), v)
        elif col.startswith("identity_claim_"):
            name = _AD_SUFFIX_RE.sub("", col[len("identity_claim_"):])
            for prefix in _AD_CLAIM_PREFIXES:
                if name.startswith(prefix):
                    name = name[len(prefix):]
            claims[name] = value
        else:
            props[_AD_SUFFIX_RE.sub("", col)] = _maybe_json(value)
    if "resourceId" not in record and row.get("_ResourceId"):
        record["resourceId"] = row["_ResourceId"]
    if claims:
        record["identity"] = {"claim": claims}
    record["properties"] = props
    return record


def _kube_audit_log(props: Dict[str, Any]) -> Dict[str, Any]:
    """Colunas do ``AKSAudit`` → o objeto de audit do Kubernetes (``properties.log``)."""
    return {
        "auditID": props.get("auditId"),
        "stage": props.get("stage"),
        "verb": props.get("verb"),
        "requestURI": props.get("requestUri"),
        "user": props.get("user"),
        "sourceIPs": props.get("sourceIps"),
        "userAgent": props.get("userAgent"),
        "objectRef": props.get("objectRef"),
        "responseStatus": props.get("responseStatus"),
        "annotations": props.get("annotations"),
        "requestReceivedTimestamp": props.get("requestReceivedTime"),
        "stageTimestamp": props.get("stageReceivedTime"),
    }


def _lift_storage(record: Dict[str, Any], props: Dict[str, Any]) -> None:
    """``StorageBlobLogs`` e irmãs: status, URI e quem chamou ficam no topo,
    como no registro do Event Hub."""
    status = props.pop("statusCode", None)
    if isinstance(status, str) and status.isdigit():
        status = int(status)
    record["statusCode"] = status
    record["statusText"] = props.pop("statusText", None)
    record["uri"] = props.pop("uri", None)
    requester = {
        name: props.pop(f"requester{name[:1].upper()}{name[1:]}", None)
        for name in ("appId", "audience", "objectId", "tenantId", "tokenIssuer", "upn")
    }
    record["identity"] = {"type": props.pop("authenticationType", None), "requester": requester}


_LA_CONVERTERS = {
    "activity": _activity_from_row,
    "entra": _entra_from_row,
    "resource": _resource_from_row,
}

_LA_HINTS = {
    "activity": "Crie o diagnostic setting da assinatura (Activity Log) apontando para este workspace",
    "entra": (
        "Em Microsoft Entra ID → Diagnostic settings, envie AuditLogs e os SignInLogs para "
        "este workspace (exportar sign-in exige Entra ID P1/P2)"
    ),
    "resource": (
        "Crie os diagnostic settings dos recursos (Key Vault, AKS, Azure SQL, Storage) "
        "apontando para este workspace"
    ),
}


class _WorkspaceSource:
    """Leitura de um stream nas tabelas de um Log Analytics workspace.

    Mixin dos três coletores: no modo workspace o cursor é
    ``{"ingested": <ingestion_time() da última linha>, "key": <_key dela>}``.
    """

    async def _collect_workspace(
        self, cfg: Dict[str, Any], workspace: str, cursor: Dict[str, Any]
    ) -> AsyncIterator[Dict[str, Any]]:
        tables = tuple(_LA_TABLES[self.stream])
        convert = _LA_CONVERTERS[self.stream]
        provider = _TokenProvider(cfg["tenant_id"], cfg["client_id"], cfg["client_secret"], _LA_SCOPE)
        now = datetime.now(timezone.utc)

        if "backfill_from_ts" in cursor:
            # Backfill pelo ``TimeGenerated``, dentro da retenção do workspace; não
            # mexe no cursor do polling.
            lo = _iso(_parse_iso(cursor["backfill_from_ts"]) or now)
            hi = _iso(min(_parse_iso(cursor.get("backfill_to_ts")) or now, now))
            async for event in self._read_pages(provider, workspace, tables, convert, "time", lo, "", hi, f"{lo}/{hi}"):
                yield event
            return

        lo = cursor.get("ingested")
        key = cursor.get("key") or ""
        fresh = not (isinstance(lo, str) and _KQL_DATETIME_RE.match(lo) and (not key or _ROW_KEY_RE.match(key)))
        if fresh:
            lo, key = _iso(now - _LA_COLD_START), ""
        lo_dt = _parse_iso(lo) or now
        hi = _iso(now - _LA_SETTLE)
        span = f"{_iso(lo_dt - _LA_SCAN_BACK)}/{_iso(now)}"

        self._la_last = (lo, key)
        emitted = 0
        async for event in self._read_pages(provider, workspace, tables, convert, "ingested", lo, key, hi, span):
            emitted += 1
            yield event
        if not emitted and fresh:
            await self._require_data(provider, workspace, tables, now)
        last_lo, last_key = self._la_last
        self.ctx.cursor = {"ingested": last_lo, "key": last_key}

    async def _read_pages(
        self, provider, workspace: str, tables: Tuple[str, ...], convert, order: str,
        lo: str, key: str, hi: str, span: str,
    ) -> AsyncIterator[Dict[str, Any]]:
        pages = 0
        while True:
            pages += 1
            if order == "ingested" and self.ctx.bounded_per_cycle and pages > _MAX_LA_PAGES_PER_CYCLE:
                # Sobrou linha no workspace: o próximo ciclo continua da última lida.
                self.mark_cycle_capped()
                return
            rows = await self._query(provider, workspace, _la_page_query(tables, order, lo, key, hi), span)
            for row in rows:
                lo, key = str(row.get("_ord") or lo), str(row.get("_key") or "")
                if order == "ingested":
                    self._la_last = (lo, key)
                yield convert(row)
            if len(rows) < _LA_PAGE_ROWS or not (_KQL_DATETIME_RE.match(lo) and _ROW_KEY_RE.match(key)):
                return

    async def _require_data(self, provider, workspace: str, tables: Tuple[str, ...], now: datetime) -> None:
        """Primeira coleta sem nada: tenant calado ou fonte que nunca recebeu o log?"""
        span = f"{_iso(now - _LA_PRESENCE_WINDOW)}/{_iso(now)}"
        rows = await self._query(provider, workspace, _la_presence_query(tables), span)
        if any(int(r.get("n") or 0) > 0 for r in rows):
            return
        raise AzureSourceError(
            f"azure {self.stream}: o workspace {workspace} não recebeu nada em "
            f"{', '.join(tables[:4])}{'…' if len(tables) > 4 else ''} nos últimos 7 dias. "
            f"{_LA_HINTS[self.stream]}"
        )

    async def _query(self, provider, workspace: str, query: str, span: str) -> List[Dict[str, Any]]:
        token = await provider.token()
        await self.ctx.rate_limiter.acquire(self.ctx.integration_id, self.platform)
        started = time.monotonic()
        try:
            async with self.ctx.domain_limiter.slot(_LA_HOST):
                return await _la_query(self.ctx.session, token, workspace, query, span)
        finally:
            API_LATENCY.labels(vendor=self.platform, stream=self.stream).observe(
                time.monotonic() - started
            )


class AzureActivityLogCollector(_WorkspaceSource, BaseCollector):
    """Azure Activity Log de todas as assinaturas visíveis (polling no ARM)."""

    platform = "azure"
    stream = "activity"
    event_type = "azure.activity"
    domain = _ARM_HOST

    async def collect(self) -> AsyncIterator[Dict[str, Any]]:
        cursor = self.ctx.cursor or {}
        cfg = await asyncio.to_thread(_load_settings, self.ctx.integration_id)
        kind, workspace = resolve_source(cfg["base_url"])
        if kind == "workspace":
            # O workspace recebe o Activity Log pelo diagnostic setting da
            # assinatura: o papel é o do workspace, nenhum no escopo da assinatura.
            async for event in self._collect_workspace(cfg, workspace, cursor):
                yield event
            return

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
        atrás do relógio por construção — é a sobreposição, não atraso. No modo
        workspace, ``ingested``: a ingestão da última linha lida."""
        if "ingested" in (cursor or {}):
            return cls.watermark_from_iso(cursor, "ingested")
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


class _TokenProvider:
    """Token do Entra para um audience que não é o do ARM (o cache do framework
    guarda UM token por integração, o do ``activity``). Guardado só durante o
    ciclo."""

    def __init__(self, tenant: str, client_id: str, secret: str, scope: str) -> None:
        self._tenant, self._client_id, self._secret = tenant, client_id, secret
        self._scope = scope
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


class _EventHubTokenProvider(_TokenProvider):
    """Token do SASL ``OAUTHBEARER`` do Event Hubs.

    O audience é o PRÓPRIO namespace (``https://<ns>.servicebus.windows.net``):
    o token não serve para nenhum outro recurso.
    """

    def __init__(self, tenant: str, client_id: str, secret: str, fqdn: str) -> None:
        super().__init__(tenant, client_id, secret, f"https://{fqdn}/.default")


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


class AzureEventHubEntraCollector(_WorkspaceSource, BaseCollector):
    """Logs do Entra ID: Event Hub ``centralops-entra`` ou as tabelas do workspace."""

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
        cfg = await asyncio.to_thread(_load_settings, self.ctx.integration_id)
        kind, target = resolve_source(cfg["base_url"])
        if kind == "workspace":
            async for event in self._collect_workspace(cfg, target, cursor):
                yield event
            return
        if "backfill_from_ts" in cursor:
            # A posição da fila vive no cursor do polling; um backfill que
            # lesse daqui andaria com ela sem gravá-la. O replay de um hub é
            # o próprio cursor: zerar ``offsets`` relê toda a retenção.
            logger.info(
                "azure %s: backfill por janela não se aplica ao Event Hub — ignorado "
                "(integration=%s)", self.stream, self.ctx.integration_id,
            )
            return
        if kind == "none":
            # Sair com sucesso e zero eventos punha "saudável" na Saúde do
            # Pipeline de uma coleta que nunca teve de onde ler.
            raise _no_source_error(self.stream)
        self._fqdn = target
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
        lido até o fim. No modo workspace, ``ingested``."""
        if "ingested" in (cursor or {}):
            return cls.watermark_from_iso(cursor, "ingested")
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
    """Teste STATELESS pré-save: token e, por stream, se há de onde ler."""
    from ..output.base import TestResult

    tenant = (cfg.get("tenant_id") or "").strip()
    client_id = (cfg.get("client_id") or "").strip()
    secret = (cfg.get("client_secret") or "").strip()
    if not tenant or not client_id or not secret:
        return TestResult.failed("Informe Tenant ID, Client ID e Client Secret.")
    try:
        validated_tenant(tenant)
        validated_client_id(client_id)
        kind, target = resolve_source(cfg.get("base_url"))
    except ValueError as exc:
        return TestResult.failed(str(exc))

    t0 = time.perf_counter()
    states: List[str] = []
    ready = 0
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            token, _ = await _client_credentials_token(session, tenant, client_id, secret, _ARM_SCOPE)
            if kind == "workspace":
                provider = _TokenProvider(tenant, client_id, secret, _LA_SCOPE)
                found, notes = await asyncio.wait_for(
                    _probe_workspace(session, provider, target), timeout=60
                )
                ready += found
                states.extend(notes)
            else:
                found, notes = await _probe_activity(session, {"Authorization": f"Bearer {token}"})
                ready += found
                states.extend(notes)
    except AzureTokenError as exc:
        return TestResult.failed(
            f"O Entra recusou a credencial: {exc.reason_code}. Confira o Tenant ID, o "
            "Client ID e o segredo (e se ele não expirou)."
        )
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        return TestResult.failed(f"Falha ao falar com o Azure: {type(exc).__name__}")

    if kind == "eventhub":
        provider = _EventHubTokenProvider(tenant, client_id, secret, target)
        try:
            hubs = await asyncio.wait_for(_probe_hubs(target, provider), timeout=30)
        except AzureTokenError as exc:
            states.append(f"Event Hubs: o Entra recusou o token ({exc.reason_code})")
        except Exception as exc:  # noqa: BLE001 — erro do broker vira estado, não 500
            logger.info("azure probe: Event Hubs inacessível", exc_info=True)
            states.append(
                f"Event Hubs ({target}): inacessível ({type(exc).__name__}) — confira o tier "
                "Standard+, a porta 9093 e o papel Azure Event Hubs Data Receiver. Se este é "
                "o nome de um Log Analytics workspace, informe o Workspace ID (GUID), não o nome"
            )
        else:
            for stream, state in hubs.items():
                if state == "ok":
                    ready += 1
                states.append(f"{stream}: {state}")
    elif kind == "none":
        states.append(
            "entra/resource: sem fonte — informe o Workspace ID do Log Analytics ou o "
            "namespace do Event Hubs"
        )

    detail = "; ".join(states)
    if not ready:
        return TestResult.failed(f"Nenhum stream utilizável — {detail}")
    ms = (time.perf_counter() - t0) * 1000.0
    return TestResult.passed(f"Conexão OK — {detail}", latency_ms=ms)


async def _probe_activity(
    session: aiohttp.ClientSession, headers: Dict[str, str]
) -> Tuple[int, List[str]]:
    """``activity`` pelo ARM: assinaturas visíveis E leitura do log no escopo delas."""
    try:
        subs = await _list_subscriptions(session, headers)
    except aiohttp.ClientResponseError as exc:
        return 0, [f"activity: erro HTTP {exc.status} ao listar assinaturas"]
    if not subs:
        return 0, ["activity: nenhuma assinatura visível (atribua Reader ou Monitoring Reader)"]
    try:
        readable = await _activity_read_scopes(session, headers, subs)
    except aiohttp.ClientResponseError as exc:
        # Não deu para conferir: o teste não reprova por isso.
        return 1, [
            f"activity: ok ({len(subs)} assinatura(s); escopo do papel não conferido, "
            f"HTTP {exc.status})"
        ]
    if len(readable) == len(subs):
        return 1, [f"activity: ok ({len(subs)} assinatura(s))"]
    if readable:
        return 1, [
            f"activity: ok em {len(readable)} de {len(subs)} assinatura(s). Nas outras o app "
            "só tem papel em recursos e só virão os eventos deles: atribua Monitoring Reader "
            "na assinatura"
        ]
    return 0, [
        f"activity: o app enxerga {len(subs)} assinatura(s), mas só tem papel em recursos "
        "dentro delas, e o Activity Log viria filtrado para esses recursos. Atribua "
        "Monitoring Reader na assinatura (ou no management group), ou use o Workspace ID "
        "do Log Analytics que recebe o Activity Log"
    ]


async def _probe_workspace(
    session: aiohttp.ClientSession, provider: "_TokenProvider", workspace: str
) -> Tuple[int, List[str]]:
    """Linhas de cada stream no workspace nos últimos 7 dias, sem ler evento."""
    tables = tuple(t for stream in ("activity", "entra", "resource") for t in _LA_TABLES[stream])
    now = datetime.now(timezone.utc)
    span = f"{_iso(now - _LA_PRESENCE_WINDOW)}/{_iso(now)}"
    try:
        token = await provider.token()
        rows = await _la_query(session, token, workspace, _la_presence_query(tables), span)
    except AzureSourceError as exc:
        return 0, [str(exc).removeprefix("azure: ")]
    except aiohttp.ClientResponseError as exc:
        return 0, [f"Log Analytics: erro HTTP {exc.status} ao consultar o workspace"]
    counts = {str(r.get("_table")): int(r.get("n") or 0) for r in rows}
    ready = 0
    notes: List[str] = []
    for stream in ("activity", "entra", "resource"):
        present = [t for t in _LA_TABLES[stream] if counts.get(t)]
        if present:
            ready += 1
            total = sum(counts[t] for t in present)
            notes.append(f"{stream}: ok ({total} linha(s) em 7 dias: {', '.join(present)})")
        else:
            notes.append(f"{stream}: sem dados no workspace em 7 dias. {_LA_HINTS[stream]}")
    return ready, notes


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
                "Defender for Cloud), sign-ins do Entra ID (inclusive não interativos) e "
                "resource logs (Key Vault, AKS, Azure SQL, Storage), lidos de um Log "
                "Analytics workspace ou de Event Hubs."
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
                              "Application (client) ID do app registration — papel Log "
                              "Analytics Reader no workspace, ou Monitoring Reader nas "
                              "assinaturas/management group"
                          )),
                AuthField(key="client_secret", label="Client Secret", type="secret", required=True),
                AuthField(key="base_url", label="Fonte: Workspace ID ou namespace do Event Hubs",
                          type="string", required=False,
                          help_text=(
                              "Workspace ID (GUID) do Log Analytics que recebe os diagnostic "
                              "settings: os três streams leem dele, com Log Analytics Reader. "
                              "Ou o namespace do Event Hubs (tier Standard+, hubs "
                              "centralops-entra e centralops-resource, papel Azure Event Hubs "
                              "Data Receiver). Não é o nome do workspace nem da storage account."
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
