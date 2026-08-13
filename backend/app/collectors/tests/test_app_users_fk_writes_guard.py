"""Guard: id de usuário indo para coluna FK de ``app_users`` passa pelo helper.

Este bug já apareceu TRÊS vezes, sempre igual e sempre em produção:

- jul/2026 — ``create_version`` (mapping): 500 ao commitar mapping via MCP;
- jul/2026 — middleware de audit: linha de ``audit_logs`` perdida em silêncio;
- ago/2026 — ``create_backfill_job``: 500 ao criar backfill via PAT, e o job
  nem chegava a existir.

A causa é sempre a mesma: service accounts autenticam como um shim transient de
``AppUser`` com id NEGATIVO (``-<sa.id>``, ver ``auth._build_sa_appuser_shim``)
que não existe na tabela. Gravar esse id numa coluna com FK para ``app_users.id``
viola a constraint e derruba a transação inteira. O fix canônico é
``auth.persistable_user_id`` — id real → id; shim → ``None`` (a atribuição fica
preservada em ``username='sa:<name>'``, que os writers gravam ao lado).

Corrigir ocorrência por ocorrência não impediu a recorrência, porque nada
avisava o próximo autor. Este guard falha o CI no momento em que a quarta
aparecer.

As colunas de risco são DERIVADAS de ``models.py`` em tempo de execução: uma
coluna FK nova entra na checagem sozinha, sem ninguém lembrar de atualizar lista.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Set, Tuple

import pytest

_REPO = Path(__file__).resolve().parents[4]
_MODELS = _REPO / "backend" / "app" / "db" / "models.py"
_SCAN_DIRS = ("routers", "services", "core", "collectors")

# Expressões que JÁ são seguras do lado direito da atribuição.
_SAFE_RHS = re.compile(r"persistable_user_id|^None$")

# Escritas legítimas de id cru, com a razão. Manter curto e justificado — cada
# entrada aqui é uma exceção que o guard deixa de proteger.
_ALLOWLIST: Dict[Tuple[str, str], str] = {
    # UserSession.user_id é NOT NULL e representa o login de um humano real.
    # Service account não abre sessão (autentica por PAT Bearer), e anular o id
    # aqui violaria a constraint NOT NULL — quebrando o login.
    ("core/auth.py", "user_id"): "UserSession.user_id é NOT NULL (login humano)",
    # ApiToken é XOR user/service_account: anular criaria token órfão. O service
    # rejeita o shim explicitamente antes de chegar ao INSERT.
    ("services/api_tokens.py", "user_id"): "ApiToken XOR — rejeitado antes do INSERT",
}


def _fk_columns() -> Set[str]:
    """Nomes de coluna com ForeignKey para ``app_users.id``, lidos do models.py."""
    if not _MODELS.is_file():
        pytest.skip(f"models.py não encontrado em {_MODELS}")
    lines = _MODELS.read_text().splitlines()
    cols: Set[str] = set()
    for i, line in enumerate(lines):
        if 'ForeignKey("app_users.id"' not in line:
            continue
        # Sobe até a declaração `nome = Column(` a que este FK pertence.
        for j in range(i, max(-1, i - 6), -1):
            m = re.match(r"\s*(\w+)\s*=\s*Column\(", lines[j])
            if m:
                cols.add(m.group(1))
                break
    return cols


def _scan_raw_writes(columns: Set[str]) -> List[str]:
    offenders: List[str] = []
    pattern = re.compile(
        r"\b(" + "|".join(sorted(columns)) + r")\s*=\s*([^,\n\)]+)"
    )
    for sub in _SCAN_DIRS:
        base = _REPO / "backend" / "app" / sub
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "/tests/" in str(path):
                continue
            rel = str(path.relative_to(_REPO / "backend" / "app"))
            for num, line in enumerate(path.read_text().splitlines(), 1):
                stripped = line.strip()
                # Ignora comentários, definição de coluna e comparações/filtros.
                if stripped.startswith("#") or "Column(" in line:
                    continue
                if "==" in line or ".filter" in line:
                    continue
                m = pattern.search(line)
                if not m:
                    continue
                col, rhs = m.group(1), m.group(2).strip()
                if _SAFE_RHS.search(rhs):
                    continue
                # Só acusa quando o id vem de um objeto de usuário autenticado.
                if not re.search(r"\b(current_user|user)\.id\b", rhs):
                    continue
                if (rel, col) in _ALLOWLIST:
                    continue
                offenders.append(f"{rel}:{num} → {col}={rhs}")
    return offenders


@pytest.mark.source_only
def test_fk_columns_are_discovered_from_models() -> None:
    """O guard perde o sentido se parar de enxergar as colunas que protege.

    São NOMES distintos de coluna, não ocorrências: ``user_id`` aparece em várias
    tabelas (AuditLog, MappingAuditLog, QueryJob, ...) e conta uma vez só. Hoje
    são 6 nomes cobrindo 16 colunas FK.
    """
    cols = _fk_columns()
    assert len(cols) >= 5, (
        f"esperado >=5 NOMES de coluna com FK app_users, achei {len(cols)}: {cols} "
        "— regex de varredura provavelmente quebrou"
    )
    # Âncoras: as colunas das três regressões reais precisam continuar visíveis.
    for expected in ("author_user_id", "requested_by_user_id", "user_id"):
        assert expected in cols, f"'{expected}' sumiu da varredura — regex quebrada?"


@pytest.mark.source_only
def test_no_raw_user_id_written_to_app_users_fk() -> None:
    """Nenhuma escrita nova de id cru em coluna FK de app_users."""
    offenders = _scan_raw_writes(_fk_columns())
    assert not offenders, (
        "id de usuário indo CRU para coluna com FK para app_users — service "
        "account autentica com id NEGATIVO e isso vira ForeignKeyViolation "
        "(500 + transação perdida) quando o endpoint é chamado via PAT/MCP.\n"
        "Use app_auth.persistable_user_id(user):\n\n"
        + "\n".join(f"  {o}" for o in offenders)
        + "\n\nSe a escrita for legítima (coluna NOT NULL, XOR com outra FK), "
        "adicione em _ALLOWLIST com a justificativa."
    )


@pytest.mark.source_only
def test_allowlist_entries_still_exist() -> None:
    """Allowlist não pode virar cemitério: entrada obsoleta esconde cobertura."""
    for (rel, col), reason in _ALLOWLIST.items():
        path = _REPO / "backend" / "app" / rel
        assert path.is_file(), f"allowlist aponta para arquivo inexistente: {rel}"
        text = path.read_text()
        assert re.search(rf"\b{col}\s*=", text), (
            f"allowlist tem ({rel}, {col}) — '{reason}' — mas essa escrita não "
            "existe mais. Remova a entrada."
        )
