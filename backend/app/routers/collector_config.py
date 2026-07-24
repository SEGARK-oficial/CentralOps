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
from typing import Any, Dict, List, Optional, Set

import redis.asyncio as redis_async
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..api import schemas
from ..core import auth as app_auth
from ..core import tenant
from ..core.config import settings
from ..core.errors import ApiError
from ..db import database, models, repository
from ..collectors import capture_session
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
        dedupe_ttl_seconds=snapshot.effective_dedupe_ttl_seconds,
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


# ── Captura ao vivo / "listening" (sessões de captura sob demanda) ────────────


def _capture_effective_org(org_id: Optional[int], user: models.AppUser) -> Optional[int]:
    effective = org_id if org_id is not None else user.organization_id
    # admin escopado só captura a PRÓPRIA subárvore — org_id
    # explícito de outra org seria leitura cross-tenant de tráfego ao vivo. O
    # check central cobre os 5 endpoints de capture-session. Global bypassa.
    if effective is not None:
        tenant.require_subtree_access(user, effective)
    return effective


# ── Escopo HIERÁRQUICO da captura ────────────────────────────────────────────
#
# O gate de autorização (``require_subtree_access``) é SUBTREE-aware: um admin da
# org PAI pode abrir captura na org FILHA. A gravação (``capture_session.record``),
# porém, indexa a sessão pela org EXATA do evento — logo, uma sessão aberta no PAI
# nunca via o tráfego dos FILHOS (o admin lia "capturei nada" com tráfego correndo
# na subárvore que ele legitimamente enxerga).
#
# Correção (a mais simples e segura): no START, a sessão é indexada em TODAS as
# orgs do escopo — ``subárvore(org efetiva) ∩ orgs acessíveis ao usuário``. Assim o
# tap de ``record()`` (que continua olhando só o índice da org do evento) encontra a
# mesma sessão e escreve num ÚNICO ring; a leitura já sai agregada, sem fan-in no
# read-path. O escopo coberto fica EXPLÍCITO na sessão (``scope_org_ids``, gravado no
# meta e devolvido pela API) — o operador vê exatamente o que a sessão cobre.
#
# Isolamento: o escopo é sempre INTERSECTADO com ``tenant.accessible_org_ids`` — nunca
# alcança uma org que o usuário já não podia ver. Falha de resolução ⇒ fail-closed
# (escopo = só a org efetiva).
#
# Trade-off conhecido: sessões do PAI ocupam slot no índice do FILHO, então contam
# para o teto ``MAX_SESSIONS_PER_ORG`` do filho. Preferimos isso a um segundo índice
# (que exigiria mudar o hot path de ``record()``).


def _org_subtree_ids(db: Session, root_org_id: int) -> Set[int]:
    """IDs da subárvore de ``root_org_id`` (inclusive), via
    ``Organization.parent_organization_id``.

    Em Community a hierarquia é FLAT (parent sempre ``None``) ⇒ ``{root_org_id}``;
    em Enterprise as colunas são materializadas e o walk devolve os descendentes.
    Fail-closed: qualquer erro ⇒ só a própria org.
    """
    try:
        rows = db.query(
            models.Organization.id, models.Organization.parent_organization_id
        ).all()
    except Exception as exc:  # pragma: no cover — defensivo
        logger.warning("capture: falha ao resolver subárvore de org=%s: %s", root_org_id, exc)
        return {root_org_id}

    children: Dict[Optional[int], List[int]] = {}
    for org_id, parent_id in rows:
        children.setdefault(parent_id, []).append(org_id)

    out: Set[int] = {root_org_id}
    frontier = [root_org_id]
    while frontier:
        nxt: List[int] = []
        for org_id in frontier:
            for child in children.get(org_id, ()):
                if child not in out:
                    out.add(child)
                    nxt.append(child)
        frontier = nxt
    return out


def _capture_scope_org_ids(
    user: models.AppUser, db: Session, effective_org: int
) -> List[int]:
    """Orgs cobertas por uma sessão aberta em ``effective_org``.

    ``subárvore(effective_org) ∩ acessíveis(user)`` — admin global não tem filtro de
    acesso (``accessible_org_ids`` ⇒ ``None``), então cobre a subárvore inteira. A org
    efetiva SEMPRE entra (o gate de autorização já a validou)."""
    subtree = _org_subtree_ids(db, effective_org)
    try:
        accessible = tenant.accessible_org_ids(user, db)
    except Exception as exc:  # pragma: no cover — defensivo, fail-closed
        logger.warning("capture: falha ao resolver escopo do usuário: %s", exc)
        accessible = set()
    if accessible is not None:
        subtree &= set(accessible)
    subtree.add(effective_org)
    return sorted(subtree)


# TTL fixo do índice — espelha ``capture_session.start_session`` (não regride).
_CAPTURE_INDEX_TTL = capture_session.MAX_DURATION_SECONDS + capture_session.GRACE_SECONDS
_SCOPE_META_FIELD = "scope_org_ids"
# Convenção OPCIONAL de contadores por desfecho no meta (``outcome:dropped`` etc.).
# Se o engine passar a mantê-los (``hincrby`` ao lado de ``event_count``), a API os
# expõe automaticamente; até lá o campo sai vazio.
_OUTCOME_META_PREFIX = "outcome:"


async def _index_session_in_scope(
    redis: redis_async.Redis,
    session_id: str,
    scope_org_ids: List[int],
    owner_org_id: int,
) -> None:
    """Indexa a sessão nas orgs do escopo (além da dona, já feita pelo engine) e
    persiste o escopo no meta. Best-effort: falhar aqui degrada a sessão para
    "só a org dona" — nunca derruba o start."""
    try:
        pipe = redis.pipeline()
        for org_id in scope_org_ids:
            if org_id == owner_org_id:
                continue
            key = capture_session._org_index_key(org_id)
            pipe.sadd(key, session_id)
            pipe.expire(key, _CAPTURE_INDEX_TTL)
        pipe.hset(
            capture_session._meta_key(session_id),
            _SCOPE_META_FIELD,
            ",".join(str(o) for o in scope_org_ids),
        )
        await pipe.execute()
        # O tap memoiza "org sem sessão" (cache NEGATIVO); as orgs recém-incluídas
        # no escopo precisam sair dele para não perder eventos da janela inicial.
        for org_id in scope_org_ids:
            capture_session.reset_session_cache(org_id)
    except Exception as exc:  # pragma: no cover — não-fatal
        logger.warning("capture: fan-out de escopo falhou para sessão %s: %s", session_id, exc)


async def _session_extras(
    redis: redis_async.Redis, session_id: str, owner_org_id: Optional[int]
) -> tuple[List[int], Dict[str, int]]:
    """``(scope_org_ids, outcome_counts)`` num único ``HGETALL`` do meta.

    ``outcome_counts`` são os contadores POR DESFECHO da sessão inteira, se o engine
    os mantiver (campos ``outcome:<nome>`` no meta, ao lado de ``event_count``). Enquanto
    não existirem, sai ``{}`` — a UI já trata como opcional e cai no breakdown da página
    de eventos. Escopo ausente (sessão anterior a esta versão) ⇒ só a org dona."""
    meta: Dict[str, Any] = {}
    try:
        raw_meta = await redis.hgetall(capture_session._meta_key(session_id))
        meta = {capture_session._s(k): capture_session._s(v) for k, v in (raw_meta or {}).items()}
    except Exception:  # pragma: no cover — não-fatal
        meta = {}

    ids: Set[int] = set()
    for part in (meta.get(_SCOPE_META_FIELD) or "").split(","):
        try:
            ids.add(int(part.strip()))
        except ValueError:
            continue
    if owner_org_id is not None:
        ids.add(int(owner_org_id))

    counts: Dict[str, int] = {}
    for key, value in meta.items():
        if not key.startswith(_OUTCOME_META_PREFIX):
            continue
        try:
            counts[key[len(_OUTCOME_META_PREFIX):]] = int(value)
        except (TypeError, ValueError):
            continue
    return sorted(ids), counts


async def _unindex_session_from_scope(
    redis: redis_async.Redis, session_id: str, scope_org_ids: List[int]
) -> None:
    """Remove o id da sessão dos índices do escopo (o engine só limpa o da dona)."""
    try:
        pipe = redis.pipeline()
        for org_id in scope_org_ids:
            pipe.srem(capture_session._org_index_key(org_id), session_id)
        await pipe.execute()
    except Exception as exc:  # pragma: no cover — não-fatal
        logger.warning("capture: limpeza de índice falhou para sessão %s: %s", session_id, exc)


# ── Response models (locais: estendem o contrato base com escopo/contadores) ──


class CaptureSessionScoped(schemas.CaptureSession):
    """Sessão + as orgs que ela realmente cobre (subárvore autorizada).

    ``outcome_counts`` é o total POR DESFECHO da sessão inteira quando o engine mantém
    esses contadores (ver ``_OUTCOME_META_PREFIX``); vazio caso contrário — nunca um
    palpite. ``event_count`` continua sendo o total geral."""

    scope_org_ids: List[int] = Field(default_factory=list)
    outcome_counts: Dict[str, int] = Field(default_factory=dict)


class CaptureSessionScopedList(BaseModel):
    count: int
    sessions: List[CaptureSessionScoped] = Field(default_factory=list)


class CaptureEventDetail(schemas.CaptureEvent):
    """Evento capturado + de qual org veio e QUAL FOI O DESFECHO.

    ``outcome`` vem do tap de ciclo de vida (``capture_session.OUTCOMES``);
    ``destination_id``/``detail`` só existem nos desfechos que os têm (entrega por
    destino, motivo do drop/quarentena) — é o par "como entrou / como saiu"."""

    organization_id: Optional[int] = None
    outcome: str = "unknown"
    destination_id: Optional[str] = None
    # Rota responsável pelo desfecho (estruturada). Presente nos desfechos que o
    # engine atribui por evento — dropped/sampled_out — respondendo "em qual rota
    # bateu" e "por que foi dropado" sem parsear texto livre.
    route_id: Optional[str] = None
    detail: Optional[str] = None


class CaptureEventPage(BaseModel):
    """Página de eventos + contadores que deixam a UI honesta.

    ``total_captured`` é o contador da SESSÃO INTEIRA (inclui o que já saiu do ring
    por trim) — é ele que distingue "sessão ativa e nada aconteceu" (0) de "houve
    tráfego, mas fora da janela lida". ``outcome_counts`` é o breakdown por desfecho
    (entregue/drop/sem-destino/quarentena/…) **dos eventos desta página**."""

    count: int
    session_id: str
    session_status: str = "active"
    total_captured: int = 0
    scope_org_ids: List[int] = Field(default_factory=list)
    outcome_counts: Dict[str, int] = Field(default_factory=dict)
    events: List[CaptureEventDetail] = Field(default_factory=list)


# ── Catálogo de vendors capturáveis (derivado do registry, sem hardcode) ──────


class CaptureVendor(BaseModel):
    vendor: str
    display_name: str
    transport: str  # "pull" | "push"
    streams: List[str] = Field(default_factory=list)


class CaptureVendorList(BaseModel):
    count: int
    vendors: List[CaptureVendor] = Field(default_factory=list)


def _capture_vendor_catalog() -> List[CaptureVendor]:
    """Vendors que podem aparecer em ``_centralops.vendor`` — TODOS os transportes.

    União do registry de collectors (``all_registrations`` — pull **e** push, ex.:
    ``fortinet_fortigate``/``windows_event_log`` via ``/api/ingest``) com o catálogo de
    plataformas (``all_platforms`` — inclui plataformas sem collector próprio, como as
    variantes de partner). Zero hardcode: registrar um vendor novo o faz aparecer aqui.
    """
    try:  # import tardio (mesmo motivo do router de collectors: evita Celery/aiohttp)
        from ..collectors import registry
    except Exception as exc:  # pragma: no cover — defensivo
        logger.warning("capture: registry indisponível: %s", exc)
        return []

    streams_by_platform: Dict[str, Set[str]] = {}
    try:
        for reg in registry.all_registrations():
            streams_by_platform.setdefault(reg.platform, set()).add(reg.stream)
        catalog = {p.platform: p for p in registry.all_platforms()}
    except Exception as exc:  # pragma: no cover — defensivo
        logger.warning("capture: leitura do registry falhou: %s", exc)
        return []

    out: List[CaptureVendor] = []
    for platform in sorted(set(streams_by_platform) | set(catalog)):
        meta = catalog.get(platform)
        out.append(
            CaptureVendor(
                vendor=platform,
                display_name=getattr(meta, "display_name", None) or platform,
                transport=getattr(meta, "transport", None) or "pull",
                streams=sorted(streams_by_platform.get(platform, ())),
            )
        )
    return out


@router.get("/capture-vendors", response_model=CaptureVendorList)
def list_capture_vendors(
    _: models.AppUser = Depends(app_auth.require_admin_user),
) -> CaptureVendorList:
    """Catálogo de vendors para o filtro da captura — pull **e** push."""
    vendors = _capture_vendor_catalog()
    return CaptureVendorList(count=len(vendors), vendors=vendors)


def _to_capture_schema(
    meta: Dict[str, Any],
    scope_org_ids: Optional[List[int]] = None,
    outcome_counts: Optional[Dict[str, int]] = None,
) -> CaptureSessionScoped:
    org_id = meta.get("organization_id")
    return CaptureSessionScoped(
        id=meta["id"],
        organization_id=org_id,
        vendor=meta.get("vendor"),
        created_at=meta.get("created_at"),
        expires_at=meta.get("expires_at"),
        status=meta.get("status", "active"),
        event_count=meta.get("event_count", 0),
        scope_org_ids=(
            scope_org_ids
            if scope_org_ids is not None
            else ([int(org_id)] if org_id is not None else [])
        ),
        outcome_counts=outcome_counts or {},
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


@router.post("/capture-sessions", response_model=CaptureSessionScoped, status_code=201)
async def start_capture_session(
    body: schemas.CaptureSessionStartRequest,
    org_id: Optional[int] = None,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    db: Session = Depends(database.get_session),
) -> CaptureSessionScoped:
    """Inicia uma sessão de captura (o "botão listening"): por uma janela, grava tudo o
    que for despachado para o tenant — opcionalmente filtrado por vendor — p/
    troubleshooting. Escopo de tenant idêntico ao da auditoria.

    A sessão cobre a subárvore AUTORIZADA da org (ver ``_capture_scope_org_ids``): quem
    pode abrir captura nos filhos também VÊ o tráfego deles, num ring único. O escopo
    efetivo volta em ``scope_org_ids``."""
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
    scope_org_ids = _capture_scope_org_ids(user, db, effective_org)
    redis = await _redis_client()
    try:
        meta = await capture_session.start_session(
            redis,
            effective_org,
            vendor=body.vendor,
            duration_seconds=body.duration_seconds,
            ring_size=body.ring_size,
        )
        await _index_session_in_scope(
            redis, meta["id"], scope_org_ids, effective_org
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
    return _to_capture_schema(meta, scope_org_ids)


@router.get("/capture-sessions", response_model=CaptureSessionScopedList)
async def list_capture_sessions(
    org_id: Optional[int] = None,
    user: models.AppUser = Depends(app_auth.require_admin_user),
) -> CaptureSessionScopedList:
    """Lista as sessões DA ORG (as que ela iniciou).

    O índice da org também guarda sessões de um ANCESTRAL cujo escopo a alcança (é
    assim que o pai captura o tráfego do filho) — essas são filtradas aqui: só o dono
    lista/para/apaga a própria sessão, o filho não enxerga a captura do pai."""
    effective_org = _capture_effective_org(org_id, user)
    if effective_org is None:
        return CaptureSessionScopedList(count=0)
    redis = await _redis_client()
    try:
        sessions = await capture_session.list_sessions(redis, effective_org)
        owned = [m for m in sessions if m.get("organization_id") == effective_org]
        items = []
        for m in owned:
            scope_org_ids, outcome_counts = await _session_extras(
                redis, m["id"], m.get("organization_id")
            )
            items.append(_to_capture_schema(m, scope_org_ids, outcome_counts))
    finally:
        await redis.aclose()
    return CaptureSessionScopedList(count=len(items), sessions=items)


def _event_outcome(entry: Dict[str, Any]) -> str:
    """Desfecho do evento: ``outcome`` do envelope de captura, ou o carimbado em
    ``_centralops``. Ausente ⇒ ``"unknown"`` (honesto: entradas de taps antigos não
    sabem o desfecho — melhor do que assumir "entregue")."""
    meta = (entry.get("event") or {}).get("_centralops") or {}
    raw = entry.get("outcome") or (meta.get("outcome") if isinstance(meta, dict) else None)
    return str(raw) if raw else "unknown"


def _event_org_id(entry: Dict[str, Any]) -> Optional[int]:
    meta = (entry.get("event") or {}).get("_centralops") or {}
    if not isinstance(meta, dict):
        return None
    try:
        return int(meta.get("organization_id"))
    except (TypeError, ValueError):
        return None


@router.get(
    "/capture-sessions/{session_id}/events",
    response_model=CaptureEventPage,
)
async def get_capture_events(
    session_id: str,
    limit: int = Query(default=200, ge=1, le=20000),
    org_id: Optional[int] = None,
    user: models.AppUser = Depends(app_auth.require_admin_user),
) -> CaptureEventPage:
    """Eventos da sessão + contadores.

    A UI precisa distinguir "sessão ativa e NADA aconteceu" de "houve tráfego":
    ``total_captured`` (contador da sessão inteira, sobrevive ao trim do ring) responde
    isso, e ``outcome_counts`` mostra o breakdown por desfecho da página lida — é o que
    revela tráfego que entrou mas NÃO foi entregue (drop/sem-destino/quarentena/…).
    O ring é único para toda a subárvore coberta (``scope_org_ids``); cada evento traz
    o ``organization_id`` de origem."""
    effective_org = _capture_effective_org(org_id, user)
    redis = await _redis_client()
    try:
        meta = await _owned_capture_or_404(redis, session_id, effective_org)
        events = await capture_session.read_events(redis, session_id, limit=limit)
        scope_org_ids, _ = await _session_extras(
            redis, session_id, meta.get("organization_id")
        )
    finally:
        await redis.aclose()

    outcome_counts: Dict[str, int] = {}
    items: List[CaptureEventDetail] = []
    for e in events:
        outcome = _event_outcome(e)
        outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1
        items.append(
            CaptureEventDetail(
                event=e.get("event") or {},
                vendor=e.get("vendor"),
                captured_at=e.get("captured_at"),
                organization_id=_event_org_id(e),
                outcome=outcome,
                destination_id=e.get("destination_id"),
                route_id=e.get("route_id"),
                detail=e.get("detail"),
            )
        )
    return CaptureEventPage(
        count=len(items),
        session_id=session_id,
        session_status=meta.get("status", "active"),
        total_captured=int(meta.get("event_count") or 0),
        scope_org_ids=scope_org_ids,
        outcome_counts=outcome_counts,
        events=items,
    )


_EXPORT_MAX_ROWS = 50_000


@router.get("/capture-sessions/{session_id}/export")
async def export_capture_events(
    session_id: str,
    request: Request,
    fmt: str = Query(default="csv", pattern="^(csv|ndjson)$"),
    org_id: Optional[int] = None,
    mask: bool = Query(default=True),
    user: models.AppUser = Depends(app_auth.require_admin_user),
) -> StreamingResponse:
    """Exporta os eventos capturados de UMA sessão como planilha (CSV) ou NDJSON,
    para o analista de SOC abrir no Excel / anexar num ticket.

    STREAMING (páginas de ``EXPORT_PAGE_SIZE`` via LRANGE) — não materializa o ring
    na RAM. Escopo SEMPRE a uma sessão (teto natural = ring ≤ 20k) e à org do
    usuário (mesmo gate ``require_admin_user`` dos demais endpoints de captura; a
    chamada é auditada pelo middleware ``audit_api_requests`` — o path traz o
    session_id). ``mask`` (default True) redige PII no serializador, porque o dado
    está SAINDO do sistema; os SEGREDOS já foram scrubbados na gravação do ring."""
    from ..collectors import capture_export

    effective_org = _capture_effective_org(org_id, user)
    redis = await _redis_client()
    # Valida posse ANTES de abrir o stream (404 vira corpo de erro limpo, não um
    # CSV meia-boca). O client de leitura das páginas é o mesmo, reusado no gerador.
    try:
        await _owned_capture_or_404(redis, session_id, effective_org)
    except Exception:
        await redis.aclose()
        raise

    separator = capture_export.csv_separator_for_locale(
        request.headers.get("accept-language")
    )

    async def _stream():
        # Serializa item a item conforme as páginas chegam do Redis — pico de
        # memória = uma página do ring + uma linha, nunca o dataset inteiro.
        try:
            written = 0
            if fmt == "csv":
                yield capture_export.csv_header(separator).encode("utf-8")
            async for entry in capture_session.iter_events(
                redis, session_id, max_events=_EXPORT_MAX_ROWS
            ):
                if written >= _EXPORT_MAX_ROWS:
                    notice = (
                        capture_export.csv_truncation_notice(_EXPORT_MAX_ROWS)
                        if fmt == "csv"
                        else capture_export.ndjson_truncation_notice(_EXPORT_MAX_ROWS)
                    )
                    yield notice.encode("utf-8")
                    break
                line = (
                    capture_export.csv_row(entry, mask=mask, separator=separator)
                    if fmt == "csv"
                    else capture_export.ndjson_line(entry, mask=mask)
                )
                yield line.encode("utf-8")
                written += 1
        finally:
            await redis.aclose()

    if fmt == "csv":
        media = "text/csv; charset=utf-8"
        filename = f"capture-{session_id}.csv"
    else:
        media = "application/x-ndjson"
        filename = f"capture-{session_id}.ndjson"

    return StreamingResponse(
        _stream(),
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-CentralOps-Export-Max-Rows": str(_EXPORT_MAX_ROWS),
        },
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
        owner_org = int(meta["organization_id"])
        # Lê o escopo ANTES do delete (o meta some junto com o campo).
        scope_org_ids, _ = await _session_extras(redis, session_id, owner_org)
        await capture_session.delete_session(redis, session_id, owner_org)
        # O engine só limpa o índice da org dona; as demais do escopo saem aqui
        # (sem isso ficariam ids órfãos até o ``record()`` podá-los).
        await _unindex_session_from_scope(redis, session_id, scope_org_ids)
    finally:
        await redis.aclose()
