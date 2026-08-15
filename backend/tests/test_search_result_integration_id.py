"""O histórico de execução precisa dizer QUAL integração rodou a query.

O defeito que estes testes travam: o model ``SearchResult`` tem a coluna
``integration_id``, e o schema de leitura declarava ``client_id``. Como
``from_attributes`` casa por NOME, o Pydantic procurava um atributo inexistente
e caía no default. O campo saía ``None`` em **toda** resposta de histórico, e a
coluna "Query / Ambiente" da tela de agendamentos mostrava "Ambiente não
informado" para todo mundo, sempre.

É uma falha silenciosa clássica: nada quebra, nada loga, e o operador conclui
que o dado não foi registrado. Por isso o teste olha o valor, e não só o formato.
"""

from __future__ import annotations

import os
from datetime import datetime

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import pytest

from backend.app.api.schemas import SearchResultRead
from backend.app.db import models


def _orm(**over) -> models.SearchResult:
    """Um ``SearchResult`` de verdade, não um dublê.

    Usar o model real é o ponto: um dublê com ``client_id`` teria passado no
    teste e escondido exatamente o defeito que ele existe para pegar.
    """
    base = dict(
        id=1,
        search_id="abc123",
        integration_id=42,
        schedule_id=7,
        status="answered",
        statement="SELECT 1",
        table="events",
        from_ts="2026-08-01T00:00:00Z",
        to_ts="2026-08-02T00:00:00Z",
        engine="query",
        language="sql",
        result_count=3,
        created_at=datetime(2026, 8, 13, 12, 0, 0),
    )
    base.update(over)
    return models.SearchResult(**base)


def test_leitura_do_orm_carrega_a_integracao() -> None:
    lido = SearchResultRead.model_validate(_orm())

    assert lido.integration_id == 42, (
        "O histórico não sabe qual integração rodou a query. A tela resolve o "
        "nome do ambiente a partir daqui e vai mostrar 'Ambiente não informado'."
    )


def test_o_campo_antigo_continua_preenchido() -> None:
    """``client_id`` está depreciado, e enquanto existir não pode sair vazio.

    Quem já consome essa chave passaria a ver o mesmo "não informado" de antes
    se a correção só tivesse acrescentado o nome novo.
    """
    lido = SearchResultRead.model_validate(_orm())

    assert lido.client_id == 42
    assert lido.client_id == lido.integration_id


def test_corpo_legado_que_so_traz_client_id_preenche_os_dois() -> None:
    lido = SearchResultRead.model_validate(
        {
            "id": 2,
            "search_id": "z",
            "client_id": 9,
            "status": "answered",
            "statement": "x",
            "table": "t",
            "from_ts": "a",
            "to_ts": "b",
        }
    )

    assert (lido.integration_id, lido.client_id) == (9, 9)


def test_execucao_sem_integracao_continua_nula() -> None:
    """Nem toda execução tem integração, e inventar um id seria pior que nada."""
    lido = SearchResultRead.model_validate(_orm(integration_id=None))

    assert lido.integration_id is None
    assert lido.client_id is None


@pytest.mark.source_only
def test_o_schema_nao_declara_campo_que_o_model_nao_tem() -> None:
    """Guard estrutural: impede o defeito de voltar por outro campo.

    A causa raiz não foi um erro de digitação, foi um schema declarando um nome
    que o model não tem. Qualquer campo novo com o mesmo problema volta a sair
    ``None`` em silêncio, então a checagem é sobre a CLASSE de erro, não sobre
    este campo.

    A lista de exceções é explícita: campo que o schema calcula ou espelha de
    propósito entra aqui, com o motivo.
    """
    espelhados_de_proposito = {
        # Depreciado, preenchido a partir de ``integration_id`` por um validator.
        "client_id",
    }
    colunas = {c.name for c in models.SearchResult.__table__.columns}
    declarados = set(SearchResultRead.model_fields)

    orfaos = sorted(declarados - colunas - espelhados_de_proposito)
    assert not orfaos, (
        f"Campos de SearchResultRead que não existem em SearchResult: {orfaos}. "
        "Com from_attributes eles saem None em toda resposta, sem erro nenhum. "
        "Ou renomeie para casar com a coluna, ou preencha por validator e "
        "declare aqui o motivo."
    )
