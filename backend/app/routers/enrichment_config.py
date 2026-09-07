"""REST da configuração de INFRAESTRUTURA do enriquecimento (admin global).

``GET/PUT /api/collectors/enrichment/config``
    O singleton ``enrichment_config``. Espelha ``routers/collector_config.py``:
    o ``.env`` é seed, o banco é a verdade em runtime, e o PUT invalida o cache
    para que os workers reflitam em segundos, sem redeploy.

``POST /api/collectors/enrichment/config/test-redis``
    Sonda o cache L2 **antes** de gravar, com a senha em rascunho que o operador
    acabou de digitar. Sem isto, o único jeito de descobrir que a credencial está
    errada é publicar, esperar o ciclo e ler a aba de Execução.

**Por que admin GLOBAL e não admin de org.** Estes parâmetros são da
INSTALAÇÃO — um Redis, um orçamento, um breaker para todos os tenants. Um admin
escopado que pudesse editá-los mudaria o comportamento do vizinho. É o mesmo
critério de ``collector_config``.

**O segredo é write-only.** ``redis_password`` entra em claro uma vez, o servidor
cifra e guarda o ciphertext; a resposta traz ``redis_secret_configured: bool`` e
uma URL mascarada. Nenhum campo desta API devolve o texto claro, e há um teste
que trava isso (``test_enrichment_config_never_returns_secret``).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import redis.asyncio as redis_async
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..collectors.enrich.config_loader import (
    EnrichmentConfigSnapshot,
    invalidate_enrichment_config,
    load_from_db_session,
    parse_redis_url,
)
from ..core import auth as app_auth
from ..core import tenant
from ..core.config import settings
from ..core.errors import ApiError
from ..db import database, models

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/collectors/enrichment/config", tags=["enrichment-config"])

#: Hosts que significam "esta máquina". Comparar strings cruas deixaria
#: ``localhost`` e ``127.0.0.1`` passarem como instâncias diferentes, que é
#: exatamente o engano que o guard existe para pegar.
_LOOPBACK_ALIASES = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}

#: Tetos de sanidade. Não são política de produto — impedem que um dígito a mais
#: num formulário vire um orçamento de 5 minutos por lote (o que, na prática,
#: para a coleta) ou um LRU maior que a memória do container.
_LIMITS: Dict[str, tuple] = {
    "remote_batch_budget_ms": (10, 60_000),
    "cycle_budget_ms": (100, 600_000),
    "l1_max_entries": (100, 1_000_000),
    "singleflight_wait_ms": (0, 5_000),
    "breaker_failure_threshold": (1, 100),
    "breaker_window_s": (10, 86_400),
    "breaker_cooldown_s": (5, 86_400),
    "breaker_max_cooldown_s": (5, 604_800),
    "max_table_bytes": (64 * 1024, 1024 * 1024 * 1024),
    "lru_bytes": (64 * 1024, 4 * 1024 * 1024 * 1024),
    "redis_port": (1, 65_535),
    "redis_db": (0, 15),
}


# ── schemas ─────────────────────────────────────────────────────────────────


class EnrichmentConfigRead(BaseModel):
    """Estado atual. **Nunca** carrega segredo."""

    is_persisted: bool
    config_version: str
    enabled: bool

    redis_host: Optional[str] = None
    redis_port: int
    redis_db: int
    redis_use_tls: bool
    #: Análogo de ``Integration.manager_api_password_configured``.
    redis_secret_configured: bool
    #: ``redis://:***@host:port/db`` — para exibir, nunca para conectar.
    redis_url_masked: Optional[str] = None
    #: ``False`` ⇒ enrichers por lote desligados em TODAS as organizações.
    redis_configured: bool

    remote_batch_budget_ms: int
    cycle_budget_ms: int
    l1_max_entries: int
    singleflight_wait_ms: int

    breaker_failure_threshold: int
    breaker_window_s: int
    breaker_cooldown_s: int
    breaker_max_cooldown_s: int

    max_table_bytes: int
    lru_bytes: int

    #: Enrichers do catálogo que dependem do L2 (``mode="remote"``). A UI usa
    #: para dizer o que exatamente está parado, com nome.
    remote_enrichers: List[str] = Field(default_factory=list)
    #: Segundos de pior caso até TODO worker enxergar uma edição.
    propagation_worst_case_s: int
    #: Diretório do GeoIP e o que existe nele. Somente leitura — ver a docstring
    #: de ``EnrichmentConfig`` para o porquê de não ser editável.
    geoip_dir: Optional[str] = None
    geoip_files: List[Dict[str, Any]] = Field(default_factory=list)

    updated_at: Optional[Any] = None


class EnrichmentConfigUpdate(BaseModel):
    """Update PARCIAL: campo ausente não é tocado.

    ``redis_password`` tem três estados, e a diferença importa:
    ``None`` (ausente) mantém o segredo atual; string vazia **remove**; string
    com conteúdo substitui. É o mesmo contrato de ``SourceUpdate.secret``.
    """

    enabled: Optional[bool] = None

    redis_host: Optional[str] = Field(None, max_length=255)
    redis_port: Optional[int] = None
    redis_db: Optional[int] = None
    redis_use_tls: Optional[bool] = None
    redis_password: Optional[str] = Field(None, max_length=1024)

    remote_batch_budget_ms: Optional[int] = None
    cycle_budget_ms: Optional[int] = None
    l1_max_entries: Optional[int] = None
    singleflight_wait_ms: Optional[int] = None

    breaker_failure_threshold: Optional[int] = None
    breaker_window_s: Optional[int] = None
    breaker_cooldown_s: Optional[int] = None
    breaker_max_cooldown_s: Optional[int] = None

    max_table_bytes: Optional[int] = None
    lru_bytes: Optional[int] = None


class RedisTestRequest(BaseModel):
    """Sonda com valores de RASCUNHO — nada é persistido por este endpoint."""

    redis_host: Optional[str] = Field(None, max_length=255)
    redis_port: int = 6379
    redis_db: int = 0
    redis_use_tls: bool = False
    #: ``None`` ⇒ usa o segredo JÁ GRAVADO (permite testar sem redigitar).
    redis_password: Optional[str] = Field(None, max_length=1024)


class RedisTestResult(BaseModel):
    ok: bool
    message: str
    latency_ms: Optional[float] = None
    #: ``maxmemory-policy`` da instância. Redis gerenciado costuma negar
    #: ``CONFIG GET``; aí vem ``None`` e a UI diz que não deu para ler, em vez
    #: de afirmar que está tudo certo.
    maxmemory_policy: Optional[str] = None
    maxmemory_bytes: Optional[int] = None
    #: ``True`` quando a sonda confirmou que NÃO é a instância do Redis principal.
    distinct_from_main: Optional[bool] = None
    warnings: List[str] = Field(default_factory=list)


# ── helpers ─────────────────────────────────────────────────────────────────


def _bad_request(code: str, detail: str) -> ApiError:
    return ApiError(
        code,
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        messages={"pt": detail, "en": detail, "es": detail},
    )


def _clamp_or_reject(field: str, value: int) -> int:
    low, high = _LIMITS[field]
    if not (low <= int(value) <= high):
        raise _bad_request(
            "enrichment.config_out_of_range",
            f"{field}={value} fora da faixa aceita ({low} a {high}).",
        )
    return int(value)


def _same_instance_as_main(host: str, port: int, db_index: int) -> bool:
    """``True`` quando (host, porta, db) aponta para o Redis PRINCIPAL.

    Comparação estrutural, com normalização de loopback. Não é o guard
    definitivo — dois nomes DNS distintos para o mesmo servidor passam por aqui
    — e por isso a sonda (``test-redis``) compara também o ``run_id`` do
    servidor, que é a checagem que não depende de nome.
    """
    main = parse_redis_url(getattr(settings, "REDIS_URL", "") or "")
    if main is None:
        return False
    main_host, main_port, main_db, _tls, _pw = main

    def norm(h: str) -> str:
        h = (h or "").strip().lower()
        return "localhost" if h in _LOOPBACK_ALIASES else h

    return (
        norm(host) == norm(main_host)
        and int(port) == int(main_port)
        and int(db_index) == int(main_db)
    )


def _remote_enricher_names() -> List[str]:
    """Nomes dos enrichers que param sem o L2. Lidos do REGISTRY, não fixos."""
    try:
        from ..collectors.enrich import enrichers as _plugins  # noqa: F401 (registro)
        from ..collectors.enrich import registry as enrich_registry

        return sorted(
            reg.name
            for reg in enrich_registry.all_registrations()
            if getattr(getattr(reg, "caps", None), "mode", "") == "remote"
        )
    except Exception:  # pragma: no cover — defensivo
        logger.debug("enrich_config: falha ao listar enrichers remotos", exc_info=True)
        return []


def _geoip_state() -> tuple:
    """``(diretório, [arquivos])``. Só leitura — a UI mostra o que o worker vê.

    A API roda em container distinto do worker no compose de produção, então
    esta listagem descreve o que a **API** enxerga. Vem com o caminho junto
    justamente para que a diferença seja visível, em vez de virar uma afirmação
    sobre um diretório que quem lê não sabe qual é.
    """
    from pathlib import Path

    directory = getattr(settings, "ENRICH_GEOIP_DIR", "") or ""
    out: List[Dict[str, Any]] = []
    if not directory:
        return None, out
    try:
        base = Path(directory)
        if base.is_dir():
            for item in sorted(base.glob("*.mmdb")):
                stat = item.stat()
                out.append(
                    {
                        "name": item.name,
                        "size_bytes": int(stat.st_size),
                        "modified_at": stat.st_mtime,
                    }
                )
    except Exception:  # pragma: no cover — defensivo
        logger.debug("enrich_config: falha ao listar %s", directory, exc_info=True)
    return directory, out


def _to_read(
    snapshot: EnrichmentConfigSnapshot, row: Optional[models.EnrichmentConfig]
) -> EnrichmentConfigRead:
    from ..collectors.enrich.config_loader import PROPAGATION_WORST_CASE_S

    geoip_dir, geoip_files = _geoip_state()
    return EnrichmentConfigRead(
        is_persisted=snapshot.is_persisted,
        config_version=snapshot.config_version,
        enabled=snapshot.enabled,
        redis_host=snapshot.redis_host,
        redis_port=snapshot.redis_port,
        redis_db=snapshot.redis_db,
        redis_use_tls=snapshot.redis_use_tls,
        redis_secret_configured=bool(snapshot.redis_secret_ref),
        redis_url_masked=snapshot.redis_url_masked(),
        redis_configured=snapshot.redis_configured,
        remote_batch_budget_ms=snapshot.remote_batch_budget_ms,
        cycle_budget_ms=snapshot.cycle_budget_ms,
        l1_max_entries=snapshot.l1_max_entries,
        singleflight_wait_ms=snapshot.singleflight_wait_ms,
        breaker_failure_threshold=snapshot.breaker_failure_threshold,
        breaker_window_s=snapshot.breaker_window_s,
        breaker_cooldown_s=snapshot.breaker_cooldown_s,
        breaker_max_cooldown_s=snapshot.breaker_max_cooldown_s,
        max_table_bytes=snapshot.max_table_bytes,
        lru_bytes=snapshot.lru_bytes,
        remote_enrichers=_remote_enricher_names(),
        propagation_worst_case_s=int(PROPAGATION_WORST_CASE_S),
        geoip_dir=geoip_dir,
        geoip_files=geoip_files,
        updated_at=getattr(row, "updated_at", None),
    )


def _get_or_create_row(db: Session) -> models.EnrichmentConfig:
    """A linha id=1, criada na hora se a migração ainda não a semeou.

    Instalação que subiu antes desta feature tem a tabela (``create_all``) e
    nenhuma linha. Criar aqui, a partir do snapshot de env, é o que faz o
    primeiro PUT do console gravar por cima do ``.env`` em vez de falhar.
    """
    row = db.query(models.EnrichmentConfig).filter_by(id=1).first()
    if row is not None:
        return row
    from ..collectors.enrich.config_loader import _snapshot_from_env

    seed = _snapshot_from_env()
    row = models.EnrichmentConfig(
        id=1,
        enabled=seed.enabled,
        redis_host=seed.redis_host,
        redis_port=seed.redis_port,
        redis_db=seed.redis_db,
        redis_use_tls=seed.redis_use_tls,
        redis_secret_ref=seed.redis_secret_ref,
        remote_batch_budget_ms=seed.remote_batch_budget_ms,
        cycle_budget_ms=seed.cycle_budget_ms,
        l1_max_entries=seed.l1_max_entries,
        singleflight_wait_ms=seed.singleflight_wait_ms,
        breaker_failure_threshold=seed.breaker_failure_threshold,
        breaker_window_s=seed.breaker_window_s,
        breaker_cooldown_s=seed.breaker_cooldown_s,
        breaker_max_cooldown_s=seed.breaker_max_cooldown_s,
        max_table_bytes=seed.max_table_bytes,
        lru_bytes=seed.lru_bytes,
    )
    db.add(row)
    db.flush()
    return row


async def _main_redis_run_id() -> Optional[str]:
    """``run_id`` do Redis principal, ou ``None`` se não der para ler.

    É o que permite dizer "é a MESMA instância" mesmo quando os nomes diferem
    (dois DNS, um alias de rede do compose, um proxy). Sem ele restaria a
    comparação por host, que um alias derrota.
    """
    url = getattr(settings, "REDIS_URL", "") or ""
    if not url:
        return None
    client = None
    try:
        client = redis_async.from_url(url, decode_responses=True, socket_timeout=3)
        info = await client.info("server")
        return str(info.get("run_id") or "") or None
    except Exception:
        return None
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # pragma: no cover — defensivo
                pass


async def _redis_client_for_invalidate() -> Optional[redis_async.Redis]:
    url = getattr(settings, "REDIS_URL", "") or ""
    if not url:
        return None
    try:
        return redis_async.from_url(url, decode_responses=True)
    except Exception:  # pragma: no cover — defensivo
        return None


# ── GET ─────────────────────────────────────────────────────────────────────


@router.get("", response_model=EnrichmentConfigRead)
def get_config(
    user: models.AppUser = Depends(app_auth.require_admin_user),
    db: Session = Depends(database.get_session),
) -> EnrichmentConfigRead:
    """Estado real do banco. Não passa pelo cache — é tela de administração."""
    tenant.require_global_scope(user)
    row = db.query(models.EnrichmentConfig).filter_by(id=1).first()
    snapshot = load_from_db_session(db)
    return _to_read(snapshot, row)


# ── PUT ─────────────────────────────────────────────────────────────────────


@router.put("", response_model=EnrichmentConfigRead)
async def update_config(
    payload: EnrichmentConfigUpdate,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    db: Session = Depends(database.get_session),
) -> EnrichmentConfigRead:
    """Update parcial + invalidação do cache. Workers refletem em segundos."""
    tenant.require_global_scope(user)
    data = payload.model_dump(exclude_unset=True)
    row = _get_or_create_row(db)

    password = data.pop("redis_password", None)

    for field, value in data.items():
        if value is None:
            continue
        if field in _LIMITS:
            value = _clamp_or_reject(field, value)
        if field == "redis_host":
            value = (value or "").strip() or None
        setattr(row, field, value)

    # O guard de instância distinta roda sobre o estado FINAL, não sobre o
    # payload: mudar só a porta para colidir com o Redis principal é tão
    # perigoso quanto mudar o host, e um check sobre o que veio na requisição
    # não enxergaria isso.
    host_final = (row.redis_host or "").strip()
    if host_final and _same_instance_as_main(
        host_final, int(row.redis_port), int(row.redis_db)
    ):
        raise ApiError(
            "enrichment.config_redis_same_as_main",
            status.HTTP_409_CONFLICT,
            messages={
                "pt": (
                    "Este endereço é o mesmo do Redis principal. O cache de "
                    "enriquecimento precisa de uma instância DEDICADA: no "
                    "principal, o dedupe roda sob volatile-lru e a evicção "
                    "silenciosa reaparece como reentrega no SIEM. Banco lógico "
                    "diferente não isola memória."
                ),
                "en": (
                    "This address is the main Redis instance. The enrichment "
                    "cache needs a DEDICATED one: on the main instance dedupe "
                    "runs under volatile-lru and silent eviction comes back as "
                    "redelivery to the SIEM. A separate logical DB does not "
                    "isolate memory."
                ),
                "es": (
                    "Esta dirección es la del Redis principal. La caché de "
                    "enriquecimiento necesita una instancia DEDICADA: en la "
                    "principal el dedupe corre bajo volatile-lru y la expulsión "
                    "silenciosa reaparece como reentrega en el SIEM."
                ),
            },
        )

    if password is not None:
        if password == "":
            row.redis_secret_ref = None
        else:
            from ..core import secrets as secrets_mod

            row.redis_secret_ref = secrets_mod.get_default_backend().encrypt(password)

    row.updated_by_user_id = app_auth.persistable_user_id(user)
    db.commit()
    db.refresh(row)

    client = await _redis_client_for_invalidate()
    try:
        await invalidate_enrichment_config(client)
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # pragma: no cover — defensivo
                pass

    snapshot = load_from_db_session(db)
    logger.info(
        "enrichment_config: atualizada por admin; version=%s remoto=%s",
        snapshot.config_version,
        "on" if snapshot.redis_configured else "off",
    )
    return _to_read(snapshot, row)


# ── sonda do cache L2 ───────────────────────────────────────────────────────


@router.post("/test-redis", response_model=RedisTestResult)
async def test_redis(
    payload: RedisTestRequest,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    db: Session = Depends(database.get_session),
) -> RedisTestResult:
    """Conecta de verdade e devolve o erro do servidor, sem gravar nada.

    Ordem das checagens é deliberada: primeiro a conexão (é o que o operador
    quer saber), depois a identidade da instância, depois a política de memória.
    Cada uma degrada sozinha — ``CONFIG GET`` negado num Redis gerenciado vira
    um aviso, não uma reprovação, porque o cache funciona sem que a gente
    consiga ler a política.
    """
    import time

    tenant.require_global_scope(user)

    host = (payload.redis_host or "").strip()
    if not host:
        raise _bad_request(
            "enrichment.config_redis_host_required",
            "Informe o endereço do Redis dedicado antes de testar.",
        )
    for field, value in (
        ("redis_port", payload.redis_port),
        ("redis_db", payload.redis_db),
    ):
        _clamp_or_reject(field, value)

    warnings: List[str] = []

    # Senha: rascunho quando veio no corpo; caso contrário a JÁ GRAVADA. Nunca
    # devolvida, em nenhum ramo.
    password = payload.redis_password
    if password is None:
        row = db.query(models.EnrichmentConfig).filter_by(id=1).first()
        ref = getattr(row, "redis_secret_ref", None)
        if ref:
            try:
                from ..core import secrets as secrets_mod

                password = secrets_mod.get_default_backend().decrypt(ref)
            except Exception:
                warnings.append(
                    "A senha gravada não pôde ser decifrada; o teste seguiu sem "
                    "autenticação. Regrave a senha."
                )
                password = None

    from urllib.parse import quote

    scheme = "rediss" if payload.redis_use_tls else "redis"
    auth = f":{quote(password, safe='')}@" if password else ""
    url = f"{scheme}://{auth}{host}:{int(payload.redis_port)}/{int(payload.redis_db)}"

    structurally_same = _same_instance_as_main(
        host, int(payload.redis_port), int(payload.redis_db)
    )

    client = None
    started = time.monotonic()
    try:
        client = redis_async.from_url(
            url, decode_responses=True, socket_timeout=5, socket_connect_timeout=5
        )
        await client.ping()
        latency_ms = (time.monotonic() - started) * 1000.0
    except Exception as exc:
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # pragma: no cover — defensivo
                pass
        # A mensagem do servidor é o que permite agir (auth, DNS, recusa de
        # conexão). Truncada para não virar parede de texto na tela.
        detail = str(exc)[:300] or exc.__class__.__name__
        return RedisTestResult(
            ok=False,
            message=f"Não foi possível conectar: {detail}",
            distinct_from_main=(False if structurally_same else None),
            warnings=warnings,
        )

    maxmemory_policy: Optional[str] = None
    maxmemory_bytes: Optional[int] = None
    distinct: Optional[bool] = not structurally_same

    try:
        # Identidade REAL da instância. Vence alias de DNS, que a comparação por
        # host não vence.
        info = await client.info("server")
        candidate_run_id = str(info.get("run_id") or "") or None
        main_run_id = await _main_redis_run_id()
        if candidate_run_id and main_run_id:
            distinct = candidate_run_id != main_run_id
        elif main_run_id is None:
            warnings.append(
                "Não foi possível ler a identidade do Redis principal para "
                "comparar; a checagem usou apenas endereço e porta."
            )
    except Exception:
        warnings.append(
            "Não foi possível ler INFO server desta instância; a checagem de "
            "instância distinta usou apenas endereço e porta."
        )

    try:
        cfg = await client.config_get("maxmemory-policy")
        maxmemory_policy = (cfg or {}).get("maxmemory-policy") or None
        mem = await client.config_get("maxmemory")
        raw_mem = (mem or {}).get("maxmemory")
        maxmemory_bytes = int(raw_mem) if raw_mem not in (None, "") else None
    except Exception:
        warnings.append(
            "Este servidor não permite CONFIG GET, então não deu para conferir "
            "a política de memória. O cache funciona; só não dá para validar "
            "daqui que a política é adequada."
        )

    try:
        await client.aclose()
    except Exception:  # pragma: no cover — defensivo
        pass

    if distinct is False:
        return RedisTestResult(
            ok=False,
            message=(
                "Conectou, mas é a MESMA instância do Redis principal. O cache "
                "de enriquecimento precisa de uma dedicada: no principal o "
                "dedupe roda sob volatile-lru e a evicção silenciosa reaparece "
                "como reentrega no SIEM."
            ),
            latency_ms=latency_ms,
            maxmemory_policy=maxmemory_policy,
            maxmemory_bytes=maxmemory_bytes,
            distinct_from_main=False,
            warnings=warnings,
        )

    if maxmemory_policy and maxmemory_policy.startswith("volatile"):
        warnings.append(
            f"A política de memória é {maxmemory_policy}. Para um cache com TTL "
            "em todas as chaves, allkeys-lru aproveita melhor a memória."
        )
    if maxmemory_bytes == 0:
        warnings.append(
            "maxmemory está em 0 (sem teto): o cache pode crescer até consumir "
            "a memória do host. Defina um teto, por exemplo 256mb."
        )

    return RedisTestResult(
        ok=True,
        message=f"Conectado. PONG em {latency_ms:.1f} ms.",
        latency_ms=latency_ms,
        maxmemory_policy=maxmemory_policy,
        maxmemory_bytes=maxmemory_bytes,
        distinct_from_main=distinct,
        warnings=warnings,
    )
