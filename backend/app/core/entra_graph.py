"""Cliente sincrono para a Microsoft Graph API — Fase 2B (Graph-sync).

Uso exclusivo pela task Celery ``sync_entra_users`` (worker prefork, sem
event loop proprio). Utiliza ``httpx.Client`` sincrono para manter compatibilidade
com o ambiente Celery sem necessitar de asyncio.

Nenhum segredo e logado: o argumento ``cfg`` nunca e passado para o logger.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .identity_config import IdentitySnapshot

_LOG = logging.getLogger(__name__)

# Base da Graph API v1.
_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
# Scope para client_credentials contra a Graph.
_GRAPH_SCOPE = "https://graph.microsoft.com/.default"
# Timeout padrao para toda chamada HTTP (segundos).
_HTTP_TIMEOUT = 30.0


class EntraGraphError(RuntimeError):
    """Erro fatal na comunicacao com a Graph API ou token endpoint."""


def get_app_token(cfg: IdentitySnapshot) -> str:
    """Obtem token client_credentials para a Graph API.

    POST https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token
      grant_type=client_credentials
      client_id={cfg.entra_client_id}
      client_secret={cfg.entra_client_secret}   # ja decifrado no snapshot
      scope=https://graph.microsoft.com/.default

    Retorna o access_token (str).
    Levanta EntraGraphError em qualquer falha HTTP ou de autenticacao.
    Nao faz cache — a task chama uma vez por ciclo de sync.
    """
    if not cfg.entra_tenant_id:
        raise EntraGraphError("entra_tenant_id nao configurado")
    if not cfg.entra_client_id:
        raise EntraGraphError("entra_client_id nao configurado")
    if not cfg.entra_client_secret:
        raise EntraGraphError("entra_client_secret nao configurado")

    # Respeita o authority configurado (sovereign clouds: US Gov / China).
    authority = (cfg.entra_authority or "https://login.microsoftonline.com").rstrip("/")
    token_url = f"{authority}/{cfg.entra_tenant_id}/oauth2/v2.0/token"
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            resp = client.post(
                token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": cfg.entra_client_id,
                    "client_secret": cfg.entra_client_secret,
                    "scope": _GRAPH_SCOPE,
                },
            )
    except httpx.HTTPError as exc:
        raise EntraGraphError(f"Falha de rede ao obter token Entra: {exc}") from exc

    if resp.status_code != 200:
        # Extrai mensagem de erro sem logar o corpo completo (pode conter hint de secret)
        try:
            body = resp.json()
            err_detail = body.get("error_description") or body.get("error") or str(resp.status_code)
        except Exception:  # noqa: BLE001
            err_detail = str(resp.status_code)
        raise EntraGraphError(f"Token endpoint recusou autenticacao: {err_detail[:300]}")

    try:
        token = resp.json()["access_token"]
    except (KeyError, Exception) as exc:
        raise EntraGraphError(f"Resposta do token endpoint sem access_token: {exc}") from exc

    return str(token)


def get_app_role_map(
    cfg: IdentitySnapshot, token: str, client: "httpx.Client | None" = None
) -> dict[str, str]:
    """Resolve appRoleId (GUID) -> value (str) do App Registration.

    GET /v1.0/servicePrincipals(appId='{client_id}')?$select=appRoles
    Reusa ``client`` quando fornecido (evita handshake TLS extra por ciclo).
    Retorna dict {role_id_guid: role_value_str}. Levanta EntraGraphError em falha.
    """
    url = f"{_GRAPH_BASE}/servicePrincipals(appId='{cfg.entra_client_id}')"
    owns = client is None
    c = client or httpx.Client(timeout=_HTTP_TIMEOUT)
    try:
        resp = c.get(
            url,
            params={"$select": "appRoles"},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
    except httpx.HTTPError as exc:
        raise EntraGraphError(f"Falha de rede ao listar appRoles: {exc}") from exc
    finally:
        if owns:
            c.close()

    if resp.status_code != 200:
        raise EntraGraphError(f"Graph recusou GET servicePrincipals: {resp.status_code}")

    try:
        body = resp.json()
        roles: list[dict[str, Any]] = body.get("appRoles") or []
    except Exception as exc:
        raise EntraGraphError(f"Resposta malformada ao obter appRoles: {exc}") from exc

    return {r["id"]: r["value"] for r in roles if r.get("id") and r.get("value")}


def _fetch_users_batch(
    client: httpx.Client, token: str, oids: list[str]
) -> dict[str, dict[str, Any]]:
    """Busca detalhes de varios usuarios numa unica chamada (elimina o N+1).

    POST /v1.0/directoryObjects/getByIds — ate 1000 ids por request. Retorna
    ``{oid: user_object}``. Falha de rede/HTTP levanta EntraGraphError: como e
    UMA chamada por lote (nao N), um erro nao encolhe silenciosamente o conjunto
    de autorizados (o que poderia disparar deprovision indevido).
    """
    details: dict[str, dict[str, Any]] = {}
    if not oids:
        return details
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    for start in range(0, len(oids), 1000):
        chunk = oids[start:start + 1000]
        try:
            resp = client.post(
                f"{_GRAPH_BASE}/directoryObjects/getByIds",
                json={"ids": chunk, "types": ["user"]},
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise EntraGraphError(f"Falha de rede no getByIds: {exc}") from exc
        if resp.status_code != 200:
            raise EntraGraphError(f"Graph recusou getByIds: {resp.status_code}")
        try:
            for obj in resp.json().get("value") or []:
                oid = obj.get("id")
                if oid:
                    details[oid] = obj
        except Exception as exc:  # noqa: BLE001
            raise EntraGraphError(f"Resposta malformada de getByIds: {exc}") from exc
    return details


def list_app_members(cfg: IdentitySnapshot, token: str) -> list[dict[str, Any]]:
    """Lista os usuarios atribuidos ao App Registration via appRoleAssignedTo.

    Pagina os assignments (@odata.nextLink), resolve o papel local de cada um
    (appRoleId -> value via cfg.entra_role_map; fallback cfg.entra_default_role)
    e busca os detalhes de TODOS em lote via ``getByIds`` (1 req por 1000, sem
    N+1). Um unico ``httpx.Client`` e reusado em todas as chamadas do ciclo.

    Shape de cada item:
      {subject, email, display_name, role, is_global, account_enabled}.

    Usuarios cujo detalhe nao retorna do getByIds (deletados/sem permissao) sao
    incluidos com email/display_name=None — preserva a autorizacao e evita
    deprovision indevido. Membros com principalType != 'User' sao ignorados.
    Levanta EntraGraphError em falha HTTP fatal.
    """
    entra_role_map: dict[str, str] = cfg.entra_role_map or {}
    assignments_url = (
        f"{_GRAPH_BASE}/servicePrincipals(appId='{cfg.entra_client_id}')"
        "/appRoleAssignedTo"
    )

    # (oid, role local) por usuario — ordem preservada, dedupe por oid.
    assigned: list[tuple[str, str]] = []
    seen: set[str] = set()
    details: dict[str, dict[str, Any]] = {}

    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            role_map_raw = get_app_role_map(cfg, token, client=client)

            next_url: str | None = assignments_url
            params: dict[str, str] | None = {"$top": "999"}
            while next_url:
                try:
                    resp = client.get(
                        next_url,
                        params=params,
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Content-Type": "application/json",
                        },
                    )
                except httpx.HTTPError as exc:
                    raise EntraGraphError(
                        f"Falha de rede ao listar appRoleAssignedTo: {exc}"
                    ) from exc
                if resp.status_code != 200:
                    raise EntraGraphError(
                        f"Graph recusou GET appRoleAssignedTo: {resp.status_code}"
                    )
                try:
                    body = resp.json()
                    assignments: list[dict[str, Any]] = body.get("value") or []
                    next_url = body.get("@odata.nextLink")
                    params = None  # o nextLink ja carrega a query
                except Exception as exc:
                    raise EntraGraphError(
                        f"Resposta malformada de appRoleAssignedTo: {exc}"
                    ) from exc

                for assignment in assignments:
                    if assignment.get("principalType") != "User":
                        continue
                    oid = assignment.get("principalId")
                    if not oid or oid in seen:
                        continue
                    seen.add(oid)
                    role_value = role_map_raw.get(assignment.get("appRoleId", ""), "")
                    assigned.append(
                        (oid, entra_role_map.get(role_value, cfg.entra_default_role))
                    )

            # Detalhes de todos os usuarios em lote (sem N+1).
            details = _fetch_users_batch(client, token, [oid for oid, _ in assigned])
    except EntraGraphError:
        raise
    except Exception as exc:
        raise EntraGraphError(f"Erro inesperado ao listar membros do app: {exc}") from exc

    members: list[dict[str, Any]] = []
    for oid, local_role in assigned:
        detail = details.get(oid)
        if detail is None:
            _LOG.warning(
                "entra_graph: detalhes ausentes p/ oid=%s — incluido sem "
                "email/displayName para preservar autorizacao",
                oid,
            )
            members.append({
                "subject": oid,
                "email": None,
                "display_name": None,
                "role": local_role,
                "is_global": bool(cfg.entra_default_is_global),
                "account_enabled": True,
            })
            continue
        raw_email = detail.get("mail") or detail.get("userPrincipalName")
        email = raw_email.strip().lower() if raw_email and raw_email.strip() else None
        members.append({
            "subject": oid,
            "email": email,
            "display_name": detail.get("displayName"),
            "role": local_role,
            "is_global": bool(cfg.entra_default_is_global),
            "account_enabled": bool(detail.get("accountEnabled", True)),
        })
    return members
