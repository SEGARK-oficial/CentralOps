"""O dropdown de condição de rota tem de espelhar a allowlist do backend.

`RouteConditionEditor.tsx` declara, em comentário, que "espelha
routing.engine.ALLOWED_FIELDS no backend". Isso era só uma frase — e **já estava
errada antes de qualquer mudança recente**: `platform` e `data_geography` são
labels de roteamento de primeira classe, documentados em `outputs/routing.md`, e
não apareciam no dropdown. `detection_matched` seria o terceiro.

O sintoma é o pior tipo: a doc ensina o operador a usar um campo, o backend
aceita, e a tela simplesmente não o oferece. Ninguém recebe erro; a capacidade só
não existe para quem usa o console.

Este teste é o espelho de verdade. Vive no backend porque é lá que mora a fonte
(`ALLOWED_FIELDS`) — e porque `frontend/` não tem gate de teste que rode no CI.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.app.collectors.routing.engine import ALLOWED_FIELDS

_EDITOR = (
    Path(__file__).resolve().parents[2]
    / "frontend"
    / "src"
    / "components"
    / "routes"
    / "RouteConditionEditor.tsx"
)


def _campos_do_editor() -> set[str]:
    if not _EDITOR.is_file():
        pytest.skip(
            f"{_EDITOR.name} ausente — rodando contra a imagem, que não recebe o "
            "frontend. O espelho vale no repositório."
        )
    texto = _EDITOR.read_text(encoding="utf-8")
    bloco = re.search(r"const FIELDS = \[(.*?)\] as const", texto, re.S)
    assert bloco, (
        "não achei `const FIELDS = [...] as const` em RouteConditionEditor.tsx. "
        "Se a forma mudou, atualize este regex — não deixe o espelho cego."
    )
    return set(re.findall(r'"([^"]+)"', bloco.group(1)))


def test_o_editor_oferece_exatamente_os_campos_que_o_backend_aceita() -> None:
    editor = _campos_do_editor()

    # Anti-vacuidade: um regex que parasse de casar devolveria conjunto vazio, e
    # a diferença abaixo passaria a acusar tudo — ou nada, se comparada ao contrário.
    assert len(editor) >= 5, f"só {len(editor)} campos extraídos — o regex quebrou?"

    faltando = set(ALLOWED_FIELDS) - editor
    assert not faltando, (
        f"o backend aceita {sorted(faltando)} em condição de rota e o dropdown "
        f"não oferece. O operador lê na documentação, tenta na tela, e o campo "
        f"não está lá."
    )

    sobrando = editor - set(ALLOWED_FIELDS)
    assert not sobrando, (
        f"o dropdown oferece {sorted(sobrando)}, que o backend REJEITA. A rota "
        f"seria escrita na tela e recusada ao salvar."
    )


def test_todo_campo_booleano_do_editor_e_convertido_para_bool() -> None:
    """`coerce` só convertia NUMERIC; todo o resto virava string.

    Um campo booleano no dropdown sem entrada em `BOOLEAN` emitiria
    `{"campo": "true"}` — string. `compare_values` compara por igualdade nativa,
    então `"true" != True` e a rota nunca casaria. Hoje o backend recusa isso com
    422, mas o erro certo é a tela não cometê-lo.
    """
    texto = _EDITOR.read_text(encoding="utf-8") if _EDITOR.is_file() else ""
    if not texto:
        pytest.skip("editor ausente")

    bloco = re.search(r"const BOOLEAN = new Set\(\[(.*?)\]\)", texto, re.S)
    assert bloco, "o editor não declara `BOOLEAN` — campo booleano viraria string"
    declarados = set(re.findall(r'"([^"]+)"', bloco.group(1)))

    from backend.app.collectors.routing.engine import _BOOLEAN_FIELDS

    assert declarados == set(_BOOLEAN_FIELDS), (
        f"os booleanos divergiram: editor={sorted(declarados)}, "
        f"backend={sorted(_BOOLEAN_FIELDS)}. Um campo booleano que o editor não "
        "conheça é gravado como string e a rota morre calada."
    )
