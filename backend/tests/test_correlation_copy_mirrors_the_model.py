"""A cópia do console tem de existir para a coluna que decide quem roda.

Companheiro de ``test_correlation_types_mirror_the_model.py``, e o par que falta:
o tipo TS deixa o formulário COMPILAR com ``eval_priority``; a cópia deixa o
campo ser COMPREENSÍVEL. Sem ela o i18next renderiza a própria chave — a tela
mostra ``form.field.evalPriority.label`` numa etiqueta e ninguém recebe erro.

Por que não basta o ``check-i18n.mjs``. Aquele gate cobre duas coisas, e nenhuma
é esta:
  * PARIDADE entre locales — pega a chave sumindo de UM idioma, não dos três;
  * todo ``t("...")`` do ``src/`` do Core resolve — e o consumidor desta cópia é
    o ``web-ee``, que vive no OUTRO repositório e que o script nem varre.

Ou seja: apagar ``evalPriority`` dos três arquivos passa verde no Core inteiro e
quebra só a tela do Enterprise, no bump seguinte de ``CORE_SHA``. É a mesma
assimetria que deixou a main do EE vermelha quando a cópia do truncamento mudou.

Este teste fecha isso pelo lado que o Core CONSEGUE observar: a coluna existe, e
por isso a cópia tem de existir também.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.app.db.models import CorrelationRule

_LOCALES = ("pt", "en", "es")
_I18N = Path(__file__).resolve().parents[2] / "frontend" / "src" / "i18n" / "locales"

#: Folhas exigidas sob ``form.field.evalPriority``. Os DOIS helpers, não um:
#: o campo troca de significado com o ``eval_mode`` (em lote ele é inerte), e um
#: helper único teria de mentir num dos dois modos — mesma decisão já tomada em
#: ``groupBy``, que tem ``helperTextBatch`` e ``helperTextInflight``.
_FOLHAS = ("label", "helperTextInflight", "helperTextBatch")


def _catalogo(locale: str) -> dict:
    p = _I18N / locale / "correlation.json"
    if not p.is_file():
        pytest.skip(
            "frontend/src/i18n ausente — rodando contra a imagem, que não recebe "
            "o frontend. O espelho vale no repositório."
        )
    return json.loads(p.read_text(encoding="utf-8"))


@pytest.mark.parametrize("locale", _LOCALES)
def test_todo_locale_tem_copia_para_a_prioridade(locale: str) -> None:
    cat = _catalogo(locale)
    campos = cat["form"]["field"]

    # Anti-vacuidade: um JSON que mudasse de forma devolveria dict vazio e a
    # asserção abaixo acusaria "falta evalPriority" pelo motivo errado.
    assert len(campos) >= 8, f"só {len(campos)} campos em {locale}/correlation.json — a forma mudou?"

    assert "evalPriority" in campos, (
        f"{locale}/correlation.json não tem form.field.evalPriority. A coluna "
        "eval_priority existe e governa quem é avaliado no ciclo; sem cópia o "
        "console renderiza a CHAVE CRUA na etiqueta do campo, sem erro nenhum."
    )
    faltando = [f for f in _FOLHAS if not campos["evalPriority"].get(f, "").strip()]
    assert not faltando, f"{locale}: folhas ausentes ou vazias em evalPriority: {faltando}"


@pytest.mark.parametrize("locale", _LOCALES)
def test_a_mensagem_de_faixa_existe(locale: str) -> None:
    """A API recusa fora de int32 com 422. Sem esta cópia o formulário não tem
    como dizer POR QUE recusou, e o operador vê um erro sem frase."""
    v = _catalogo(locale)["form"]["validation"]
    assert v.get("evalPriorityRange", "").strip(), f"{locale}: falta form.validation.evalPriorityRange"


@pytest.mark.parametrize("locale", _LOCALES)
def test_a_tabela_tem_copia_para_a_coluna_de_prioridade(locale: str) -> None:
    """A coluna na LISTA, e não só o campo no formulário.

    Sem ela o operador digita um número às cegas: a tela não mostra a prioridade
    de nenhuma outra regra, e a própria cópia do truncamento manda "suba a
    prioridade da regra que precisa rodar" — instrução impossível de seguir
    quando não há placar. Desktop e mobile renderizam listas SEPARADAS, então as
    duas chaves são exigidas: cobrir só uma deixaria metade dos operadores sem a
    informação e passaria verde."""
    t = _catalogo(locale)["table"]

    assert len(t.get("column", {})) >= 6, (
        f"só {len(t.get('column', {}))} colunas em {locale} — a forma mudou?"
    )
    for onde in ("column", "mobile"):
        assert t[onde].get("evalPriority", "").strip(), (
            f"{locale}: falta table.{onde}.evalPriority"
        )
    assert t.get("evalPriorityTooltip", "").strip(), (
        f"{locale}: falta table.evalPriorityTooltip — o número sozinho não diz "
        "que MAIOR roda primeiro nem que é inerte em lote."
    )


def test_a_coluna_que_a_copia_descreve_existe_mesmo() -> None:
    """Amarra tudo acima ao MODELO — senão isto vira uma lista de chaves que
    alguém mantém por inércia depois de a coluna sumir."""
    assert "eval_priority" in {c.name for c in CorrelationRule.__table__.columns}


def test_os_dois_helpers_nao_sao_o_mesmo_texto() -> None:
    """O campo é INERTE em lote e decisivo em voo. Dois helpers idênticos
    passariam nos testes acima e não informariam nada — é a forma que um
    copy-paste apressado toma."""
    for locale in _LOCALES:
        ep = _catalogo(locale)["form"]["field"]["evalPriority"]
        assert ep["helperTextBatch"] != ep["helperTextInflight"], (
            f"{locale}: os dois helpers de evalPriority são idênticos."
        )
