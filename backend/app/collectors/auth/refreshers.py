"""Adaptadores vendor-específicos de refresh OAuth2.

Cada refresher é uma ``async fn(integration_id: int) -> dict`` que:

1. Lê ``Integration`` do banco (abre ``SessionLocal`` efêmera).
2. Lê ``client_secret`` do store ``integration_credentials`` (``read_secret``).
3. Faz a chamada OAuth2 real contra o IdP do vendor.
4. Persiste os novos tokens no store ``integration_credentials``
   (reutiliza ``IntegrationRepository.update_integration_tokens``
   para o Sophos, que já faz whoami).
5. Retorna dict no formato esperado pelo ``oauth_cache``:
   ``{"access_token", "expires_in", "refresh_token"}``.

Sophos:
    Reutiliza o ``TokenManager`` existente (``backend/app/services/token_manager.py``)
    via ``asyncio.to_thread`` — evita reescrever OAuth/whoami.

Microsoft Defender (Graph):
    POST https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token
    com ``grant_type=client_credentials`` e ``scope=https://graph.microsoft.com/.default``.

NinjaOne:
    POST https://app.ninjarmm.com/ws/oauth/token com client_credentials
    e ``scope=monitoring management`` (conforme escopo da integração).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Awaitable, Callable, Dict

import aiohttp

from ...db import database, models
from ...services import integration_secrets

logger = logging.getLogger(__name__)

RefreshFn = Callable[[int], Awaitable[Dict[str, object]]]


def _load_integration(integration_id: int) -> models.Integration:
    with database.SessionLocal() as db:
        integ = (
            db.query(models.Integration)
            .filter(models.Integration.id == integration_id)
            .first()
        )
        if not integ:
            raise RuntimeError(f"integration id={integration_id} not found")
        # Força carregamento dos atributos antes de fechar a sessão.
        # ``credentials`` (lazy="selectin") materializa os segredos do store
        # vendor-neutro ANTES do expunge — read_secret funciona com a row detached.
        _ = (
            integ.client_id,
            integ.client_secret,
            integ.tenant_id,
            integ.region,
            integ.platform,
            integ.name,
            integ.credentials,
        )
        db.expunge(integ)
        return integ


# ── Sophos ────────────────────────────────────────────────────────────


async def sophos_refresher(integration_id: int) -> Dict[str, object]:
    """Força ida ao IdP do Sophos Central — **nunca** reutiliza o
    ``access_token`` cifrado no banco.

    Contexto: o ``oauth_cache`` já é a autoridade de caching (Redis com
    TTL). Quando ele chama este refresher, é porque o cache venceu ou
    está vazio — **o sinal de "preciso de token novo"**. Qualquer cache
    abaixo deste nível (como ``TokenManager.ensure_valid_token``, que
    devolve o token do banco sem validar ``exp``) vaza tokens expirados
    e causa ``401`` constantes no worker.

    Ordem:
    1. Se ``refresh_token`` existe → tenta ``auth.refresh(refresh_token)``.
       Sophos rotaciona o ``refresh_token`` no response — guardamos o novo.
    2. Fallback ou falha → ``client_credentials`` (full auth) +
       whoami para descobrir ``region`` / ``tenant_id``.

    Persiste sempre no banco via ``IntegrationRepository`` (secrets cifrados).
    """
    from ...db.repository import IntegrationRepository
    from ...services.auth import SophosAuthService

    def _sync_refresh() -> Dict[str, object]:
        with database.SessionLocal() as db:
            integ = (
                db.query(models.Integration)
                .filter(models.Integration.id == integration_id)
                .first()
            )
            if not integ:
                raise RuntimeError(f"integration id={integration_id} not found")

            # Sophos Partner Mode: child integrations inherit OAuth credentials
            # from their parent. Resolve the credential holder once here so the
            # refresh path works for legacy standalone tenants AND for the new
            # Partner-managed children without forking the logic below.
            #
            # ``getattr`` with default ``None`` keeps test fakes (``_FakeIntegration``)
            # that predate the Partner schema working — they're just standalone tenants.
            credential_holder = integ
            kind = getattr(integ, "kind", "tenant") or "tenant"
            parent_id = getattr(integ, "parent_integration_id", None)
            if kind == "tenant" and parent_id is not None:
                parent = db.get(models.Integration, parent_id)
                if parent is None:
                    raise RuntimeError(
                        f"integration id={integration_id} child of "
                        f"missing parent id={parent_id}"
                    )
                credential_holder = parent

            client_id = (credential_holder.client_id or "").strip()
            if not client_id:
                raise RuntimeError(
                    f"integration id={credential_holder.id} sem client_id"
                )
            # secrets do holder vêm do store integration_credentials.
            client_secret = integration_secrets.read_secret(credential_holder, "client_secret")
            if not client_secret:
                raise RuntimeError(
                    f"integration id={credential_holder.id} sem client_secret"
                )
            refresh_token_plain = integration_secrets.read_secret(
                credential_holder, "refresh_token"
            )

            auth = SophosAuthService(client_id, client_secret)
            repo = IntegrationRepository(db)

            # 1. Refresh token (rápido — 1 HTTP).
            if refresh_token_plain:
                try:
                    logger.info(
                        "sophos_refresher: refresh_token integration=%s holder=%s",
                        integration_id,
                        credential_holder.id,
                    )
                    tokens = auth.refresh(refresh_token_plain)
                    new_access = tokens["access_token"]
                    new_refresh = tokens.get(
                        "refresh_token", refresh_token_plain
                    )
                    repo.update_tokens(
                        credential_holder,
                        access_token=new_access,
                        refresh_token=new_refresh,
                    )
                    return {
                        "access_token": new_access,
                        "expires_in": int(tokens.get("expires_in", 3600)),
                    }
                except Exception as exc:
                    logger.warning(
                        "sophos_refresher: refresh_token rejeitado "
                        "integration=%s holder=%s (%s) — caindo para client_credentials",
                        integration_id,
                        credential_holder.id,
                        type(exc).__name__,
                    )

            # 2. Fallback: client_credentials.
            logger.info(
                "sophos_refresher: client_credentials integration=%s holder=%s",
                integration_id,
                credential_holder.id,
            )
            tokens = auth.authenticate()
            new_access = tokens["access_token"]
            new_refresh = tokens.get("refresh_token", "")

            if credential_holder.id == integ.id:
                # Standalone tenant — also rediscovers region/tenant_id (legacy flow).
                region, tenant_id = auth.discover_region_and_tenant(new_access)
                repo.update_integration_tokens(
                    integration_id=credential_holder.id,
                    access_token=new_access,
                    refresh_token=new_refresh,
                    region=region,
                    tenant_id=tenant_id,
                )
                # Mirror tenant_id into the new external_id column when empty.
                # Guarded with hasattr() so legacy test fakes that predate the
                # Partner schema don't blow up — they're just standalone tenants.
                if (
                    hasattr(credential_holder, "external_id")
                    and not credential_holder.external_id
                ):
                    credential_holder.external_id = tenant_id
                    if hasattr(credential_holder, "id_type"):
                        credential_holder.id_type = "tenant"
                    credential_holder.updated_at = datetime.utcnow()
                    db.commit()
            else:
                # Child path — store tokens on the parent. The child keeps using
                # the parent's tokens via SophosProvider on the next call.
                repo.update_tokens(
                    credential_holder,
                    access_token=new_access,
                    refresh_token=new_refresh,
                )
            return {
                "access_token": new_access,
                "expires_in": int(tokens.get("expires_in", 3600)),
            }

    return await asyncio.to_thread(_sync_refresh)


# ── Microsoft Defender (Graph Security) ───────────────────────────────

_GRAPH_TOKEN_URL = (
    "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
)
_GRAPH_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"


async def defender_refresher(integration_id: int) -> Dict[str, object]:
    integ = _load_integration(integration_id)
    if not integ.tenant_id:
        raise RuntimeError(
            f"integration id={integration_id} sem tenant_id (Azure AD)"
        )

    # vendor-neutro lê o client_secret do store (integration_credentials).
    client_secret = integration_secrets.read_secret(integ, "client_secret") or ""
    data = {
        "grant_type": "client_credentials",
        "client_id": integ.client_id or "",
        "client_secret": client_secret,
        "scope": _GRAPH_DEFAULT_SCOPE,
    }
    url = _GRAPH_TOKEN_URL.format(tenant=integ.tenant_id)

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, data=data) as r:
            r.raise_for_status()
            payload = await r.json()

    return {
        "access_token": payload["access_token"],
        "expires_in": int(payload.get("expires_in", 3600)),
    }


# ── NinjaOne ──────────────────────────────────────────────────────────

_NINJA_TOKEN_URL = "https://app.ninjarmm.com/ws/oauth/token"


async def ninjaone_refresher(integration_id: int) -> Dict[str, object]:
    integ = _load_integration(integration_id)
    # vendor-neutro lê o client_secret do store (integration_credentials).
    client_secret = integration_secrets.read_secret(integ, "client_secret") or ""
    data = {
        "grant_type": "client_credentials",
        "client_id": integ.client_id or "",
        "client_secret": client_secret,
        "scope": "monitoring management",
    }

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(_NINJA_TOKEN_URL, data=data) as r:
            r.raise_for_status()
            payload = await r.json()

    return {
        "access_token": payload["access_token"],
        "expires_in": int(payload.get("expires_in", 3600)),
    }


# ── Wazuh (Indexer, basic auth) ───────────────────────────────────────


async def wazuh_indexer_refresher(integration_id: int) -> Dict[str, object]:
    """No-op para o contrato do ``oauth_cache``.

    O Wazuh Indexer usa **basic auth**, não OAuth/bearer. O
    ``WazuhDetectionsCollector`` é auto-contido — lê ``indexer_url`` + credenciais
    do store no ``collect()`` e monta o header ``Basic`` ele mesmo, ignorando
    ``ctx.headers``. Este refresher só devolve um token vazio para satisfazer o
    framework (nada é cifrado/colocado no Redis além do placeholder)."""
    return {"access_token": "", "expires_in": 3600}


# ── Dispatcher ────────────────────────────────────────────────────────
#
# O ``refresher_for()`` é mantido por compatibilidade (imports antigos) mas
# hoje delega ao ``collectors.registry`` — que é fonte única da verdade.
# Novos vendors **não** devem editar este arquivo; apenas expor a função de
# refresh async e registrá-la no ``CollectorRegistration``.


def refresher_for(platform: str) -> RefreshFn:
    # Import tardio evita ciclo registry→vendors→refreshers.
    from ..registry import iter_for_platform

    for reg in iter_for_platform(platform):
        return reg.refresh_fn  # todos os streams do mesmo vendor compartilham refresher
    raise RuntimeError(f"no refresher registered for platform={platform!r}")
