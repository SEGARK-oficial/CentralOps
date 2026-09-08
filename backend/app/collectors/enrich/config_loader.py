"""Leitor central da configuração de infraestrutura do enriquecimento.

Espelha ``collectors/config_loader.py`` linha a linha, e pelas mesmas razões:
fonte da verdade em runtime é a tabela ``enrichment_config`` (singleton id=1),
editada pela UI; o ``.env`` fica como **seed inicial**; e os workers leem por um
cache com invalidação no PUT, para que uma mudança não exija redeploy.

Três diferenças em relação ao irmão da coleta, todas deliberadas:

1. **Memo de processo além do cache Redis.** O ``get_collector_config`` paga um
   round-trip Redis por ciclo. Aqui isso não serve: a resolução acontece no gate
   que decide se o subsistema INTEIRO roda, e com o enriquecimento desligado esse
   gate custaria uma ida à rede por ciclo para descobrir que não há nada a fazer.
   O memo de processo (``_PROCESS_MEMO_TTL_S``) transforma o caso comum numa
   comparação de relógio monotônico. Propagação no pior caso é a soma dos dois
   TTLs, e está documentada em ``PROPAGATION_WORST_CASE_S``.

2. **O snapshot carrega o CIPHERTEXT da senha, nunca o texto claro.** O snapshot
   é serializado para o Redis principal (compartilhado), e escrever ali a senha
   em claro do Redis dedicado seria trocar um segredo de arquivo por um segredo
   em cache de rede. Guardamos a mesma string que está no Postgres e deciframos
   só em processo, na hora de construir o cliente (:meth:`redis_url`). Isso é
   estritamente mais conservador do que o que já existe naquele Redis: o cache de
   token OAuth dos coletores guarda ``access_token`` em claro.

3. **Fail-closed é preservado.** Sem host configurado, :meth:`redis_url` devolve
   ``None`` e o enriquecimento remoto não roda — exatamente o que
   ``ENRICH_REDIS_URL`` vazia já fazia. A migração para banco muda ONDE se
   configura, nunca o que acontece quando não se configura.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import quote, urlparse

import redis.asyncio as redis_async

from ...core.config import settings
from ...db import database, models

logger = logging.getLogger(__name__)

#: Chave do cache compartilhado. Namespace distinto de ``collector:config``.
CACHE_KEY = "enrich:config"
CACHE_TTL_SECONDS = 30
#: Janela do memo de processo. Curta de propósito: é o que o gate por ciclo paga.
_PROCESS_MEMO_TTL_S = 5.0
#: Pior caso de propagação de uma edição na UI até TODO worker enxergar, quando
#: a invalidação do PUT não alcança (Redis momentaneamente fora, por exemplo).
PROPAGATION_WORST_CASE_S = CACHE_TTL_SECONDS + int(_PROCESS_MEMO_TTL_S)

#: Defaults canônicos. Fonte ÚNICA — o model, o seed da migração e o snapshot de
#: env leem daqui. Divergência entre esses três já mordeu este produto no TTL do
#: dedupe (quatro cópias do literal, e baixar o default não mudava nada).
DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "redis_host": None,
    "redis_port": 6379,
    "redis_db": 0,
    "redis_use_tls": False,
    "remote_batch_budget_ms": 300,
    "cycle_budget_ms": 30_000,
    "l1_max_entries": 10_000,
    "singleflight_wait_ms": 50,
    "breaker_failure_threshold": 3,
    "breaker_window_s": 600,
    "breaker_cooldown_s": 120,
    "breaker_max_cooldown_s": 1920,
    "max_table_bytes": 32 * 1024 * 1024,
    "lru_bytes": 64 * 1024 * 1024,
}

#: Campos que entram no ``config_version``. O ciphertext ENTRA: rotacionar a
#: senha sem trocar o host precisa derrubar o cliente Redis já construído nos
#: workers, senão eles seguem autenticados com a credencial revogada até o
#: próximo restart.
_VERSIONED_FIELDS = tuple(DEFAULTS.keys()) + ("redis_secret_ref",)


@dataclass(frozen=True)
class EnrichmentConfigSnapshot:
    """Config imutável de um instante. ``frozen`` para não mutar cache compartilhado."""

    enabled: bool = DEFAULTS["enabled"]

    redis_host: Optional[str] = DEFAULTS["redis_host"]
    redis_port: int = DEFAULTS["redis_port"]
    redis_db: int = DEFAULTS["redis_db"]
    redis_use_tls: bool = DEFAULTS["redis_use_tls"]
    #: Ciphertext de ``core.secrets``, ou ``None``. Nunca o texto claro.
    redis_secret_ref: Optional[str] = None

    remote_batch_budget_ms: int = DEFAULTS["remote_batch_budget_ms"]
    cycle_budget_ms: int = DEFAULTS["cycle_budget_ms"]
    l1_max_entries: int = DEFAULTS["l1_max_entries"]
    singleflight_wait_ms: int = DEFAULTS["singleflight_wait_ms"]

    breaker_failure_threshold: int = DEFAULTS["breaker_failure_threshold"]
    breaker_window_s: int = DEFAULTS["breaker_window_s"]
    breaker_cooldown_s: int = DEFAULTS["breaker_cooldown_s"]
    breaker_max_cooldown_s: int = DEFAULTS["breaker_max_cooldown_s"]

    max_table_bytes: int = DEFAULTS["max_table_bytes"]
    lru_bytes: int = DEFAULTS["lru_bytes"]

    #: ``False`` quando o snapshot veio do ``.env`` (linha ausente ou DB fora).
    is_persisted: bool = False

    # ── derivados ────────────────────────────────────────────────────────────

    @property
    def remote_batch_budget_s(self) -> float:
        return max(int(self.remote_batch_budget_ms), 0) / 1000.0

    @property
    def cycle_budget_s(self) -> float:
        return max(int(self.cycle_budget_ms), 0) / 1000.0

    @property
    def redis_configured(self) -> bool:
        """``True`` quando há host. É o gate do enriquecimento REMOTO."""
        return bool((self.redis_host or "").strip())

    @property
    def config_version(self) -> str:
        payload = {k: getattr(self, k) for k in _VERSIONED_FIELDS}
        raw = json.dumps(payload, sort_keys=True, default=str).encode()
        return hashlib.sha1(raw).hexdigest()[:12]

    def redis_url(self) -> Optional[str]:
        """URL completa do L2, com a senha JÁ DECIFRADA, ou ``None``.

        Decifrar aqui (e não ao montar o snapshot) é o que mantém o texto claro
        fora do cache compartilhado: o snapshot viaja com o ciphertext, e o
        plaintext só existe no processo que vai abrir a conexão.

        Falha de decifragem devolve ``None`` em vez de propagar: uma
        ``APP_MASTER_KEY`` trocada não pode derrubar a COLETA, que é o produto.
        O enriquecimento remoto para, com log — mesmo desfecho de host ausente.
        """
        host = (self.redis_host or "").strip()
        if not host:
            return None
        password = ""
        if self.redis_secret_ref:
            try:
                from ...core import secrets as secrets_mod

                password = secrets_mod.get_default_backend().decrypt(
                    self.redis_secret_ref
                )
            except Exception:
                logger.error(
                    "enrich_config: senha do cache L2 não decifrou — enriquecimento "
                    "remoto DESLIGADO. Regrave a senha em Configuração › "
                    "Enriquecimento (a APP_MASTER_KEY pode ter mudado).",
                    extra={"event": "enrich.config_secret_undecryptable"},
                )
                return None
        scheme = "rediss" if self.redis_use_tls else "redis"
        auth = f":{quote(password, safe='')}@" if password else ""
        return f"{scheme}://{auth}{host}:{int(self.redis_port)}/{int(self.redis_db)}"

    def redis_url_masked(self) -> Optional[str]:
        """Para exibir e logar. Nunca contém segredo."""
        host = (self.redis_host or "").strip()
        if not host:
            return None
        scheme = "rediss" if self.redis_use_tls else "redis"
        auth = ":***@" if self.redis_secret_ref else ""
        return f"{scheme}://{auth}{host}:{int(self.redis_port)}/{int(self.redis_db)}"

    def to_dict(self) -> Dict[str, Any]:
        """Serializa para o cache Redis. Carrega o ciphertext, nunca o plaintext."""
        out: Dict[str, Any] = {k: getattr(self, k) for k in DEFAULTS}
        out["redis_secret_ref"] = self.redis_secret_ref
        out["is_persisted"] = self.is_persisted
        return out


def _int(value: Any, fallback: int) -> int:
    """Inteiro tolerante: linha antiga com NULL não pode derrubar o worker."""
    try:
        if value is None:
            return int(fallback)
        return int(value)
    except (TypeError, ValueError):
        return int(fallback)


#: ``plaintext -> ciphertext`` da senha vinda do ``.env``.
#:
#: Existe por uma razão de CORREÇÃO, não de desempenho. O Fernet usa nonce, então
#: ``encrypt`` do MESMO texto devolve bytes diferentes a cada chamada — e
#: ``redis_secret_ref`` entra no ``config_version``. Sem memo, todo snapshot de
#: fallback teria versão nova, o runtime concluiria "a config mudou" e destruiria
#: o cliente Redis do L2 a cada ciclo, reconectando para sempre. Foi encontrado ao
#: escrever ``test_config_version_estavel_no_fallback_de_env``.
_env_secret_memo: Dict[str, str] = {}


def _encrypt_env_password(password: str) -> Optional[str]:
    cached = _env_secret_memo.get(password)
    if cached is not None:
        return cached
    try:
        from ...core import secrets as secrets_mod

        ciphertext = secrets_mod.get_default_backend().encrypt(password)
    except Exception:  # pragma: no cover — defensivo
        logger.warning(
            "enrich_config: não foi possível cifrar a senha de ENRICH_REDIS_URL; "
            "seguindo sem autenticação no L2"
        )
        return None
    _env_secret_memo[password] = ciphertext
    return ciphertext


def _snapshot_from_env() -> EnrichmentConfigSnapshot:
    """Último recurso: linha ausente, DB fora, ou boot antes da migração.

    Traduz ``ENRICH_REDIS_URL`` (uma string) para os campos separados, para que
    uma instalação existente não perca a configuração ao subir esta versão.
    """
    host: Optional[str] = None
    port = DEFAULTS["redis_port"]
    db_index = DEFAULTS["redis_db"]
    use_tls = DEFAULTS["redis_use_tls"]
    secret_ref: Optional[str] = None

    raw_url = (getattr(settings, "ENRICH_REDIS_URL", "") or "").strip()
    if raw_url:
        parsed = parse_redis_url(raw_url)
        if parsed is not None:
            host, port, db_index, use_tls, password = parsed
            if password:
                # O env traz a senha em CLARO. Cifrar aqui mantém a invariante
                # "o snapshot só carrega ciphertext" mesmo no caminho de
                # fallback, que é justamente o que roda antes da migração
                # popular a linha. Memoizado: ver ``_env_secret_memo``.
                secret_ref = _encrypt_env_password(password)

    return EnrichmentConfigSnapshot(
        enabled=bool(getattr(settings, "ENRICHMENT_ENABLED", DEFAULTS["enabled"])),
        redis_host=host,
        redis_port=port,
        redis_db=db_index,
        redis_use_tls=use_tls,
        redis_secret_ref=secret_ref,
        remote_batch_budget_ms=int(
            float(getattr(settings, "ENRICH_REMOTE_BATCH_BUDGET_S", 0.3) or 0.3) * 1000
        ),
        cycle_budget_ms=int(
            float(getattr(settings, "ENRICH_CYCLE_BUDGET_S", 30.0) or 30.0) * 1000
        ),
        l1_max_entries=_int(
            getattr(settings, "ENRICH_L1_MAX_ENTRIES", None), DEFAULTS["l1_max_entries"]
        ),
        singleflight_wait_ms=_int(
            getattr(settings, "ENRICH_SINGLEFLIGHT_WAIT_MS", None),
            DEFAULTS["singleflight_wait_ms"],
        ),
        breaker_failure_threshold=_int(
            getattr(settings, "ENRICH_BREAKER_FAILURE_THRESHOLD", None),
            DEFAULTS["breaker_failure_threshold"],
        ),
        breaker_window_s=_int(
            getattr(settings, "ENRICH_BREAKER_WINDOW_S", None),
            DEFAULTS["breaker_window_s"],
        ),
        breaker_cooldown_s=_int(
            getattr(settings, "ENRICH_BREAKER_COOLDOWN_S", None),
            DEFAULTS["breaker_cooldown_s"],
        ),
        breaker_max_cooldown_s=_int(
            getattr(settings, "ENRICH_BREAKER_MAX_COOLDOWN_S", None),
            DEFAULTS["breaker_max_cooldown_s"],
        ),
        max_table_bytes=_int(
            getattr(settings, "ENRICH_MAX_TABLE_BYTES", None),
            DEFAULTS["max_table_bytes"],
        ),
        lru_bytes=_int(
            getattr(settings, "ENRICH_LRU_BYTES", None), DEFAULTS["lru_bytes"]
        ),
        is_persisted=False,
    )


def parse_redis_url(url: str):
    """``(host, port, db, use_tls, password)`` de uma URL Redis, ou ``None``.

    Usada para migrar ``ENRICH_REDIS_URL`` e para comparar com o ``REDIS_URL``
    principal no guard de instância distinta.
    """
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return None
    if parsed.scheme not in ("redis", "rediss"):
        return None
    host = parsed.hostname or ""
    if not host:
        return None
    port = int(parsed.port or 6379)
    path = (parsed.path or "").lstrip("/")
    try:
        db_index = int(path) if path else 0
    except ValueError:
        db_index = 0
    return host, port, db_index, parsed.scheme == "rediss", (parsed.password or "")


def _snapshot_from_row(row: "models.EnrichmentConfig") -> EnrichmentConfigSnapshot:
    return EnrichmentConfigSnapshot(
        enabled=bool(row.enabled),
        redis_host=(row.redis_host or None),
        redis_port=_int(row.redis_port, DEFAULTS["redis_port"]),
        redis_db=_int(row.redis_db, DEFAULTS["redis_db"]),
        redis_use_tls=bool(row.redis_use_tls),
        redis_secret_ref=(row.redis_secret_ref or None),
        remote_batch_budget_ms=_int(
            row.remote_batch_budget_ms, DEFAULTS["remote_batch_budget_ms"]
        ),
        cycle_budget_ms=_int(row.cycle_budget_ms, DEFAULTS["cycle_budget_ms"]),
        l1_max_entries=_int(row.l1_max_entries, DEFAULTS["l1_max_entries"]),
        singleflight_wait_ms=_int(
            row.singleflight_wait_ms, DEFAULTS["singleflight_wait_ms"]
        ),
        breaker_failure_threshold=_int(
            row.breaker_failure_threshold, DEFAULTS["breaker_failure_threshold"]
        ),
        breaker_window_s=_int(row.breaker_window_s, DEFAULTS["breaker_window_s"]),
        breaker_cooldown_s=_int(row.breaker_cooldown_s, DEFAULTS["breaker_cooldown_s"]),
        breaker_max_cooldown_s=_int(
            row.breaker_max_cooldown_s, DEFAULTS["breaker_max_cooldown_s"]
        ),
        max_table_bytes=_int(row.max_table_bytes, DEFAULTS["max_table_bytes"]),
        lru_bytes=_int(row.lru_bytes, DEFAULTS["lru_bytes"]),
        is_persisted=True,
    )


def load_from_db_session(db) -> EnrichmentConfigSnapshot:
    """Lê o singleton com uma Session **injetada**. Preferido em routers (DI)."""
    try:
        row = db.query(models.EnrichmentConfig).filter_by(id=1).first()
        if row is None:
            return _snapshot_from_env()
        return _snapshot_from_row(row)
    except Exception as exc:  # pragma: no cover — defensivo
        logger.warning("enrich_config: falha ao ler DB (%s) — usando env", exc)
        return _snapshot_from_env()


def _load_from_db_sync() -> EnrichmentConfigSnapshot:
    """Sem DI (workers, beat, CLI). Abre a própria Session."""
    try:
        with database.SessionLocal() as db:
            return load_from_db_session(db)
    except Exception as exc:  # pragma: no cover — defensivo
        logger.warning("enrich_config: falha ao abrir session (%s)", exc)
        return _snapshot_from_env()


# ── memo de processo ─────────────────────────────────────────────────────────
#
# Guarda ``(expira_em_monotonic, snapshot)``. Módulo-global de propósito: o
# Celery é prefork e cada fork quer o próprio memo — compartilhar entre
# processos é justamente o papel do Redis.
_memo: Dict[str, Any] = {"expires_at": 0.0, "snapshot": None}


def _memo_get() -> Optional[EnrichmentConfigSnapshot]:
    import time

    if _memo["snapshot"] is None or _memo["expires_at"] <= time.monotonic():
        return None
    return _memo["snapshot"]


def _memo_put(snapshot: EnrichmentConfigSnapshot) -> None:
    import time

    _memo["snapshot"] = snapshot
    _memo["expires_at"] = time.monotonic() + _PROCESS_MEMO_TTL_S


def reset_process_memo() -> None:
    """Zera o memo. Para testes e para o PUT no mesmo processo da API."""
    _memo["snapshot"] = None
    _memo["expires_at"] = 0.0


async def get_enrichment_config(
    redis: Optional[redis_async.Redis] = None,
) -> EnrichmentConfigSnapshot:
    """Caminho async principal: memo de processo → Redis → DB → env.

    ``redis`` opcional para o caso de quem chama não ter cliente à mão; aí o
    memo e o DB respondem sozinhos.
    """
    memoized = _memo_get()
    if memoized is not None:
        return memoized

    cached = None
    if redis is not None:
        try:
            cached = await redis.get(CACHE_KEY)
        except Exception as exc:  # pragma: no cover — defensivo
            logger.debug("enrich_config: Redis indisponível (%s) — caindo no DB", exc)
            cached = None

    if cached:
        try:
            data = json.loads(cached)
            snapshot = EnrichmentConfigSnapshot(**data)
            _memo_put(snapshot)
            return snapshot
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("enrich_config: cache corrompido (%s) — refetch DB", exc)

    snapshot = await asyncio.to_thread(_load_from_db_sync)

    if redis is not None:
        try:
            await redis.set(
                CACHE_KEY,
                json.dumps(snapshot.to_dict(), separators=(",", ":"), default=str),
                ex=CACHE_TTL_SECONDS,
            )
        except Exception as exc:  # pragma: no cover — defensivo
            logger.debug("enrich_config: falha ao popular Redis (%s)", exc)

    _memo_put(snapshot)
    return snapshot


async def invalidate_enrichment_config(
    redis: Optional[redis_async.Redis] = None,
) -> None:
    """Chamado pelo ``PUT`` após gravar. Best-effort nos dois níveis."""
    reset_process_memo()
    if redis is None:
        return
    try:
        await redis.delete(CACHE_KEY)
    except Exception as exc:  # pragma: no cover — defensivo
        logger.warning("enrich_config: falha ao invalidar Redis (%s)", exc)


__all__ = [
    "CACHE_KEY",
    "CACHE_TTL_SECONDS",
    "DEFAULTS",
    "PROPAGATION_WORST_CASE_S",
    "EnrichmentConfigSnapshot",
    "get_enrichment_config",
    "invalidate_enrichment_config",
    "load_from_db_session",
    "parse_redis_url",
    "reset_process_memo",
]
