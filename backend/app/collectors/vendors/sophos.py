"""Sophos Central — coleta de alertas com paginação cursor-based.

Endpoint: ``GET /common/v1/alerts`` com parâmetros:
- ``from``        → timestamp ISO-8601 (delta time)
- ``pageSize``    → 200 (máximo suportado)
- ``pageFromKey`` → cursor opaco retornado em ``pages.nextKey``
- ``sort``        → ``createdAt:asc`` para cursor determinístico

Paginação encerra quando ``pages.nextKey`` é ausente/nulo.
Rate limit 429 é capturado e propagado ao ``RedisRateLimiter`` para
coordenar o backoff entre todos os workers.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict

from ..base import BaseCollector
from ..capabilities import (
    CAP_QUERY_XDR_DATA_LAKE,
    DIALECT_XDR_DATA_LAKE,
    QUERY_MODE_DATA_LAKE,
    QUERY_MODE_LIVE,
    QueryCapability,
)
from ..metrics import API_LATENCY
from ._sophos_common import MissingApiHostError, resolve_sophos_domain

logger = logging.getLogger(__name__)

# Contrato de query do Sophos — XDR Query (Data Lake) assíncrono, com
# teto de janela de 30 dias por query (limite do Data Lake). ``live`` cobre o Live
# Discover (osquery); o caminho async é o do Data Lake. Fonte
# ÚNICA: o provider (child tenant) lê isto de volta via registry; partner/org → None.
SOPHOS_QUERY_CAPABILITY = QueryCapability(
    dialect=DIALECT_XDR_DATA_LAKE,
    modes=(QUERY_MODE_LIVE, QUERY_MODE_DATA_LAKE),
    supports_async=True,
    max_window=timedelta(days=30),
    required_secrets=("access_token", "refresh_token"),
    ocsf_mapping_version="1",
)


from ._rate_limit import VendorRateLimitedError


class SophosRateLimitedError(VendorRateLimitedError):
    def __init__(self, retry_after: int) -> None:
        super().__init__(retry_after, vendor="sophos")


# Teto de páginas por CICLO Celery (25 × 200 = 5.000 alertas/ciclo). Sem este guard,
# um backlog grande é drenado num ÚNICO run — o while abaixo pagina ``pages.nextKey``
# após nextKey até exaurir o vendor — estourando o ``task_soft_time_limit`` (720s). No
# soft-timeout o pipeline reverte o cursor p/ cursor_before e solta TODAS as claims →
# loop sem progresso (a coleta trava). Ao atingir o teto, salvamos o cursor RESUMÍVEL
# (o ``pageFromKey`` da PRÓXIMA página, NÃO o watermark final) e retornamos gracioso;
# o próximo ciclo retoma exatamente de onde paramos. Espelha ``_MAX_PAGES_PER_CYCLE``
# do coletor de detections da Sophos (``sophos_detections.py``).
_MAX_PAGES_PER_CYCLE = 25


class SophosAlertsCollector(BaseCollector):
    platform = "sophos"
    stream = "alerts"
    event_type = "sophos.alert"

    @property
    def domain(self) -> str:
        # Preferimos ``X-Api-Host`` (populado a partir de
        # ``integration.api_host``, que vem direto da Sophos via Partner sync).
        # Fallback de ``X-Region`` é estrito: só aceita slug de datacenter
        # (``eu03``/``us02``/...). Geo-codes (``EU``/``US``) ou region vazio
        # disparam ``MissingApiHostError`` — fail loud em vez de NXDOMAIN
        # silencioso (ver ``_sophos_common.resolve_sophos_domain``).
        return resolve_sophos_domain(
            self.ctx.headers, integration_id=getattr(self.ctx, "integration_id", None)
        )

    async def collect(self) -> AsyncIterator[Dict[str, Any]]:
        cursor = self.ctx.cursor or {}
        # AUTO-CURA de cursor envenenado. Sophos só aceita ``from`` em UTC com
        # precisão de segundos; qualquer outra forma responde 400 em TODA
        # requisição seguinte, e o caminho de erro do pipeline regrava o cursor
        # anterior byte-a-byte (pipeline.py, ``cursor_before``) — o feed fica
        # travado até alguém zerar o coletor à mão. Aqui o valor inválido é
        # descartado em favor do lookback padrão: perde-se no máximo uma janela
        # (o dedupe absorve a re-leitura), em vez do feed inteiro.
        _fallback = _default_lookback_iso()
        from_ts: str = _safe_cursor_ts(
            cursor.get("from_ts") or _fallback, _fallback
        )
        page_key: str | None = cursor.get("pageFromKey")
        latest_ts = from_ts

        base_url = f"https://{self.domain}/common/v1/alerts"

        # Headers Sophos exigem ``X-Tenant-ID`` além do Bearer.
        # O pipeline já popula no ``ctx.headers``.
        page_count = 0
        while True:
            await self.ctx.rate_limiter.acquire(
                self.ctx.integration_id, self.platform
            )
            # Params conforme docs oficiais:
            # https://developer.sophos.com/docs/common-v1/1/routes/alerts/get
            # Endpoint aceita: from, to, pageFromKey, pageSize, category,
            # severity, product, groupKey, ids. NÃO aceita ``sort``.
            params: Dict[str, Any] = {
                "from": from_ts,
                "pageSize": 200,
            }
            if page_key:
                params["pageFromKey"] = page_key

            started = time.monotonic()
            stale_page_key = False
            payload: Dict[str, Any] = {}
            async with self.ctx.domain_limiter.slot(self.domain):
                async with self.ctx.session.get(
                    base_url, headers=self.ctx.headers, params=params
                ) as resp:
                    if resp.status == 429:
                        retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                        await self.ctx.rate_limiter.backoff(
                            self.platform, retry_after
                        )
                        raise SophosRateLimitedError(retry_after)
                    if 400 <= resp.status < 500 and resp.status != 401:
                        # Log body para debug de 4xx (não 401 — aquele é tratado
                        # pelo pipeline como recovery de token). Ajuda a achar
                        # params/filtros inválidos sem depender de curl manual.
                        body_preview = (await resp.text())[:500]
                        logger.warning(
                            "sophos alerts: HTTP %s params=%s body=%s",
                            resp.status, params, body_preview,
                        )
                        # AUTO-CURA do cursor de paginação. ``pageFromKey`` é
                        # opaco e tem validade do lado do vendor; reenviar um
                        # expirado dá 400 em TODO ciclo seguinte. Encerramos o
                        # ciclo SEM levantar, de propósito: no caminho de
                        # exceção o pipeline regrava ``cursor_before``
                        # byte-a-byte e a chave morta voltaria: o feed só
                        # destravaria com reset manual. Saindo limpo, a escrita
                        # final abaixo persiste ``pageFromKey=None`` e o próximo
                        # ciclo recomeça pela janela (o dedupe absorve a
                        # re-leitura).
                        if resp.status == 400 and page_key:
                            stale_page_key = True
                            logger.warning(
                                "sophos alerts: 400 com pageFromKey — chave "
                                "descartada, próximo ciclo reinicia de from=%s "
                                "(integration=%s)",
                                from_ts, self.ctx.integration_id,
                            )
                    if not stale_page_key:
                        resp.raise_for_status()
                        payload = await resp.json()
            if stale_page_key:
                page_key = None
                break

            API_LATENCY.labels(vendor=self.platform, stream=self.stream).observe(
                time.monotonic() - started
            )

            items = payload.get("items") or []
            for ev in items:
                raw_created = ev.get("createdAt") or ev.get("raisedAt") or latest_ts
                # Canonicaliza ANTES de comparar. A comparação é lexicográfica,
                # o que só é correto porque ``_safe_cursor_ts`` garante o mesmo
                # formato UTC/``Z`` nos dois lados — com um ``-03:00`` no meio,
                # ``'...T18:56:10-03:00' < '...T18:56:10Z'`` e o watermark
                # andaria para trás (ou congelaria).
                created = (
                    _safe_cursor_ts(raw_created, latest_ts)
                    if isinstance(raw_created, str)
                    else latest_ts
                )
                if created > latest_ts:
                    latest_ts = created
                yield ev

            page_key = (payload.get("pages") or {}).get("nextKey")

            # Teto por ciclo (regressão do poison-loop de soft-timeout): se ainda há
            # próxima página (``page_key`` truthy) E batemos o teto, salvamos o cursor
            # RESUMÍVEL — o ``pageFromKey`` da PRÓXIMA página — e retornamos ANTES da
            # escrita final abaixo. CRÍTICO: cair na escrita final moveria ``from`` p/
            # ``latest_ts`` e zeraria o ``pageFromKey``; como o endpoint NÃO aceita
            # ``sort`` (ver params acima), isso PULARIA as páginas ainda não lidas
            # (perda de dados). Mantemos ``from_ts`` no valor original — o próximo
            # ciclo retoma exatamente de ``page_key``; a escrita final só roda quando
            # ``nextKey`` realmente some (backlog drenado).
            page_count += 1
            if self.ctx.bounded_per_cycle and page_key and page_count >= _MAX_PAGES_PER_CYCLE:
                self.ctx.cursor = {"from_ts": from_ts, "pageFromKey": page_key}
                # Sobrou backlog: ``from_ts`` fica no valor original de propósito,
                # e sem este sinal esse "parado" é lido como tenant sem alertas.
                self.mark_cycle_capped()
                logger.info(
                    "sophos alerts: teto de %d páginas/ciclo atingido — cursor RESUMÍVEL "
                    "em pageFromKey (from_ts=%s) p/ próximo ciclo (integration=%s)",
                    _MAX_PAGES_PER_CYCLE, from_ts, self.ctx.integration_id,
                )
                return
            if not page_key:
                break

            # Cursor intermediário: se o worker morrer mid-loop, retomamos
            # da próxima página (e também do latest_ts que já capturamos).
            self.ctx.cursor = {"from_ts": from_ts, "pageFromKey": page_key}

        # Cursor final: próximo ciclo começa onde paramos.
        self.ctx.cursor = {"from_ts": latest_ts, "pageFromKey": None}

    def extract_message_id(self, event: Dict[str, Any]) -> str:
        return str(event.get("id") or event.get("alertId") or event.get("uuid") or "")

    @classmethod
    def watermark_at(cls, cursor: Optional[Dict[str, Any]]) -> Optional[datetime]:
        """``from_ts`` — o ``from`` enviado à Sophos, sempre com precisão de segundos.

        ``_normalize_ts`` já tirou os microssegundos antes de gravar (a Sophos
        rejeita o formato), então o que chega aqui é ISO com ``Z``.
        """
        return cls.watermark_from_iso(cursor, "from_ts")


def _default_lookback_iso() -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=1)
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


#: Único formato que a Sophos aceita em ``from``/``createdAfter``: UTC, precisão
#: de segundos, sufixo ``Z``. Usado para *verificar* a saída de ``_normalize_ts``
#: — se algo escapar dela, o guard de cursor abaixo prefere o cold start a
#: gravar um valor que envenena todas as coletas seguintes.
_CANON_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

#: ``...+0000`` / ``...-0300`` (offset ISO-8601 sem dois-pontos) -> grupos para
#: reinserir o separador antes de ``datetime.fromisoformat``.
_OFFSET_NO_COLON_RE = re.compile(r"(.*)([+-]\d{2})(\d{2})$")


def _normalize_ts(value: str) -> str:
    """Canonicaliza um timestamp ISO-8601 para ``YYYY-MM-DDTHH:MM:SSZ`` (UTC).

    Sophos rejeita qualquer outra forma com ``validationException: Timestamp
    ... is not in the right format``, e o valor entra no cursor a partir do
    ``createdAt`` do próprio evento — então um único evento fora do formato
    envenena TODAS as coletas seguintes daquele tenant (a versão anterior
    tratava só microsegundos e deixava passar offset não-UTC, naive e ``+0000``,
    o que produzia 400 permanente "depois de um tempo").

    Converte para UTC de verdade: ``...T18:56:10-03:00`` vira
    ``...T21:56:10Z``, não ``...T18:56:10-03:00``. Um valor naive (sem fuso) é
    assumido UTC — é o que a Sophos documenta para os campos de data.
    """
    if not isinstance(value, str) or not value:
        return value
    raw = value.strip()
    try:
        candidate = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        # ``+0000`` -> ``+00:00``. ``fromisoformat`` só aceita offset sem
        # dois-pontos a partir do 3.11; tratamos aqui para o resultado não
        # depender da versão do interpretador (o backend roda 3.12, mas um
        # ambiente de teste mais antigo cairia no fallback silenciosamente).
        candidate = _OFFSET_NO_COLON_RE.sub(r"\1\2:\3", candidate)
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        # Não parseia: devolve como veio. O chamador decide — o guard de
        # cursor descarta em vez de persistir.
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _safe_cursor_ts(value: str, fallback: str) -> str:
    """``value`` canonicalizado, ou ``fallback`` se ele não for utilizável.

    Fail-safe do cursor: gravar um timestamp que a Sophos rejeita transforma um
    evento malformado num 400 permanente, curável só por reset manual. Recoletar
    uma janela (o dedupe do pipeline absorve) é sempre melhor que parar o feed.
    """
    canon = _normalize_ts(value)
    # Forma E validade. A regex sozinha aceitaria ``2026-13-45T99:99:99Z``, que
    # tem o shape certo mas é uma data impossível: ``_normalize_ts`` não
    # consegue parseá-la e a devolve crua (passthrough), então o valor chegaria
    # ao query param e a API responderia 400 — exatamente o que este guard
    # existe para impedir.
    if isinstance(canon, str) and _CANON_TS_RE.match(canon):
        try:
            datetime.fromisoformat(canon[:-1] + "+00:00")
        except ValueError:
            pass
        else:
            return canon
    logger.warning(
        "sophos: timestamp fora do formato aceito (%r) — descartado do cursor "
        "em favor de %r; sem isto o próximo ciclo receberia HTTP 400 permanente",
        value, fallback,
    )
    return fallback


def _parse_retry_after(value: str | None) -> int:
    if not value:
        return 5
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 5


# ── Self-registration ────────────────────────────────────────────────

def _sophos_provider(integration):
    """Factory tardia do ``SophosProvider`` rico (alerts/health/ações/discover).

    Import tardio (só no 1º ``get_provider``) evita puxar o pacote ``providers``
    durante o boot do registry de collectors — zero risco de ciclo de import."""
    from ...providers.sophos.provider import SophosProvider

    return SophosProvider(integration)


def _register() -> None:
    # Import tardio evita ciclo registry → vendors → registry.
    from datetime import timedelta as _td
    from ..auth.probes import sophos_probe as _sophos_probe
    from ..auth.refreshers import sophos_refresher
    from ..queues import Q_PRIORITY, T_COLLECT_PRIORITY
    from ..registry import (
        AuthField,
        CollectorRegistration,
        PlatformRegistration,
        register,
        register_platform,
    )

    # Catálogo da UI (dono do platform "sophos" — registra 1× p/ os 3 streams:
    # alerts, cases, detections). Self-describing — sem hardcode em providers.py.
    register_platform(
        PlatformRegistration(
            platform="sophos",
            display_name="Sophos Central",
            category="EDR / XDR",
            description="Sophos Central — alertas, casos e detecções (EDR/XDR).",
            icon_id="sophos",
            docs_url="https://developer.sophos.com/",
            order=10,
            test_fn=_sophos_probe,
            provider_factory=_sophos_provider,
            # secrets vivem no store integration_credentials (sem flag legada).
            # client_secret é digitado no create; access_token/refresh_token são
            # cunhados no reauth (provider/refresher/token_manager) — todos no store.
            required_secrets=("client_secret", "access_token", "refresh_token"),
            capabilities=frozenset({
                "catalog", "auth:test", "health",
                "collect:alerts", "collect:cases", "collect:detections",
                "discover:children",
                CAP_QUERY_XDR_DATA_LAKE,
            }),
            # Só o card base ("sophos" = tenant) roda query; as variantes MSSP
            # (partner/organization) abaixo NÃO declaram query_capabilities.
            query_capabilities=(SOPHOS_QUERY_CAPABILITY,),
            auth_fields=(
                AuthField(key="client_id", label="Client ID", type="string", required=True,
                          help_text="Client ID da API Sophos Central (Sophos Central Admin > API Credentials)"),
                AuthField(key="client_secret", label="Client Secret", type="secret", required=True),
                AuthField(key="region", label="Região", type="string", required=False,
                          help_text="Descoberto automaticamente na primeira conexão"),
            ),
        )
    )

    # ── Variantes MSSP ────────────────────────────────────────────────
    # Cards distintos na galeria que mapeiam para platform="sophos" + kind no
    # create (via base_platform). ``discover:children`` destrava a auto-descoberta
    # de tenants. Sem campo ``region`` (descoberto por filho). O client_secret e
    # os tokens OAuth vivem no store integration_credentials.
    _mssp_caps = frozenset({"catalog", "auth:test", "health", "discover:children"})
    _mssp_auth = (
        AuthField(key="client_id", label="Client ID", type="string", required=True,
                  help_text="Client ID da API Sophos Central (Partner/Organization)"),
        AuthField(key="client_secret", label="Client Secret", type="secret", required=True),
    )
    register_platform(
        PlatformRegistration(
            platform="sophos_partner",
            display_name="Sophos Central — Partner",
            category="EDR / XDR",
            description="Sophos Central Partner — descobre e gerencia os tenants dos clientes (MSSP).",
            icon_id="sophos",
            docs_url="https://developer.sophos.com/getting-started",
            order=11,
            test_fn=_sophos_probe,
            required_secrets=("client_secret", "access_token", "refresh_token"),
            variant="partner",
            base_platform="sophos",
            capabilities=_mssp_caps,
            auth_fields=_mssp_auth,
        )
    )
    register_platform(
        PlatformRegistration(
            platform="sophos_organization",
            display_name="Sophos Central — Organization",
            category="EDR / XDR",
            description="Sophos Central Organization — tier organizacional sobre múltiplos tenants.",
            icon_id="sophos",
            docs_url="https://developer.sophos.com/getting-started",
            order=12,
            test_fn=_sophos_probe,
            required_secrets=("client_secret", "access_token", "refresh_token"),
            variant="organization",
            base_platform="sophos",
            capabilities=_mssp_caps,
            auth_fields=_mssp_auth,
        )
    )

    register(
        CollectorRegistration(
            platform=SophosAlertsCollector.platform,
            stream=SophosAlertsCollector.stream,
            collector_cls=SophosAlertsCollector,
            refresh_fn=sophos_refresher,
            schedule=_td(minutes=1),
            queue=Q_PRIORITY,
            task_name=T_COLLECT_PRIORITY,
        )
    )


_register()
