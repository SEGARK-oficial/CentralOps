"""Router para saúde do pipeline de normalização por integration.

Endpoint novo — completamente separado de
``routers/integrations.py`` para não inflar aquele módulo.
NÃO executa chamadas live ao vendor; agrega métricas persistidas
em CollectionState, UnknownField, QuarantineEvent e MappingDefinition.

Endpoints expostos:
- ``GET /api/integrations/{integration_id}/pipeline-health``
- ``GET /api/integrations/pipeline-health``  (bulk, todos acessíveis ao user)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import List, Literal, Optional

import redis.asyncio as redis_async
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core import auth as app_auth
from ..core import tenant
from ..core.config import settings
from ..core.errors import ApiError
from ..db import database, models

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/integrations", tags=["pipeline-health"])

# TTL do cache em segundos
_CACHE_TTL = 60
# Janela do snapshot para cálculo de events_per_minute
_SNAPSHOT_TTL = 300  # 5 minutos


# ── Schemas ───────────────────────────────────────────────────────────


class IntegrationPipelineHealth(BaseModel):
    """Saúde do pipeline de normalização de uma integration.

    Todos os campos exceto ``integration_id``, ``status``,
    ``drift_count_24h``, ``quarantine_count_24h`` e ``cached_at``
    podem ser ``None`` quando dados insuficientes estão disponíveis.

    ``events_per_minute`` é aproximado — baseado em deltas de contadores
    cumulativos (``CollectionState.events_collected_total``) comparados
    com snapshot Redis 5 min atrás. Não representa eventos/min exatos
    em cenários de restart do worker ou reset de contador.
    """

    integration_id: int
    status: Literal["healthy", "degraded", "unhealthy", "unknown"]
    events_per_minute: Optional[float]
    lag_seconds: Optional[int]
    last_error: Optional[str]
    last_success_at: Optional[datetime]
    mapped_field_ratio: Optional[float]
    drift_count_24h: int
    quarantine_count_24h: int
    cached_at: datetime


class BulkPipelineHealthResponse(BaseModel):
    """Resposta do endpoint bulk de pipeline-health."""

    items: List[IntegrationPipelineHealth]
    total: int
    cached_at: datetime


# ── Lógica pura (sem cache — testável isolada) ────────────────────────


def _determine_status(
    last_success_at: Optional[datetime],
    lag_seconds: Optional[int],
    consecutive_failures_max: int,
    last_error: Optional[str],
) -> Literal["healthy", "degraded", "unhealthy", "unknown"]:
    """Determina status de saúde a partir dos indicadores do pipeline.

    Regras (em ordem de prioridade):
    1. ``unknown`` — nunca coletou (last_success_at IS NULL).
    2. ``unhealthy`` — lag > 300s OU consecutive_failures >= 3.
    3. ``degraded`` — last_error presente e lag <= 300.
    4. ``healthy`` — caso contrário.
    """
    if last_success_at is None:
        return "unknown"
    if lag_seconds is not None and lag_seconds > 300:
        return "unhealthy"
    if consecutive_failures_max >= 3:
        return "unhealthy"
    if last_error:
        return "degraded"
    return "healthy"


def compute_pipeline_health(
    db: Session,
    integration_id: int,
    *,
    events_per_minute: Optional[float] = None,
    cached_at: Optional[datetime] = None,
) -> IntegrationPipelineHealth:
    """Computa métricas de saúde do pipeline a partir do banco.

    Realiza até 4 queries SQL separadas. Simplificação:
    4 queries sequenciais é aceitável para <= 50 integrations.

    ``events_per_minute`` é injetado externamente porque exige estado
    Redis (snapshot 5 min) — mantém a função testável sem mock Redis.

    ``cached_at`` é o timestamp de quando este resultado foi produzido.
    Quando ``None``, usa ``datetime.utcnow()``.
    """
    now = datetime.utcnow()
    ts_cached_at = cached_at or now

    # ── Query 1: CollectionState ─────────────────────────────────────
    # Agrega todos os streams da integration de uma só vez.
    states = (
        db.execute(
            select(
                func.max(models.CollectionState.last_success_at).label("max_success"),
                func.max(models.CollectionState.last_attempt_at).label("max_attempt"),
                func.max(models.CollectionState.consecutive_failures).label("max_failures"),
            ).where(models.CollectionState.integration_id == integration_id)
        ).one()
    )

    # Stream com last_attempt_at mais recente para capturar o último erro.
    last_error_row = db.execute(
        select(models.CollectionState.last_error).where(
            models.CollectionState.integration_id == integration_id,
            models.CollectionState.last_attempt_at.isnot(None),
        ).order_by(models.CollectionState.last_attempt_at.desc()).limit(1)
    ).scalar_one_or_none()

    max_success: Optional[datetime] = states.max_success
    max_failures: int = states.max_failures or 0

    # Lag em segundos: now - max(last_success_at)
    lag_seconds: Optional[int] = None
    if max_success is not None:
        delta = now - max_success
        lag_seconds = max(0, int(delta.total_seconds()))

    # Trunca erro em 500 chars
    last_error: Optional[str] = None
    if last_error_row:
        last_error = str(last_error_row)[:500]

    # ── Query 2: Mappings da integration — mapped_field_ratio ─────────
    # Busca (vendor, event_type) dos mappings que pertencem à integration.
    # A FK integration→mapping não existe diretamente; a relação é via
    # CollectionState.stream que mapeia para (vendor, event_type) do
    # MappingDefinition. Aproximação via vendor da
    # integration (platform) — todas as MappingDefinitions cujo vendor
    # bate com a platform da integration são candidatas.
    #
    # Abordagem mais precisa exigiria tabela de associação integration↔mapping.
    # Aqui usamos todos os MappingDefinitions com current_version
    # para calcular total_known_paths.
    #
    # TODO: criar tabela integration_mapping_bindings para
    # associar explicitamente uma integration aos seus (vendor, event_type).

    integration = db.get(models.Integration, integration_id)
    if integration is None:
        # Não deve ocorrer — caller já validou, mas defensivo
        raise ApiError(
            "integration.not_found",
            404,
            messages={
                "pt": "Integração não encontrada.",
                "en": "Integration not found.",
                "es": "Integración no encontrada.",
            },
        )

    vendor = integration.platform  # "sophos" | "wazuh" | "microsoft_defender" etc.

    # Busca MappingDefinitions com current_version para o vendor
    mapping_rows = db.execute(
        select(
            models.MappingDefinition.id,
            models.MappingDefinition.current_version_id,
        ).where(
            models.MappingDefinition.vendor == vendor,
            models.MappingDefinition.current_version_id.isnot(None),
        )
    ).all()

    mapping_ids = [row.id for row in mapping_rows]
    version_ids = [row.current_version_id for row in mapping_rows]

    total_known_paths: int = 0
    if version_ids:
        # Conta total de regras nas versões correntes
        version_rows = db.execute(
            select(models.MappingVersion.rules).where(
                models.MappingVersion.id.in_(version_ids)
            )
        ).scalars().all()
        for rules_json in version_rows:
            try:
                rules = json.loads(rules_json)
            except (ValueError, TypeError):
                continue
            # A DSL tem duas formas (engine.compile_rules): v1 = lista de regras;
            # v2 = dict {"preprocess": [...], "rules": [...]}. Antes só a v1 (list)
            # era contada → toda versão v2 (o default do editor, dsl_version=2)
            # somava 0 e mapped_field_ratio virava None permanentemente. Conta as
            # regras nas DUAS formas (v2 = payload["rules"], igual a _compile_v2).
            if isinstance(rules, list):
                total_known_paths += len(rules)
            elif isinstance(rules, dict):
                rule_list = rules.get("rules")
                if isinstance(rule_list, list):
                    total_known_paths += len(rule_list)

    mapped_field_ratio: Optional[float] = None
    drift_count_24h: int = 0

    if mapping_ids:
        cutoff_24h = now - timedelta(hours=24)

        # ── Query 3: UnknownField (drift_count_24h) ──────────────────
        # Usa índice idx_unknown_fields_lookup (vendor, event_type, last_seen).
        # Filtra por (vendor, event_type) dos mappings da integration.
        vendor_event_pairs = db.execute(
            select(
                models.MappingDefinition.vendor,
                models.MappingDefinition.event_type,
            ).where(models.MappingDefinition.id.in_(mapping_ids))
        ).all()

        if vendor_event_pairs:
            drift_count_24h = 0
            for row in vendor_event_pairs:
                count = db.execute(
                    select(func.count(models.UnknownField.id)).where(
                        models.UnknownField.vendor == row.vendor,
                        models.UnknownField.event_type == row.event_type,
                        # Escopa o count à org da integração —
                        # senão soma drift de TODOS os tenants do mesmo vendor
                        # (side-channel de contagem cross-tenant).
                        models.UnknownField.organization_id == integration.organization_id,
                        models.UnknownField.last_seen > cutoff_24h,
                        models.UnknownField.status == "new",
                    )
                ).scalar_one()
                drift_count_24h += count

        # mapped_field_ratio = conhecidos / (conhecidos + desconhecidos).
        #
        # A forma anterior era `1 - desconhecidos/conhecidos`, que não é uma
        # proporção: o quociente passa de 1 assim que há mais paths novos do que
        # regras, e o clamp então PRENDE o indicador em 0 — a partir daí ele para
        # de distinguir "10 campos novos" de "500". Como proporção real, o valor
        # cai suavemente e nunca precisa de clamp.
        #
        # Isto também absorve o degrau esperado da correção do detector de drift:
        # o diff passou a ser por PATH em vez de por chave de topo, então
        # drift_count_24h sobe de uma ordem de grandeza sem que nada tenha
        # piorado no pipeline. Na forma antiga esse degrau zerava o KPI aqui, o
        # "Mapping coverage" do dashboard (routers/dashboard.py) e o mesmo campo
        # exposto pelo servidor MCP.
        denominator = total_known_paths + drift_count_24h
        if denominator > 0:
            mapped_field_ratio = total_known_paths / denominator
        # Se não há regra nem drift: nenhum mapping configurado → None

    # ── Query 4: QuarantineEvent (quarantine_count_24h) ───────────────
    cutoff_24h = now - timedelta(hours=24)
    quarantine_count_24h: int = db.execute(
        select(func.count(models.QuarantineEvent.id)).where(
            models.QuarantineEvent.integration_id == integration_id,
            models.QuarantineEvent.created_at > cutoff_24h,
        )
    ).scalar_one()

    # ── Determina status ──────────────────────────────────────────────
    health_status = _determine_status(
        last_success_at=max_success,
        lag_seconds=lag_seconds,
        consecutive_failures_max=max_failures,
        last_error=last_error,
    )

    return IntegrationPipelineHealth(
        integration_id=integration_id,
        status=health_status,
        events_per_minute=events_per_minute,
        lag_seconds=lag_seconds,
        last_error=last_error,
        last_success_at=max_success,
        mapped_field_ratio=mapped_field_ratio,
        drift_count_24h=drift_count_24h,
        quarantine_count_24h=quarantine_count_24h,
        cached_at=ts_cached_at,
    )


# ── Cache Redis ───────────────────────────────────────────────────────


def _cache_key(integration_id: int) -> str:
    return f"pipeline_health:{integration_id}"


def _snapshot_key(integration_id: int) -> str:
    return f"pipeline_events_snapshot:{integration_id}"


def _bulk_cache_key(user_id: int) -> str:
    return f"pipeline_health_bulk:{user_id}"


async def _get_events_per_minute(
    redis: redis_async.Redis,
    db: Session,
    integration_id: int,
) -> Optional[float]:
    """Calcula events_per_minute usando snapshot Redis de 5 min atrás.

    Aproximação baseada em deltas de ``CollectionState.events_collected_total``.
    Se snapshot não existe (primeira chamada), salva snapshot e retorna None.
    Contadores reiniciados pelo worker podem produzir valor negativo — nesse
    caso retorna None (não soma lixo).
    """
    snapshot_key = _snapshot_key(integration_id)
    now_ts = datetime.utcnow().timestamp()

    # Busca total atual de eventos coletados
    current_total: int = db.execute(
        select(func.sum(models.CollectionState.events_collected_total)).where(
            models.CollectionState.integration_id == integration_id
        )
    ).scalar_one() or 0

    raw = await redis.get(snapshot_key)
    if raw is None:
        # Primeira chamada — salva snapshot e retorna None
        snapshot = json.dumps({"total": current_total, "ts": now_ts})
        await redis.set(snapshot_key, snapshot, ex=_SNAPSHOT_TTL + 30)
        return None

    try:
        data = json.loads(raw)
        prev_total: int = int(data["total"])
        prev_ts: float = float(data["ts"])
    except (ValueError, KeyError, TypeError):
        # Snapshot corrompido — descarta e recomeça
        await redis.delete(snapshot_key)
        return None

    delta_events = current_total - prev_total
    delta_seconds = now_ts - prev_ts

    if delta_events < 0 or delta_seconds <= 0:
        # Contador resetado ou clock inconsistente
        return None

    if delta_seconds < 10:
        # Janela muito curta — pouca confiabilidade
        return None

    return round((delta_events / delta_seconds) * 60, 2)


async def get_cached_pipeline_health(
    db: Session,
    integration_id: int,
    redis: redis_async.Redis,
) -> IntegrationPipelineHealth:
    """Retorna saúde do pipeline com cache Redis de 60s.

    Cache miss → computa via ``compute_pipeline_health``, persiste no
    Redis com TTL 60s, retorna. ``cached_at`` indica quando foi computado.
    """
    cache_key = _cache_key(integration_id)
    raw = await redis.get(cache_key)

    if raw is not None:
        try:
            data = json.loads(raw)
            return IntegrationPipelineHealth(**data)
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning(
                "pipeline_health cache inválido integration=%s: %s",
                integration_id,
                exc,
            )

    # Cache miss — calcula events_per_minute antes de compute (requer Redis)
    epm = await _get_events_per_minute(redis, db, integration_id)

    cached_at = datetime.utcnow()
    result = compute_pipeline_health(
        db,
        integration_id,
        events_per_minute=epm,
        cached_at=cached_at,
    )

    logger.info(
        "pipeline_health computed integration=%s status=%s lag_s=%s drift_24h=%s quarantine_24h=%s",
        integration_id,
        result.status,
        result.lag_seconds,
        result.drift_count_24h,
        result.quarantine_count_24h,
    )

    # Persiste no cache
    try:
        payload = result.model_dump(mode="json")
        # datetime → ISO string para serialização
        for key in ("last_success_at", "cached_at"):
            if isinstance(payload.get(key), datetime):
                payload[key] = payload[key].isoformat()
        await redis.set(cache_key, json.dumps(payload), ex=_CACHE_TTL)
    except Exception as exc:  # noqa: BLE001 — falha de cache nunca bloqueia resposta
        logger.warning("pipeline_health falha ao escrever cache integration=%s: %s", integration_id, exc)

    return result


def _get_redis() -> redis_async.Redis:
    """Dependency FastAPI: cria cliente Redis."""
    return redis_async.from_url(
        settings.REDIS_URL or "redis://localhost:6379/0",
        decode_responses=True,
    )


# ── Endpoints ─────────────────────────────────────────────────────────


@router.get(
    "/pipeline-health",
    response_model=BulkPipelineHealthResponse,
    summary="Saúde do pipeline de normalização — todas as integrations acessíveis",
)
async def get_bulk_pipeline_health(
    db: Session = Depends(database.get_session),
    current_user: models.AppUser = Depends(
        app_auth.require_permission(app_auth.Permission.INTEGRATION_READ)
    ),
) -> BulkPipelineHealthResponse:
    """Lista saúde do pipeline de todas as integrations que o user tem acesso.

    Admin vê todas; non-admin vê apenas as da própria org.
    Cache 60s por user (chave ``pipeline_health_bulk:{user_id}``).
    """
    redis = _get_redis()
    try:
        # Cache bulk por user
        bulk_key = _bulk_cache_key(int(current_user.id))
        raw = await redis.get(bulk_key)
        if raw is not None:
            try:
                data = json.loads(raw)
                return BulkPipelineHealthResponse(**data)
            except (ValueError, KeyError, TypeError) as exc:
                logger.warning("pipeline_health bulk cache inválido user=%s: %s", current_user.id, exc)

        # Busca integrations acessíveis ao user
        query = select(models.Integration.id)
        org_ids = tenant.accessible_org_ids(current_user, db)
        if org_ids is not None:
            query = query.where(models.Integration.organization_id.in_(org_ids))

        integration_ids = db.execute(query).scalars().all()

        # Simplificação: serial para <= 50 integrations
        items: List[IntegrationPipelineHealth] = []
        for iid in integration_ids:
            try:
                health = await get_cached_pipeline_health(db, iid, redis)
                items.append(health)
            except (HTTPException, ApiError):
                pass  # Integration não encontrada ou sem acesso — pula silenciosamente

        cached_at = datetime.utcnow()
        response = BulkPipelineHealthResponse(
            items=items,
            total=len(items),
            cached_at=cached_at,
        )

        # Persiste cache bulk
        try:
            payload = response.model_dump(mode="json")
            await redis.set(bulk_key, json.dumps(payload), ex=_CACHE_TTL)
        except Exception as exc:  # noqa: BLE001
            logger.warning("pipeline_health bulk falha cache user=%s: %s", current_user.id, exc)

        return response
    finally:
        await redis.aclose()


@router.get(
    "/{integration_id}/pipeline-health",
    response_model=IntegrationPipelineHealth,
    summary="Saúde do pipeline de normalização de uma integration",
)
async def get_pipeline_health(
    integration_id: int,
    db: Session = Depends(database.get_session),
    current_user: models.AppUser = Depends(
        app_auth.require_permission(app_auth.Permission.INTEGRATION_READ)
    ),
) -> IntegrationPipelineHealth:
    """Agrega métricas do pipeline de normalização sem chamadas live ao vendor.

    Cache Redis de 60s. ``cached_at`` indica quando foi computado.
    Admin vê qualquer integration; non-admin só da própria org (403 caso contrário).
    """
    # Valida existência e acesso antes de tocar Redis
    integration = db.get(models.Integration, integration_id)
    if integration is None:
        raise ApiError(
            "integration.not_found",
            status.HTTP_404_NOT_FOUND,
            messages={
                "pt": "Integração não encontrada.",
                "en": "Integration not found.",
                "es": "Integración no encontrada.",
            },
        )

    # Verifica scope de org (admin passa direto; non-admin verifica org_id)
    tenant.require_subtree_access(current_user, int(integration.organization_id))

    redis = _get_redis()
    try:
        return await get_cached_pipeline_health(db, integration_id, redis)
    finally:
        await redis.aclose()
