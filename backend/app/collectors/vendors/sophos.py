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
        # Sanitiza cursor: Sophos rejeita timestamps com microsegundos
        # ("Timestamp ... is not in the right format"). Eventos de alguns
        # tenants retornam ``createdAt`` com microsegundos; se o cursor
        # herdou esse valor antes do fix, precisa ser normalizado antes
        # de ir pro query param ``from``.
        from_ts: str = _normalize_ts(
            cursor.get("from_ts") or _default_lookback_iso()
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
                    resp.raise_for_status()
                    payload = await resp.json()

            API_LATENCY.labels(vendor=self.platform, stream=self.stream).observe(
                time.monotonic() - started
            )

            items = payload.get("items") or []
            for ev in items:
                raw_created = ev.get("createdAt") or ev.get("raisedAt") or latest_ts
                created = _normalize_ts(raw_created) if isinstance(raw_created, str) else latest_ts
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


def _normalize_ts(value: str) -> str:
    """Remove microsegundos de um timestamp ISO-8601.

    Sophos rejeita ``2026-04-23T18:56:10.439851Z`` com
    ``validationException: Timestamp ... is not in the right format``.
    Aceita apenas precisão de segundos: ``2026-04-23T18:56:10Z``.
    Alguns tenants devolvem ``createdAt`` com microsegundos nos próprios
    eventos — se herdado no cursor, estraga a próxima coleta.
    """
    if not isinstance(value, str) or not value:
        return value
    try:
        # Python >=3.11 aceita ``Z`` nativamente. Para versões antigas,
        # trocamos por ``+00:00`` primeiro.
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        return dt.isoformat(timespec="seconds").replace("+00:00", "Z")
    except ValueError:
        # Se não parsear, devolve como está — deixa a API rejeitar com
        # mensagem clara, melhor que mascarar um cursor corrompido.
        return value


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
