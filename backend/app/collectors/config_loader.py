"""Leitor central de configuração do Collector — banco + cache Redis.

Arquitetura:
- **Fonte da verdade em runtime**: tabela ``collector_config`` (singleton
  id=1). UI gerencia via ``/api/collectors/config``.
- **Cache**: Redis ``collector:config`` com TTL 30s. Mitiga carga de DB
  quando N workers lêem a mesma config simultaneamente.
- **Invalidação**: ``PUT /api/collectors/config`` chama
  ``invalidate_collector_config(redis)`` → próxima leitura repopula.
- **Fallback defensivo**: se DB e Redis falharem, usa ``settings`` do
  ``.env`` para que o pipeline nunca morra só por config ausente.
- **Bootstrap**: valores de ``.env`` são **seed inicial** via
  ``database._run_lightweight_migrations()``. Após isso, operação diária
  ocorre pela UI sem editar ``.env``.

Uso típico no ``pipeline.py``::

    redis = redis_async.from_url(settings.REDIS_URL)
    snapshot = await get_collector_config(redis)
    rate_limiter = RedisRateLimiter(redis, snapshot.rate_limits_by_vendor)
    # ... usa snapshot.collector_batch_size, etc.

``CollectorConfigSnapshot`` é um dataclass frozen — evita mutação
acidental do cache compartilhado entre tasks.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import redis.asyncio as redis_async

from ..core.config import settings
from ..db import database, models

logger = logging.getLogger(__name__)

CACHE_KEY = "collector:config"
CACHE_TTL_SECONDS = 30

# ADR-0015, Fase 0 — fonte ÚNICA do default de TTL do dedupe.
#
# Este literal existia em QUATRO lugares (``Settings.DEDUPE_TTL_DAYS``, o default
# da dataclass abaixo, e dois fallbacks ``or 7`` em ``_snapshot_from_env`` e
# ``_snapshot_from_row``). O efeito da duplicação era pior que estética: baixar o
# default no ``Settings`` NÃO mudava nada, porque os ``or 7`` reintroduziam o
# valor antigo — a invariante "existe um default e ele vale em todo lugar" era
# apenas um comentário implícito.
#
# É o mesmo padrão que já mordeu este produto três vezes (lock do RedBeat com TTL
# menor que o loop do beat; coletor sem teto por ciclo; o próprio dedupe com TTL
# de 7 dias contra memória finita). Guard executável em
# ``backend/tests/test_dedupe_ttl_invariant.py``.
#
# O valor (1 dia) é justificado em ``state/dedupe.py`` junto de ``DEFAULT_TTL_DAYS``:
# cobre com ~24x de folga a maior janela de reentrega automática
# (``visibility_timeout`` de 1h em celery_app.py).
DEFAULT_DEDUPE_TTL_DAYS = 1

# Campos que entram no hash de versão. Mudanças disparam recriação do
# singleton de ``wazuh_target`` (única parte com estado de processo).
_VERSIONED_FIELDS = (
    "wazuh_syslog_host",
    "wazuh_syslog_port",
    "wazuh_syslog_use_tls",
    "wazuh_ca_bundle",
    "wazuh_dispatch_mode",
    "wazuh_syslog_format",
    "collector_jsonl_dir",
)


@dataclass(frozen=True)
class CollectorConfigSnapshot:
    """Snapshot imutável da config. Consumido por ``pipeline`` e ``wazuh_target``."""

    # Destino Wazuh
    wazuh_syslog_host: Optional[str] = None
    wazuh_syslog_port: int = 514
    wazuh_syslog_use_tls: bool = False
    wazuh_ca_bundle: Optional[str] = None
    wazuh_dispatch_mode: str = "syslog"
    # formato syslog. rfc3164 = Wazuh JSON_Decoder compatível.
    # rfc5424 = legado (configs antigas em prod).
    wazuh_syslog_format: str = "rfc3164"
    collector_jsonl_dir: str = "/var/log/centralops/collectors"

    # Batching / dedupe
    collector_batch_size: int = 200
    collector_batch_flush_seconds: int = 5
    # ADR-0015: fonte ÚNICA do default (era literal 7 em 4 lugares — ver
    # DEFAULT_DEDUPE_TTL_DAYS). Divergência entre eles fazia o env default ser
    # silenciosamente sobreposto pelos fallbacks ``or 7`` abaixo.
    dedupe_ttl_days: int = DEFAULT_DEDUPE_TTL_DAYS

    # Mapas
    domain_concurrency_limits: Dict[str, int] = field(default_factory=dict)
    rate_limits_by_vendor: Dict[str, Dict[str, int]] = field(default_factory=dict)

    # Meta
    is_persisted: bool = False  # True se veio do DB; False se fallback de env

    @property
    def config_version(self) -> str:
        payload = {k: getattr(self, k) for k in _VERSIONED_FIELDS}
        raw = json.dumps(payload, sort_keys=True, default=str).encode()
        return hashlib.sha1(raw).hexdigest()[:12]

    def to_dict(self) -> Dict[str, Any]:
        """Serializa para JSON (cache Redis). ``config_version`` recomputado ao carregar."""
        return {
            "wazuh_syslog_host": self.wazuh_syslog_host,
            "wazuh_syslog_port": self.wazuh_syslog_port,
            "wazuh_syslog_use_tls": self.wazuh_syslog_use_tls,
            "wazuh_ca_bundle": self.wazuh_ca_bundle,
            "wazuh_dispatch_mode": self.wazuh_dispatch_mode,
            "wazuh_syslog_format": self.wazuh_syslog_format,
            "collector_jsonl_dir": self.collector_jsonl_dir,
            "collector_batch_size": self.collector_batch_size,
            "collector_batch_flush_seconds": self.collector_batch_flush_seconds,
            "dedupe_ttl_days": self.dedupe_ttl_days,
            "domain_concurrency_limits": dict(self.domain_concurrency_limits),
            "rate_limits_by_vendor": {
                k: dict(v) for k, v in self.rate_limits_by_vendor.items()
            },
            "is_persisted": self.is_persisted,
        }


def _snapshot_from_env() -> CollectorConfigSnapshot:
    """Último recurso — só cai aqui se DB e Redis falharem."""
    return CollectorConfigSnapshot(
        wazuh_syslog_host=settings.WAZUH_SYSLOG_HOST,
        wazuh_syslog_port=int(settings.WAZUH_SYSLOG_PORT or 514),
        wazuh_syslog_use_tls=bool(settings.WAZUH_CA_BUNDLE),
        wazuh_ca_bundle=settings.WAZUH_CA_BUNDLE,
        wazuh_dispatch_mode=settings.WAZUH_DISPATCH_MODE or "syslog",
        wazuh_syslog_format="rfc3164",
        collector_jsonl_dir=settings.COLLECTOR_JSONL_DIR
        or "/var/log/centralops/collectors",
        collector_batch_size=int(settings.COLLECTOR_BATCH_SIZE or 200),
        collector_batch_flush_seconds=int(settings.COLLECTOR_BATCH_FLUSH_SECONDS or 5),
        dedupe_ttl_days=int(settings.DEDUPE_TTL_DAYS or DEFAULT_DEDUPE_TTL_DAYS),
        domain_concurrency_limits=dict(settings.DOMAIN_CONCURRENCY_LIMITS or {}),
        rate_limits_by_vendor=dict(settings.RATE_LIMITS_BY_VENDOR or {}),
        is_persisted=False,
    )


def _snapshot_from_row(row: models.CollectorConfig) -> CollectorConfigSnapshot:
    """Constrói snapshot a partir de linha DB (deserializa mapas JSON)."""
    try:
        dcl = json.loads(row.domain_concurrency_limits or "{}")
    except json.JSONDecodeError:
        logger.warning("collector_config: domain_concurrency_limits corrompido, usando {}")
        dcl = {}
    try:
        rlv = json.loads(row.rate_limits_by_vendor or "{}")
    except json.JSONDecodeError:
        logger.warning("collector_config: rate_limits_by_vendor corrompido, usando {}")
        rlv = {}

    return CollectorConfigSnapshot(
        wazuh_syslog_host=row.wazuh_syslog_host,
        wazuh_syslog_port=int(row.wazuh_syslog_port or 514),
        wazuh_syslog_use_tls=bool(row.wazuh_syslog_use_tls),
        wazuh_ca_bundle=row.wazuh_ca_bundle,
        wazuh_dispatch_mode=row.wazuh_dispatch_mode or "syslog",
        wazuh_syslog_format=getattr(row, "wazuh_syslog_format", None) or "rfc3164",
        collector_jsonl_dir=row.collector_jsonl_dir
        or "/var/log/centralops/collectors",
        collector_batch_size=int(row.collector_batch_size or 200),
        collector_batch_flush_seconds=int(row.collector_batch_flush_seconds or 5),
        dedupe_ttl_days=int(row.dedupe_ttl_days or DEFAULT_DEDUPE_TTL_DAYS),
        domain_concurrency_limits=dict(dcl),
        rate_limits_by_vendor=dict(rlv),
        is_persisted=True,
    )


def load_from_db_session(db) -> CollectorConfigSnapshot:
    """Lê o singleton usando uma Session **injetada**. Preferido em
    contextos onde há DI (routers FastAPI) — respeita overrides/tests.
    """
    try:
        row = db.query(models.CollectorConfig).filter_by(id=1).first()
        if row is None:
            logger.info(
                "collector_config: linha id=1 ausente — usando seed de env"
            )
            return _snapshot_from_env()
        return _snapshot_from_row(row)
    except Exception as exc:  # pragma: no cover
        logger.exception("collector_config: falha ao ler DB (%s) — usando env", exc)
        return _snapshot_from_env()


def _load_from_db_sync() -> CollectorConfigSnapshot:
    """Lê a linha do DB síncrona abrindo SessionLocal própria.

    Usado em contextos sem DI: workers Celery, Beat, CLI. Para o router,
    prefira ``load_from_db_session(db)`` — respeita dependency_overrides.
    """
    try:
        with database.SessionLocal() as db:
            return load_from_db_session(db)
    except Exception as exc:  # pragma: no cover — defensivo
        logger.exception("collector_config: falha ao abrir session (%s)", exc)
        return _snapshot_from_env()


async def get_collector_config(
    redis: redis_async.Redis,
) -> CollectorConfigSnapshot:
    """Caminho async principal. Redis cache → DB → env.

    Cada chamada custa 1 round-trip Redis (hit) ou Redis+DB (miss).
    """
    try:
        cached = await redis.get(CACHE_KEY)
    except Exception as exc:  # pragma: no cover
        logger.warning("collector_config: Redis indisponível (%s) — caindo no DB", exc)
        cached = None

    if cached:
        try:
            data = json.loads(cached)
            return CollectorConfigSnapshot(**data)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("collector_config: cache corrompido (%s) — refetch DB", exc)

    # Cache miss → lê DB em thread (SQLAlchemy é síncrono).
    snapshot = await asyncio.to_thread(_load_from_db_sync)

    # Popula cache (best-effort).
    try:
        await redis.set(
            CACHE_KEY,
            json.dumps(snapshot.to_dict(), separators=(",", ":"), default=str),
            ex=CACHE_TTL_SECONDS,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("collector_config: falha ao popular Redis (%s)", exc)

    return snapshot


async def invalidate_collector_config(redis: redis_async.Redis) -> None:
    """Chamado pelo router ``PUT`` após gravar no DB. Best-effort."""
    try:
        await redis.delete(CACHE_KEY)
    except Exception as exc:  # pragma: no cover
        logger.warning("collector_config: falha ao invalidar Redis (%s)", exc)
