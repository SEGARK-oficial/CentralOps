"""Ack tokens: ligam um ``dry_run_mapping``/``patch_mapping_rules`` ao commit.

Portado do servidor externo, com duas mudanças que o embutido exige:

1. **Compartilhado entre réplicas.** O externo guardava tudo num dict do
   processo — um token emitido pela réplica A não existia na réplica B. Aqui o
   armazenamento é o Redis da plataforma (``core.redis_client``), com o
   fallback in-memory do próprio helper quando não há Redis (dev, testes).
   Cada entrada tem TTL próprio, então o Redis expira sozinho.

2. **Por analista.** Um token é emitido no contexto de um principal
   (``context.current_principal``) e só pode ser consumido por ele. Não é
   fronteira de segurança (o backend revalida no commit); é o que impede um
   ack de um analista "confirmar" o commit de outro por acidente.

O teto de entradas por analista continua: cada patch encenado carrega o
array inteiro de regras (~30 KB), e um agente que propõe sem commitar
cresceria isso sem limite.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import asdict, dataclass
from typing import Any

from ..core.redis_client import get_redis
from .context import require_principal

ACK_TTL_SECONDS = 300  # 5 minutes — must stay short

#: Hard cap on live entries PER ANALYST. Eviction is oldest-expiry-first.
MAX_ENTRIES = 64

_KEY_PREFIX = "mcp:ack"


@dataclass(frozen=True)
class _Entry:
    rules_fingerprint: str
    definition_id: str
    expires_at: float  # epoch seconds (wall clock — shared across replicas)
    #: Present only for patch tokens: the merged rules the agent never saw in
    #: full. Keeping it here is the whole point — ``commit_mapping_patch`` can
    #: send 193 rules to the backend while the model only ever handled the ops.
    staged_rules: Any = None
    #: Version the ops were addressed against. Carried so the commit can send it
    #: as ``base_version_id`` and let the backend reject a lost update.
    base_version_id: str | None = None


class AckTokenError(RuntimeError):
    pass


class AckCache:
    """Cache binding `dry_run_mapping` results to `commit_mapping`.

    The CentralOps backend re-runs validation on commit, so this cache is not a
    security boundary — it is a UX guard that prevents the LLM from calling
    `commit_mapping` on rules it never dry-ran. Tokens expire fast (5min) and
    are scoped to (analyst, definition_id, rules-fingerprint).
    """

    def __init__(self, store: Any | None = None, *, clock=time.time) -> None:
        # ``store`` injetável para testes; em produção resolve o Redis (ou o
        # fallback in-memory) do helper compartilhado a cada operação.
        self._store = store
        self._clock = clock

    # -- chaves --------------------------------------------------------

    def _s(self) -> Any:
        return self._store if self._store is not None else get_redis()

    @staticmethod
    def _actor() -> str:
        return require_principal().actor_key

    @classmethod
    def _entry_key(cls, actor: str, token: str) -> str:
        return f"{_KEY_PREFIX}:{actor}:{token}"

    @classmethod
    def _index_key(cls, actor: str) -> str:
        return f"{_KEY_PREFIX}:{actor}:index"

    # -- emissão ---------------------------------------------------------

    async def issue(self, definition_id: str, rules: Any) -> str:
        entry = _Entry(
            rules_fingerprint=_fingerprint(rules),
            definition_id=definition_id,
            expires_at=self._clock() + ACK_TTL_SECONDS,
        )
        return await self._put(entry)

    async def issue_patch(
        self,
        definition_id: str,
        merged_rules: Any,
        *,
        base_version_id: str | None,
        patch_digest: str,
    ) -> str:
        """Stage a merged rules dict and bind a token to the PATCH that made it.

        The binding is on ``patch_digest`` — a hash of (definition_id,
        base_version_id, ops) — rather than on the rules body, because the agent
        never handles the rules body. ``commit_mapping_patch`` re-sends the same
        ops and we recompute the digest, so a token cannot be replayed against a
        different set of ops or a different base version.
        """
        entry = _Entry(
            rules_fingerprint=patch_digest,
            definition_id=definition_id,
            expires_at=self._clock() + ACK_TTL_SECONDS,
            staged_rules=merged_rules,
            base_version_id=base_version_id,
        )
        return await self._put(entry)

    async def _put(self, entry: _Entry) -> str:
        actor = self._actor()
        token = secrets.token_urlsafe(24)
        store = self._s()
        await store.set(
            self._entry_key(actor, token),
            json.dumps(asdict(entry), separators=(",", ":"), default=str),
            ex=ACK_TTL_SECONDS,
        )
        await self._index_add(store, actor, token, entry.expires_at)
        return token

    # -- consumo ---------------------------------------------------------

    async def peek_base_version(self, token: str) -> str | None:
        """Base version do token, sem consumi-lo (o digest depende dela)."""
        entry = await self._load(self._actor(), token)
        return entry.base_version_id if entry else None

    async def consume_patch(self, token: str, definition_id: str, patch_digest: str) -> _Entry:
        """Consume a patch token and hand back the staged merge.

        Same single-use/expiry/definition guarantees as :meth:`consume`; the
        difference is what is compared (the patch digest) and what comes back
        (the merged rules, so the caller can POST them without ever having
        materialised them in the conversation).
        """
        actor = self._actor()
        entry = await self._load(actor, token)
        if entry is None:
            raise AckTokenError(
                "ack_token is unknown or already consumed. "
                "Call patch_mapping_rules again to obtain a fresh token."
            )
        if entry.expires_at < self._clock():
            await self._drop(actor, token)
            raise AckTokenError(
                "ack_token expired. Re-run patch_mapping_rules to confirm the change."
            )
        if entry.definition_id != definition_id:
            raise AckTokenError("ack_token was issued for a different definition_id.")
        if entry.staged_rules is None:
            raise AckTokenError(
                "this ack_token came from dry_run_mapping, not patch_mapping_rules. "
                "Use commit_mapping for that flow."
            )
        if entry.rules_fingerprint != patch_digest:
            raise AckTokenError(
                "ops differ from the patch that produced the ack_token. "
                "Re-run patch_mapping_rules with the exact ops you intend to commit."
            )
        await self._drop(actor, token)
        return entry

    async def consume(self, token: str, definition_id: str, rules: Any) -> None:
        actor = self._actor()
        fingerprint = _fingerprint(rules)
        entry = await self._load(actor, token)
        if entry is None:
            raise AckTokenError(
                "ack_token is unknown or already consumed. "
                "Call dry_run_mapping again to obtain a fresh token."
            )
        if entry.expires_at < self._clock():
            await self._drop(actor, token)
            raise AckTokenError(
                "ack_token expired. Re-run dry_run_mapping to confirm the rules."
            )
        if entry.definition_id != definition_id:
            raise AckTokenError(
                "ack_token was issued for a different definition_id."
            )
        if entry.staged_rules is not None:
            raise AckTokenError(
                "this ack_token came from patch_mapping_rules. "
                "Use commit_mapping_patch for that flow."
            )
        if entry.rules_fingerprint != fingerprint:
            raise AckTokenError(
                "rules differ from the dry-run that produced the ack_token. "
                "Re-run dry_run_mapping with the exact rules you intend to commit."
            )
        await self._drop(actor, token)

    # -- armazenamento ---------------------------------------------------

    async def _load(self, actor: str, token: str) -> _Entry | None:
        raw = await self._s().get(self._entry_key(actor, token))
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return _Entry(**data)
        except (ValueError, TypeError):  # pragma: no cover — entrada corrompida
            return None

    async def _drop(self, actor: str, token: str) -> None:
        store = self._s()
        await store.delete(self._entry_key(actor, token))
        index = await self._index_load(store, actor)
        index = [row for row in index if row[0] != token]
        await self._index_save(store, actor, index)

    async def _index_load(self, store: Any, actor: str) -> list[list[Any]]:
        raw = await store.get(self._index_key(actor))
        if not raw:
            return []
        try:
            rows = json.loads(raw)
        except ValueError:  # pragma: no cover — índice corrompido
            return []
        return [row for row in rows if isinstance(row, list) and len(row) == 2]

    async def _index_save(self, store: Any, actor: str, rows: list[list[Any]]) -> None:
        key = self._index_key(actor)
        if not rows:
            await store.delete(key)
            return
        await store.set(key, json.dumps(rows, separators=(",", ":")), ex=ACK_TTL_SECONDS)

    async def _index_add(self, store: Any, actor: str, token: str, expires_at: float) -> None:
        """Mantém o índice por analista: poda expirados e aplica o teto.

        Staged patches hold a full rules dict each, so expiry alone is not a
        bound: an agent can mint faster than the TTL retires. Evict by nearest
        expiry — those are the least useful entries remaining.
        """
        now = self._clock()
        rows = [row for row in await self._index_load(store, actor) if row[1] >= now]
        rows.append([token, expires_at])
        rows.sort(key=lambda row: row[1])
        if len(rows) > MAX_ENTRIES:
            evicted, rows = rows[: len(rows) - MAX_ENTRIES], rows[len(rows) - MAX_ENTRIES:]
            for old_token, _ in evicted:
                await store.delete(self._entry_key(actor, old_token))
        await self._index_save(store, actor, rows)


def _fingerprint(rules: Any) -> str:
    canonical = json.dumps(rules, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = ["AckCache", "AckTokenError", "ACK_TTL_SECONDS", "MAX_ENTRIES", "_fingerprint"]
