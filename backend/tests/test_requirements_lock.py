"""Testes de sanidade para o lockfile de dependências.

Verifica que:
- ``requirements.lock`` existe no repositório backend.
- Todas as linhas de pacote (não-comentário, não-vazias) têm versão pinada com ``==``.

Não testa ``pip-audit`` diretamente — isso é responsabilidade do job de CI em
``.github/workflows/security.yml``.
"""

from __future__ import annotations

from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
LOCKFILE = BACKEND_ROOT / "requirements.lock"


class TestRequirementsLock:

    def test_requirements_lock_exists(self) -> None:
        """O lockfile deve existir e não estar vazio."""
        assert LOCKFILE.exists(), (
            f"requirements.lock não encontrado em {LOCKFILE}. "
            "Gere com: pip-compile requirements.txt -o requirements.lock"
        )
        assert LOCKFILE.stat().st_size > 0, "requirements.lock está vazio"

    def test_requirements_lock_has_pinned_versions(self) -> None:
        """Todas as linhas de pacote devem ter versão pinada com ``==``.

        Linhas de comentário (``#``), espaços em branco, opções (``-r``, ``--``)
        e anotações de via (``    # via``) são ignoradas.
        """
        lines = LOCKFILE.read_text(encoding="utf-8").splitlines()

        unpinned: list[str] = []
        for line in lines:
            stripped = line.strip()
            # Pula linhas vazias, comentários e opções pip
            if not stripped or stripped.startswith("#") or stripped.startswith("-"):
                continue
            # Pula opções com backslash continuation
            if stripped.startswith("\\"):
                continue

            # Linha de pacote: deve conter ==
            if "==" not in stripped:
                unpinned.append(stripped)

        assert not unpinned, (
            f"requirements.lock contém {len(unpinned)} linha(s) sem versão pinada:\n"
            + "\n".join(f"  {line}" for line in unpinned[:10])
        )
