"""Task Celery de sincronizacao de usuarios do Entra via Graph — Fase 2B.

Fluxo:
  1. Carrega config da tabela identity_config.
  2. Verifica prerequisitos (sync habilitado, credenciais preenchidas).
  3. Adquire lock Redis para evitar execucoes concorrentes.
  4. Busca membros do App Registration via Graph API.
  5. Faz upsert dos usuarios locais (criar / atualizar).
  6. Deprovision opcional de contas que sairam do App Registration — com
     fail-safes anti-lockout (lista vazia, circuit-breaker, ultimo admin).
  7. Persiste status e resultado no banco.
  8. Libera o lock.

NUNCA levanta excecao — todo caminho de erro retorna o dict de resultado
e grava o status de erro no banco.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any, Dict

import redis as _redis_sync
from celery import shared_task
from sqlalchemy.orm import Session

from ..core.config import settings
from ..core.identity_config import load as _load_cfg
from ..db import database, models, repository
from ..services.api_tokens import ApiTokenService
from ..services.audit import AuditService

_LOG = logging.getLogger(__name__)

# Chave do lock no Redis — evita execucoes concorrentes.
_LOCK_KEY = "sync:entra:users"
# TTL do lock em segundos (15 min — margem para tenants grandes).
_LOCK_TTL = 900

# ── Fail-safes de deprovision (anti-lockout) ──────────────────────────
# Circuit-breaker: nao desativar mais do que esta fracao dos usuarios Entra
# ativos num unico ciclo (protege contra resposta parcial/errada do Graph).
_DEPROVISION_MAX_FRACTION = 0.5
# A fracao so e aplicada quando ha pelo menos este numero de usuarios ativos
# (em tenants minusculos, desativar 1 de 2 e legitimo).
_DEPROVISION_MIN_GUARD = 4


# ── Helpers internos ──────────────────────────────────────────────────


def _get_redis() -> "_redis_sync.Redis | None":
    """Cliente Redis sincrono para o lock. Retorna None se indisponivel."""
    try:
        redis_url = settings.REDIS_URL or "redis://localhost:6379/0"
        return _redis_sync.Redis.from_url(redis_url, decode_responses=True)
    except Exception as exc:  # noqa: BLE001
        _LOG.warning("entra_sync: Redis indisponivel para lock: %s", exc)
        return None


def _update_sync_status(
    db: Session,
    *,
    status: str,
    summary: Dict[str, Any] | None = None,
) -> None:
    """Grava status na linha id=1 da identity_config. Tolerante a falha.

    ``summary=None`` (ex: ao marcar 'running') NAO sobrescreve o
    ``entra_last_sync_summary`` anterior — preserva o resultado do ciclo
    anterior para quem consultar /sync-status durante a execucao.
    """
    try:
        now = datetime.utcnow()
        kwargs: Dict[str, Any] = {
            "entra_last_sync_at": now,
            "entra_last_sync_status": status,
        }
        if summary is not None:
            kwargs["entra_last_sync_summary"] = json.dumps(
                {**summary, "finished_at": now.isoformat()},
                separators=(",", ":"),
            )
        repository.IdentityConfigRepository(db).update(**kwargs)
        db.commit()
    except Exception:  # noqa: BLE001
        _LOG.warning("sync_entra_users: falha ao gravar status", exc_info=True)


def _resolve_username(email: str | None, subject: str, db: Session) -> str | None:
    """Gera um username unico a partir do email ou do subject do Entra.

    Tenta a base e ate 10 variantes com sufixo numerico (base, base_1..base_10).
    Retorna None se todas colidirem.
    """
    user_repo = repository.UserRepository(db)
    if email and "@" in email:
        base = email.split("@")[0].lower()
    else:
        base = f"entra_{subject[:8]}"

    candidate = base
    for attempt in range(1, 11):
        if user_repo.get_by_username(candidate) is None:
            return candidate
        candidate = f"{base}_{attempt}"
    return None


def _is_last_active_admin(db: Session, user: models.AppUser) -> bool:
    """True se ``user`` for admin ativo E o unico admin ativo restante.

    Replica o invariante 'ao menos um admin ativo' que os endpoints manuais
    ja garantem (routers/auth.py). O sync NUNCA pode causar lockout admin.
    """
    if user.role != "admin" or not user.is_active:
        return False
    count = (
        db.query(models.AppUser)
        .filter(
            models.AppUser.role == "admin",
            models.AppUser.is_active.is_(True),
        )
        .count()
    )
    return count <= 1


def _audit_sync_event(db: Session, action: str, user: models.AppUser, detail: dict) -> None:
    """Registra evento de auditoria de uma mutacao automatica do sync."""
    try:
        AuditService(db).log_event(
            action=action,
            endpoint="celery:sync_entra_users",
            method="TASK",
            username=user.username,
            user_role=user.role,
            detail=json.dumps(detail, separators=(",", ":")),
        )
    except Exception:  # noqa: BLE001
        _LOG.warning("entra_sync: falha ao auditar %s", action, exc_info=True)


def _deactivate_user(db: Session, user: models.AppUser, result: Dict[str, Any], reason: str) -> bool:
    """Desativa um usuario federado com guard de ultimo admin + revoga acesso
    (sessoes e PATs) + auditoria. Retorna True se desativou."""
    if _is_last_active_admin(db, user):
        result["warnings"].append(
            f"{user.username!r}: ultimo admin ativo — NAO desativado ({reason})"
        )
        return False
    try:
        repository.UserRepository(db).update(user, is_active=False)
        repository.UserSessionRepository(db).revoke_all_for_user(user.id)
        ApiTokenService(db).revoke_all_for_user(user.id)
        _audit_sync_event(
            db, "entra_sync_user_deactivated", user,
            {"subject": user.external_subject, "reason": reason},
        )
        return True
    except Exception as exc:  # noqa: BLE001
        result["errors"].append(f"Falha ao desativar {user.username}: {exc}")
        return False


# ── Task principal ────────────────────────────────────────────────────


@shared_task(bind=True, queue="maintenance")
def sync_entra_users(self: Any) -> Dict[str, Any]:
    """Sincroniza usuarios atribuidos ao App Registration do Entra com AppUser.

    Retorna dict JSON-serializavel; NUNCA levanta excecao.
    """
    started_at = datetime.utcnow()
    result: Dict[str, Any] = {
        "created": 0,
        "updated": 0,
        "deactivated": 0,
        "errors": [],
        "warnings": [],
        "started_at": started_at.isoformat(),
        "status": "ok",
        "finished_at": None,
    }

    # Passo 2 — Carrega config e verifica se sync esta habilitado
    try:
        with database.SessionLocal() as db_cfg:
            cfg = _load_cfg(db_cfg)
    except Exception as exc:  # noqa: BLE001
        result["status"] = "error"
        result["errors"].append(f"Falha ao carregar identity_config: {exc}")
        result["finished_at"] = datetime.utcnow().isoformat()
        return result

    if not cfg.entra_sync_enabled:
        result["status"] = "ok"
        result["warnings"].append("sync desabilitado (entra_sync_enabled=false)")
        result["finished_at"] = datetime.utcnow().isoformat()
        return result

    # Prerequisitos obrigatorios antes de adquirir o lock
    missing = [
        field
        for field, val in (
            ("entra_tenant_id", cfg.entra_tenant_id),
            ("entra_client_id", cfg.entra_client_id),
            ("entra_client_secret", cfg.entra_client_secret),
        )
        if not val
    ]
    if missing:
        result["status"] = "error"
        result["errors"].append(
            f"Configuracao incompleta — campos obrigatorios ausentes: {', '.join(missing)}"
        )
        result["finished_at"] = datetime.utcnow().isoformat()
        return result

    # Passo 3 — Adquirir lock Redis
    redis_client = _get_redis()
    lock_value = uuid.uuid4().hex
    if redis_client is not None:
        acquired = bool(redis_client.set(_LOCK_KEY, lock_value, nx=True, ex=_LOCK_TTL))
    else:
        # Sem Redis (dev/test): prossegue sem lock.
        acquired = True

    if not acquired:
        result["status"] = "error"
        result["errors"].append("outro sync de usuarios Entra em andamento (lock ativo)")
        result["finished_at"] = datetime.utcnow().isoformat()
        return result

    # Grava 'running' (sem mexer no summary anterior).
    try:
        with database.SessionLocal() as db_running:
            _update_sync_status(db_running, status="running")
    except Exception:  # noqa: BLE001
        pass

    try:
        # Passo 4 — Buscar membros via Graph
        from ..core.entra_graph import get_app_token, list_app_members

        try:
            token = get_app_token(cfg)
            members = list_app_members(cfg, token)
        except Exception as exc:  # noqa: BLE001 — inclui EntraGraphError e rede
            result["status"] = "error"
            result["errors"].append(f"Graph API falhou: {exc}")
            with database.SessionLocal() as db_err:
                _update_sync_status(db_err, status="error", summary=result)
            result["finished_at"] = datetime.utcnow().isoformat()
            return result

        set_authorized_subjects: set[str] = {m["subject"] for m in members}

        with database.SessionLocal() as db:
            user_repo = repository.UserRepository(db)

            # Passo 5 — Upsert (criar/atualizar) usuarios
            for member in members:
                subject: str = member["subject"]
                email: str | None = member["email"]
                role: str = member["role"]
                is_global: bool = member["is_global"]
                account_enabled: bool = member["account_enabled"]
                display_name: str | None = member.get("display_name")

                existing = user_repo.get_by_external_subject("entra", subject)
                if existing is None and email:
                    existing = user_repo.get_by_email(email)

                if existing is None:
                    username = _resolve_username(email, subject, db)
                    if username is None:
                        result["errors"].append(
                            f"Sem username unico para subject={subject} email={email} — ignorado"
                        )
                        continue
                    try:
                        user_repo.add(models.AppUser(
                            username=username,
                            email=email,
                            display_name=display_name,
                            auth_provider="entra",
                            external_subject=subject,
                            role=role,
                            is_global=is_global,
                            is_active=account_enabled,
                            password_hash=None,
                        ))
                        result["created"] += 1
                    except Exception as exc:  # noqa: BLE001
                        result["errors"].append(f"Falha ao criar subject={subject}: {exc}")
                    continue

                # Conta local com mesmo email: NUNCA federa nem desativa —
                # so atualiza display_name/email; registra como aviso (nao erro).
                if existing.auth_provider != "entra":
                    local_changes: dict[str, Any] = {}
                    if existing.display_name != display_name:
                        local_changes["display_name"] = display_name
                    if local_changes:
                        user_repo.update(existing, **local_changes)
                        result["warnings"].append(
                            f"Conta local {existing.username!r} compartilha email={email!r} "
                            f"com membro Entra — so display_name atualizado"
                        )
                    continue

                # Conta federada: reconcilia campos gerenciados.
                changed = False
                update_kwargs: dict[str, Any] = {}
                if existing.email != email:
                    update_kwargs["email"] = email
                if existing.display_name != display_name:
                    update_kwargs["display_name"] = display_name
                if existing.role != role:
                    update_kwargs["role"] = role
                if existing.is_global != is_global:
                    update_kwargs["is_global"] = is_global
                if update_kwargs:
                    try:
                        user_repo.update(existing, **update_kwargs)
                        changed = True
                    except Exception as exc:  # noqa: BLE001
                        result["errors"].append(f"Falha ao atualizar {existing.username}: {exc}")

                # Estado ativo segue o accountEnabled do Entra.
                if not account_enabled and existing.is_active:
                    if _deactivate_user(db, existing, result, reason="accountEnabled=false no Entra"):
                        changed = True
                elif account_enabled and not existing.is_active:
                    user_repo.update(existing, is_active=True)
                    _audit_sync_event(
                        db, "entra_sync_user_reactivated", existing,
                        {"subject": subject},
                    )
                    changed = True

                if changed:
                    result["updated"] += 1

            # Passo 6 — Deprovision (com fail-safes anti-lockout)
            if cfg.entra_sync_deprovision:
                entra_active_users = (
                    db.query(models.AppUser)
                    .filter(
                        models.AppUser.auth_provider == "entra",
                        models.AppUser.is_active.is_(True),
                    )
                    .all()
                )
                candidates = [
                    u for u in entra_active_users
                    if u.external_subject not in set_authorized_subjects
                ]
                total_active = len(entra_active_users)

                if not members:
                    # FAIL-SAFE 1: Graph vazio (transiente/permissao/paginacao) nunca
                    # deprovisiona — evitaria lockout em massa.
                    result["errors"].append(
                        "Graph retornou 0 membros — deprovision PULADO (fail-safe anti-lockout)"
                    )
                elif (
                    candidates
                    and total_active >= _DEPROVISION_MIN_GUARD
                    and len(candidates) / total_active > _DEPROVISION_MAX_FRACTION
                ):
                    # FAIL-SAFE 2: circuit-breaker — desativacao em massa suspeita.
                    result["errors"].append(
                        f"Deprovision PULADO: {len(candidates)}/{total_active} usuarios "
                        f"seriam desativados (> {int(_DEPROVISION_MAX_FRACTION * 100)}%) — "
                        f"possivel erro de sync; revise manualmente."
                    )
                else:
                    for u in candidates:
                        if _deactivate_user(db, u, result, reason="ausente do App Registration"):
                            result["deactivated"] += 1

            # Passo 7 — Persiste status final (warnings NAO degradam o status)
            final_status = "ok" if not result["errors"] else "partial"
            result["status"] = final_status
            _update_sync_status(db, status=final_status, summary=result)

    except Exception as exc:  # noqa: BLE001
        result["status"] = "error"
        result["errors"].append(f"Erro inesperado na task: {exc}")
        _LOG.error("sync_entra_users: erro inesperado", exc_info=True)
        try:
            with database.SessionLocal() as db_final_err:
                _update_sync_status(db_final_err, status="error", summary=result)
        except Exception:  # noqa: BLE001
            pass

    finally:
        # Passo 8 — Libera o lock Redis (so se foi este worker que o adquiriu).
        if acquired and redis_client is not None:
            try:
                if redis_client.get(_LOCK_KEY) == lock_value:
                    redis_client.delete(_LOCK_KEY)
            except Exception:  # noqa: BLE001
                pass
            try:
                redis_client.close()
            except Exception:  # noqa: BLE001
                pass

    result["finished_at"] = datetime.utcnow().isoformat()
    return result
