import logging
import uuid
from contextlib import asynccontextmanager
import json

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import os

from .core import auth as app_auth
from .core.config import settings
from .core.logging_config import configure_logging, set_correlation_id
from .db import models  # noqa: F401
from .db.database import SessionLocal
# Force Celery singleton instantiation in the FastAPI process so that any
# ``@shared_task`` ``.delay()`` call resolves the configured broker (Redis)
# instead of falling back to ``amqp://guest@localhost:5672`` (Celery's
# default app). The collector worker entrypoint imports this module via
# ``celery -A backend.app.collectors.celery_app worker`` and Beat does the
# same — but the API process never imported it before, so producers
# silently used Celery's default app and tasks went to a broker no one
# was listening on. See diagnóstico Erro A (Sophos partner sync).
from .collectors.celery_app import celery_app  # noqa: F401
from .routers import (
    api_tokens, auth, backfill, collector_config, collectors, config_bundle,
    dashboard, destinations, detections, drift, emails, health, history, identity_config,
    ingest, integrations, internal, iris, mappings, ocsf, organizations, pipeline_health, providers,
    quarantine, queries, results, routes, scheduled_queries,
    service_accounts, sso,
)
from .services.audit import AuditService
from .services.scheduler import start_scheduler  # no-op (migrado para Celery Beat)


log_level = logging.DEBUG if settings.DEBUG_REQUESTS else logging.INFO
configure_logging(
    level=log_level,
    enable_wazuh_jsonl=settings.LOGGING_WAZUH_JSONL_ENABLED,
    wazuh_jsonl_path=settings.LOGGING_WAZUH_JSONL_PATH,
)

if settings.DEBUG_REQUESTS:
    debug_handler = logging.FileHandler("debug_requests.log")
    debug_handler.setLevel(logging.DEBUG)
    debug_handler.setFormatter(
        logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )
    logging.getLogger().addHandler(debug_handler)

# o init de schema NÃO roda mais no import (schema-no-import
# impedia ``api`` em replicas>1 e acoplava o boot). Agora é etapa explícita —
# os entrypoints rodam ``python -m app.db.migrate`` ANTES de subir a app. Os
# testes garantem o schema via fixture de sessão no conftest. Ver app/db/migrate.py.
AUDIT_SKIP_PATHS = {
    "/api/auth/bootstrap",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/me",
    "/api/auth/status",
}
AUDIT_REDACTED_FIELDS = {
    # ── Secrets (sempre redactar) ──────────────────────────────────────
    "api_password",
    "api_username",
    "api_key",
    "authorization",
    "access_token",
    "bearer",
    "client_secret",
    "cookie",
    "indexer_password",
    "indexer_username",
    "manager_api_password",
    "manager_api_username",
    "master_key",
    "password",
    "refresh_token",
    "session",
    "smtp_password",
    "token",
    # ── Identificadores PII / cross-vendor (defense-in-depth) ──────────
    # Não são secrets, mas combinados com timestamp/IP no audit log
    # geram fingerprint do estoque de tenants. Em uma imagem pública +
    # log shipping para SIEM externo, vale redactar.
    "client_id",
    "external_id",
    "iris_customer_id",
    "tenant_id",
}


def _redact_audit_payload(value):
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if key.lower() in AUDIT_REDACTED_FIELDS:
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = _redact_audit_payload(item)
        return redacted
    if isinstance(value, list):
        return [_redact_audit_payload(item) for item in value]
    return value


def _compact_json(value) -> str:
    serialized = json.dumps(value, ensure_ascii=False)
    if len(serialized) <= 4000:
        return serialized
    return f"{serialized[:4000]}... [truncated]"


def _serialize_request_payload(request: Request, body: bytes) -> str | None:
    payload = {}

    if request.query_params:
        query_payload = {}
        for key, value in request.query_params.multi_items():
            existing = query_payload.get(key)
            if existing is None:
                query_payload[key] = value
            elif isinstance(existing, list):
                existing.append(value)
            else:
                query_payload[key] = [existing, value]
        payload["query"] = _redact_audit_payload(query_payload)

    if body:
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        parsed_body = None
        if not content_type or content_type == "application/json" or content_type.endswith("+json"):
            try:
                parsed_body = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                parsed_body = None

        if parsed_body is None:
            payload["body"] = {"_omitted": f"unsupported content type: {content_type or 'unknown'}"}
        else:
            payload["body"] = _redact_audit_payload(parsed_body)

    if not payload:
        return None
    if set(payload.keys()) == {"body"}:
        return _compact_json(payload["body"])
    return _compact_json(payload)


async def _capture_request_payload(request: Request) -> tuple[Request, str | None]:
    body = b""
    try:
        body = await request.body()
    except Exception:
        body = b""

    body_sent = False

    async def receive():
        nonlocal body_sent
        if body_sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        body_sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    rebound_request = Request(request.scope, receive)
    return rebound_request, _serialize_request_payload(rebound_request, body)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ``start_scheduler()`` virou no-op: o scheduler de ``ScheduledQuery``
    # agora roda como tasks Celery em ``backend.app.collectors.scheduler_tasks``
    # disparadas pelo Beat (serviço ``collector-beat`` no docker-compose).
    # Chamada preservada só para log informativo — pode remover com segurança.
    start_scheduler()
    # Edição mal-configurada: licença paga concede multi_tenant/reseller mas
    # o pacote centralops_ee não ativou (sem scope resolver) → serviria FLAT em silêncio.
    # ``/readyz`` já falha (503) nesse caso; logamos AQUI no boot p/ o operador ver de
    # imediato nos logs de startup, sem depender do primeiro poll da probe.
    try:
        from .core import edition as _edition

        _integrity = _edition.enterprise_integrity_problem()
        if _integrity:
            logging.getLogger(__name__).error(
                "edição Enterprise MAL-CONFIGURADA — %s; /readyz falhará até o "
                "centralops_ee ativar (o app está servindo em modo FLAT).",
                _integrity,
            )
    except Exception:  # noqa: BLE001 — nunca bloquear o boot por causa do check
        logging.getLogger(__name__).debug(
            "check de integridade de edição no boot falhou (não-fatal)", exc_info=True
        )
    yield


app = FastAPI(
    title="CentralOps",
    lifespan=lifespan,
    docs_url="/docs" if settings.api_docs_enabled else None,
    redoc_url="/redoc" if settings.api_docs_enabled else None,
    openapi_url="/openapi.json" if settings.api_docs_enabled else None,
)


@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    """Lê X-Correlation-Id do request ou gera novo UUID v4.

    Propaga o CID via contextvar para que todos os logs emitidos durante
    o processamento do request incluam ``correlation_id``. O mesmo valor
    é devolvido no header ``X-Correlation-Id`` da response.

    Declarado ANTES dos demais middlewares para executar por último em
    request (o último ``@app.middleware`` declarado executa primeiro).
    """
    cid = request.headers.get("X-Correlation-Id") or str(uuid.uuid4())
    set_correlation_id(cid)
    try:
        response = await call_next(request)
        response.headers["X-Correlation-Id"] = cid
        return response
    finally:
        set_correlation_id(None)


@app.middleware("http")
async def request_locale_middleware(request: Request, call_next):
    """Resolve the request's UI language.

    The SPA sends the active locale on every API call via ``Accept-Language``.
    We parse it once into a contextvar so localized ApiError messages
    and emails render in the user's language for EVERY client — not just the SPA.
    Operational logs stay English.
    """
    from .core.request_locale import parse_accept_language, set_locale

    set_locale(parse_accept_language(request.headers.get("Accept-Language")))
    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Adiciona headers de segurança padrão a todas as respostas.

    Adicionado ANTES do audit middleware (em Starlette o último @middleware
    declarado é o primeiro executado em request e último em response, logo este
    middleware envolve o audit_api_requests na cadeia de execução).

    - X-Content-Type-Options, X-Frame-Options, Referrer-Policy,
      Permissions-Policy: presentes em todos os ambientes.
    - Strict-Transport-Security (HSTS): somente em produção.
    - Content-Security-Policy: strict em produção, permissivo em dev
      (Vite + React DevTools usam unsafe-eval e ws:// para HMR).
    """
    response = await call_next(request)

    # Headers universais
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"

    # HSTS somente em produção (HTTP não tem efeito; em dev pode atrapalhar)
    if settings.APP_ENV == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

    # CSP — strict em produção, permissivo em dev
    if settings.APP_ENV == "production":
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
    else:
        response.headers["Content-Security-Policy"] = (
            "default-src 'self' ws://localhost:* http://localhost:*; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self' ws://localhost:* http://localhost:*"
        )

    return response


@app.middleware("http")
async def audit_api_requests(request: Request, call_next):
    if not request.url.path.startswith("/api") or request.method == "OPTIONS":
        return await call_next(request)

    request, request_payload = await _capture_request_payload(request)

    try:
        response = await call_next(request)
    except Exception as exc:
        if request.url.path not in AUDIT_SKIP_PATHS:
            db = SessionLocal()
            try:
                try:
                    AuditService(db).log_request(
                        request,
                        user=None,
                        user_id=getattr(request.state, "authenticated_user_id", None),
                        username=getattr(request.state, "authenticated_username", None),
                        user_role=getattr(request.state, "authenticated_user_role", None),
                        status_code=500,
                        request_payload=request_payload,
                        detail=f"Unhandled error: {type(exc).__name__}",
                    )
                except Exception as audit_exc:
                    logging.getLogger(__name__).warning("Failed to write audit log: %s", audit_exc)
            finally:
                db.close()
        raise

    if request.url.path not in AUDIT_SKIP_PATHS:
        db = SessionLocal()
        try:
            try:
                AuditService(db).log_request(
                    request,
                    user=None,
                    user_id=getattr(request.state, "authenticated_user_id", None),
                    username=getattr(request.state, "authenticated_username", None),
                    user_role=getattr(request.state, "authenticated_user_role", None),
                    status_code=response.status_code,
                    request_payload=request_payload,
                )
            except Exception as audit_exc:
                logging.getLogger(__name__).warning("Failed to write audit log: %s", audit_exc)
        finally:
            db.close()

    return response

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Localized, coded API errors.
from .core.errors import ApiError, api_error_handler  # noqa: E402

app.add_exception_handler(ApiError, api_error_handler)

protected_api = [Depends(app_auth.require_authenticated_user)]

# Probes liveness/readiness — públicas, sem prefixo /api, sem
# auth. Incluídas ANTES do catch-all do SPA para que /livez e /readyz vençam
# o roteamento. O healthcheck do container bate direto no uvicorn (:8000).
app.include_router(health.router)

app.include_router(auth.router, prefix="/api")
app.include_router(sso.router, prefix="/api")  # SSO Entra — público (sem auth)
# New multi-integration routers
app.include_router(backfill.router, prefix="/api", dependencies=protected_api)
app.include_router(dashboard.router, prefix="/api", dependencies=protected_api)
app.include_router(organizations.router, prefix="/api", dependencies=protected_api)
app.include_router(iris.router, prefix="/api", dependencies=protected_api)
app.include_router(pipeline_health.router, prefix="/api", dependencies=protected_api)
app.include_router(ocsf.router, prefix="/api", dependencies=protected_api)
app.include_router(integrations.router, prefix="/api", dependencies=protected_api)
# Internal service-to-service router — auth via X-Internal-Api-Key, NOT cookie/session.
app.include_router(internal.router, prefix="/api")
# Push-ingestion — o POST /api/ingest/{stream} autentica por TOKEN de
# ingestão (não por sessão), por isso fica FORA de ``protected_api``. As rotas de
# gestão de token aplicam ``require_admin_user`` por-rota.
app.include_router(ingest.router, prefix="/api")
app.include_router(results.router, prefix="/api", dependencies=protected_api)
app.include_router(history.router, prefix="/api", dependencies=protected_api)
app.include_router(queries.router, prefix="/api", dependencies=protected_api)
app.include_router(scheduled_queries.router, prefix="/api", dependencies=protected_api)
# query-jobs (federated search) + correlation-rules are an
# Enterprise lock — their routers ship in centralops_ee and mount via activate(app).
# detections STAYS in the Community core (the scheduler also emits Detections; triage
# is base SOC). The QUERY_RUN/QUERY_SAVE permissions remain in the Core auth matrix.
app.include_router(detections.router, prefix="/api", dependencies=protected_api)
app.include_router(collectors.router, prefix="/api", dependencies=protected_api)
app.include_router(collector_config.router, prefix="/api", dependencies=protected_api)
app.include_router(destinations.router, prefix="/api", dependencies=protected_api)
app.include_router(destinations.lineage_router, prefix="/api", dependencies=protected_api)
app.include_router(routes.router, prefix="/api", dependencies=protected_api)
app.include_router(config_bundle.router, prefix="/api", dependencies=protected_api)
app.include_router(identity_config.router, prefix="/api", dependencies=protected_api)
app.include_router(providers.router, prefix="/api", dependencies=protected_api)
app.include_router(mappings.router, prefix="/api", dependencies=protected_api)
app.include_router(quarantine.router, prefix="/api", dependencies=protected_api)
app.include_router(drift.router, prefix="/api", dependencies=protected_api)
app.include_router(emails.router, prefix="/api", dependencies=protected_api)
app.include_router(api_tokens.router, prefix="/api", dependencies=protected_api)
app.include_router(service_accounts.router, prefix="/api", dependencies=protected_api)

# GET /api/edition — observable edition + licensed features.
from .routers import edition as edition_api  # noqa: E402

app.include_router(edition_api.router, prefix="/api", dependencies=protected_api)

# /api/licenses — admin license activation: persist the signed token
# (encrypted) in the DB so the deploy reads it DB-first, no env editing required.
from .routers import licenses as licenses_api  # noqa: E402

app.include_router(licenses_api.router, prefix="/api", dependencies=protected_api)

# ── Enterprise edition activation ───────────────────────────
# Optional discovery seam: if the proprietary `centralops_ee` package is installed,
# it registers its routers/services/resolvers on top of the Community core (no-op
# when absent). Runs at import in the uvicorn process only, and HERE — after the
# core routers and BEFORE the SPA catch-all below — so EE /api routes are not
# shadowed by the catch-all. The Core never imports the EE beyond this guarded hook.
from .core import edition as edition_core  # noqa: E402 (after routers by design)

edition_core.activate_enterprise(app)
# Warm the edition cache at boot so feature gates and GET /api/edition are pure
# in-memory reads (no keyring file I/O on the first request). Fail-closed.
edition_core.refresh()

# Serve compiled frontend
static_dir = os.path.join(os.path.dirname(__file__), "..", "..", "static")
if os.path.isdir(static_dir):
    assets_dir = os.path.join(static_dir, "assets")
    if os.path.isdir(assets_dir):
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/", include_in_schema=False)
    async def serve_index():
        return FileResponse(os.path.join(static_dir, "index.html"))

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_frontend(full_path: str):
        if full_path.split("/", 1)[0] == "api":
            raise HTTPException(status_code=404, detail="Not Found")

        requested_path = os.path.abspath(os.path.join(static_dir, full_path))
        static_root = os.path.abspath(static_dir)
        try:
            is_inside_static = os.path.commonpath([static_root, requested_path]) == static_root
        except ValueError:
            is_inside_static = False

        if is_inside_static and os.path.isfile(requested_path):
            return FileResponse(requested_path)

        return FileResponse(os.path.join(static_dir, "index.html"))
