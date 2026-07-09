"""Tokens de ingestão push (FortiGate/WEC/agente → ``POST /api/ingest/...``).

Um edge-collector (Vector/OTel/agente) autentica no endpoint de ingestão com um
**token de ingestão** por integração — análogo ao HEC token do Splunk, mas de
ENTRADA. O token é apresentado em ``Authorization: Bearer <token>``.

**Formato.** ``coi_<integration_id>_<random>``. O prefixo carrega o
``integration_id`` para o endpoint resolver a integração em O(1) (sem varredura),
e o sufixo é 32 bytes urlsafe de aleatoriedade. O ``integration_id`` no token NÃO
é segredo — a segurança vem do sufixo aleatório.

**Armazenamento.** Guardamos apenas o **SHA-256** do token (cifrado pelo cofre via
``integration_secrets``, logical_name ``ingest_token``). Nem o banco nem o cofre
revelam o token em claro; a verificação é por comparação de hash em tempo
constante. Rotação = gerar novo token (invalida o anterior). Revogação =
``revoke_secret``.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from typing import Optional, Tuple

from ..db import models
from . import integration_secrets

logger = logging.getLogger(__name__)

LOGICAL_NAME = "ingest_token"
_PREFIX = "coi"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate(integration_id: int) -> Tuple[str, str]:
    """Gera ``(token_plaintext, sha256_hex)`` para uma integração.

    O plaintext é mostrado UMA vez ao operador (e injetado no edge-collector); só
    o hash é persistido."""
    token = f"{_PREFIX}_{integration_id}_{secrets.token_urlsafe(32)}"
    return token, _hash(token)


def parse_integration_id(token: str) -> Optional[int]:
    """Extrai o ``integration_id`` do prefixo do token (sem validar o segredo)."""
    if not token:
        return None
    parts = token.split("_", 2)
    if len(parts) != 3 or parts[0] != _PREFIX:
        return None
    try:
        return int(parts[1])
    except (TypeError, ValueError):
        return None


def issue(integration: models.Integration) -> str:
    """Gera e ARMAZENA um novo token (rotaciona o anterior). Devolve o plaintext.

    O caller faz o ``commit`` da sessão (``write_secret`` muta
    ``integration.credentials``)."""
    token, digest = generate(integration.id)
    integration_secrets.write_secret(integration, LOGICAL_NAME, digest)
    return token


def verify(integration: models.Integration, token: str) -> bool:
    """Compara (tempo constante) o hash do ``token`` com o hash armazenado."""
    stored = integration_secrets.read_secret(integration, LOGICAL_NAME)
    if not stored:
        return False
    return hmac.compare_digest(stored, _hash(token))


def has_token(integration: models.Integration) -> bool:
    return integration_secrets.has_secret(integration, LOGICAL_NAME)


def revoke(integration: models.Integration) -> bool:
    """Revoga o token de ingestão sem apagá-lo (preserva trilha de auditoria) —
    qualquer token vazado é morto na hora, sem precisar rotacionar. Após revogar,
    ``verify`` falha (read_secret só retorna segredos ativos). Caller faz o ``commit``.
    Devolve True se havia um token ativo para revogar."""
    return integration_secrets.revoke_secret(integration, LOGICAL_NAME)
