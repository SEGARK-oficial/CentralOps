"""Contrato base para collectors assíncronos por vendor (RF03, RF04).

Cada ``(vendor, stream)`` é uma subclasse concreta de ``BaseCollector``
que expõe um ``async def collect()`` produzindo eventos crus do vendor.

Após a Sprint 2 do plano de evolução, o collector NÃO mais transforma
o evento — ele só produz raw events. A transformação para o envelope
canônico ``{_centralops, normalized, raw}`` é feita pelo pipeline
chamando ``normalize.engine.MappingEngine.apply`` com o mapping
versionado correspondente a ``event_type`` e em seguida
``normalize.envelope.build_envelope``.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, AsyncIterator, Dict, Optional

import aiohttp

if TYPE_CHECKING:
    import redis.asyncio as redis_async

    from .domain_limiter import DomainLimiter
    from .rate_limit_redis import RedisRateLimiter


@dataclass
class CollectorContext:
    """Estado in-flight de uma coleta. Vive apenas durante ``run_collection_once``."""

    integration_id: int
    organization_id: int
    platform: str
    headers: Dict[str, str]
    session: aiohttp.ClientSession
    cursor: Optional[Dict[str, Any]]
    domain_limiter: "DomainLimiter"
    rate_limiter: "RedisRateLimiter"
    redis: "redis_async.Redis"


class BaseCollector(abc.ABC):
    """Um collector por (vendor, stream). Stateless entre chamadas."""

    platform: str  # "sophos" | "microsoft_defender" | "ninjaone" …
    stream: str  # "alerts" | "detections" | "incidents" | "activities" …
    # event_type é a chave de roteamento de mapping (RF3.3) — combinada
    # com vendor resolve ``MappingDefinition``. Convenção:
    # ``"<vendor_slug>.<event_kind>"`` (ex: ``"sophos.alert"``).
    event_type: str

    def __init__(self, ctx: CollectorContext) -> None:
        self.ctx = ctx

    # ── API pública ────────────────────────────────────────────────────

    @property
    @abc.abstractmethod
    def domain(self) -> str:
        """Host usado para o semáforo por domínio (RNF08)."""

    @abc.abstractmethod
    def collect(self) -> AsyncIterator[Dict[str, Any]]:
        """Yield eventos crus do vendor.

        Implementações devem:

        1. Respeitar paginação até exaurir (RF03).
        2. Atualizar ``self.ctx.cursor`` no fim da iteração (RF02).
        3. Fazer ``await self.ctx.rate_limiter.acquire(...)`` antes de cada
           requisição e envolver a requisição em
           ``async with self.ctx.domain_limiter.slot(self.domain)`` (RNF08).

        Eventos saem **crus** — sem transformação. O pipeline aplica o
        mapping versionado depois.
        """

    @abc.abstractmethod
    def extract_message_id(self, event: Dict[str, Any]) -> str:
        """ID usado para dedupe (RNF07). Prefere id nativo do vendor."""


def utcnow_iso() -> str:
    """Timestamp ISO-8601 UTC com sufixo Z (RFC 3339-friendly)."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
