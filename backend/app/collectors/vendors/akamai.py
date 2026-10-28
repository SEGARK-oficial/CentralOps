"""Akamai App & API Protector (WAF) — eventos de segurança pela SIEM Integration API.

Vendor novo = 1 módulo, ZERO core. Um stream:

- ``siem`` — ``GET /siem/v1/configs/{configId}`` devolve os eventos de segurança
  das configurações de segurança pedidas: cada requisição que disparou regra do
  WAF (ataque, rate control, reputação, bot, Account Protector), com a ação
  aplicada (``deny``/``alert``/``monitor``/…), as regras, o IP do cliente, a geo
  e o recorte da mensagem HTTP. Várias configurações saem numa chamada só
  (``/configs/111;222``), com UM offset.

**Modo offset, não modo tempo.** A API tem dois modos: por tempo (``from``/``to``)
e por ``offset`` opaco. O modo tempo faz uma chamada só e corta em ``limit`` sem
avisar, e pode perder os 5 s de latência da coleta da Akamai; o modo offset
retoma exatamente de onde parou e recolhe esses 5 s na chamada seguinte. Então:
cold start por tempo (``from = agora - 1h``), dali em diante sempre por offset.
A última linha da resposta (NDJSON) é o contexto ``{total, offset, limit?}``:
``limit`` presente = a página bateu no teto e há mais dado.

**O offset expira (HTTP 416) e a Akamai guarda só 12 h.** Offset recusado volta
para o modo tempo a partir do evento mais novo já lido (``last_ts_ms``), preso à
janela de 12 h — o que passou dela não existe mais na API e o log diz quanto se
perdeu. A troca acontece uma vez por ciclo: o 416 nunca vira laço.

**Credencial EdgeGrid, assinada aqui.** O cliente cola o bloco do ``.edgerc``
(``host``, ``client_token``, ``client_secret``, ``access_token``) — ou o mesmo
em JSON — no ÚNICO campo secreto editável (``client_secret``). A assinatura
``EG1-HMAC-SHA256`` é feita neste módulo (hmac/hashlib), sem dependência nova,
seguindo o ``edgegrid.py`` oficial da Akamai. ``tenant_id`` = IDs das
configurações de segurança.

**Superfície de rede fechada.** O pedido leva uma assinatura válida para a
credencial do cliente: o host só é aceito sob ``akamaiapis.net``, só HTTPS,
sem seguir redirect. Erro da API (``application/problem+json``) sobe só com
``title``/``type`` — nunca com os headers do pedido.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote

import aiohttp
from yarl import URL

from ..base import BaseCollector
from ..metrics import API_LATENCY
from ._cursor_selfheal import log_stale_cursor
from ._rate_limit import VendorRateLimitedError

logger = logging.getLogger(__name__)

_API_PATH = "/siem/v1/configs/"

#: Eventos por página. A Akamai aceita até 600000, mas a borda corta a resposta
#: depois de 2 min e a página inteira fica em memória até ser validada (a linha
#: de contexto é a ÚLTIMA): 5000 eventos de ~3 KB é uma página que cabe com folga.
_PAGE_LIMIT = 5000
#: Piso da redução de ``limit`` quando a resposta chega cortada (sem a linha de
#: contexto) — é a orientação da Akamai para a resposta incompleta.
_MIN_PAGE_LIMIT = 500
_MAX_TRUNCATED_RETRIES = 3
#: Teto de páginas por CICLO (agendado a cada minuto). Sem ele um backlog (cold
#: start de 1 h num site atacado, worker parado) seria drenado num run só e
#: estouraria o ``task_soft_time_limit``; o pipeline reverteria o cursor e o
#: ciclo seguinte recomeçaria do mesmo offset — poison-loop. No teto o cursor já
#: guarda o offset da próxima página.
_MAX_PAGES_PER_CYCLE = 10

_COLD_START_LOOKBACK = timedelta(hours=1)
#: A Akamai guarda os eventos por 12 h. A margem evita pedir um ``from`` que
#: vença entre o cálculo e a chegada do pedido.
_RETENTION = timedelta(hours=12)
_RETENTION_MARGIN = timedelta(minutes=5)
#: ``to`` não pode passar de 5 s antes de agora (latência da coleta da Akamai).
_API_LATENCY_S = 5
#: Backfill por tempo: fatias e o piso da bisseção quando a fatia bate no limit.
_BACKFILL_SLICE = timedelta(minutes=15)
_BACKFILL_MIN_SLICE_S = 60

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=180)
_PROBE_TIMEOUT = aiohttp.ClientTimeout(total=30)

#: Teto do decode do ``attackData``: um payload hostil (ou corrompido) com
#: milhares de ``;`` não pode virar milhares de decodes por evento.
_MAX_RULES_PER_EVENT = 64
_MAX_RULE_FIELD_CHARS = 64 * 1024
_MAX_CONFIG_IDS = 50

_RULE_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("rules", "id"),
    ("ruleVersions", "version"),
    ("ruleMessages", "message"),
    ("ruleTags", "tag"),
    ("ruleData", "data"),
    ("ruleSelectors", "selector"),
    ("ruleActions", "action"),
)

#: Host da credencial: sempre sob ``akamaiapis.net`` (``akab-….luna.akamaiapis.net``,
#: ``….cloudsecurity.akamaiapis.net``). Labels DNS válidos, sem porta, sem path.
_HOST_RE = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.){1,3}akamaiapis\.net$"
)
_CONFIG_ID_RE = re.compile(r"^\d{1,12}$")
_CRED_KEYS = ("host", "client_token", "client_secret", "access_token")
_PROBLEM_TEXT_MAX = 120


class AkamaiRateLimitedError(VendorRateLimitedError):
    def __init__(self, retry_after: int) -> None:
        super().__init__(retry_after, vendor="akamai")


class AkamaiCredentialError(ValueError):
    """Credencial EdgeGrid ou IDs de configuração inválidos (mensagem sem segredo)."""


class AkamaiApiError(aiohttp.ClientResponseError):
    """A SIEM API devolveu erro.

    É um ``ClientResponseError`` com o status da Akamai (401/403 fazem o pipeline
    marcar erro de autenticação), mas montado SÓ a partir da resposta: o
    ``request_info`` de um ``raise_for_status`` carregaria o header
    ``Authorization`` do pedido para o log de exceção. A mensagem é o ``title``
    do problem+json, nunca o corpo inteiro.
    """

    def __init__(self, resp: aiohttp.ClientResponse, title: str) -> None:
        info = aiohttp.RequestInfo(resp.url, resp.method, resp.headers, resp.url)
        super().__init__(
            info, getattr(resp, "history", ()), status=resp.status, message=title,
            headers=resp.headers,
        )
        self.title = title


# ── Credencial ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EdgeGridCredential:
    host: str
    client_token: str
    client_secret: str
    access_token: str

    def __repr__(self) -> str:  # nunca imprime os tokens em log/traceback
        return f"EdgeGridCredential(host={self.host!r})"


def validated_host(raw: str) -> str:
    """``host`` do ``.edgerc`` → hostname, ou ``AkamaiCredentialError``.

    Aceita o que o Control Center entrega (``akab-….luna.akamaiapis.net/``) e a
    forma com ``https://``. Recusa HTTP, porta, path, userinfo e qualquer host
    fora de ``akamaiapis.net``: o worker assina e envia o pedido para lá.
    """
    text = (raw or "").strip().strip('"').strip("'").lower()
    if text.startswith("http://"):
        raise AkamaiCredentialError("host da credencial deve usar HTTPS")
    if text.startswith("https://"):
        text = text[len("https://"):]
    text = text.rstrip("/")
    if not _HOST_RE.match(text):
        raise AkamaiCredentialError(
            "host da credencial inválido — use o host do API client "
            "(ex.: akab-xxxx.luna.akamaiapis.net)"
        )
    return text


def parse_edgegrid_credential(text: str) -> EdgeGridCredential:
    """Bloco do ``.edgerc`` ou JSON com as mesmas chaves → credencial validada.

    Do ``.edgerc`` lê a PRIMEIRA seção (o header ``[default]`` é opcional), aceita
    ``client-token`` com hífen (como o SDK oficial) e ignora comentários e chaves
    que não usa (``max_body``, ``headers_to_sign``). A mensagem de erro cita só o
    NOME das chaves que faltam, nunca valores.
    """
    raw = (text or "").strip()
    if not raw:
        raise AkamaiCredentialError("credencial EdgeGrid vazia")
    values: Dict[str, str] = {}
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except ValueError:
            raise AkamaiCredentialError("credencial EdgeGrid: JSON inválido") from None
        if not isinstance(data, dict):
            raise AkamaiCredentialError("credencial EdgeGrid: JSON deve ser um objeto")
        for key, value in data.items():
            values[str(key).strip().lower().replace("-", "_")] = str(value or "").strip()
    else:
        seen_section = False
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith(("#", ";")):
                continue
            if line.startswith("[") and line.endswith("]"):
                if seen_section or values:
                    break  # só a primeira seção
                seen_section = True
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip().lower().replace("-", "_")] = value.strip().strip('"').strip("'")
    missing = [k for k in _CRED_KEYS if not values.get(k)]
    if missing:
        raise AkamaiCredentialError(
            "credencial EdgeGrid incompleta — faltam: " + ", ".join(missing)
        )
    return EdgeGridCredential(
        host=validated_host(values["host"]),
        client_token=values["client_token"],
        client_secret=values["client_secret"],
        access_token=values["access_token"],
    )


def parse_config_ids(raw: str) -> List[str]:
    """``"111, 222;333"`` → ``["111", "222", "333"]`` (ordem preservada, sem repetição)."""
    parts = [p for p in re.split(r"[,;\s]+", (raw or "").strip()) if p]
    if not parts:
        raise AkamaiCredentialError("informe ao menos um ID de configuração de segurança")
    bad = [p for p in parts if not _CONFIG_ID_RE.match(p)]
    if bad:
        raise AkamaiCredentialError(
            "ID de configuração de segurança inválido (use só dígitos): " + ", ".join(bad[:5])
        )
    ids = list(dict.fromkeys(parts))
    if len(ids) > _MAX_CONFIG_IDS:
        raise AkamaiCredentialError(f"no máximo {_MAX_CONFIG_IDS} configurações por integração")
    return ids


def _load_settings(integration_id: int) -> Dict[str, str]:
    """Credencial e IDs de configuração da integração (sync, rodar em thread)."""
    from ...db import database, models
    from ...services import integration_secrets

    with database.SessionLocal() as db:
        integ = db.get(models.Integration, integration_id)
        if integ is None:
            raise RuntimeError(f"akamai: integração {integration_id} não encontrada")
        return {
            "credential": integration_secrets.read_secret(integ, "client_secret") or "",
            "config_ids": integ.tenant_id or "",
        }


# ── EdgeGrid ─────────────────────────────────────────────────────────────


def _b64_hmac_sha256(data: str, key: str) -> str:
    digest = hmac.new(key.encode("utf-8"), data.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def eg_timestamp(now: Optional[float] = None) -> str:
    return time.strftime("%Y%m%dT%H:%M:%S+0000", time.gmtime(now))


def edgegrid_authorization(
    cred: EdgeGridCredential,
    method: str,
    path_and_query: str,
    *,
    timestamp: Optional[str] = None,
    nonce: Optional[str] = None,
) -> str:
    """Header ``Authorization`` EG1-HMAC-SHA256 de um pedido SEM corpo.

    Mesmo algoritmo do ``edgegrid.py`` oficial (github.com/akamai/
    AkamaiOPEN-edgegrid-python: ``make_auth_header``/``make_data_to_sign``/
    ``make_signing_key``): chave = HMAC(timestamp, client_secret); assinatura =
    HMAC(dados, chave), com ``dados`` = método, esquema, host, path+query,
    headers canônicos (nenhum) e hash do corpo (vazio num GET), separados por
    TAB, seguidos do próprio header sem assinatura.
    """
    ts = timestamp or eg_timestamp()
    nonce = nonce or str(uuid.uuid4())
    auth = (
        f"EG1-HMAC-SHA256 client_token={cred.client_token};"
        f"access_token={cred.access_token};timestamp={ts};nonce={nonce};"
    )
    data_to_sign = "\t".join([method.upper(), "https", cred.host, path_and_query, "", "", auth])
    signing_key = _b64_hmac_sha256(ts, cred.client_secret)
    return auth + "signature=" + _b64_hmac_sha256(data_to_sign, signing_key)


def _path_and_query(config_ids: List[str], params: List[Tuple[str, Any]]) -> str:
    """Path + query EXATAMENTE como vão no fio — é a string assinada.

    ``;`` separa as configurações no path. O query é montado aqui (e o pedido
    sai com ``encoded=True``) para o aiohttp não reescrever nada depois de
    assinado.
    """
    query = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params)
    return f"{_API_PATH}{';'.join(config_ids)}?{query}"


# ── attackData ───────────────────────────────────────────────────────────


def decode_rule_field(raw: Any) -> List[str]:
    """Campo de regras do ``attackData`` → lista de textos, por posição.

    Formato documentado: URL-encoded; decodificado, são pedaços base64 separados
    por ``;``. ``unquote`` e não ``unquote_plus``: ``+`` é caractere de base64.
    Pedaço vazio ou inválido vira ``""`` e mantém a posição (o N-ésimo item de
    cada campo é da mesma regra).
    """
    if not isinstance(raw, str) or not raw:
        return []
    text = unquote(raw[:_MAX_RULE_FIELD_CHARS])
    out: List[str] = []
    for chunk in text.split(";")[:_MAX_RULES_PER_EVENT]:
        chunk = chunk.strip()
        if not chunk:
            out.append("")
            continue
        try:
            padded = chunk + "=" * (-len(chunk) % 4)
            out.append(base64.b64decode(padded, validate=True).decode("utf-8", "replace"))
        except (binascii.Error, ValueError):
            out.append("")
    return out


def decode_attack_data(event: Dict[str, Any]) -> Dict[str, Any]:
    """Acrescenta ``attackData.decoded_rules`` (e o User-Agent decodificado).

    Os campos originais ficam como vieram; o mapping lê só o decodificado.
    """
    attack = event.get("attackData")
    if isinstance(attack, dict):
        columns = {name: decode_rule_field(attack.get(field)) for field, name in _RULE_FIELDS}
        count = min(max((len(v) for v in columns.values()), default=0), _MAX_RULES_PER_EVENT)
        attack["decoded_rules"] = [
            {name: (values[i] if i < len(values) else "") for name, values in columns.items()}
            for i in range(count)
        ]
    http = event.get("httpMessage")
    if isinstance(http, dict):
        ua = _user_agent(http.get("requestHeaders"))
        if ua:
            http["decoded_user_agent"] = ua
    return event


def _user_agent(raw: Any) -> Optional[str]:
    if not isinstance(raw, str) or not raw:
        return None
    for line in unquote(raw[:_MAX_RULE_FIELD_CHARS]).split("\r\n"):
        name, sep, value = line.partition(":")
        if sep and name.strip().lower() == "user-agent":
            return value.strip()[:1024] or None
    return None


def _event_start(event: Dict[str, Any]) -> Optional[int]:
    """``httpMessage.start`` (epoch em segundos, string ou número) → int."""
    raw = (event.get("httpMessage") or {}).get("start")
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


# ── Página NDJSON ────────────────────────────────────────────────────────


class _Page:
    __slots__ = ("events", "offset", "has_more", "complete", "bad_lines")

    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []
        self.offset: Optional[str] = None
        self.has_more = False
        self.complete = False
        self.bad_lines = 0


def parse_page(body: str) -> _Page:
    """Corpo NDJSON → eventos + contexto da última linha.

    Sem a linha de contexto a resposta foi cortada (a borda encerra depois de
    2 min): ``complete`` fica ``False`` e o chamador NÃO usa nada desta página.
    """
    page = _Page()
    lines = [ln for ln in body.splitlines() if ln.strip()]
    for index, line in enumerate(lines):
        try:
            obj = json.loads(line)
        except ValueError:
            page.bad_lines += 1
            continue
        if not isinstance(obj, dict):
            page.bad_lines += 1
            continue
        is_last = index == len(lines) - 1
        if is_last and "offset" in obj and "httpMessage" not in obj and "attackData" not in obj:
            page.offset = str(obj.get("offset") or "") or None
            page.has_more = "limit" in obj
            page.complete = True
            continue
        page.events.append(obj)
    return page


async def _problem_title(resp: aiohttp.ClientResponse) -> str:
    """Só ``title`` (ou ``type``) do problem+json — o resto do corpo não sobe."""
    try:
        body = await resp.json(content_type=None)
    except Exception:  # noqa: BLE001 — corpo ilegível não muda o erro
        return f"HTTP {resp.status}"
    if isinstance(body, dict):
        title = body.get("title") or body.get("type")
        if title:
            return str(title)[:_PROBLEM_TEXT_MAX]
    return f"HTTP {resp.status}"


def _parse_retry_after(value: Optional[str]) -> int:
    try:
        return max(1, int(value or ""))
    except (TypeError, ValueError):
        return 60


# ── Coletor ──────────────────────────────────────────────────────────────


class AkamaiSiemCollector(BaseCollector):
    """Eventos de segurança do WAF da Akamai (SIEM Integration API, modo offset)."""

    platform = "akamai"
    stream = "siem"
    event_type = "akamai.siem"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._cred: Optional[EdgeGridCredential] = None
        self._config_ids: List[str] = []

    @property
    def domain(self) -> str:
        return self._cred.host if self._cred else "akamaiapis.net"

    async def collect(self) -> AsyncIterator[Dict[str, Any]]:
        cfg = await asyncio.to_thread(_load_settings, self.ctx.integration_id)
        self._cred = parse_edgegrid_credential(cfg["credential"])
        self._config_ids = parse_config_ids(cfg["config_ids"])

        cursor = self.ctx.cursor or {}
        now = time.time()
        if "backfill_from_ts" in cursor:
            async for event in self._backfill(cursor, now):
                yield event
            return

        offset: Optional[str] = cursor.get("offset") or None
        last_ms = cursor.get("last_ts_ms")
        last_ms = int(last_ms) if isinstance(last_ms, (int, float)) and not isinstance(last_ms, bool) else None
        from_s: Optional[int] = None if offset else int(now - _COLD_START_LOOKBACK.total_seconds())
        fell_back = False
        limit = _PAGE_LIMIT
        truncated = 0
        pages = 0

        while True:
            params: List[Tuple[str, Any]] = (
                [("offset", offset), ("limit", limit)] if offset else [("from", from_s), ("limit", limit)]
            )
            page = await self._fetch(params, offset_mode=bool(offset))
            if page is None:
                # Offset expirado (416). Uma vez por ciclo: volta ao modo tempo a
                # partir do último evento lido, dentro das 12 h que a Akamai guarda.
                if fell_back:
                    raise RuntimeError("akamai siem: offset recusado duas vezes no mesmo ciclo")
                fell_back = True
                from_s = self._resume_from(last_ms, now)
                log_stale_cursor(
                    "akamai siem",
                    status=416,
                    integration_id=self.ctx.integration_id,
                    token_field="offset",
                    resume_from=datetime.fromtimestamp(from_s, tz=timezone.utc).isoformat(),
                )
                offset = None
                self.ctx.cursor = {"last_ts_ms": last_ms} if last_ms else {}
                continue
            if not page.complete:
                truncated += 1
                if truncated > _MAX_TRUNCATED_RETRIES:
                    raise RuntimeError(
                        "akamai siem: resposta cortada pela borda repetidamente (sem linha de contexto)"
                    )
                limit = max(_MIN_PAGE_LIMIT, limit // 2)
                logger.warning(
                    "akamai siem: resposta incompleta — repetindo com limit=%d (integration=%s)",
                    limit, self.ctx.integration_id,
                )
                continue
            if page.bad_lines:
                logger.warning(
                    "akamai siem: %d linha(s) ilegível(is) descartada(s) (integration=%s)",
                    page.bad_lines, self.ctx.integration_id,
                )

            for event in page.events:
                decode_attack_data(event)
                start = _event_start(event)
                if start is not None and (last_ms is None or start * 1000 > last_ms):
                    last_ms = start * 1000
                yield event

            if page.offset:
                offset = page.offset
            state: Dict[str, Any] = {}
            if offset:
                state["offset"] = offset
            if last_ms:
                state["last_ts_ms"] = last_ms
            self.ctx.cursor = state
            if not page.has_more or not offset:
                break

            # Teto por ciclo: o cursor acima já aponta para a PRÓXIMA página.
            pages += 1
            if self.ctx.bounded_per_cycle and pages >= _MAX_PAGES_PER_CYCLE:
                self.mark_cycle_capped()
                logger.info(
                    "akamai siem: teto de %d páginas/ciclo — offset salvo p/ o próximo "
                    "ciclo (integration=%s)", _MAX_PAGES_PER_CYCLE, self.ctx.integration_id,
                )
                break

    def _resume_from(self, last_ms: Optional[int], now: float) -> int:
        """Início do modo tempo depois de um offset expirado, preso às 12 h."""
        floor = int(now - (_RETENTION - _RETENTION_MARGIN).total_seconds())
        if last_ms is None:
            logger.warning(
                "akamai siem: offset expirado e nenhum evento anterior no cursor — "
                "retomando do limite de retenção (integration=%s)", self.ctx.integration_id,
            )
            return floor
        wanted = last_ms // 1000
        if wanted < floor:
            logger.warning(
                "akamai siem: offset expirado e o último evento lido é de %s — a Akamai "
                "guarda só 12 h; %s de eventos não podem mais ser lidos (integration=%s)",
                datetime.fromtimestamp(wanted, tz=timezone.utc).isoformat(),
                timedelta(seconds=floor - wanted), self.ctx.integration_id,
            )
            return floor
        return wanted

    async def _backfill(self, cursor: Dict[str, Any], now: float) -> AsyncIterator[Dict[str, Any]]:
        """Backfill por tempo, em fatias, sem mexer no cursor do polling.

        Fatia que bate no ``limit`` é dividida ao meio (o modo tempo corta em
        silêncio); no piso de 1 min o corte é registrado como perda parcial.
        """
        start = _parse_epoch(cursor.get("backfill_from_ts"))
        end = _parse_epoch(cursor.get("backfill_to_ts")) or int(now)
        floor = int(now - (_RETENTION - _RETENTION_MARGIN).total_seconds())
        ceiling = int(now) - _API_LATENCY_S
        start = max(start or floor, floor)
        end = min(end, ceiling)
        pending: List[Tuple[int, int]] = []
        t = start
        while t < end:
            nxt = min(end, t + int(_BACKFILL_SLICE.total_seconds()))
            pending.append((t, nxt))
            t = nxt
        while pending:
            lo, hi = pending.pop(0)
            page = await self._fetch(
                [("from", lo), ("to", hi), ("limit", _PAGE_LIMIT)], offset_mode=False
            )
            if page is None or not page.complete:
                raise RuntimeError("akamai siem: backfill recebeu resposta incompleta")
            if page.has_more and hi - lo > _BACKFILL_MIN_SLICE_S:
                mid = lo + (hi - lo) // 2
                pending[:0] = [(lo, mid), (mid, hi)]
                continue
            if page.has_more:
                logger.warning(
                    "akamai siem: backfill — mais de %d eventos em %d s; a fatia veio "
                    "incompleta (integration=%s)", _PAGE_LIMIT, hi - lo, self.ctx.integration_id,
                )
            for event in page.events:
                yield decode_attack_data(event)

    async def _fetch(self, params: List[Tuple[str, Any]], *, offset_mode: bool) -> Optional[_Page]:
        """Uma página. ``None`` = offset expirado (416 em modo offset)."""
        assert self._cred is not None
        path_query = _path_and_query(self._config_ids, params)
        url = URL(f"https://{self._cred.host}{path_query}", encoded=True)
        headers = {
            "Authorization": edgegrid_authorization(self._cred, "GET", path_query),
            "Accept": "application/json",
        }
        await self.ctx.rate_limiter.acquire(self.ctx.integration_id, self.platform)
        started = time.monotonic()
        try:
            async with self.ctx.domain_limiter.slot(self.domain):
                async with self.ctx.session.get(
                    url, headers=headers, allow_redirects=False, timeout=_REQUEST_TIMEOUT
                ) as resp:
                    if resp.status == 416 and offset_mode:
                        return None
                    if resp.status == 429:
                        raise AkamaiRateLimitedError(_parse_retry_after(resp.headers.get("Retry-After")))
                    if resp.status != 200:
                        title = await _problem_title(resp)
                        logger.warning("akamai siem: HTTP %s — %s", resp.status, title)
                        raise AkamaiApiError(resp, title)
                    body = await resp.text()
        finally:
            API_LATENCY.labels(vendor=self.platform, stream=self.stream).observe(
                time.monotonic() - started
            )
        return parse_page(body)

    def extract_message_id(self, event: Dict[str, Any]) -> str:
        """Configuração + política + ``requestId`` + início + hash das regras.

        O ``requestId`` sozinho não basta: a mesma requisição aparece uma vez por
        configuração/política que a avaliou, e é o conjunto de regras que
        distingue dois eventos dela. O hash mantém a chave curta (vai para o
        dedupe e para a memória do cursor).
        """
        attack = event.get("attackData") or {}
        http = event.get("httpMessage") or {}
        rules = str(attack.get("rules") or "")
        digest = hashlib.sha1(rules.encode("utf-8")).hexdigest()[:12] if rules else ""
        return "|".join(
            str(part or "")
            for part in (
                attack.get("configId"),
                attack.get("policyId"),
                http.get("requestId"),
                http.get("start"),
                digest,
            )
        )

    @classmethod
    def watermark_at(cls, cursor: Optional[Dict[str, Any]]) -> Optional[datetime]:
        """``last_ts_ms``: início do evento mais novo já lido (o offset é opaco)."""
        return cls.watermark_from_epoch_ms(cursor, "last_ts_ms")


def _parse_epoch(value: Any) -> Optional[int]:
    """Instante do backfill (ISO-8601 ou epoch em segundos) → epoch em segundos."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.isdigit():
            return int(text)
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    return None


# ── Refresher + teste de conexão ─────────────────────────────────────────


async def _akamai_refresher(integration_id: int) -> Dict[str, object]:
    """No-op p/ o framework: EdgeGrid assina CADA pedido, não há token a renovar."""
    return {"access_token": "", "expires_in": 3600}


_HINT_403 = (
    "o API client precisa do serviço SIEM API em READ-WRITE e a configuração de "
    "segurança precisa de 'Data collection for SIEM Integrations' ligado"
)


async def _probe_config(
    session: aiohttp.ClientSession, cred: EdgeGridCredential, config_id: str
) -> Tuple[bool, str]:
    path_query = _path_and_query(
        [config_id], [("from", int(time.time()) - 60), ("limit", 1)]
    )
    url = URL(f"https://{cred.host}{path_query}", encoded=True)
    headers = {
        "Authorization": edgegrid_authorization(cred, "GET", path_query),
        "Accept": "application/json",
    }
    async with session.get(url, headers=headers, allow_redirects=False) as resp:
        if resp.status == 200:
            return True, "ok"
        title = await _problem_title(resp)
        if resp.status == 401:
            return False, f"credencial recusada ({title})"
        if resp.status == 403:
            return False, f"sem permissão ({title}) — {_HINT_403}"
        if resp.status == 404:
            return False, f"configuração não encontrada ({title})"
        if 300 <= resp.status < 400:
            return False, f"redirecionamento recusado (HTTP {resp.status})"
        return False, f"erro HTTP {resp.status} ({title})"


async def _akamai_probe(cfg: Dict[str, Any]):
    """Teste STATELESS pré-save: credencial, host e cada configuração de segurança.

    Uma chamada por configuração (``from = agora - 60 s``, ``limit=1``). Passa só
    se TODAS respondem: a coleta pede as configurações numa chamada só, e uma
    configuração sem acesso derruba a chamada inteira.
    """
    from ..output.base import TestResult

    try:
        cred = parse_edgegrid_credential(cfg.get("client_secret") or "")
        config_ids = parse_config_ids(cfg.get("tenant_id") or "")
    except AkamaiCredentialError as exc:
        return TestResult.failed(str(exc))

    t0 = time.perf_counter()
    states: List[str] = []
    ok = 0
    try:
        async with aiohttp.ClientSession(timeout=_PROBE_TIMEOUT) as session:
            for config_id in config_ids:
                good, state = await _probe_config(session, cred, config_id)
                ok += int(good)
                states.append(f"config {config_id}: {state}")
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        return TestResult.failed(f"Falha ao falar com a Akamai ({cred.host}): {type(exc).__name__}")
    detail = "; ".join(states)
    if ok != len(config_ids):
        return TestResult.failed(f"Nem toda configuração está acessível — {detail}")
    ms = (time.perf_counter() - t0) * 1000.0
    return TestResult.passed(f"Conexão OK — {detail}", latency_ms=ms)


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
            platform="akamai",
            display_name="Akamai App & API Protector (WAF)",
            category="Rede / Firewall",
            description=(
                "Akamai App & API Protector / Kona Site Defender — eventos de segurança do "
                "WAF pela SIEM Integration API: regras disparadas, ação aplicada (deny/alert), "
                "IP e geo do cliente, requisição HTTP, bots e Account Protector."
            ),
            icon_id="akamai",
            docs_url="https://techdocs.akamai.com/siem-integration/reference/api-get-started",
            order=42,
            test_fn=_akamai_probe,
            required_secrets=("client_secret",),
            capabilities=frozenset({"catalog", "auth:test", "collect:siem"}),
            auth_fields=(
                AuthField(key="tenant_id", label="IDs das configurações de segurança",
                          type="string", required=True,
                          help_text=(
                              "Um ou mais Security Configuration IDs (só dígitos), separados "
                              "por vírgula. Cada um precisa de 'Data collection for SIEM "
                              "Integrations' ligado e ativado em produção."
                          )),
                AuthField(key="client_secret", label="Credencial EdgeGrid (.edgerc)",
                          type="secret", required=True,
                          help_text=(
                              "Cole a seção do .edgerc do API client (host, client_token, "
                              "client_secret, access_token) — serviço SIEM API em READ-WRITE."
                          )),
            ),
        )
    )

    register(
        CollectorRegistration(
            platform=AkamaiSiemCollector.platform,
            stream=AkamaiSiemCollector.stream,
            collector_cls=AkamaiSiemCollector,
            refresh_fn=_akamai_refresher,
            schedule=_td(minutes=1),
            queue=Q_BULK,
            task_name=T_COLLECT_BULK,
        )
    )


_register()
