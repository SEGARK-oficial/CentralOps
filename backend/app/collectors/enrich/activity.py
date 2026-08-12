"""Log curto de atividade do enriquecimento, para a pergunta "isso está funcionando?".

**O problema que resolve.** Hoje, credencial errada ou feed fora do ar aparecem
como evento chegando ao destino sem contexto, horas depois, com o motivo enterrado
no log do worker. Os contadores OTel existentes não ajudam: são NO-OP com
``OTEL_ENABLED`` desligado, que é o default.

**Cardinalidade, que é a decisão de desenho.** Um registro por TENTATIVA DE
CONSULTA, jamais por evento:

- ``table_load``  → 1× por ciclo de coleta (``runtime.load_tables``)
- ``remote_batch`` → 1× por lote no flush (``runtime.resolve_remote``)

O applier roda 1× por evento no seam local e mais 1× no remoto; gravar ali seriam
duas idas ao Redis por evento por regra. Contagem por evento já é agregada em
memória pelo ``ApplyStats`` e sai 1×/ciclo em ``emit_apply_stats``.

Ring em Redis (LPUSH+LTRIM+TTL), no mesmo padrão de ``audit_buffer``: o dado é
descartável, tem janela curta e volume por ciclo. Tabela em Postgres para isso
seria migração nova para guardar o que expira em um dia.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

__all__ = ["record", "read_recent", "ACTIVITY_KINDS"]

#: Tipos de tentativa registrados. Fechado de propósito: a UI filtra por isto, e
#: um valor novo aparecendo sem a UI saber renderizar vira linha em branco.
ACTIVITY_KINDS = ("table_load", "remote_batch")

_RING_SIZE = 200
_TTL_SECONDS = 24 * 3600


def _key(org_id: int) -> str:
    return f"obs:enrichlog:{org_id}"


def record(
    org_id: Optional[int],
    kind: str,
    *,
    ok: bool,
    rule_id: Optional[str] = None,
    enricher: Optional[str] = None,
    source: Optional[str] = None,
    reason: Optional[str] = None,
    detail: Optional[str] = None,
    keys: Optional[int] = None,
    entries: Optional[int] = None,
    latency_ms: Optional[float] = None,
) -> None:
    """Registra uma tentativa. Best-effort: nunca levanta, nunca bloqueia o ciclo.

    ``detail`` carrega a mensagem do provedor (401, DNS, schema divergente), que é
    exatamente o que falta hoje para o operador diagnosticar sem abrir log de
    worker. Truncado porque mensagem de erro de biblioteca às vezes traz payload.
    """
    if org_id is None:
        return
    try:
        from ..observability_store import _redis  # mesmo cliente/pool do store

        entry: Dict[str, Any] = {
            "ts": time.time(),
            "kind": kind if kind in ACTIVITY_KINDS else "table_load",
            "ok": bool(ok),
        }
        for field, value in (
            ("rule_id", rule_id),
            ("enricher", enricher),
            ("source", source),
            ("reason", reason),
            ("detail", (detail or "")[:400] or None),
            ("keys", keys),
            ("entries", entries),
            ("latency_ms", round(latency_ms, 1) if latency_ms is not None else None),
        ):
            if value is not None:
                entry[field] = value

        key = _key(int(org_id))
        r = _redis()
        pipe = r.pipeline()
        pipe.lpush(key, json.dumps(entry, ensure_ascii=False))
        pipe.ltrim(key, 0, _RING_SIZE - 1)
        pipe.expire(key, _TTL_SECONDS)
        pipe.execute()
    except Exception:  # noqa: BLE001
        logger.debug("enrich.activity: falha ao registrar (%s)", kind, exc_info=True)


def read_recent(org_id: int, limit: int = 100) -> List[Dict[str, Any]]:
    """Últimas tentativas, mais recente primeiro. Lista vazia quando não há nada.

    Devolve ``[]`` também em falha de leitura: a aba de atividade não pode
    derrubar a página de enriquecimento por causa de um Redis indisponível.
    """
    try:
        from ..observability_store import _redis

        raw = _redis().lrange(_key(int(org_id)), 0, max(1, min(limit, _RING_SIZE)) - 1)
    except Exception:  # noqa: BLE001
        logger.debug("enrich.activity: falha ao ler", exc_info=True)
        return []

    out: List[Dict[str, Any]] = []
    for item in raw or []:
        try:
            out.append(json.loads(item if isinstance(item, str) else item.decode("utf-8")))
        except Exception:  # noqa: BLE001 — uma linha corrompida não invalida o resto
            continue
    return out
