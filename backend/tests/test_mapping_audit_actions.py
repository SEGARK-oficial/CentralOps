"""O vocabulário de ações de ``mapping_audit_log`` não pode divergir do código.

A lista vivia duplicada em três lugares e divergiu: o filtro da UI oferecia
``version_created`` (o backend grava ``create_version``), ``drift_detected`` e
``quarantine`` (nunca gravadas por lugar nenhum). Como o filtro é igualdade
exata server-side, cada uma dessas opções devolvia 200 com tabela VAZIA — o
operador lia "nenhuma atividade" quando a opção simplesmente não existia.

Estes testes travam a fonte única (``db/mapping_audit.py``) contra os literais
que os routers realmente gravam. Um ``action="..."`` novo sem entrada na lista
quebra aqui, e não em produção.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import pytest

from backend.app.db import mapping_audit

_BACKEND = Path(__file__).resolve().parents[1] / "app"

#: Arquivos que gravam MappingAuditLog. Se um novo aparecer, some aqui.
_WRITERS = (
    "routers/mappings.py",
    "routers/drift.py",
    "routers/quarantine.py",
    "routers/backfill.py",
    "collectors/tasks.py",
)


def _literal_actions() -> set[str]:
    """Toda string literal passada como ``action=`` nos writers.

    Só literais: valores indiretos (``action=audit_action``) são resolvidos nos
    call-sites, que também são literais e portanto já entram na varredura.
    """
    found: set[str] = set()
    pattern = re.compile(r"""action=["']([a-z_]+)["']""")
    for rel in _WRITERS:
        path = _BACKEND / rel
        assert path.exists(), f"writer sumiu do repo: {rel}"
        found.update(pattern.findall(path.read_text(encoding="utf-8")))
    return found


def test_every_written_action_is_in_the_vocabulary() -> None:
    written = _literal_actions()
    assert written, "varredura não achou nenhum action= — o regex quebrou"
    missing = written - set(mapping_audit.ACTIONS)
    assert not missing, (
        f"ações gravadas mas ausentes de mapping_audit.ACTIONS: {sorted(missing)}. "
        "Adicione-as lá (e em DEFINITION_SCOPED_ACTIONS se gravarem "
        "mapping_definition_id)."
    )


def test_vocabulary_has_no_dead_entries() -> None:
    """O inverso: nada na lista que o código não grave.

    É o defeito que gerou o bug — o comentário do modelo citava três ações
    fantasma (``create_definition``, ``set_current``, ``create_from_drift``).
    """
    written = _literal_actions()
    dead = set(mapping_audit.ACTIONS) - written
    assert not dead, (
        f"ações declaradas mas nunca gravadas: {sorted(dead)}. "
        "Remova-as ou implemente quem as grave — oferecê-las num filtro de "
        "igualdade exata produz tabela vazia silenciosa."
    )


def test_definition_scoped_is_a_subset() -> None:
    assert set(mapping_audit.DEFINITION_SCOPED_ACTIONS) <= set(mapping_audit.ACTIONS)


def test_quarantine_actions_are_not_definition_scoped() -> None:
    """Quarentena/backfill gravam ``mapping_definition_id=None``, então são
    INALCANÇÁVEIS por ``GET /mappings/{id}/audit``. Oferecê-las no filtro
    daquela tela repetiria o bug original com outro nome.
    """
    for action in (
        mapping_audit.DISCARD_QUARANTINE,
        mapping_audit.BULK_REPROCESS_QUARANTINE,
        mapping_audit.REPROCESS_QUARANTINE_SUCCESS,
        mapping_audit.REPROCESS_QUARANTINE_FAILED,
    ):
        assert action not in mapping_audit.DEFINITION_SCOPED_ACTIONS


def test_no_duplicates() -> None:
    assert len(mapping_audit.ACTIONS) == len(set(mapping_audit.ACTIONS))


@pytest.mark.parametrize("action", mapping_audit.DEFINITION_SCOPED_ACTIONS)
def test_definition_scoped_actions_are_written_with_a_definition_id(action: str) -> None:
    """Cada ação escopada precisa ter um writer que preencha
    ``mapping_definition_id`` — senão ela nunca chega à aba de Auditoria.
    """
    sources = "\n".join(
        (_BACKEND / rel).read_text(encoding="utf-8")
        for rel in ("routers/mappings.py", "routers/drift.py")
    )
    assert f'"{action}"' in sources or f"'{action}'" in sources, (
        f"{action} está em DEFINITION_SCOPED_ACTIONS mas nenhum writer com "
        "mapping_definition_id a grava"
    )
