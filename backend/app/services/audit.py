"""Serviço de auditoria e helper de resolução de IP do cliente.

``get_client_ip`` aceita X-Forwarded-For SOMENTE quando o request chegou de
um proxy confiável configurado em ``settings.TRUSTED_PROXIES_CIDRS``.  Sem
essa validação, um atacante poderia forjar IPs arbitrários no cabeçalho XFF
para bypassar o lockout de autenticação.
"""

from __future__ import annotations

import ipaddress
import logging
from datetime import datetime, time, timedelta
import threading

from fastapi import Request
from sqlalchemy.orm import Session

from ..core import auth as app_auth
from ..core.config import settings
from ..db import models, repository


logger = logging.getLogger(__name__)

_audit_cleanup_lock = threading.Lock()
_last_audit_cleanup_at: datetime | None = None
_AUDIT_CLEANUP_INTERVAL = timedelta(hours=1)


def _ip_in_cidrs(ip: str, cidrs: list[str]) -> bool:
    """Retorna True se ``ip`` pertence a alguma das redes CIDR em ``cidrs``.

    Ignora silenciosamente entradas inválidas de IP ou CIDR.
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False

    for cidr in cidrs:
        try:
            if addr in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            # CIDR inválido na configuração — ignora e continua
            logger.warning("TRUSTED_PROXIES_CIDRS contém CIDR inválido: %r", cidr)
            continue
    return False


def get_client_ip(request: Request) -> str | None:
    """Resolve o IP real do cliente de forma segura.

    Aceita ``X-Forwarded-For`` SOMENTE se o request chegou diretamente de um
    proxy confiável (configurado via ``settings.TRUSTED_PROXIES_CIDRS``).

    Sem proxy confiável configurado (padrão dev/local): ignora XFF por
    completo e retorna o IP direto do socket. Isso previne que um atacante
    forje ``X-Forwarded-For: <ip_qualquer>`` para bypassar o lockout de
    autenticação em ambientes sem reverse proxy.

    Fluxo de decisão:
    1. Se ``TRUSTED_PROXIES_CIDRS`` está vazio → retorna ``direct_ip``.
    2. Se ``direct_ip`` NÃO está nos CIDRs confiáveis → ignora XFF, retorna ``direct_ip``.
    3. Se ``direct_ip`` está nos CIDRs → usa o primeiro IP da lista XFF (cliente real).
    """
    direct_ip: str | None = request.client.host if request.client else None
    trusted_cidrs = settings.TRUSTED_PROXIES_CIDRS

    # Sem proxies configurados: usa IP direto do socket
    if not trusted_cidrs:
        return direct_ip

    # Sem IP direto disponível: não é possível validar proxy
    if direct_ip is None:
        return None

    # Request não veio de proxy confiável: ignora XFF
    if not _ip_in_cidrs(direct_ip, trusted_cidrs):
        return direct_ip

    # Request veio de proxy confiável: usa primeiro IP do XFF (cliente original)
    xff = request.headers.get("X-Forwarded-For", "")
    first = xff.split(",")[0].strip()
    return first if first else direct_ip


class AuditService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = repository.AuditLogRepository(db)

    def prune_expired_entries(self, *, force: bool = False) -> int:
        global _last_audit_cleanup_at

        retention_days = settings.AUDIT_LOG_RETENTION_DAYS
        if retention_days <= 0:
            return 0

        now = datetime.utcnow()
        with _audit_cleanup_lock:
            if not force and _last_audit_cleanup_at and now - _last_audit_cleanup_at < _AUDIT_CLEANUP_INTERVAL:
                return 0

            cutoff = now - timedelta(days=retention_days)
            deleted = self.repo.delete_older_than(cutoff)
            _last_audit_cleanup_at = now
            return deleted

    def log_event(
        self,
        *,
        action: str,
        endpoint: str,
        user: models.AppUser | None = None,
        user_id: int | None = None,
        username: str | None = None,
        user_role: str | None = None,
        method: str | None = None,
        status_code: int | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        request_payload: str | None = None,
        detail: str | None = None,
    ) -> models.AuditLog:
        self.prune_expired_entries()

        actor_user_id = user_id if user_id is not None else getattr(user, "id", None)
        # Service accounts autenticam como shim com id NEGATIVO (não existe em
        # app_users) — gravado cru, violava a FK e a linha de audit era PERDIDA
        # (jul/2026). None mantém a linha; username='sa:<name>' dá a atribuição.
        actor_user_id = app_auth.persistable_user_id(actor_user_id)
        actor_username = username if username is not None else getattr(user, "username", None)
        actor_role = user_role if user_role is not None else getattr(user, "role", None)

        log = models.AuditLog(
            user_id=actor_user_id,
            username=actor_username,
            user_role=actor_role,
            action=action,
            endpoint=endpoint,
            method=method,
            status_code=status_code,
            ip_address=ip_address,
            user_agent=user_agent,
            request_payload=request_payload,
            detail=detail,
        )
        return self.repo.add(log)

    def log_request(
        self,
        request: Request,
        *,
        user: models.AppUser | None,
        user_id: int | None = None,
        username: str | None = None,
        user_role: str | None = None,
        status_code: int,
        action: str | None = None,
        request_payload: str | None = None,
        detail: str | None = None,
    ) -> models.AuditLog:
        route = request.scope.get("route")
        route_name = getattr(route, "name", None)
        resolved_action = action or route_name
        if not resolved_action:
            resolved_action = f"{request.method} {request.url.path}"

        return self.log_event(
            action=resolved_action,
            endpoint=request.url.path,
            user=user,
            user_id=user_id,
            username=username,
            user_role=user_role,
            method=request.method,
            status_code=status_code,
            ip_address=get_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            request_payload=request_payload,
            detail=detail,
        )

    @staticmethod
    def _parse_date_boundary(raw_value: str | None, *, end_of_day: bool = False) -> datetime | None:
        if not raw_value:
            return None

        normalized = raw_value.strip()
        if not normalized:
            return None

        try:
            if "T" in normalized:
                return datetime.fromisoformat(normalized.replace("Z", "+00:00")).replace(tzinfo=None)

            parsed_date = datetime.strptime(normalized, "%Y-%m-%d").date()
            boundary_time = time.max if end_of_day else time.min
            return datetime.combine(parsed_date, boundary_time)
        except ValueError:
            return None

    def list_entries(
        self,
        *,
        username: str | None = None,
        ip_address: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 500,
        viewer: models.AppUser | None = None,
        include_all: bool = False,
    ) -> list[models.AuditLog]:
        self.prune_expired_entries()

        return self.repo.list(
            username=username,
            ip_address=ip_address,
            date_from=self._parse_date_boundary(date_from, end_of_day=False),
            date_to=self._parse_date_boundary(date_to, end_of_day=True),
            limit=limit,
            viewer=viewer,
            include_all=include_all,
        )
