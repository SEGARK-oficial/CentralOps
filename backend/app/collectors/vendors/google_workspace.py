"""Google Workspace — logs de auditoria (Reports API) e Alert Center.

Vendor novo = 1 módulo, ZERO core. Dois streams, um por API do Google:

- ``activity`` — **Admin SDK Reports API** (``activities.list``,
  ``userKey=all``) para cada aplicação configurada: ``login`` (login, falha,
  desafio, login suspeito, 2SV, alerta de ataque governamental), ``saml``
  (SSO para apps de terceiros), ``admin`` (mudanças no Admin console: criação e
  suspensão de usuário, reset de senha, papel de admin), ``user_accounts``,
  ``token`` (consentimento OAuth — o phishing de consentimento aparece aqui),
  ``groups``, ``mobile`` e ``rules`` (DLP/regras de atividade). ``drive``,
  ``gmail``, ``chrome`` e o resto entram por opt-in: volume alto ou
  dependência de edição.
- ``alerts``   — **Alert Center** (``alertcenter.googleapis.com/v1beta1``):
  senha vazada, login suspeito bloqueado, dispositivo comprometido, phishing
  reportado, admin principal trocado, alerta de regra de atividade.

**Credencial: chave de service account + delegação em todo o domínio.** As
duas APIs exigem agir COMO um usuário do domínio (o ``sub`` do JWT): a service
account assina o JWT com a própria chave e o Google troca por um token daquele
admin. Por isso só chave de service account é aceita — WIF (``external_account``)
não assina JWT com ``sub``. Um token por integração cobre os dois streams
(os dois escopos vão no mesmo JWT), então o cache de token por integração do
framework serve aos dois sem conflito.

**Colunas (todas editáveis pelo PUT):** ``client_secret`` = JSON da chave;
``client_id`` = e-mail do admin a impersonar; ``tenant_id`` = Customer ID
(opcional); ``base_url`` = lista de aplicações (opcional).

**Atraso de publicação.** O Google publica cada log com atraso próprio: login e
admin em minutos; ``token``, ``groups`` e ``calendar`` em horas
(https://knowledge.workspace.google.com/admin/reports/data-retention-and-lag-times).
Cada aplicação tem cursor próprio e a janela seguinte recomeça
``_LATE_ARRIVAL[app]`` antes do fim da anterior; o que volta repetido é pulado
pela memória da fronteira (``cursor_boundary``) e, acima dela, pelo dedupe.

**Cota.** Só ``startTime``/``endTime``: qualquer filtro (``eventName``,
``filters``…) cai na cota de 250 consultas/min, a janela pura não
(https://developers.google.com/workspace/admin/reports/v1/limits). A cota
estourada volta como HTTP 503, não 429.

**Superfície de rede fechada.** O token é do admin do cliente: os pedidos só vão
a ``admin.googleapis.com`` e ``alertcenter.googleapis.com``, sem redirect, e a
paginação usa só o ``nextPageToken`` (nenhuma URL vinda da resposta é seguida).
O token sai pelo transporte guardado do ``gcp_pubsub`` (allowlist dos hosts de
auth do Google, sem ``requests``).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict, Iterator, List, Optional, Tuple

import aiohttp

from ..base import BaseCollector, _parse_iso_utc_naive
from ..cursor_boundary import BoundaryKeys
from ..metrics import API_LATENCY
from ._cursor_selfheal import is_stale_cursor_response, log_stale_cursor
from ._rate_limit import VendorRateLimitedError
from .gcp_pubsub import GcpCredentialError, _guarded_request, parse_credential_info

logger = logging.getLogger(__name__)

_REPORTS_HOST = "admin.googleapis.com"
_ALERTS_HOST = "alertcenter.googleapis.com"
_REPORTS_API = f"https://{_REPORTS_HOST}/admin/reports/v1/activity/users/all/applications"
_ALERTS_API = f"https://{_ALERTS_HOST}/v1beta1/alerts"
_SCOPES = (
    "https://www.googleapis.com/auth/admin.reports.audit.readonly",
    "https://www.googleapis.com/auth/apps.alerts",
)
_NO_SDK_MSG = "google-auth não instalado — instale: pip install -r requirements-sinks.txt"

#: ``applicationName`` aceitos pela Reports API (activities.list). Validar evita
#: que um valor com ``/`` vire outro PATH e dá erro claro na UI em vez de 400.
KNOWN_APPLICATIONS = frozenset(
    {
        "access_transparency", "admin", "calendar", "chat", "drive", "gcp", "gmail",
        "gplus", "groups", "groups_enterprise", "jamboard", "login", "meet", "mobile",
        "rules", "saml", "token", "user_accounts", "context_aware_access", "chrome",
        "data_studio", "keep", "vault", "gemini_in_workspace_apps", "classroom",
        "assignments", "cloud_search", "tasks", "data_migration", "meet_hardware",
        "directory_sync", "ldap", "profile", "access_evaluation", "admin_data_action",
        "contacts", "takeout", "graduation", "voice", "chrome_sync", "workspace_studio",
        "gemini_notebook",
    }
)
#: Padrão: o que importa para segurança e tem volume baixo. ``drive`` e ``gmail``
#: (volume alto; o Drive só registra a maioria dos eventos em edições Enterprise/
#: Frontline), ``chrome``, ``context_aware_access`` e ``access_transparency``
#: (dependem de edição/licença) são opt-in pela lista de aplicações.
DEFAULT_APPLICATIONS: Tuple[str, ...] = (
    "login", "saml", "admin", "user_accounts", "token", "groups", "mobile", "rules",
)

#: Sobreposição entre janelas por aplicação, a partir da tabela oficial de
#: atraso: "near real time" → 20 min; "tens of minutes" (eventos de conta) → 1 h;
#: "a couple of hours"/"up to a few hours" (token/OAuth, grupos, agenda, Cloud
#: Search) → 3 a 4 h. Evento publicado mais tarde que isso não é lido — o
#: Google avisa que atrasos maiores acontecem ocasionalmente.
_DEFAULT_LATE_ARRIVAL = timedelta(minutes=20)
_LATE_ARRIVAL: Dict[str, timedelta] = {
    "user_accounts": timedelta(hours=1),
    "groups": timedelta(hours=3),
    "groups_enterprise": timedelta(hours=3),
    "calendar": timedelta(hours=3),
    "cloud_search": timedelta(hours=4),
    "token": timedelta(hours=4),
}
#: Janela do cold start e retenção da API (auditoria: 6 meses).
_LOOKBACK = timedelta(hours=1)
_RETENTION = timedelta(days=179)
_PAGE_SIZE = 1000
_ALERTS_PAGE_SIZE = 100
#: Teto de páginas por CICLO (somando as aplicações). Sem ele, um cold start ou
#: um worker parado drena o backlog num run só, estoura o ``task_soft_time_limit``
#: (720 s), o pipeline reverte o cursor e o ciclo seguinte recomeça do mesmo
#: ponto: poison-loop. No teto o cursor guarda janela + ``pageToken`` da
#: aplicação em curso.
_MAX_PAGES_PER_CYCLE = 30
_MAX_ALERT_PAGES_PER_CYCLE = 20
#: Alert Center: o alerta aparece minutos depois do ``createTime``.
_ALERT_LATE_ARRIVAL = timedelta(minutes=10)
#: Teto de parâmetros achatados por evento (``event.params``). Eventos normais
#: têm < 30; o teto só protege o envelope contra um evento patológico.
_MAX_PARAMS = 100

_EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9-]{1,63})+$")
_CUSTOMER_RE = re.compile(r"^C?[A-Za-z0-9]{5,20}$")


class GoogleWorkspaceRateLimitedError(VendorRateLimitedError):
    def __init__(self, retry_after: int) -> None:
        super().__init__(retry_after, vendor="google_workspace")


class WorkspaceConfigError(ValueError):
    """Configuração inválida da integração (mensagem segura para a UI)."""


# ── Configuração ─────────────────────────────────────────────────────────


def parse_service_account(text: str) -> Dict[str, Any]:
    """JSON da chave → info validada. Só ``service_account`` (assina o JWT com ``sub``)."""
    try:
        info = parse_credential_info(text)
    except GcpCredentialError as exc:
        raise WorkspaceConfigError(str(exc)) from None
    if info.get("type") != "service_account":
        raise WorkspaceConfigError(
            "a delegação em todo o domínio exige a chave JSON de uma service account "
            "(WIF/external_account não assina o JWT em nome do admin)"
        )
    return info


def validated_admin_email(raw: Optional[str]) -> str:
    email = (raw or "").strip()
    if not _EMAIL_RE.match(email):
        raise WorkspaceConfigError(
            "e-mail do admin a impersonar inválido — use o e-mail de um usuário do "
            "domínio com acesso aos relatórios e ao Alert Center"
        )
    return email


def parse_applications(raw: Optional[str]) -> Tuple[str, ...]:
    """``base_url`` → aplicações a ler (vazio = :data:`DEFAULT_APPLICATIONS`)."""
    text = (raw or "").strip()
    if not text:
        return DEFAULT_APPLICATIONS
    apps: List[str] = []
    for part in re.split(r"[,;\s]+", text.lower()):
        if not part:
            continue
        if part not in KNOWN_APPLICATIONS:
            raise WorkspaceConfigError(
                f"aplicação desconhecida na lista: {part!r} — use nomes da Reports API "
                "(ex.: login, admin, token, drive)"
            )
        if part not in apps:
            apps.append(part)
    return tuple(apps) or DEFAULT_APPLICATIONS


def validated_customer(raw: Optional[str]) -> Optional[str]:
    """Customer ID opcional (``C0xxxxxxx``); vazio = o cliente do admin."""
    text = (raw or "").strip()
    if not text or text == "my_customer":
        return None
    if not _CUSTOMER_RE.match(text):
        raise WorkspaceConfigError(
            "Customer ID inválido — copie de Admin console > Conta > Configurações da "
            "conta (ex.: C01abc23d) ou deixe vazio"
        )
    return text


def _load_settings(integration_id: int) -> Dict[str, Any]:
    """Colunas da integração (sync, rodar em thread)."""
    from ...db import database, models
    from ...services import integration_secrets

    with database.SessionLocal() as db:
        integ = db.get(models.Integration, integration_id)
        if integ is None:
            raise RuntimeError(f"google_workspace: integração {integration_id} não encontrada")
        return {
            "client_secret": integration_secrets.read_secret(integ, "client_secret") or "",
            "client_id": integ.client_id or "",
            "tenant_id": integ.tenant_id or "",
            "base_url": integ.base_url or "",
        }


# ── Credencial → token ───────────────────────────────────────────────────


def _mint_token(info: Dict[str, Any], subject: str) -> Tuple[str, int]:
    """Chave + admin → ``(access_token, expires_in)``. Síncrono: rodar em thread."""
    try:
        from google.oauth2 import service_account  # noqa: PLC0415 — lazy (requirements-sinks)
    except ImportError as exc:
        raise RuntimeError(_NO_SDK_MSG) from exc
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=list(_SCOPES), subject=subject
    )
    creds.refresh(_guarded_request())
    expires_in = 3600
    if creds.expiry is not None:  # naive UTC, convenção do google-auth
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        expires_in = max(60, int((creds.expiry - now).total_seconds()))
    return str(creds.token), expires_in


def _auth_hint(exc: BaseException) -> str:
    """Erro do Google → dica para a UI, SEM o texto da resposta.

    ``unauthorized_client`` é o erro clássico de delegação: o Client ID numérico
    da service account não foi autorizado no Admin console com estes escopos (ou
    a autorização ainda não propagou — até 24 h).
    """
    text = str(exc)
    if "unauthorized_client" in text:
        return (
            "delegação em todo o domínio não autorizada: no Admin console, autorize o "
            "Client ID numérico da service account com os escopos "
            "admin.reports.audit.readonly e apps.alerts (a propagação pode levar até 24 h)"
        )
    if "invalid_grant" in text:
        return (
            "o Google recusou a impersonação: confira o e-mail do admin (usuário ativo do "
            "domínio), se a chave não foi revogada e o relógio do servidor"
        )
    return f"o Google recusou a credencial ({type(exc).__name__}) — confira a chave e a delegação"


async def _workspace_refresher(integration_id: int) -> Dict[str, object]:
    """Token do admin impersonado; o framework o guarda no cache (um por integração)."""
    cfg = await asyncio.to_thread(_load_settings, integration_id)
    info = parse_service_account(cfg["client_secret"])
    subject = validated_admin_email(cfg["client_id"])
    try:
        token, expires_in = await asyncio.to_thread(_mint_token, info, subject)
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001 — o texto do google-auth não sobe
        logger.warning("google_workspace: token recusado (integration=%s): %s",
                       integration_id, type(exc).__name__)
        raise RuntimeError(f"google_workspace: {_auth_hint(exc)}") from None
    return {"access_token": token, "expires_in": expires_in}


# ── HTTP ─────────────────────────────────────────────────────────────────


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value: Any) -> Optional[datetime]:
    """ISO → aware UTC, ou ``None``."""
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = _parse_iso_utc_naive(value)
    return parsed.replace(tzinfo=timezone.utc) if parsed else None


def _parse_retry_after(value: Optional[str]) -> int:
    try:
        return max(1, int(value or ""))
    except (TypeError, ValueError):
        return 60


class _Skip(Exception):
    """403/404 numa aplicação ou na API de alertas: pula, não derruba o ciclo."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


class _StaleToken(Exception):
    """``pageToken`` recusado: refaz a janela sem ele."""


async def _get_json(
    collector: BaseCollector,
    url: str,
    params: Dict[str, str],
    *,
    op: str,
    has_token: bool,
) -> Dict[str, Any]:
    await collector.ctx.rate_limiter.acquire(collector.ctx.integration_id, collector.platform)
    started = time.monotonic()
    try:
        async with collector.ctx.domain_limiter.slot(collector.domain):
            async with collector.ctx.session.get(
                url, params=params, headers=collector.ctx.headers, allow_redirects=False
            ) as resp:
                if resp.status in (429, 503):
                    # A Reports API devolve 503 na cota estourada (doc de limites).
                    raise GoogleWorkspaceRateLimitedError(
                        _parse_retry_after(resp.headers.get("Retry-After"))
                    )
                if is_stale_cursor_response(resp.status, has_opaque_token=has_token) and resp.status not in (403, 404):
                    log_stale_cursor(
                        f"google_workspace {op}",
                        status=resp.status,
                        integration_id=collector.ctx.integration_id,
                        token_field="pageToken",
                        resume_from="início da janela",
                    )
                    raise _StaleToken()
                if resp.status in (403, 404):
                    raise _Skip(resp.status)
                if resp.status >= 300:
                    # 401 sai como ``ClientResponseError``: o pipeline invalida o token
                    # em cache e marca erro de autenticação. Corpo nunca vai para log.
                    logger.warning("google_workspace: %s devolveu HTTP %s", op, resp.status)
                    resp.raise_for_status()
                    # 3xx: redirect não é seguido (o bearer não sai para outro host).
                    info = aiohttp.RequestInfo(resp.url, resp.method, resp.headers, resp.url)
                    raise aiohttp.ClientResponseError(
                        info, (), status=resp.status, message=f"HTTP {resp.status}",
                    )
                payload = await resp.json()
                return payload if isinstance(payload, dict) else {}
    finally:
        API_LATENCY.labels(vendor=collector.platform, stream=collector.stream).observe(
            time.monotonic() - started
        )


# ── Activity: achatamento ────────────────────────────────────────────────


def _param_value(param: Dict[str, Any]) -> Any:
    for key in ("value", "intValue", "boolValue", "multiValue", "multiIntValue",
                "messageValue", "multiMessageValue"):
        if key in param:
            return param[key]
    return None


def flatten_params(params: Any) -> Dict[str, Any]:
    """``[{name, value|intValue|boolValue|multiValue…}]`` → ``{name: valor}``."""
    out: Dict[str, Any] = {}
    if not isinstance(params, list):
        return out
    for param in params[:_MAX_PARAMS]:
        if not isinstance(param, dict):
            continue
        name = param.get("name")
        if isinstance(name, str) and name and name not in out:
            out[name] = _param_value(param)
    return out


def explode_activity(activity: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    """Uma atividade → um registro POR item de ``events[]``.

    Uma atividade agrupa eventos do mesmo ator no mesmo instante (ex.: um
    ``login_success`` e um ``login_verification``); a classe OCSF é por evento,
    então o mapping precisa ver um de cada vez em ``event``.
    """
    base = {k: v for k, v in activity.items() if k != "events"}
    events = activity.get("events")
    if not isinstance(events, list) or not events:
        yield {**base, "event": {}, "_event_index": 0}
        return
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            continue
        item = dict(event)
        item["params"] = flatten_params(event.get("parameters"))
        yield {**base, "event": item, "_event_index": index}


def activity_message_id(record: Dict[str, Any]) -> str:
    """``app:uniqueQualifier:time:índice``.

    O ``uniqueQualifier`` só desempata atividades no MESMO instante (é o que a
    referência da API diz dele) — sozinho não é chave. Com aplicação e horário ele
    identifica a atividade; o índice separa os eventos dela.
    """
    ident = record.get("id") or {}
    if not isinstance(ident, dict) or not ident.get("time"):
        return ""
    return (
        f"{ident.get('applicationName') or ''}:{ident.get('uniqueQualifier') or ''}:"
        f"{ident.get('time')}:{record.get('_event_index', 0)}"
    )


# ── Stream: activity ─────────────────────────────────────────────────────


class GoogleWorkspaceActivityCollector(BaseCollector):
    """Reports API: ``activities.list`` por aplicação, cursor por aplicação."""

    platform = "google_workspace"
    stream = "activity"
    event_type = "google_workspace.activity"
    domain = _REPORTS_HOST

    async def collect(self) -> AsyncIterator[Dict[str, Any]]:
        cfg = await asyncio.to_thread(_load_settings, self.ctx.integration_id)
        apps = parse_applications(cfg["base_url"])
        customer = validated_customer(cfg["tenant_id"])
        cursor = self.ctx.cursor or {}
        now = datetime.now(timezone.utc)
        floor = now - _RETENTION

        if "backfill_from_ts" in cursor:
            # A API é endereçável por janela: o backfill lê exatamente o pedido,
            # dentro dos 6 meses de retenção, sem mexer no cursor do polling.
            start = _iso(max(_parse_iso(cursor["backfill_from_ts"]) or now, floor))
            end = _iso(min(_parse_iso(cursor.get("backfill_to_ts")) or now, now))
            for app in apps:
                token: Optional[str] = None
                while True:
                    try:
                        page = await self._page(app, start, end, token, customer)
                    except _Skip:
                        break
                    for activity in page.get("items") or []:
                        if isinstance(activity, dict):
                            for record in explode_activity(activity):
                                yield record
                    token = page.get("nextPageToken")
                    if not token:
                        break
            return

        previous = cursor.get("apps") if isinstance(cursor.get("apps"), dict) else {}
        states: Dict[str, Dict[str, Any]] = {
            app: dict(previous[app]) for app in apps if isinstance(previous.get(app), dict)
        }
        # Quem está no meio de uma janela vai primeiro; depois a mais atrasada.
        # Sem isso, uma aplicação volumosa que sempre bate o teto deixaria as do
        # fim da lista sem nunca serem lidas.
        order = sorted(
            apps,
            key=lambda a: (not states.get(a, {}).get("window_end"), states.get(a, {}).get("start") or ""),
        )
        pages = 0
        for app in order:
            state = states.get(app, {})
            start = state.get("start") or _iso(now - _LOOKBACK)
            start_dt = _parse_iso(start) or (now - _LOOKBACK)
            if start_dt < floor:
                logger.warning(
                    "google_workspace activity: cursor de %s além da retenção (6 meses) — "
                    "retomando no limite (integration=%s)", app, self.ctx.integration_id,
                )
                start_dt, start = floor, _iso(floor)
            resuming = bool(state.get("window_end"))
            end = state["window_end"] if resuming else _iso(now)
            token = state.get("pageToken") if resuming else None
            boundary = BoundaryKeys.load(state, window_start=start)
            skipped = False
            while True:
                if self.ctx.bounded_per_cycle and pages >= _MAX_PAGES_PER_CYCLE:
                    # Retoma esta aplicação na mesma janela; as outras guardam o
                    # estado que tinham e entram no próximo ciclo (mais atrasada
                    # primeiro).
                    states[app] = {"start": start, "window_end": end, "pageToken": token,
                                   **boundary.carry()}
                    self.ctx.cursor = {"apps": states}
                    self.mark_cycle_capped()
                    logger.info(
                        "google_workspace activity: teto de %d páginas/ciclo em %s — "
                        "retoma no próximo ciclo (integration=%s)",
                        _MAX_PAGES_PER_CYCLE, app, self.ctx.integration_id,
                    )
                    return
                pages += 1
                try:
                    page = await self._page(app, start, end, token, customer)
                except _StaleToken:
                    token = None
                    continue
                except _Skip as skip:
                    logger.warning(
                        "google_workspace activity: HTTP %s em %s — aplicação pulada "
                        "(edição sem o log ou admin sem acesso aos relatórios; "
                        "integration=%s)", skip.status, app, self.ctx.integration_id,
                    )
                    skipped = True
                    break
                for activity in page.get("items") or []:
                    if not isinstance(activity, dict):
                        continue
                    for record in explode_activity(activity):
                        when = (record.get("id") or {}).get("time")
                        if boundary.seen(when, self.extract_message_id(record)):
                            continue
                        yield record
                token = page.get("nextPageToken")
                if not token:
                    break
            if skipped:
                # Janela intacta para quando o acesso voltar; ``forbidden`` tira a
                # aplicação do watermark (não é atraso, é ausência de acesso).
                states[app] = {**{k: v for k, v in state.items()
                                  if k not in ("window_end", "pageToken")},
                               "start": start, "forbidden": True}
                continue
            late = _LATE_ARRIVAL.get(app, _DEFAULT_LATE_ARRIVAL)
            end_dt = _parse_iso(end) or now
            next_start = _iso(max(start_dt, end_dt - late))
            states[app] = {"start": next_start, **boundary.dump(next_start)}
        self.ctx.cursor = {"apps": states}

    async def _page(
        self, app: str, start: str, end: str, token: Optional[str], customer: Optional[str]
    ) -> Dict[str, Any]:
        params = {"startTime": start, "endTime": end, "maxResults": str(_PAGE_SIZE)}
        if token:
            params["pageToken"] = token
        if customer:
            params["customerId"] = customer
        return await _get_json(self, f"{_REPORTS_API}/{app}", params,
                               op=f"activities.list {app}", has_token=bool(token))

    def extract_message_id(self, event: Dict[str, Any]) -> str:
        return activity_message_id(event)

    @classmethod
    def watermark_at(cls, cursor: Optional[Dict[str, Any]]) -> Optional[datetime]:
        """O início de janela MAIS ANTIGO entre as aplicações acessíveis.

        Uma aplicação atrasada atrasa o stream: reportar a mais nova esconderia o
        backlog. Fica até ``_LATE_ARRIVAL`` atrás do relógio por construção.
        """
        apps = (cursor or {}).get("apps")
        if not isinstance(apps, dict):
            return None
        instants = [
            cls.watermark_from_iso(state, "start")
            for state in apps.values()
            if isinstance(state, dict) and not state.get("forbidden")
        ]
        instants = [i for i in instants if i is not None]
        return min(instants) if instants else None


# ── Stream: alerts ───────────────────────────────────────────────────────


class GoogleWorkspaceAlertsCollector(BaseCollector):
    """Alert Center: alertas por ``createTime``, janela com sobreposição."""

    platform = "google_workspace"
    stream = "alerts"
    event_type = "google_workspace.alert"
    domain = _ALERTS_HOST

    async def collect(self) -> AsyncIterator[Dict[str, Any]]:
        cfg = await asyncio.to_thread(_load_settings, self.ctx.integration_id)
        customer = validated_customer(cfg["tenant_id"])
        cursor = self.ctx.cursor or {}
        now = datetime.now(timezone.utc)

        if "backfill_from_ts" in cursor:
            start = _iso(_parse_iso(cursor["backfill_from_ts"]) or now)
            end = _iso(min(_parse_iso(cursor.get("backfill_to_ts")) or now, now))
            token: Optional[str] = None
            while True:
                try:
                    page = await self._page(start, end, token, customer)
                except _Skip:
                    return
                for alert in page.get("alerts") or []:
                    if isinstance(alert, dict):
                        yield alert
                token = page.get("nextPageToken")
                if not token:
                    return

        start = cursor.get("createTime") or _iso(now - _LOOKBACK)
        start_dt = _parse_iso(start) or (now - _LOOKBACK)
        resuming = bool(cursor.get("window_end"))
        end = cursor["window_end"] if resuming else _iso(now)
        token = cursor.get("pageToken") if resuming else None
        boundary = BoundaryKeys.load(cursor, window_start=start)
        pages = 0
        while True:
            if self.ctx.bounded_per_cycle and pages >= _MAX_ALERT_PAGES_PER_CYCLE:
                self.ctx.cursor = {"createTime": start, "window_end": end, "pageToken": token,
                                   **boundary.carry()}
                self.mark_cycle_capped()
                logger.info(
                    "google_workspace alerts: teto de %d páginas/ciclo — retoma no "
                    "próximo ciclo (integration=%s)",
                    _MAX_ALERT_PAGES_PER_CYCLE, self.ctx.integration_id,
                )
                return
            pages += 1
            try:
                page = await self._page(start, end, token, customer)
            except _StaleToken:
                token = None
                continue
            except _Skip as skip:
                # Sem acesso ao Alert Center (escopo não delegado / admin sem
                # privilégio): o stream de atividade segue; o cursor fica parado.
                logger.warning(
                    "google_workspace alerts: HTTP %s — sem acesso ao Alert Center "
                    "(escopo apps.alerts delegado? integration=%s)",
                    skip.status, self.ctx.integration_id,
                )
                self.ctx.cursor = {"createTime": start, **boundary.carry()}
                return
            for alert in page.get("alerts") or []:
                if not isinstance(alert, dict):
                    continue
                if boundary.seen(alert.get("createTime"), self.extract_message_id(alert)):
                    continue
                yield alert
            token = page.get("nextPageToken")
            if not token:
                break
        end_dt = _parse_iso(end) or now
        next_start = _iso(max(start_dt, end_dt - _ALERT_LATE_ARRIVAL))
        self.ctx.cursor = {"createTime": next_start, **boundary.dump(next_start)}

    async def _page(
        self, start: str, end: str, token: Optional[str], customer: Optional[str]
    ) -> Dict[str, Any]:
        params = {
            "filter": f'createTime >= "{start}" AND createTime < "{end}"',
            "orderBy": "createTime asc",
            "pageSize": str(_ALERTS_PAGE_SIZE),
        }
        if token:
            params["pageToken"] = token
        if customer:
            # O Alert Center quer o Customer ID SEM o "C" inicial.
            params["customerId"] = customer[1:] if customer.startswith("C") else customer
        return await _get_json(self, _ALERTS_API, params, op="alerts.list", has_token=bool(token))

    def extract_message_id(self, event: Dict[str, Any]) -> str:
        return str(event.get("alertId") or "")

    @classmethod
    def watermark_at(cls, cursor: Optional[Dict[str, Any]]) -> Optional[datetime]:
        return cls.watermark_from_iso(cursor, "createTime")


_COLLECTORS = (GoogleWorkspaceActivityCollector, GoogleWorkspaceAlertsCollector)


# ── Teste de conexão (pré-save) ──────────────────────────────────────────


async def _probe_get(
    session: aiohttp.ClientSession, url: str, params: Dict[str, str], headers: Dict[str, str]
) -> str:
    async with session.get(url, params=params, headers=headers, allow_redirects=False) as resp:
        if resp.status < 300:
            return "ok"
        if resp.status == 403:
            return "sem permissão (admin sem o privilégio ou escopo não delegado)"
        if resp.status in (429, 503):
            return "cota excedida — tente de novo em instantes"
        return f"erro HTTP {resp.status}"


async def _workspace_probe(cfg: Dict[str, Any]):
    """Teste STATELESS pré-save: token do admin + 1 leitura em cada API."""
    from ..output.base import TestResult

    raw = (cfg.get("client_secret") or "").strip()
    if not raw or not (cfg.get("client_id") or "").strip():
        return TestResult.failed("Informe a chave JSON da service account e o e-mail do admin.")
    try:
        info = parse_service_account(raw)
        subject = validated_admin_email(cfg.get("client_id"))
        apps = parse_applications(cfg.get("base_url"))
        customer = validated_customer(cfg.get("tenant_id"))
    except WorkspaceConfigError as exc:
        return TestResult.failed(str(exc))

    t0 = time.perf_counter()
    try:
        token, _ = await asyncio.to_thread(_mint_token, info, subject)
    except RuntimeError as exc:
        return TestResult.failed(str(exc))
    except Exception as exc:  # noqa: BLE001 — só a dica; o texto do Google não volta
        logger.info("google_workspace probe: credencial recusada (%s)", type(exc).__name__)
        hint = _auth_hint(exc)
        return TestResult.failed(f"{hint[0].upper()}{hint[1:]}.")

    headers = {"Authorization": f"Bearer {token}"}
    app = "login" if "login" in apps else apps[0]
    activity_params = {"maxResults": "1"}
    alert_params = {"pageSize": "1"}
    if customer:
        activity_params["customerId"] = customer
        alert_params["customerId"] = customer[1:] if customer.startswith("C") else customer
    states: List[str] = []
    ready = 0
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            for label, url, params in (
                (f"activity ({app})", f"{_REPORTS_API}/{app}", activity_params),
                ("alerts", _ALERTS_API, alert_params),
            ):
                state = await _probe_get(session, url, params, headers)
                ready += state == "ok"
                states.append(f"{label}: {state}")
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        return TestResult.failed(f"Falha ao falar com o Google: {type(exc).__name__}")
    detail = "; ".join(states)
    if not ready:
        return TestResult.failed(f"Nenhum stream utilizável — {detail}")
    ms = (time.perf_counter() - t0) * 1000.0
    return TestResult.passed(f"Conexão OK — {detail}; aplicações: {', '.join(apps)}", latency_ms=ms)


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
            platform="google_workspace",
            display_name="Google Workspace",
            category="Identity",
            description=(
                "Google Workspace — logs de auditoria da Reports API (login, 2SV, Admin "
                "console, consentimento OAuth, SAML, grupos, dispositivos, regras/DLP) e "
                "alertas do Alert Center."
            ),
            icon_id="googleworkspace",
            docs_url="https://developers.google.com/workspace/admin/reports/v1/get-start/overview",
            order=27,
            test_fn=_workspace_probe,
            required_secrets=("client_secret",),
            capabilities=frozenset(
                {"catalog", "auth:test"} | {f"collect:{cls.stream}" for cls in _COLLECTORS}
            ),
            auth_fields=(
                AuthField(key="client_secret", label="Chave da service account (JSON)",
                          type="secret", required=True,
                          help_text=(
                              "Chave JSON de uma service account com delegação em todo o "
                              "domínio para admin.reports.audit.readonly e apps.alerts"
                          )),
                AuthField(key="client_id", label="E-mail do admin a impersonar", type="string",
                          required=True,
                          help_text=(
                              "Usuário do domínio com acesso aos relatórios de auditoria e "
                              "ao Alert Center (ex.: auditoria@empresa.com)"
                          )),
                AuthField(key="tenant_id", label="Customer ID", type="string", required=False,
                          help_text="Opcional. Vazio = o cliente do admin (ex.: C01abc23d)"),
                AuthField(key="base_url", label="Aplicações", type="string", required=False,
                          help_text=(
                              "Opcional, separadas por vírgula. Padrão: "
                              + ", ".join(DEFAULT_APPLICATIONS)
                              + ". drive e gmail são opt-in (volume alto)."
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
                refresh_fn=_workspace_refresher,
                schedule=_td(minutes=5),
                queue=Q_BULK,
                task_name=T_COLLECT_BULK,
            )
        )


_register()
