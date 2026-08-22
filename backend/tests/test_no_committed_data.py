"""Guard: nenhum DADO real rastreado neste repo PÚBLICO (AGPLv3).

Espelha, como teste da suíte normal, o step ``Nenhum dado real (dataset/SQLite)
commitado`` de ``.github/workflows/openness-gate.yml``.

Por que existe, em uma frase: ``.gitignore`` NÃO desrastreia nada — um arquivo já
no índice continua no índice depois de ignorado, e foi assim que
``backend/data/sophos.db-shm`` (sidecar do SQLite de coleta) acabou publicado
neste repo. Logo o guard olha o ÍNDICE do git, nunca o working tree.

Por que no openness-gate e não em ``source-tree-guards``: aquele job NÃO está no
``needs`` do publish (``ci.yml:422`` = ``[build, pg-migrations, e2e]``), então um
guard que viva só lá deixa o check vermelho mas NÃO impede a promoção da imagem
para ``latest``. Aqui é a rede de segurança local/PR do mesmo invariante.

O par positivo/negativo é obrigatório: sozinho, ``assert not offenders`` sobre um
repo limpo passa por VACUIDADE — inclusive se o scanner tiver parado de casar
qualquer coisa. Por isso todo teste negativo aqui vem ao lado de um positivo que
planta o arquivo proibido e exige a detecção.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

# backend/tests/test_no_committed_data.py -> raiz do repo
_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "openness-gate.yml"

_WORKFLOW_STEP_NAME = "Nenhum dado real (dataset/SQLite) commitado"

# UMA string só serve aos dois lados: é POSIX-ERE válido para ``grep -E`` no step
# do workflow E regex válido para o ``re`` aqui (sem \b, sem grupo não-capturante,
# sem lookaround). Empatar por construção é o que impede o espelho de derivar do
# original — ``test_workflow_step_shares_the_same_patterns`` cobra a igualdade
# literal.
_FORBIDDEN_ERE = (
    r"(\.ndjson|\.db|\.db-wal|\.db-shm|\.db-journal)$|\.sqlite[^/.]*$"
    r"|^backend/data/|(^|/)(ds-bruto|ds-anon|dataset-local)/"
)
# ÚNICA allowlist: fixture curada (anonimizada) de UM nível. O ``[^/]+`` é
# deliberado — subdiretório sob curated/ NÃO é allowlist, senão bastaria criar
# ``curated/tmp/`` para reabrir o buraco inteiro.
_CURATED_ALLOW_ERE = r"^backend/app/collectors/tests/fixtures/curated/[^/]+\.ndjson$"

_FORBIDDEN_RE = re.compile(_FORBIDDEN_ERE)
_CURATED_ALLOW_RE = re.compile(_CURATED_ALLOW_ERE)

# Espelha o pathspec ``':!...'`` do step: os dois arquivos que CARREGAM a lista de
# padrões ficam fora da varredura por construção, para que ampliá-la (p.ex. passar
# a casar conteúdo, não só caminho) nunca faça o guard reprovar a si mesmo.
_SELF_PATHS = frozenset(
    {
        ".github/workflows/openness-gate.yml",
        "backend/tests/test_no_committed_data.py",
    }
)


def _offenders(paths: "list[str]") -> "list[str]":
    """O scanner. Recebe caminhos relativos à raiz (POSIX) e devolve os proibidos."""
    return [
        p
        for p in paths
        if p not in _SELF_PATHS
        and _FORBIDDEN_RE.search(p)
        and not _CURATED_ALLOW_RE.match(p)
    ]


def _tracked_paths(root: Path) -> "list[str] | None":
    """Caminhos no ÍNDICE de ``root``. ``None`` quando não dá para perguntar ao git
    (binário ausente na imagem Cython, ou diretório que não é repositório) — o
    chamador então PULA explicitamente. Sem esse skip, a lista viria vazia e o
    ``assert not offenders`` aprovaria por vacuidade."""
    if not (root / ".git").exists():
        return None
    try:
        res = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    if res.returncode != 0:
        return None
    return [p for p in res.stdout.split("\0") if p]


def _relative_files(root: Path) -> "list[str]":
    return sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(root).parts
    )


def _write(root: Path, rel: str, body: str = "{}\n") -> None:
    dest = root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(body, encoding="utf-8")


# Um exemplar de cada família proibida do step, e o motivo de estar na lista.
_PLANTED_FORBIDDEN = {
    "coleta.ndjson": "dump de coleta na raiz",
    "backend/data/sophos.db-shm": "sidecar shared-memory do SQLite (o caso real)",
    "backend/data/sophos.db-wal": "write-ahead log do SQLite",
    "backend/data/sophos.db": "o próprio banco de coleta",
    "backend/app/collectors/cache.sqlite3": "SQLite fora de backend/data/",
    "ds-bruto/2026-08/eventos.ndjson": "dataset BRUTO (dado de cliente cru)",
    "tools/ds-anon/amostra.ndjson": "dataset anonimizado ainda é dado de cliente",
    "scratch/dataset-local/x.ndjson": "área de dataset local",
}
# Vizinhos legítimos: se algum destes for acusado, o guard virou ruído e alguém
# vai desligá-lo. O positivo tem que provar o SIM e o NÃO.
_PLANTED_ALLOWED = {
    "backend/app/collectors/tests/fixtures/curated/sophos.ndjson": "fixture curada (allowlist)",
    "backend/tests/test_sqlite_wal_mode.py": "nome contém 'sqlite', mas é código",
    "README.md": "arquivo comum",
    "backend/app/collectors/sophos.py": "código do coletor",
}


def test_scanner_detects_planted_data_files(tmp_path: Path) -> None:
    """POSITIVO. Planta um arquivo de cada família proibida em disco e exige que o
    scanner acuse TODOS — e que não acuse nenhum vizinho legítimo. É este teste que
    impede o negativo abaixo de virar decoração quando o scanner parar de casar."""
    for rel in (*_PLANTED_FORBIDDEN, *_PLANTED_ALLOWED):
        _write(tmp_path, rel)

    found = set(_offenders(_relative_files(tmp_path)))

    missed = {rel: why for rel, why in _PLANTED_FORBIDDEN.items() if rel not in found}
    assert not missed, f"o scanner NÃO detectou dado proibido: {missed}"

    false_positives = {rel: why for rel, why in _PLANTED_ALLOWED.items() if rel in found}
    assert not false_positives, f"o scanner acusou arquivo legítimo: {false_positives}"


def test_curated_allowlist_is_narrow() -> None:
    """Invariante da allowlist recém-criada: ela cobre .ndjson de UM nível dentro de
    ``fixtures/curated/`` e MAIS NADA. Alargar isso (subdiretório, outra extensão,
    diretório irmão) reabre o buraco que o guard fecha."""
    curated = "backend/app/collectors/tests/fixtures/curated"
    assert _offenders([f"{curated}/sophos.ndjson"]) == []
    for rel in (
        f"{curated}/sub/nested.ndjson",       # allowlist não é recursiva
        f"{curated}/dump.db",                 # allowlist é só .ndjson
        "backend/app/collectors/tests/fixtures/sophos.ndjson",  # irmão de curated/
    ):
        assert _offenders([rel]) == [rel], f"allowlist larga demais: {rel} passou"


def test_scanner_detects_through_git_index(tmp_path: Path) -> None:
    """POSITIVO da plumbing: prova que o caminho real do gate — ``git ls-files``
    sobre o ÍNDICE — entrega os caminhos que o scanner acusa. Cobre a metade que o
    teste em disco não vê: o gate julga o que foi ``git add``-ado, não o working tree."""
    try:
        init = subprocess.run(
            ["git", "-C", str(tmp_path), "init", "-q"],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        pytest.skip("git indisponível (imagem compilada) — o gate de CI cobre isto")
    if init.returncode != 0:
        pytest.skip(f"git init falhou: {init.stderr.strip()}")

    _write(tmp_path, "backend/data/sophos.db-shm")
    _write(tmp_path, "backend/app/collectors/tests/fixtures/curated/sophos.ndjson")
    _write(tmp_path, "README.md", "# ok\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True, capture_output=True)

    tracked = _tracked_paths(tmp_path)
    assert tracked, "git ls-files não devolveu nada — o teste não provaria nada"
    assert "backend/data/sophos.db-shm" in tracked

    assert _offenders(tracked) == ["backend/data/sophos.db-shm"]


def test_repo_index_has_no_committed_data() -> None:
    """NEGATIVO (o invariante que interessa): o repo real não rastreia dado.

    O ``assert tracked`` antes é o antídoto da vacuidade — sem ele, git ausente ou
    listagem vazia fariam este teste passar exatamente no cenário em que ele deveria
    ser inútil."""
    tracked = _tracked_paths(_REPO_ROOT)
    if tracked is None:
        pytest.skip("git indisponível ou não é repositório — o openness-gate cobre isto")
    assert len(tracked) > 100, (
        f"git ls-files devolveu só {len(tracked)} caminho(s); a varredura não é confiável"
    )

    offenders = _offenders(tracked)
    assert not offenders, (
        "dado real rastreado neste repo PÚBLICO (AGPLv3): "
        f"{offenders}\nMova para fora do repo e rode 'git rm --cached <arquivo>', ou "
        "anonimize e commite em backend/app/collectors/tests/fixtures/curated/<nome>.ndjson."
    )


def test_workflow_step_shares_the_same_patterns() -> None:
    """O espelho só vale se os dois lados usarem a MESMA lista. Compara literalmente
    as duas EREs com o que está no workflow: se alguém afrouxar o step e esquecer o
    teste (ou o contrário), isto falha apontando a divergência."""
    if not _WORKFLOW.is_file():
        pytest.skip("workflow ausente (imagem só com backend/) — nada a comparar")
    text = _WORKFLOW.read_text(encoding="utf-8")
    assert _WORKFLOW_STEP_NAME in text, (
        f"step '{_WORKFLOW_STEP_NAME}' sumiu do openness-gate.yml — "
        "o guard de CI é quem bloqueia o publish; este teste é só o espelho"
    )
    for label, ere in (("padrão proibido", _FORBIDDEN_ERE), ("allowlist", _CURATED_ALLOW_ERE)):
        assert ere in text, (
            f"{label} divergiu do step no openness-gate.yml.\nesperado: {ere}"
        )
