"""Vocabulário fechado das ações de ``mapping_audit_log`` — FONTE ÚNICA.

Existe porque a lista vivia duplicada em três lugares que divergiram entre si:
um comentário no modelo, os literais espalhados pelos routers, e um array
hardcoded no filtro da UI (``MappingAuditTable.tsx``). O filtro oferecia
``version_created``, ``drift_detected`` e ``quarantine`` — nenhuma das três é
gravada por lugar nenhum do backend. Como o filtro é server-side por igualdade
exata, selecionar qualquer uma delas devolvia tabela VAZIA, sem erro: o
operador concluía "não há registros" quando na verdade a opção era inexistente.

Duas listas, porque o escopo importa:

``ACTIONS`` — tudo que é gravado em ``mapping_audit_log``, para qualquer escopo.

``DEFINITION_SCOPED_ACTIONS`` — o subconjunto que chega à aba de Auditoria de UM
mapping. ``GET /mappings/{id}/audit`` filtra por ``mapping_definition_id``, e as
ações de quarentena/backfill gravam essa coluna como ``NULL`` (pendem de
``integration_id``): elas existem na tabela mas são INALCANÇÁVEIS por aquele
endpoint. Oferecê-las no filtro daquela tela seria repetir o bug original com
nomes diferentes.

Ao adicionar uma ação nova, inclua-a aqui e — se ela gravar
``mapping_definition_id`` — também em ``DEFINITION_SCOPED_ACTIONS``.
``backend/tests/test_mapping_audit_actions.py`` trava a lista contra os literais
que o código realmente grava, então esquecer aqui quebra a suíte.
"""
from __future__ import annotations

from typing import Final, Tuple

#: Versionamento do mapping (``routers/mappings.py``) — grava ``mapping_definition_id``.
CREATE_VERSION: Final = "create_version"
ROLLBACK: Final = "rollback"

#: Drift Explorer (``routers/drift.py``) — resolve e grava ``mapping_definition_id``.
IGNORE_FIELD: Final = "ignore_field"
MARK_MAPPED: Final = "mark_mapped"
DELETE_FIELD: Final = "delete_field"

#: Quarentena (``routers/quarantine.py``, ``collectors/tasks.py``) e backfill
#: (``routers/backfill.py``) — ``mapping_definition_id`` é NULL nesses caminhos.
DISCARD_QUARANTINE: Final = "discard_quarantine"
BULK_REPROCESS_QUARANTINE: Final = "bulk_reprocess_quarantine"
REPROCESS_QUARANTINE_SUCCESS: Final = "reprocess_quarantine_success"
REPROCESS_QUARANTINE_FAILED: Final = "reprocess_quarantine_failed"
BACKFILL_REQUESTED: Final = "backfill_requested"
BACKFILL_CANCELLED: Final = "backfill_cancelled"

#: Alcançáveis por ``GET /mappings/{definition_id}/audit``. Ordem estável: é a
#: ordem em que a UI monta o seletor.
DEFINITION_SCOPED_ACTIONS: Final[Tuple[str, ...]] = (
    CREATE_VERSION,
    ROLLBACK,
    IGNORE_FIELD,
    MARK_MAPPED,
    DELETE_FIELD,
)

#: Vocabulário completo da tabela (inclui o que só aparece na auditoria global).
ACTIONS: Final[Tuple[str, ...]] = DEFINITION_SCOPED_ACTIONS + (
    DISCARD_QUARANTINE,
    BULK_REPROCESS_QUARANTINE,
    REPROCESS_QUARANTINE_SUCCESS,
    REPROCESS_QUARANTINE_FAILED,
    BACKFILL_REQUESTED,
    BACKFILL_CANCELLED,
)
