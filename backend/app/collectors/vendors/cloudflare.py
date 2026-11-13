"""Cloudflare — eventos de segurança (WAF, regras de firewall, rate limiting, bots, DDoS L7).

Vendor novo = 1 módulo, ZERO core. Um stream:

- ``security_events`` — dataset ``firewallEventsAdaptive`` da GraphQL Analytics API
  (``POST https://api.cloudflare.com/client/v4/graphql``): cada evento é uma
  requisição que um produto de segurança da Cloudflare ACIONOU ou SINALIZOU (a
  mesma requisição pode gerar vários eventos), com a ação aplicada
  (``block``/``managed_challenge``/``log``/``skip``/…), a regra, o IP REAL do
  cliente, a geo/ASN e o recorte da requisição. É o que o WAF/proxy do cliente
  não enxerga quando a zona está atrás da Cloudflare (ele só vê IPs da Cloudflare).

**Janelas por tempo, uma consulta por página.** A API não tem cursor opaco: o
filtro é ``datetime_geq`` (inclusivo) / ``datetime_lt`` (exclusivo) e a ordem é
``datetime_ASC``. Por isso o cursor é POR ZONA — ``{"zones": {"<zone>": {"since":
ISO, "boundary": {...}}}}`` — e o limite inferior inclusivo usa a memória de
fronteira (``cursor_boundary``): vários eventos caem no mesmo segundo e o último
visto voltaria a cada ciclo.

**Limites vêm da API, nunca do código.** Antes de consultar uma zona o coletor lê o
nó ``settings.firewallEventsAdaptive`` (``enabled``, ``availableFields``,
``maxDuration``, ``maxNumberOfFields``, ``maxPageSize``, ``notOlderThan``) e deriva
dele a largura da janela, o tamanho da página, os campos pedidos e o piso de
retenção. Zona com o dataset desligado (plano que não o expõe) é PULADA com
WARNING — não derruba o ciclo das demais. Se TODAS forem puladas, o ciclo falha
(coleta "verde" sem coletar nada é o pior modo de falha).

**Página cheia.** ``len(rows) == limit`` pode haver mais: a leitura continua do
``datetime`` do último evento (inclusivo + memória de fronteira). Se uma página
cheia NÃO avança o instante (mais eventos que uma página no MESMO segundo), o
excedente é irrecuperável por esta API: o log diz isso e a janela anda 1 s —
nunca laço infinito.

**Cota.** 300 consultas GraphQL por usuário a cada 5 min (a Cloudflare aceita uma
por segundo em regime). Cada zona gasta 1 consulta de ``settings`` + 1 por página
a cada ciclo de 1 min: ~10 zonas por usuário de token com folga, ~30 no limite. 429
(HTTP ou erro GraphQL de "budget") vira ``CloudflareRateLimitedError`` com o
``Retry-After``.

**Superfície de rede fechada.** Host fixo ``api.cloudflare.com``, só HTTPS, sem
seguir redirect. O token vai só no header ``Authorization`` e nunca em mensagem de
erro, log ou resultado do teste de conexão. Os IDs de zona (32 hex) só entram como
variável GraphQL, nunca na URL.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import aiohttp

from ..base import BaseCollector, CollectorContext, _parse_iso_utc_naive
from ..cursor_boundary import BoundaryKeys
from ..metrics import API_LATENCY
from ._rate_limit import VendorRateLimitedError

logger = logging.getLogger(__name__)

_API_HOST = "api.cloudflare.com"
_GRAPHQL_URL = f"https://{_API_HOST}/client/v4/graphql"
_VERIFY_URL = f"https://{_API_HOST}/client/v4/user/tokens/verify"
_ZONES_URL = f"https://{_API_HOST}/client/v4/zones"

#: Teto de páginas de EVENTOS por ciclo (agendado a cada minuto). Sem ele um
#: backlog (cold start, worker parado, ataque) seria drenado num run só e
#: estouraria o ``task_soft_time_limit``; o ciclo seguinte recomeçaria do mesmo
#: ponto — poison-loop. No teto o cursor já aponta para a próxima página.
_MAX_PAGES_PER_CYCLE = 10
#: Piso do tamanho de página que o coletor aceita da API (``maxPageSize``): evita
#: segurar dezenas de MB por página se um plano devolver um limite enorme.
_PAGE_SIZE_CEILING = 10_000

_COLD_START_LOOKBACK = timedelta(hours=1)
#: A ingestão da Cloudflare tem latência: não pedir o último minuto.
_INGESTION_LAG = timedelta(seconds=60)
#: Margem sobre ``notOlderThan``: o ``datetime_geq`` não pode vencer entre o
#: cálculo e a chegada do pedido.
_RETENTION_MARGIN = timedelta(minutes=5)
#: Backfill: a fatia que bate no limite é dividida ao meio até este piso.
_BACKFILL_MIN_SLICE_S = 1

_MAX_ZONES = 50
_ZONES_PER_PAGE = 50
_MAX_ZONE_PAGES = 20
_ZONE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=120)
_PROBE_TIMEOUT = aiohttp.ClientTimeout(total=30)
_DEFAULT_RETRY_AFTER = 60
_ERROR_TEXT_MAX = 200

#: Campos pedidos, por prioridade, filtrados por ``availableFields`` e limitados
#: por ``maxNumberOfFields``. Os obrigatórios entram sempre (ver ``plan_zone``).
_FIELD_PRIORITY: Tuple[str, ...] = (
    "datetime", "rayName", "action", "source", "ruleId", "description", "kind",
    "matchIndex", "rulesetId", "clientIP", "clientIPClass", "clientAsn",
    "clientASNDescription", "clientCountryName", "clientRequestHTTPHost",
    "clientRequestHTTPMethodName", "clientRequestHTTPProtocol", "clientRequestScheme",
    "clientRequestPath", "clientRequestQuery", "clientRefererHost", "userAgent",
    "edgeColoName", "edgeResponseStatus", "originResponseStatus", "sampleInterval", "ref",
)
_MANDATORY_FIELDS: Tuple[str, ...] = ("datetime", "rayName", "action", "source", "clientIP")

_PERMISSION_HINT = (
    "o token precisa da permissão Zone > Analytics > Read nas zonas coletadas "
    "(e Zone > Zone > Read se as zonas forem descobertas automaticamente)"
)

_SETTINGS_QUERY = (
    "query ZoneSettings($zoneTag: string) { viewer { zones(filter: { zoneTag: $zoneTag }) "
    "{ settings { firewallEventsAdaptive { enabled availableFields maxDuration "
    "maxNumberOfFields maxPageSize notOlderThan } } } } }"
)

_RATE_ERROR_RE = re.compile(
    r"rate limit|budget|too many requests|excessive resources|queries too many", re.I
)
_AUTH_ERROR_RE = re.compile(
    r"not authorized|are not authorized|does not have access|unauthori[sz]ed|authentication", re.I
)
_RETRY_MINUTES_RE = re.compile(r"after\s+(\d+)\s+minute", re.I)


# ── Erros ────────────────────────────────────────────────────────────────


class CloudflareRateLimitedError(VendorRateLimitedError):
    def __init__(self, retry_after: int) -> None:
        super().__init__(retry_after, vendor="cloudflare")


class CloudflareCredentialError(ValueError):
    """Token ou IDs de zona inválidos (mensagem sem segredo)."""


class CloudflareGraphQLError(RuntimeError):
    """A API respondeu 200 com ``errors``. A mensagem é só o texto do erro."""


class CloudflareApiError(aiohttp.ClientResponseError):
    """Erro HTTP da Cloudflare (401/403 fazem o pipeline marcar erro de autenticação).

    Montado SÓ a partir da resposta: o ``request_info`` de um ``raise_for_status``
    carregaria o header ``Authorization`` do pedido para o log de exceção. A
    mensagem é o texto do erro da API, já sem o token.
    """

    def __init__(self, resp: aiohttp.ClientResponse, message: str, status: Optional[int] = None) -> None:
        info = aiohttp.RequestInfo(resp.url, resp.method, resp.headers, resp.url)
        super().__init__(
            info, getattr(resp, "history", ()), status=status or resp.status,
            message=message, headers=resp.headers,
        )
        self.title = message


# ── Configuração ─────────────────────────────────────────────────────────


def parse_zone_ids(raw: str) -> List[str]:
    """``"a1.., b2.."`` → IDs de zona (32 hex), sem repetição, ordem preservada.

    Vazio é válido: significa "toda zona que o token enxerga" (descoberta por ciclo).
    """
    parts = [p for p in re.split(r"[,;\s]+", (raw or "").strip().lower()) if p]
    bad = [p for p in parts if not _ZONE_ID_RE.match(p)]
    if bad:
        shown = ", ".join(p[:40] for p in bad[:5])
        raise CloudflareCredentialError(
            "ID de zona inválido (use os 32 caracteres hexadecimais do Zone ID): " + shown
        )
    ids = list(dict.fromkeys(parts))
    if len(ids) > _MAX_ZONES:
        raise CloudflareCredentialError(f"no máximo {_MAX_ZONES} zonas por integração")
    return ids


def _load_settings(integration_id: int) -> Dict[str, str]:
    """Token e IDs de zona da integração (sync, rodar em thread)."""
    from ...db import database, models
    from ...services import integration_secrets

    with database.SessionLocal() as db:
        integ = db.get(models.Integration, integration_id)
        if integ is None:
            raise RuntimeError(f"cloudflare: integração {integration_id} não encontrada")
        return {
            "token": integration_secrets.read_secret(integ, "client_secret") or "",
            "zone_ids": integ.tenant_id or "",
        }


# ── Limites da zona (nó settings) ────────────────────────────────────────


@dataclass(frozen=True)
class ZonePlan:
    """O que a API permite para UMA zona, já traduzido em parâmetros de consulta."""

    fields: Tuple[str, ...]
    max_duration: int
    page_size: int
    not_older_than: int
    max_fields: int
    available: int


def plan_zone(node: Any) -> "ZonePlan | str":
    """Nó ``settings.firewallEventsAdaptive`` → ``ZonePlan``, ou o MOTIVO (texto) de a
    zona não ser utilizável. Não levanta: o chamador decide entre pular e falhar."""
    if not isinstance(node, dict):
        return "a Cloudflare não devolveu os limites do dataset firewallEventsAdaptive"
    if node.get("enabled") is not True:
        return "dataset firewallEventsAdaptive desativado para esta zona (o plano não o expõe)"
    numbers: Dict[str, int] = {}
    for key in ("maxDuration", "maxPageSize", "maxNumberOfFields", "notOlderThan"):
        value = node.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return f"limites incompletos no settings da zona ({key} ausente ou inválido)"
        numbers[key] = value
    raw_available = node.get("availableFields")
    available = {f for f in raw_available if isinstance(f, str)} if isinstance(raw_available, list) else set()
    missing = [f for f in _MANDATORY_FIELDS if f not in available]
    if missing:
        return "campos obrigatórios indisponíveis na zona: " + ", ".join(missing)
    cap = numbers["maxNumberOfFields"]
    if cap < len(_MANDATORY_FIELDS):
        return (
            f"maxNumberOfFields={cap} é menor que os {len(_MANDATORY_FIELDS)} campos "
            "obrigatórios da coleta"
        )
    chosen = set(_MANDATORY_FIELDS)
    for name in _FIELD_PRIORITY:
        if len(chosen) >= cap:
            break
        if name in available and _FIELD_RE.match(name):
            chosen.add(name)
    return ZonePlan(
        fields=tuple(f for f in _FIELD_PRIORITY if f in chosen),
        max_duration=numbers["maxDuration"],
        page_size=min(numbers["maxPageSize"], _PAGE_SIZE_CEILING),
        not_older_than=numbers["notOlderThan"],
        max_fields=cap,
        available=len(available),
    )


def _zone_node(data: Any) -> Optional[Dict[str, Any]]:
    """``data.viewer.zones[0]`` → dict, ou ``None`` (token sem acesso à zona)."""
    try:
        zones = data["viewer"]["zones"]
    except (KeyError, TypeError):
        return None
    if isinstance(zones, list) and zones and isinstance(zones[0], dict):
        return zones[0]
    return None


def settings_node(data: Any) -> Any:
    zone = _zone_node(data)
    return ((zone or {}).get("settings") or {}).get("firewallEventsAdaptive")


def events_query(fields: Tuple[str, ...], limit: int) -> str:
    """Consulta de eventos. ``fields`` e ``limit`` são validados (nome GraphQL / int)
    porque entram no TEXTO da consulta; zona e janela vão como variáveis."""
    if not all(_FIELD_RE.match(f) for f in fields) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("campos ou limite inválidos para a consulta de eventos")
    return (
        "query ListFirewallEvents($zoneTag: string, $filter: FirewallEventsAdaptiveFilter_InputObject) "
        "{ viewer { zones(filter: { zoneTag: $zoneTag }) { "
        f"firewallEventsAdaptive(filter: $filter, limit: {limit}, orderBy: [datetime_ASC]) "
        "{ " + " ".join(fields) + " } } } }"
    )


# ── Tempo ────────────────────────────────────────────────────────────────


def _utcnow() -> datetime:
    """Agora em UTC, naive e TRUNCADO ao segundo (a API filtra em segundos)."""
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_epoch(value: Any) -> Optional[int]:
    """Instante do backfill (ISO-8601 ou epoch em segundos) → epoch em segundos."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.isdigit():
            return int(text)
        parsed = _parse_iso_utc_naive(text)
        if parsed is not None:
            return int(parsed.replace(tzinfo=timezone.utc).timestamp())
    return None


def _from_epoch(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(tzinfo=None)


def _parse_retry_after(value: Optional[str]) -> int:
    try:
        return max(1, int(value or ""))
    except (TypeError, ValueError):
        return _DEFAULT_RETRY_AFTER


# ── Transporte ───────────────────────────────────────────────────────────


def _scrub(text: Any, token: str) -> str:
    """Texto de erro da API, truncado e SEM o token (defesa em profundidade)."""
    out = str(text)[:_ERROR_TEXT_MAX]
    return out.replace(token, "***") if token else out


async def _json_or_none(resp: aiohttp.ClientResponse) -> Any:
    try:
        return await resp.json(content_type=None)
    except (ValueError, aiohttp.ClientError):
        return None


def _error_messages(body: Any, token: str) -> List[str]:
    """``errors[].message`` do corpo (GraphQL ou REST), sem token. Nada além do texto."""
    if not isinstance(body, dict) or not isinstance(body.get("errors"), list):
        return []
    out: List[str] = []
    for item in body["errors"][:3]:
        text = item.get("message") if isinstance(item, dict) else item
        if text:
            out.append(_scrub(text, token))
    return out


async def _call(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    token: str,
    *,
    json_body: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: aiohttp.ClientTimeout = _REQUEST_TIMEOUT,
    forbidden_hint: str = _PERMISSION_HINT,
) -> Dict[str, Any]:
    """Uma chamada à Cloudflare → corpo JSON (dict) ou exceção com mensagem limpa.

    Nunca segue redirect. 429 → ``CloudflareRateLimitedError`` (``Retry-After``,
    padrão 60 s). ``errors`` num 200 → ``CloudflareGraphQLError`` (rate/budget vira
    rate limit; permissão vira ``CloudflareApiError`` 403 que nomeia a permissão).
    """
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    kwargs: Dict[str, Any] = {"headers": headers, "allow_redirects": False, "timeout": timeout}
    if params:
        kwargs["params"] = params
    if method == "POST":
        kwargs["json"] = json_body
        resp_cm = session.post(url, **kwargs)
    else:
        resp_cm = session.get(url, **kwargs)
    async with resp_cm as resp:
        retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
        if resp.status == 429:
            raise CloudflareRateLimitedError(retry_after)
        if 300 <= resp.status < 400:
            raise CloudflareApiError(resp, f"redirecionamento recusado (HTTP {resp.status})")
        body = await _json_or_none(resp)
        messages = _error_messages(body, token)
        if resp.status == 401:
            raise CloudflareApiError(resp, "token inválido, expirado ou revogado")
        if resp.status == 403:
            raise CloudflareApiError(resp, f"sem permissão — {forbidden_hint}")
        if resp.status != 200:
            detail = messages[0] if messages else f"HTTP {resp.status}"
            logger.warning("cloudflare: HTTP %s — %s", resp.status, detail)
            raise CloudflareApiError(resp, detail)
        if not isinstance(body, dict):
            raise CloudflareGraphQLError("resposta da Cloudflare não é um JSON válido")
        if messages:
            joined = "; ".join(messages)
            if _RATE_ERROR_RE.search(joined):
                minutes = _RETRY_MINUTES_RE.search(joined)
                raise CloudflareRateLimitedError(int(minutes.group(1)) * 60 if minutes else retry_after)
            if _AUTH_ERROR_RE.search(joined):
                raise CloudflareApiError(resp, f"sem permissão — {forbidden_hint}", status=403)
            raise CloudflareGraphQLError(joined)
        if body.get("success") is False:
            raise CloudflareGraphQLError("a Cloudflare recusou o pedido (success=false)")
        return body


def _graphql_body(query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    return {"query": query, "variables": variables}


async def _discover_zones(
    session: aiohttp.ClientSession, token: str, *, timeout: aiohttp.ClientTimeout
) -> List[str]:
    """Zonas ativas que o token enxerga (``GET /zones``, paginado). Exige Zone > Zone > Read."""
    found: List[str] = []
    page, total_pages = 1, 1
    while page <= total_pages and page <= _MAX_ZONE_PAGES:
        body = await _call(
            session, "GET", _ZONES_URL, token, timeout=timeout,
            params={"per_page": _ZONES_PER_PAGE, "page": page},
            forbidden_hint="a descoberta automática de zonas exige Zone > Zone > Read "
            "(ou informe os Zone IDs no campo da integração)",
        )
        for item in body.get("result") or []:
            if isinstance(item, dict) and _ZONE_ID_RE.match(str(item.get("id", "")).lower()):
                if item.get("status") in (None, "active"):
                    found.append(str(item["id"]).lower())
        info = body.get("result_info")
        total = info.get("total_pages") if isinstance(info, dict) else None
        total_pages = total if isinstance(total, int) and total > 0 else page
        page += 1
    found = list(dict.fromkeys(found))
    if len(found) > _MAX_ZONES:
        logger.warning(
            "cloudflare: o token enxerga %d zonas; coletando só as %d primeiras — informe "
            "os Zone IDs para escolher quais", len(found), _MAX_ZONES,
        )
        found = found[:_MAX_ZONES]
    return found


# ── Coletor ──────────────────────────────────────────────────────────────


class CloudflareSecurityEventsCollector(BaseCollector):
    """Eventos de segurança da Cloudflare (GraphQL Analytics, ``firewallEventsAdaptive``)."""

    platform = "cloudflare"
    stream = "security_events"
    event_type = "cloudflare.security_events"

    def __init__(self, ctx: CollectorContext) -> None:
        super().__init__(ctx)
        self._token = ""
        self._pages = 0
        self._capped = False

    @property
    def domain(self) -> str:
        return _API_HOST

    # ── coleta de polling ────────────────────────────────────────────────

    async def collect(self) -> AsyncIterator[Dict[str, Any]]:
        cfg = await asyncio.to_thread(_load_settings, self.ctx.integration_id)
        self._token = cfg["token"].strip()
        if not self._token:
            raise CloudflareCredentialError("token da Cloudflare não configurado")
        configured = parse_zone_ids(cfg["zone_ids"])

        cursor = self.ctx.cursor or {}
        if "backfill_from_ts" in cursor:
            async for event in self._backfill(cursor, configured):
                yield event
            return

        zones = configured or await self._discover()
        if not zones:
            raise RuntimeError("cloudflare security_events: o token não enxerga nenhuma zona ativa")
        raw_saved = cursor.get("zones")
        saved: Dict[str, Any] = raw_saved if isinstance(raw_saved, dict) else {}
        # Zona removida da configuração perde o cursor; zona nova começa a frio.
        state: Dict[str, Dict[str, Any]] = {
            z: saved[z] for z in zones if isinstance(saved.get(z), dict)
        }
        self.ctx.cursor = {"zones": state}

        # A zona mais atrasada primeiro: com o teto de páginas é ela que precisa do ciclo.
        zones = sorted(zones, key=lambda z: self._since_of(state.get(z)) or datetime.min)
        usable = 0
        for zone in zones:
            plan = await self._plan(zone)
            if isinstance(plan, str):
                logger.warning(
                    "cloudflare security_events: zona %s pulada — %s (integration=%s)",
                    zone, plan, self.ctx.integration_id,
                )
                continue
            usable += 1
            async for event in self._drain_zone(zone, plan, state):
                yield event
            if self._capped:
                return
        if not usable:
            raise RuntimeError(
                "cloudflare security_events: nenhuma zona utilizável — confira o plano, "
                "os campos disponíveis e a permissão do token (veja o WARNING de cada zona)"
            )

    def _since_of(self, zstate: Optional[Dict[str, Any]]) -> Optional[datetime]:
        raw = (zstate or {}).get("since")
        return _parse_iso_utc_naive(raw) if isinstance(raw, str) else None

    async def _discover(self) -> List[str]:
        await self.ctx.rate_limiter.acquire(self.ctx.integration_id, self.platform)
        async with self.ctx.domain_limiter.slot(self.domain):
            return await _discover_zones(self.ctx.session, self._token, timeout=_REQUEST_TIMEOUT)

    async def _graphql(self, query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
        await self.ctx.rate_limiter.acquire(self.ctx.integration_id, self.platform)
        started = time.monotonic()
        try:
            async with self.ctx.domain_limiter.slot(self.domain):
                body = await _call(
                    self.ctx.session, "POST", _GRAPHQL_URL, self._token,
                    json_body=_graphql_body(query, variables),
                )
        finally:
            API_LATENCY.labels(vendor=self.platform, stream=self.stream).observe(
                time.monotonic() - started
            )
        return body.get("data") or {}

    async def _plan(self, zone: str) -> "ZonePlan | str":
        """Limites da zona, lidos UMA vez por ciclo."""
        data = await self._graphql(_SETTINGS_QUERY, {"zoneTag": zone})
        if _zone_node(data) is None:
            return "o token não tem acesso a esta zona (ou o ID não existe)"
        return plan_zone(settings_node(data))

    async def _fetch(self, zone: str, plan: ZonePlan, lo: datetime, hi: datetime) -> List[Dict[str, Any]]:
        """Uma página de eventos de ``[lo, hi)``, em ordem crescente de ``datetime``."""
        data = await self._graphql(
            events_query(plan.fields, plan.page_size),
            {"zoneTag": zone, "filter": {"datetime_geq": _iso(lo), "datetime_lt": _iso(hi)}},
        )
        node = _zone_node(data)
        if node is None:
            raise CloudflareGraphQLError(f"zona {zone} inacessível ao token durante a coleta")
        rows = node.get("firewallEventsAdaptive")
        if not isinstance(rows, list):
            raise CloudflareGraphQLError("resposta de eventos sem a lista firewallEventsAdaptive")
        return [r for r in rows if isinstance(r, dict)]

    def _start_of(self, zone: str, plan: ZonePlan, zstate: Optional[Dict[str, Any]], now: datetime) -> datetime:
        """Limite inferior do ciclo: cursor (ou ``agora − 1 h``) preso à retenção."""
        since = self._since_of(zstate)
        had_cursor = since is not None
        if since is None:
            if zstate and zstate.get("since"):
                logger.warning(
                    "cloudflare security_events: zona %s com cursor ilegível — recomeçando a frio "
                    "(integration=%s)", zone, self.ctx.integration_id,
                )
            since = now - _COLD_START_LOOKBACK
        floor = (now - timedelta(seconds=plan.not_older_than) + _RETENTION_MARGIN).replace(microsecond=0)
        if since < floor:
            if had_cursor:
                logger.warning(
                    "cloudflare security_events: zona %s — o cursor (%s) passou da retenção da API "
                    "(notOlderThan=%ds); %s de eventos não podem mais ser lidos (integration=%s)",
                    zone, _iso(since), plan.not_older_than, floor - since, self.ctx.integration_id,
                )
            since = floor
        return since

    async def _drain_zone(
        self, zone: str, plan: ZonePlan, state: Dict[str, Dict[str, Any]]
    ) -> AsyncIterator[Dict[str, Any]]:
        now = _utcnow()
        ceiling = now - _INGESTION_LAG
        since = self._start_of(zone, plan, state.get(zone), now)
        boundary = BoundaryKeys.load(state.get(zone), window_start=_iso(since))

        while since < ceiling:
            end = min(since + timedelta(seconds=plan.max_duration), ceiling)
            if end <= since:
                break
            rows = await self._fetch(zone, plan, since, end)
            self._pages += 1
            full = len(rows) >= plan.page_size

            for row in rows:
                event = dict(row)
                event["zoneTag"] = zone
                if boundary.seen(row.get("datetime"), self.extract_message_id(event)):
                    continue
                yield event

            if full:
                next_since = self._resume_point(zone, plan, rows, since, end)
            else:
                next_since = end
            memory = boundary.dump(_iso(next_since))
            state[zone] = {"since": _iso(next_since), **memory}
            self.ctx.cursor = {"zones": state}
            boundary = BoundaryKeys.load(state[zone], window_start=_iso(next_since))
            since = next_since

            # Teto por ciclo: o cursor acima já aponta para a PRÓXIMA página.
            if (full or since < ceiling) and self.ctx.bounded_per_cycle and self._pages >= _MAX_PAGES_PER_CYCLE:
                self.mark_cycle_capped()
                self._capped = True
                logger.info(
                    "cloudflare security_events: teto de %d páginas/ciclo — cursor salvo p/ o "
                    "próximo ciclo (integration=%s)", _MAX_PAGES_PER_CYCLE, self.ctx.integration_id,
                )
                return

    def _resume_point(
        self, zone: str, plan: ZonePlan, rows: List[Dict[str, Any]], since: datetime, end: datetime
    ) -> datetime:
        """Para onde a leitura anda depois de uma página CHEIA.

        Normal: o ``datetime`` do último evento (inclusivo; a memória de fronteira
        evita reemitir os do mesmo segundo). Se isso não avança o instante há mais
        eventos que uma página num único segundo: o excedente não é alcançável por
        esta API — registra e anda 1 s.
        """
        last = _parse_iso_utc_naive(str(rows[-1].get("datetime") or ""))
        if last is None:
            logger.warning(
                "cloudflare security_events: zona %s — página cheia com datetime ilegível no último "
                "evento; pulando até %s, podem ter ficado eventos para trás (integration=%s)",
                zone, _iso(end), self.ctx.integration_id,
            )
            return end
        candidate = last.replace(microsecond=0)
        if candidate > since:
            return min(candidate, end)
        step = min(since + timedelta(seconds=1), end)
        logger.warning(
            "cloudflare security_events: zona %s — mais de %d eventos em %s; o excedente desse "
            "segundo não é alcançável pela API e foi perdido (perda parcial; integration=%s)",
            zone, plan.page_size, _iso(since), self.ctx.integration_id,
        )
        return step

    # ── backfill ─────────────────────────────────────────────────────────

    async def _backfill(
        self, cursor: Dict[str, Any], configured: List[str]
    ) -> AsyncIterator[Dict[str, Any]]:
        """Backfill por fatias de ``maxDuration``, sem mexer no cursor do polling.

        Fatia que enche a página é dividida ao meio; no piso de 1 s o corte é
        registrado como perda parcial. As fatias são ``[lo, hi)`` disjuntas: não há
        reemissão entre elas.
        """
        zones = configured or await self._discover()
        start = _parse_epoch(cursor.get("backfill_from_ts"))
        stop = _parse_epoch(cursor.get("backfill_to_ts"))
        now = _utcnow()
        for zone in zones:
            plan = await self._plan(zone)
            if isinstance(plan, str):
                logger.warning(
                    "cloudflare security_events: backfill — zona %s pulada: %s (integration=%s)",
                    zone, plan, self.ctx.integration_id,
                )
                continue
            floor = (now - timedelta(seconds=plan.not_older_than) + _RETENTION_MARGIN)
            ceiling = now - _INGESTION_LAG
            lo = _from_epoch(start) if start is not None else floor
            hi = _from_epoch(stop) if stop is not None else ceiling
            if lo < floor:
                logger.warning(
                    "cloudflare security_events: backfill da zona %s pedido desde %s mas a API guarda "
                    "só até %s — %s de eventos não podem mais ser lidos (integration=%s)",
                    zone, _iso(lo), _iso(floor), floor - lo, self.ctx.integration_id,
                )
                lo = floor
            hi = min(hi, ceiling)
            pending: List[Tuple[datetime, datetime]] = []
            cursor_t = lo
            while cursor_t < hi:
                nxt = min(hi, cursor_t + timedelta(seconds=plan.max_duration))
                pending.append((cursor_t, nxt))
                cursor_t = nxt
            while pending:
                a, b = pending.pop(0)
                rows = await self._fetch(zone, plan, a, b)
                span = int((b - a).total_seconds())
                if len(rows) >= plan.page_size and span > _BACKFILL_MIN_SLICE_S:
                    mid = a + timedelta(seconds=span // 2)
                    pending[:0] = [(a, mid), (mid, b)]
                    continue
                if len(rows) >= plan.page_size:
                    logger.warning(
                        "cloudflare security_events: backfill — mais de %d eventos em %d s na zona %s; "
                        "a fatia veio incompleta (perda parcial; integration=%s)",
                        plan.page_size, span, zone, self.ctx.integration_id,
                    )
                for row in rows:
                    event = dict(row)
                    event["zoneTag"] = zone
                    yield event

    # ── identidade / watermark ───────────────────────────────────────────

    def extract_message_id(self, event: Dict[str, Any]) -> str:
        """``rayName`` legível + hash de zona/``datetime``/``source``/``ruleId``/``action``/``matchIndex``.

        Uma requisição (``rayName``) gera VÁRIOS eventos — um por produto/regra que a
        acionou — então o ray sozinho não identifica o evento. O hash mantém a chave
        curta (vai para o dedupe e para a memória de fronteira do cursor).
        """
        digest = hashlib.sha1(
            "|".join(
                str(event.get(k) if event.get(k) is not None else "")
                for k in ("zoneTag", "rayName", "datetime", "source", "ruleId", "action", "matchIndex")
            ).encode("utf-8")
        ).hexdigest()[:20]
        return f"{event.get('rayName') or ''}:{digest}"

    @classmethod
    def watermark_at(cls, cursor: Optional[Dict[str, Any]]) -> Optional[datetime]:
        """O ``since`` MAIS ANTIGO entre as zonas (conservador: a zona atrasada manda)."""
        zones = (cursor or {}).get("zones")
        if not isinstance(zones, dict):
            return None
        instants = [
            moment
            for zstate in zones.values()
            if isinstance(zstate, dict)
            for moment in [cls.watermark_from_iso(zstate, "since")]
            if moment is not None
        ]
        return min(instants) if instants else None


# ── Refresher + teste de conexão ─────────────────────────────────────────


async def _cloudflare_refresher(integration_id: int) -> Dict[str, object]:
    """No-op p/ o framework: o API token é estático, não há o que renovar."""
    return {"access_token": "", "expires_in": 3600}


async def _probe_zone(session: aiohttp.ClientSession, token: str, zone: str) -> Tuple[bool, str]:
    try:
        body = await _call(
            session, "POST", _GRAPHQL_URL, token, timeout=_PROBE_TIMEOUT,
            json_body=_graphql_body(_SETTINGS_QUERY, {"zoneTag": zone}),
        )
    except CloudflareRateLimitedError as exc:
        return False, f"limite de consultas da Cloudflare atingido (tente em {exc.retry_after}s)"
    except (CloudflareApiError, CloudflareGraphQLError) as exc:
        return False, f"{getattr(exc, 'message', None) or exc}"
    data = body.get("data") or {}
    if _zone_node(data) is None:
        return False, "o token não tem acesso a esta zona (ou o ID não existe)"
    plan = plan_zone(settings_node(data))
    if isinstance(plan, str):
        return False, plan
    return True, (
        f"ok ({len(plan.fields)} de {plan.available} campos, página de {plan.page_size}, "
        f"janela de {plan.max_duration // 3600}h, retenção de {plan.not_older_than // 86400} dias)"
    )


async def _cloudflare_probe(cfg: Dict[str, Any]) -> Any:
    """Teste STATELESS pré-save: token, zonas e o dataset de cada uma.

    Passa só se TODA zona for utilizável (dataset ligado e campos obrigatórios
    disponíveis): a coleta falha se nenhuma servir e fica incompleta se alguma não.
    O token nunca é ecoado.
    """
    from ..output.base import TestResult

    token = (cfg.get("client_secret") or "").strip()
    if not token:
        return TestResult.failed("informe o API token da Cloudflare")
    try:
        zones = parse_zone_ids(cfg.get("tenant_id") or "")
    except CloudflareCredentialError as exc:
        return TestResult.failed(str(exc))

    t0 = time.perf_counter()
    notes: List[str] = []
    try:
        async with aiohttp.ClientSession(timeout=_PROBE_TIMEOUT) as session:
            try:
                body = await _call(
                    session, "GET", _VERIFY_URL, token, timeout=_PROBE_TIMEOUT,
                    forbidden_hint="o token não pôde ser verificado",
                )
            except CloudflareApiError as exc:
                if exc.status not in (401, 403):
                    return TestResult.failed(f"Falha ao verificar o token: {exc.message}")
                # Token de CONTA não é verificável em /user/tokens/verify: os testes
                # por zona abaixo são a prova real (um token inválido falha neles).
                notes.append("token não verificável em /user/tokens/verify (token de conta?)")
            except CloudflareRateLimitedError as exc:
                return TestResult.failed(f"limite de requisições da Cloudflare atingido (tente em {exc.retry_after}s)")
            except CloudflareGraphQLError as exc:
                return TestResult.failed(f"Falha ao verificar o token: {exc}")
            else:
                status = str((body.get("result") or {}).get("status") or "desconhecido")
                if status != "active":
                    return TestResult.failed(f"token {status} — gere um token ativo na Cloudflare")

            if not zones:
                try:
                    zones = await _discover_zones(session, token, timeout=_PROBE_TIMEOUT)
                except CloudflareRateLimitedError as exc:
                    return TestResult.failed(f"limite de requisições da Cloudflare atingido (tente em {exc.retry_after}s)")
                except (CloudflareApiError, CloudflareGraphQLError) as exc:
                    return TestResult.failed(f"Falha ao descobrir as zonas: {getattr(exc, 'message', None) or exc}")
                if not zones:
                    return TestResult.failed("o token não enxerga nenhuma zona ativa")
                notes.append(f"{len(zones)} zona(s) descoberta(s)")

            good = 0
            states: List[str] = []
            for zone in zones:
                ok, state = await _probe_zone(session, token, zone)
                good += int(ok)
                states.append(f"zona {zone}: {state}")
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        return TestResult.failed(f"Falha ao falar com a Cloudflare ({_API_HOST}): {type(exc).__name__}")

    detail = "; ".join([*notes, *states])
    if good != len(zones):
        return TestResult.failed(f"Nem toda zona está utilizável — {detail}")
    return TestResult.passed(f"Conexão OK — {detail}", latency_ms=(time.perf_counter() - t0) * 1000.0)


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
            platform="cloudflare",
            display_name="Cloudflare (WAF e segurança)",
            category="Rede / Firewall",
            description=(
                "Cloudflare — eventos de segurança das zonas (WAF, regras de firewall, rate "
                "limiting, bots, DDoS L7) pela GraphQL Analytics API: ação aplicada, regra, IP "
                "real do cliente, geo/ASN e a requisição HTTP."
            ),
            icon_id="cloudflare",
            docs_url="https://developers.cloudflare.com/analytics/graphql-api/",
            order=43,
            test_fn=_cloudflare_probe,
            required_secrets=("client_secret",),
            capabilities=frozenset({"catalog", "auth:test", "collect:security_events"}),
            auth_fields=(
                AuthField(key="client_secret", label="API token da Cloudflare",
                          type="secret", required=True,
                          help_text=(
                              "Token de API com Zone > Analytics > Read nas zonas coletadas "
                              "(mais Zone > Zone > Read se não informar os Zone IDs)."
                          )),
                AuthField(key="tenant_id", label="Zone IDs (opcional)",
                          type="string", required=False,
                          help_text=(
                              "Até 50 Zone IDs (32 caracteres hexadecimais), separados por "
                              "vírgula. Vazio = todas as zonas que o token enxerga, descobertas "
                              "a cada ciclo (exige Zone > Zone > Read)."
                          )),
            ),
        )
    )

    register(
        CollectorRegistration(
            platform=CloudflareSecurityEventsCollector.platform,
            stream=CloudflareSecurityEventsCollector.stream,
            collector_cls=CloudflareSecurityEventsCollector,
            refresh_fn=_cloudflare_refresher,
            schedule=_td(minutes=1),
            queue=Q_BULK,
            task_name=T_COLLECT_BULK,
        )
    )


_register()
