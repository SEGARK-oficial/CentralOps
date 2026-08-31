"""Testes de sanidade para o lockfile de dependências.

Verifica que:
- ``requirements.lock`` existe no repositório backend.
- Todas as linhas de pacote (não-comentário, não-vazias) têm versão pinada com ``==``.
- ``requirements.txt`` (o arquivo que o CI e a imagem realmente instalam) tem
  TODA dependência direta pinada, e pinada na MESMA versão do lock.
- Nenhum pin caiu abaixo do FLOOR documentado ao lado dele (quase sempre a
  versão que corrige um CVE).

O terceiro e o quarto item existem porque o lock, sozinho, não protegia nada:
o CI instala ``requirements.txt`` (ver ``.github/workflows/ci.yml``), e enquanto
as diretas estavam abertas (``fastapi`` sem pin) cada build resolvia o que
estivesse publicado no dia. Foi assim que o ``fastapi`` saltou de 0.136 para
0.14x, ``include_router`` deixou de achatar ``app.routes``, e o gate de
fronteira open-core passou a aprovar qualquer coisa sem uma linha vermelha.
Um lockfile que ninguém instala é documentação, não defesa.

Não testa ``pip-audit`` diretamente — isso é responsabilidade do job de CI em
``.github/workflows/security.yml``.
"""

from __future__ import annotations

import re
from pathlib import Path

from packaging.specifiers import SpecifierSet
from packaging.version import Version

BACKEND_ROOT = Path(__file__).resolve().parents[1]
LOCKFILE = BACKEND_ROOT / "requirements.lock"
REQUIREMENTS = BACKEND_ROOT / "requirements.txt"

#: ``nome[extras]==versao`` no início da linha (o resto é comentário/anotação).
_PIN_RE = re.compile(r"^(?P<name>[A-Za-z0-9._-]+(?:\[[^\]]+\])?)==(?P<version>[^\s#]+)")
#: ``# FLOOR: >=1.3.1`` ou ``# FLOOR: >=5.3,<6`` — o motivo do piso fica no texto.
_FLOOR_RE = re.compile(r"#\s*FLOOR:\s*(?P<spec>[<>=!,.0-9a-zA-Z]+)")


def _parse_pins(path: Path) -> dict[str, str]:
    """``{nome[extras]: versao}`` das linhas de pacote pinadas em ``==``."""
    pins: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "-")):
            continue
        match = _PIN_RE.match(stripped)
        if match:
            pins[match.group("name")] = match.group("version")
    return pins


def _direct_package_lines(path: Path) -> list[str]:
    """Linhas de pacote de ``requirements.txt`` (sem comentários nem opções)."""
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith(("#", "-"))
    ]


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


class TestDirectRequirementsPinned:
    """``requirements.txt`` é o arquivo que o CI instala — ele que precisa pinar."""

    def test_every_direct_dependency_is_pinned(self) -> None:
        """Nenhuma dependência direta pode ficar aberta (``fastapi`` sem versão).

        Uma linha aberta aqui não falha nada na hora: ela só faz o build de
        amanhã ser diferente do de hoje, e a diferença aparece como um teste
        alheio quebrando — ou, pior, como um gate que para de olhar.
        """
        unpinned = [
            line for line in _direct_package_lines(REQUIREMENTS) if "==" not in line
        ]
        assert not unpinned, (
            f"{len(unpinned)} dependência(s) direta(s) sem pin em requirements.txt "
            "(o CI instala ESTE arquivo, não o lock):\n"
            + "\n".join(f"  {line}" for line in unpinned)
        )

    def test_direct_pins_match_the_lockfile(self) -> None:
        """O pin da direta e o do lock têm de ser a MESMA versão.

        Divergência aqui significa que instalar ``requirements.txt`` (o CI) e
        instalar o lock (auditoria, pip-audit) produzem ambientes diferentes —
        e a auditoria passa a certificar algo que ninguém roda.
        """
        lock_pins = _parse_pins(LOCKFILE)
        direct_pins = _parse_pins(REQUIREMENTS)
        assert direct_pins, "requirements.txt não tem nenhuma dependência pinada"

        mismatched = [
            f"{name}: requirements.txt=={version} vs lock=={lock_pins.get(name)}"
            for name, version in direct_pins.items()
            if lock_pins.get(name) != version
        ]
        assert not mismatched, (
            "requirements.txt divergiu do requirements.lock — regenere com "
            "`pip-compile requirements.txt -o requirements.lock`:\n"
            + "\n".join(f"  {line}" for line in mismatched)
        )

    def test_pins_respect_the_documented_floors(self) -> None:
        """Um pin nunca pode cair abaixo do ``# FLOOR:`` anotado ao lado.

        Os floors são, quase todos, a versão que corrige um CVE (o texto ao lado
        diz qual). Ao subir ou baixar uma versão é fácil desfazer um desses sem
        perceber: o pacote instala, os testes passam, e a correção sumiu.
        """
        regressions: list[str] = []
        for line in _direct_package_lines(REQUIREMENTS):
            pin = _PIN_RE.match(line)
            floor = _FLOOR_RE.search(line)
            if not pin or not floor:
                continue
            if Version(pin.group("version")) not in SpecifierSet(floor.group("spec")):
                regressions.append(
                    f"{pin.group('name')}=={pin.group('version')} viola FLOOR "
                    f"{floor.group('spec')}"
                )
        assert not regressions, (
            "pin abaixo do piso documentado (o comentário ao lado diz o motivo, "
            "normalmente um CVE):\n" + "\n".join(f"  {r}" for r in regressions)
        )
