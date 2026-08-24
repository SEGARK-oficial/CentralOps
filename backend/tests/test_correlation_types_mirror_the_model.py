"""Os tipos que o console usa têm de enxergar a coluna que decide quem roda.

``correlation_rules.eval_priority`` nasceu no backend com a ordenação
(``eval_priority DESC, id ASC``) e ficou INVISÍVEL para o console por dois
níveis de silêncio empilhados: nenhum schema Pydantic do EE a declarava, e
nenhuma interface TS daqui a declarava. Cada camada filtra em silêncio o que não
conhece — o operador escrevia a prioridade e recebia 200 sem efeito nenhum.

Este arquivo é o espelho do lado do TIPO. Vive no backend pelo mesmo motivo que
``test_route_editor_mirrors_allowed_fields.py``: é aqui que mora a fonte (a
coluna), e ``frontend/`` não tem gate de teste no CI do Core.

As duas metades são deliberadas e uma não substitui a outra:
  * a POSITIVA cobre o campo AUSENTE — a capacidade existe no motor e não chega
    ao console;
  * a NEGATIVA cobre o campo FANTASMA — a interface declara um nome que não
    existe no modelo, e a resposta vem ``null`` para sempre sem erro nenhum
    (foi o "Ambiente não informado" da face Pydantic desse mesmo silêncio).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.app.db.models import CorrelationRule

_TIPOS = (
    Path(__file__).resolve().parents[2] / "frontend" / "src" / "types" / "index.ts"
)

#: Nomes que a interface de LEITURA declara e que legitimamente não são colunas.
#: Lista FECHADA e comentada de propósito: cada entrada aqui é uma renúncia ao
#: espelho, e uma renúncia sem justificativa é como o campo órfão entra.
_DERIVADOS_CONHECIDOS = {
    # Coluna ``where_json`` (TEXT) desserializada para lista tipada.
    "where",
    # Metadado DERIVADO, sem coluna por trás — calculado por
    # ``_inflight_snapshot`` e só quando ``include_inflight_status=true``.
    "not_evaluated_inflight",
}


def _texto() -> str:
    if not _TIPOS.is_file():
        pytest.skip(
            "frontend/src/types/index.ts ausente — rodando contra a imagem, que "
            "não recebe o frontend. O espelho vale no repositório."
        )
    return _TIPOS.read_text(encoding="utf-8")


def _campos_da_interface(nome: str) -> set[str]:
    """Nomes de campo declarados em ``export interface <nome> { ... }``.

    Casa a PRIMEIRA ``}`` na coluna 0, que é o fecho da interface — as chaves
    internas dos blocos de doc ``/** ... */`` são indentadas e não confundem."""
    texto = _texto()
    bloco = re.search(
        r"export interface %s \{\n(.*?)\n\}" % re.escape(nome), texto, re.S
    )
    assert bloco, (
        f"não achei `export interface {nome}` em index.ts. Se o arquivo foi "
        "reorganizado, ajuste este regex — não deixe o espelho cego."
    )
    corpo = re.sub(r"/\*\*.*?\*/", "", bloco.group(1), flags=re.S)  # tira os docs
    return set(re.findall(r"^\s{2}(\w+)\??:", corpo, re.M))


def _colunas() -> set[str]:
    return {c.name for c in CorrelationRule.__table__.columns}


# ── metade POSITIVA: a capacidade chega ao console ───────────────────────────


@pytest.mark.parametrize(
    "interface",
    ["CorrelationRuleRead", "CorrelationRuleCreate", "CorrelationRuleUpdate"],
)
def test_as_tres_interfaces_declaram_eval_priority(interface: str) -> None:
    """As TRÊS, e não só a de leitura: sem ``Create``/``Update`` o console lê a
    prioridade e não tem como escrevê-la — meio caminho é o estado em que esta
    feature ficou."""
    campos = _campos_da_interface(interface)

    # Anti-vacuidade: um regex que parasse de casar devolveria conjunto vazio, e
    # a asserção abaixo acusaria "falta eval_priority" por motivo errado.
    assert len(campos) >= 8, f"só {len(campos)} campos extraídos de {interface} — o regex quebrou?"

    assert "eval_priority" in campos, (
        f"{interface} não declara eval_priority. A coluna existe e governa QUEM "
        "É AVALIADO no ciclo (eval_priority DESC, id ASC): sem o campo no tipo, "
        "o console não consegue mexer no único controle que o operador tem sobre "
        "o teto de 50 regras."
    )


def test_a_coluna_que_o_espelho_persegue_existe_mesmo() -> None:
    """Amarra o teste acima ao MODELO. Sem isto, apagar a coluna deixaria os
    parametrizados verdes espelhando um campo que não existe mais."""
    assert "eval_priority" in _colunas()


# ── metade NEGATIVA: nenhum campo fantasma ───────────────────────────────────


def test_a_interface_de_leitura_nao_declara_campo_orfao() -> None:
    """Um nome que não casa com coluna nenhuma sai ``null`` em TODA resposta,
    sem erro — no Pydantic (``from_attributes`` casa por NOME) e no TS (o campo
    simplesmente nunca vem). É o modo de falha inverso do de cima, e o par que
    impede este arquivo de virar uma lista de nomes que alguém acrescenta."""
    campos = _campos_da_interface("CorrelationRuleRead")
    assert len(campos) >= 8, f"só {len(campos)} campos extraídos — o regex quebrou?"

    orfaos = campos - _colunas() - _DERIVADOS_CONHECIDOS
    assert not orfaos, (
        f"CorrelationRuleRead declara {sorted(orfaos)}, que não é coluna de "
        "correlation_rules nem derivado conhecido. Campo órfão volta null para "
        "sempre e ninguém recebe erro — se for derivado de verdade, registre-o "
        "em _DERIVADOS_CONHECIDOS com o motivo."
    )
