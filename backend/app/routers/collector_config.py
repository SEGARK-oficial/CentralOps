"""REST endpoints para a configuração runtime do Collector.

Endpoints (todos admin-only):

- ``GET /api/collectors/config`` — retorna snapshot atual + meta
  (``is_persisted``, ``config_version``).
- ``PUT /api/collectors/config`` — update parcial; invalida cache Redis
  para propagar aos workers em até 30s.
- ``POST /api/collectors/config/test`` — valida destino atual rodando
  probe real (Syslog TCP/TLS + JSONL write) conforme ``dispatch_mode``.

Gerencia os mesmos parâmetros que o ``.env`` segue — mas com UI, sem
restart de container e com teste ao vivo.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import ssl
from datetime import datetime
from typing import Any, Dict, Optional

import redis.asyncio as redis_async
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from ..api import schemas
from ..core import auth as app_auth
from ..core import tenant
from ..core.config import settings
from ..core.errors import ApiError
from ..db import database, models, repository
from ..collectors import capture_session
from ..collectors.audit_buffer import clear as audit_clear, read_recent as audit_read_recent
from ..collectors.config_loader import (
    CollectorConfigSnapshot,
    invalidate_collector_config,
    load_from_db_session,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/collectors/config", tags=["collector-config"])


# ── Helpers ───────────────────────────────────────────────────────────


def _get_repo(db: Session = Depends(database.get_session)) -> repository.CollectorConfigRepository:
    return repository.CollectorConfigRepository(db)


def _snapshot_to_read(snapshot: CollectorConfigSnapshot) -> schemas.CollectorConfigRead:
    return schemas.CollectorConfigRead(
        id=1,
        is_persisted=snapshot.is_persisted,
        config_version=snapshot.config_version,
        wazuh_syslog_host=snapshot.wazuh_syslog_host,
        wazuh_syslog_port=snapshot.wazuh_syslog_port,
        wazuh_syslog_use_tls=snapshot.wazuh_syslog_use_tls,
        wazuh_ca_bundle=snapshot.wazuh_ca_bundle,
        wazuh_dispatch_mode=snapshot.wazuh_dispatch_mode,
        wazuh_syslog_format=snapshot.wazuh_syslog_format,
        collector_jsonl_dir=snapshot.collector_jsonl_dir,
        collector_batch_size=snapshot.collector_batch_size,
        collector_batch_flush_seconds=snapshot.collector_batch_flush_seconds,
        dedupe_ttl_days=snapshot.dedupe_ttl_days,
        domain_concurrency_limits=snapshot.domain_concurrency_limits,
        rate_limits_by_vendor=snapshot.rate_limits_by_vendor,
        updated_at=None,  # preenchido abaixo se tiver row
    )


async def _redis_client() -> redis_async.Redis:
    return redis_async.from_url(
        settings.REDIS_URL or "redis://localhost:6379/0",
        decode_responses=True,
    )


# ── GET ───────────────────────────────────────────────────────────────


@router.get("", response_model=schemas.CollectorConfigRead)
def get_config(
    _: models.AppUser = Depends(app_auth.require_admin_user),
    db: Session = Depends(database.get_session),
    repo: repository.CollectorConfigRepository = Depends(_get_repo),
) -> schemas.CollectorConfigRead:
    """Lê config atual. Não usa cache Redis aqui (é admin UI — quer estado real do DB)."""
    row = repo.get()
    snapshot = load_from_db_session(db)
    result = _snapshot_to_read(snapshot)
    if row is not None:
        result.updated_at = row.updated_at
    return result


# ── PUT ───────────────────────────────────────────────────────────────


@router.put("", response_model=schemas.CollectorConfigRead)
async def update_config(
    payload: schemas.CollectorConfigUpdate,
    current_user: models.AppUser = Depends(app_auth.require_admin_user),
    db: Session = Depends(database.get_session),
    repo: repository.CollectorConfigRepository = Depends(_get_repo),
) -> schemas.CollectorConfigRead:
    """Partial update + invalida cache Redis. Workers reflete em até 30s."""
    # Config do coletor é PLATAFORMA (rate limits, dedupe TTL, concorrência —
    # vale p/ todos os tenants) — só admin global.
    tenant.require_global_scope(current_user)
    data = payload.model_dump(exclude_unset=True)
    row = repo.update(**data)

    # Invalida cache best-effort.
    redis = await _redis_client()
    try:
        await invalidate_collector_config(redis)
    finally:
        await redis.aclose()

    snapshot = load_from_db_session(db)
    result = _snapshot_to_read(snapshot)
    result.updated_at = row.updated_at
    logger.info(
        "collector_config: updated by admin; version=%s",
        snapshot.config_version,
    )
    return result


# ── POST /test ────────────────────────────────────────────────────────


def _error_details(exc: BaseException) -> Dict[str, Any]:
    """Sanitiza exceção para o response — sem stack trace completo."""
    return {
        "error_class": type(exc).__name__,
        "reason": str(exc)[:300],
    }


async def _test_syslog(snapshot: CollectorConfigSnapshot) -> schemas.CollectorConfigTestResult:
    """Abre conexão TCP (+TLS opcional), envia probe RFC 5424, fecha."""
    if not snapshot.wazuh_syslog_host:
        return schemas.CollectorConfigTestResult(
            component="syslog",
            status="error",
            details={"reason": "wazuh_syslog_host não configurado"},
        )

    # Valida CA bundle antes de tentar conectar (evita erro confuso do TLS).
    ssl_ctx = None
    if snapshot.wazuh_syslog_use_tls:
        try:
            ssl_ctx = (
                ssl.create_default_context(cafile=snapshot.wazuh_ca_bundle)
                if snapshot.wazuh_ca_bundle
                else ssl.create_default_context()
            )
            ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        except (FileNotFoundError, ssl.SSLError) as exc:
            # Não retornar o caminho absoluto do CA bundle no body (vaza
            # layout interno do filesystem do container). O log do servidor
            # captura para o operador.
            logger.warning(
                "collector test: invalid CA bundle path=%s exc=%s",
                snapshot.wazuh_ca_bundle, exc,
            )
            return schemas.CollectorConfigTestResult(
                component="syslog",
                status="error",
                details={"reason": "CA bundle inválido (ver log do servidor)"},
            )

    writer = None
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(
                snapshot.wazuh_syslog_host,
                snapshot.wazuh_syslog_port,
                ssl=ssl_ctx,
                server_hostname=(
                    snapshot.wazuh_syslog_host if ssl_ctx else None
                ),
            ),
            timeout=8.0,
        )
        # RFC 5424 probe curto. Octet-counting framing (RFC 6587).
        msg = _build_probe_message()
        frame = f"{len(msg)} ".encode("ascii") + msg
        writer.write(frame)
        await asyncio.wait_for(writer.drain(), timeout=3.0)
        return schemas.CollectorConfigTestResult(
            component="syslog",
            status="healthy",
            details={
                "host": snapshot.wazuh_syslog_host,
                "port": snapshot.wazuh_syslog_port,
                "tls": snapshot.wazuh_syslog_use_tls,
                "bytes_sent": len(frame),
            },
        )
    except asyncio.TimeoutError:
        return schemas.CollectorConfigTestResult(
            component="syslog",
            status="error",
            details={
                "reason": "timeout após 8s — host inacessível ou firewall bloqueado",
                "host": snapshot.wazuh_syslog_host,
                "port": snapshot.wazuh_syslog_port,
            },
        )
    except ssl.SSLError as exc:
        # Hint detalhado sobre o downstream fica só no log; resposta HTTP
        # genérica para não documentar a topologia.
        logger.warning(
            "collector test: SSL error against syslog host=%s port=%s exc=%s "
            "(consider stunnel/rsyslog if downstream lacks TLS)",
            snapshot.wazuh_syslog_host, snapshot.wazuh_syslog_port, exc,
        )
        return schemas.CollectorConfigTestResult(
            component="syslog",
            status="error",
            details=_error_details(exc),
        )
    except (ConnectionRefusedError, OSError, socket.gaierror) as exc:
        return schemas.CollectorConfigTestResult(
            component="syslog",
            status="error",
            details=_error_details(exc),
        )
    finally:
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # pragma: no cover
                pass


def _build_probe_message() -> bytes:
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    hostname = socket.gethostname()
    body = json.dumps(
        {
            "_centralops": {
                "probe": True,
                "collected_at": now,
                "integration_id": 0,
                "customer_id": "_probe",
                "platform": "centralops",
                "stream": "config-probe",
            },
            "message": "CentralOps collector config probe",
        },
        separators=(",", ":"),
    )
    sd = '[centralops@32473 probe="true"]'
    line = f"<134>1 {now} {hostname} centralops-collector-probe - - {sd} {body}"
    return line.encode("utf-8")


def _test_jsonl(snapshot: CollectorConfigSnapshot) -> schemas.CollectorConfigTestResult:
    """Verifica existência/permissão de escrita no diretório JSONL."""
    target_dir = snapshot.collector_jsonl_dir
    probe_file = os.path.join(target_dir, ".centralops-probe")

    try:
        os.makedirs(target_dir, exist_ok=True)
        with open(probe_file, "w") as fh:
            fh.write("probe")
        os.remove(probe_file)
        return schemas.CollectorConfigTestResult(
            component="jsonl",
            status="healthy",
            details={"directory": target_dir, "writable": True},
        )
    except PermissionError as exc:
        # "container" / "volume" são detalhes de deploy; o operador pode
        # rodar fora de container. Mensagem genérica + log do path real.
        logger.warning(
            "collector test: jsonl dir not writable dir=%s exc=%s",
            target_dir, exc,
        )
        return schemas.CollectorConfigTestResult(
            component="jsonl",
            status="error",
            details={
                **_error_details(exc),
                "directory": target_dir,
                "hint": "Verifique permissões do diretório destino",
            },
        )
    except OSError as exc:
        return schemas.CollectorConfigTestResult(
            component="jsonl",
            status="error",
            details={**_error_details(exc), "directory": target_dir},
        )


@router.post("/test", response_model=schemas.CollectorConfigTestResponse)
async def test_config(
    _: models.AppUser = Depends(app_auth.require_admin_user),
    db: Session = Depends(database.get_session),
) -> schemas.CollectorConfigTestResponse:
    """Testa a config **atualmente salva** no banco (não o request body).

    Executa condicionalmente conforme ``wazuh_dispatch_mode``:
    - ``syslog`` → testa só Syslog TCP/TLS.
    - ``jsonl`` → testa só diretório JSONL.
    - ``both``  → testa ambos.
    """
    snapshot = load_from_db_session(db)
    mode = snapshot.wazuh_dispatch_mode
    results = []

    if mode in ("syslog", "both"):
        results.append(await _test_syslog(snapshot))
    if mode in ("jsonl", "both"):
        results.append(_test_jsonl(snapshot))

    return schemas.CollectorConfigTestResponse(mode=mode, results=results)


# ── GET /audit/recent ────────────────────────────────────────────────


@router.get("/audit/recent", response_model=schemas.CollectorAuditResponse)
async def audit_recent(
    limit: int = 100,
    platform: Optional[str] = None,
    stream: Optional[str] = None,
    org_id: Optional[int] = None,
    user: models.AppUser = Depends(app_auth.require_admin_user),
) -> schemas.CollectorAuditResponse:
    """Lista últimos N eventos efetivamente enviados ao Wazuh (POR TENANT).

    Fonte: ring buffer Redis ``collector:audit:{org_id}:recent`` (antes era
    global e misturava todos os tenants), atualizado pelo
    ``dispatch_batch`` após ``send_batch``. Janela: 500 eventos; TTL 24h.

    **Escopo de tenant:** o ``org_id`` efetivo é o query param (admin
    nomeia o tenant explicitamente) ou, na ausência, o ``organization_id`` do
    próprio admin. Admin global sem ``org_id`` → lista vazia (fail-closed, sem
    leitura cross-tenant implícita).
    """
    effective_org = org_id if org_id is not None else user.organization_id
    if effective_org is None:
        # Admin global sem tenant especificado — não há ring a ler.
        return schemas.CollectorAuditResponse(count=0, events=[])
    # admin escopado não lê o ring de OUTRA org via org_id
    # explícito (o ring contém eventos reais do tenant). Global bypassa.
    tenant.require_subtree_access(user, effective_org)
    redis = await _redis_client()
    try:
        events = await audit_read_recent(
            redis, effective_org, limit=limit, platform=platform, stream=stream
        )
    finally:
        await redis.aclose()

    out = []
    for entry in events:
        raw_event = entry.get("event") or {}
        envelope = entry.get("envelope") or {}
        raw_fmt = entry.get("syslog_format")
        # Valida contra os valores permitidos — entradas legadas/corrompidas
        # ficam como None (UI exibe aviso "legado").
        syslog_fmt = raw_fmt if raw_fmt in ("rfc3164", "rfc5424") else None
        out.append(
            schemas.CollectorAuditEvent(
                event=raw_event,
                envelope=schemas.CollectorAuditEnvelope(
                    hostname=envelope.get("hostname"),
                    pri=envelope.get("pri"),
                ),
                meta=raw_event.get("_centralops") or {},
                syslog_format=syslog_fmt,
            )
        )
    return schemas.CollectorAuditResponse(count=len(out), events=out)


@router.delete("/audit/recent", status_code=204)
async def audit_clear_endpoint(
    org_id: Optional[int] = None,
    user: models.AppUser = Depends(app_auth.require_admin_user),
) -> None:
    """Zera o ring buffer de auditoria DO TENANT — útil para começar
    uma janela limpa durante tuning. Escopo: query param ``org_id`` ou o
    ``organization_id`` do admin; admin global sem ``org_id`` → no-op."""
    effective_org = org_id if org_id is not None else user.organization_id
    if effective_org is None:
        return
    # escopado não zera o ring de outra org.
    tenant.require_subtree_access(user, effective_org)
    redis = await _redis_client()
    try:
        removed = await audit_clear(redis, effective_org)
        logger.info(
            "audit: ring zerado org=%s (%d eventos removidos)", effective_org, removed
        )
    finally:
        await redis.aclose()


# ── Captura ao vivo / "listening" (sessões de captura sob demanda) ────────────


def _capture_effective_org(org_id: Optional[int], user: models.AppUser) -> Optional[int]:
    effective = org_id if org_id is not None else user.organization_id
    # admin escopado só captura a PRÓPRIA subárvore — org_id
    # explícito de outra org seria leitura cross-tenant de tráfego ao vivo. O
    # check central cobre os 5 endpoints de capture-session. Global bypassa.
    if effective is not None:
        tenant.require_subtree_access(user, effective)
    return effective


def _to_capture_schema(meta: Dict[str, Any]) -> schemas.CaptureSession:
    return schemas.CaptureSession(
        id=meta["id"],
        organization_id=meta.get("organization_id"),
        vendor=meta.get("vendor"),
        created_at=meta.get("created_at"),
        expires_at=meta.get("expires_at"),
        status=meta.get("status", "active"),
        event_count=meta.get("event_count", 0),
    )


async def _owned_capture_or_404(
    redis: redis_async.Redis, session_id: str, effective_org: Optional[int]
) -> Dict[str, Any]:
    """Isolamento de tenant: a sessão só é acessível pelo próprio org (fail-closed)."""
    meta = await capture_session.get_session(redis, session_id)
    if (
        meta is None
        or effective_org is None
        or meta.get("organization_id") != effective_org
    ):
        raise ApiError(
            "collector_config.capture_session_not_found",
            404,
            messages={
                "pt": "Sessão de captura não encontrada.",
                "en": "Capture session not found.",
                "es": "Sesión de captura no encontrada.",
            },
        )
    return meta


@router.post("/capture-sessions", response_model=schemas.CaptureSession, status_code=201)
async def start_capture_session(
    body: schemas.CaptureSessionStartRequest,
    org_id: Optional[int] = None,
    user: models.AppUser = Depends(app_auth.require_admin_user),
) -> schemas.CaptureSession:
    """Inicia uma sessão de captura (o "botão listening"): por uma janela, grava tudo o
    que for despachado para o tenant — opcionalmente filtrado por vendor — p/
    troubleshooting. Escopo de tenant idêntico ao da auditoria."""
    effective_org = _capture_effective_org(org_id, user)
    if effective_org is None:
        raise ApiError(
            "collector_config.org_id_required",
            400,
            messages={
                "pt": "org_id é obrigatório para admin global",
                "en": "org_id is required for a global admin",
                "es": "org_id es obligatorio para un administrador global",
            },
        )
    redis = await _redis_client()
    try:
        meta = await capture_session.start_session(
            redis,
            effective_org,
            vendor=body.vendor,
            duration_seconds=body.duration_seconds,
            ring_size=body.ring_size,
        )
    except capture_session.CaptureLimitReached as exc:
        raise ApiError(
            "collector_config.capture_limit_reached",
            429,
            messages={
                "pt": "limite de {limit} sessões de captura simultâneas atingido",
                "en": "limit of {limit} concurrent capture sessions reached",
                "es": "límite de {limit} sesiones de captura simultáneas alcanzado",
            },
            params={"limit": capture_session.MAX_SESSIONS_PER_ORG},
        ) from exc
    finally:
        await redis.aclose()
    return _to_capture_schema(meta)


@router.get("/capture-sessions", response_model=schemas.CaptureSessionList)
async def list_capture_sessions(
    org_id: Optional[int] = None,
    user: models.AppUser = Depends(app_auth.require_admin_user),
) -> schemas.CaptureSessionList:
    effective_org = _capture_effective_org(org_id, user)
    if effective_org is None:
        return schemas.CaptureSessionList(count=0)
    redis = await _redis_client()
    try:
        sessions = await capture_session.list_sessions(redis, effective_org)
    finally:
        await redis.aclose()
    items = [_to_capture_schema(m) for m in sessions]
    return schemas.CaptureSessionList(count=len(items), sessions=items)


@router.get(
    "/capture-sessions/{session_id}/events",
    response_model=schemas.CaptureEventList,
)
async def get_capture_events(
    session_id: str,
    limit: int = Query(default=200, ge=1, le=20000),
    org_id: Optional[int] = None,
    user: models.AppUser = Depends(app_auth.require_admin_user),
) -> schemas.CaptureEventList:
    effective_org = _capture_effective_org(org_id, user)
    redis = await _redis_client()
    try:
        await _owned_capture_or_404(redis, session_id, effective_org)
        events = await capture_session.read_events(redis, session_id, limit=limit)
    finally:
        await redis.aclose()
    return schemas.CaptureEventList(
        count=len(events),
        session_id=session_id,
        events=[
            schemas.CaptureEvent(
                event=e.get("event") or {},
                vendor=e.get("vendor"),
                captured_at=e.get("captured_at"),
            )
            for e in events
        ],
    )


@router.post("/capture-sessions/{session_id}/stop", status_code=204)
async def stop_capture_session(
    session_id: str,
    org_id: Optional[int] = None,
    user: models.AppUser = Depends(app_auth.require_admin_user),
) -> None:
    effective_org = _capture_effective_org(org_id, user)
    redis = await _redis_client()
    try:
        await _owned_capture_or_404(redis, session_id, effective_org)
        await capture_session.stop_session(redis, session_id, int(effective_org))
    finally:
        await redis.aclose()


@router.delete("/capture-sessions/{session_id}", status_code=204)
async def delete_capture_session(
    session_id: str,
    org_id: Optional[int] = None,
    user: models.AppUser = Depends(app_auth.require_admin_user),
) -> None:
    effective_org = _capture_effective_org(org_id, user)
    redis = await _redis_client()
    try:
        meta = await _owned_capture_or_404(redis, session_id, effective_org)
        await capture_session.delete_session(
            redis, session_id, int(meta["organization_id"])
        )
    finally:
        await redis.aclose()
