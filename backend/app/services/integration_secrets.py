"""Store de segredos de integração, vendor-neutro.

Lê/escreve a tabela ``integration_credentials`` via a relationship
``Integration.credentials`` (carregada com ``lazy="selectin"``). Por isso a
LEITURA não exige uma ``Session`` viva — os segredos vêm junto da integração e
sobrevivem a ``db.expunge`` (providers leem creds com a row detached).

A ESCRITA muta a coleção em memória (append/rotate/revoke); o caller é dono do
``commit`` (a cascade ``all, delete-orphan`` persiste as linhas). Todo segredo
passa por ``core.crypto.encrypt`` (Vault-aware) — proibido Fernet ad-hoc.

Cobertura: TODOS os vendors — OAuth genéricos (ninjaone/defender/…), creds
exóticas, e sophos/wazuh. As colunas batizadas legadas do
Integration (``client_secret``/``manager_api_*``/…) não são mais lidas/escritas.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from ..core.crypto import decrypt, encrypt
from ..db import models

logger = logging.getLogger(__name__)


def read_secret(integration: models.Integration, logical_name: str) -> Optional[str]:
    """Decifra o segredo ``logical_name`` da integração, ou ``None``.

    Lê da relationship já carregada — NÃO faz query (funciona com a row detached).
    Ignora segredos revogados.

    FAIL-CLOSED: ``secret_ref`` é SEMPRE escrito via
    ``encrypt()`` (com prefixo). Se ``decrypt`` devolver o valor inalterado
    (passthrough = não reconheceu como cifrado, ex.: corrupção/edição manual de
    DB), NÃO entregamos a credencial não-autenticada — retornamos ``None`` e
    logamos. Token cifrado mas indecifrável já levanta ``ValueError`` no backend
    (também fail-closed)."""
    for cred in (integration.credentials or ()):
        if cred.logical_name == logical_name and cred.revoked_at is None:
            ref = cred.secret_ref
            if not ref:
                return None
            plain = decrypt(ref)
            if plain == ref:
                logger.error(
                    "integration_secrets: secret_ref de '%s' (integration_id=%s) não está "
                    "cifrado — recusando (fail-closed).",
                    logical_name, getattr(integration, "id", "?"),
                )
                return None
            return plain
    return None


def has_secret(integration: models.Integration, logical_name: str) -> bool:
    """True se há um segredo ativo (não revogado) com esse nome lógico."""
    return any(
        c.logical_name == logical_name and c.revoked_at is None
        for c in (integration.credentials or ())
    )


def write_secret(
    integration: models.Integration,
    logical_name: str,
    plaintext: str,
    *,
    key_version: Optional[str] = None,
) -> models.IntegrationCredential:
    """Upsert (cria ou ROTACIONA) um segredo. Cifra via ``encrypt`` (Vault-aware).

    Muta a coleção ``integration.credentials`` — o caller faz o ``commit``. Na
    criação (integração ainda transiente, sem ``id``) a cascade preenche o
    ``integration_id`` no flush. Numa rotação incrementa ``secret_version`` e
    carimba ``rotated_at`` (e des-revoga, se estava revogado).

    Valida entradas — ``logical_name`` não-vazio e
    ``plaintext`` não-vazio (segredo vazio mascararia falha de config como
    'sem credencial')."""
    if not logical_name or not logical_name.strip():
        raise ValueError("integration_secrets.write_secret: logical_name vazio")
    if not plaintext:
        raise ValueError(
            f"integration_secrets.write_secret: plaintext vazio para '{logical_name}'"
        )
    now = datetime.utcnow()
    existing = next(
        (c for c in integration.credentials if c.logical_name == logical_name), None
    )
    if existing is None:
        cred = models.IntegrationCredential(
            logical_name=logical_name,
            secret_ref=encrypt(plaintext),
            key_version=key_version,
            secret_version=1,
            created_at=now,
        )
        integration.credentials.append(cred)
        return cred
    existing.secret_ref = encrypt(plaintext)
    existing.key_version = key_version
    existing.secret_version = (existing.secret_version or 1) + 1
    existing.rotated_at = now
    existing.revoked_at = None
    return existing


def revoke_secret(integration: models.Integration, logical_name: str) -> bool:
    """Revoga (sem apagar) o segredo — preserva a trilha de auditoria.

    Retorna True se algo foi revogado. O caller faz o ``commit``."""
    revoked = False
    now = datetime.utcnow()
    for cred in integration.credentials:
        if cred.logical_name == logical_name and cred.revoked_at is None:
            cred.revoked_at = now
            revoked = True
    return revoked
