"""Exceção de banco não pode carregar os VALORES da linha.

Todo ``IntegrityError``/``DataError`` do SQLAlchemy embute ``[parameters: (...)]``
no próprio ``str(exc)``. Qualquer ``logger.exception`` que capture essa exceção —
e há dezenas espalhados — despeja os valores da linha no log.

Numa plataforma de segurança isso é PII de cliente saindo pelo caminho mais
discreto que existe: o tratamento de erro, que só roda quando algo já deu errado
e ninguém está olhando.

Foi reproduzido no detector em voo, onde uma falha de FK no flush de Detection
colocava a ``dedup_key`` — que embute o valor de ``group_by``, tipicamente
usuário ou host — e o nome da regra num traceback de ~5,5 KB. O repositório é
PÚBLICO e o log vai para agregador externo.

A defesa é ``hide_parameters=True`` no ``create_engine`` (``db/database.py``).
Este arquivo prova as DUAS metades: que os valores somem, e que o erro continua
diagnosticável. Só a primeira seria satisfeita por um engine que engolisse tudo.
"""

from __future__ import annotations

import io
import logging
import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db import database
from backend.app.db.models import Base, Detection

#: Valores que só existem neste teste. Se um deles aparecer no log, o vazamento
#: é real — e a busca é por substring, não por igualdade, porque o traceback
#: interpola os valores no meio de uma tupla de parâmetros.
SENTINELA_GROUP_BY = "zzz-sentinela-valor-de-group-by-zzz"
SENTINELA_NOME_REGRA = "zzz-sentinela-nome-de-regra-zzz"


def _engine(*, hide_parameters: bool):
    """Engine :memory: com FK LIGADA — sem ela o SQLite ignora a constraint e
    nenhuma das duas metades do teste chega a acontecer."""
    eng = create_engine(
        "sqlite://",
        hide_parameters=hide_parameters,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(eng, "connect")
    def _fk(dbapi_conn, _record):  # noqa: ANN001
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(eng)
    return eng


def _provoca_e_loga(eng) -> str:
    """Viola a FK de ``organization_id`` e devolve o que o log recebeu.

    ``logger.exception`` é o ponto exato do vazamento: ele anexa o traceback, e
    é o traceback que carrega os parâmetros — a MENSAGEM formatada pelo chamador
    costuma estar limpa, o que torna o problema invisível numa revisão de código.
    """
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    logger = logging.getLogger(f"teste_pii_{id(eng)}")
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    maker = sessionmaker(bind=eng)
    try:
        with maker() as session:
            session.add(
                Detection(
                    organization_id=999_999,  # org inexistente → viola a FK
                    source="inflight",
                    dedup_key=f"inflight:1:7:{SENTINELA_GROUP_BY}",
                    rule_name=SENTINELA_NOME_REGRA,
                    severity_id=4,
                )
            )
            session.commit()
    except IntegrityError:
        logger.exception("falha ao gravar Detection (org %s)", 1)
    else:  # pragma: no cover — só acontece se a FK parar de ser aplicada
        pytest.fail(
            "o INSERT passou: a FK não foi aplicada e este teste não mediu nada. "
            "Verifique o PRAGMA foreign_keys do fixture."
        )
    return buf.getvalue()


def test_o_vazamento_existe_sem_a_defesa() -> None:
    """ÂNCORA. Sem este caso, o teste principal passaria mesmo que o SQLAlchemy
    parasse de embutir parâmetros por conta própria — e estaríamos guardando uma
    porta que já não existe, com um assert negativo verde por vacuidade.

    Ele documenta o comportamento default e falha no dia em que a premissa mudar,
    que é quando a defesa pode ser reavaliada."""
    log = _provoca_e_loga(_engine(hide_parameters=False))

    assert SENTINELA_GROUP_BY in log, (
        "o SQLAlchemy parou de embutir os parâmetros na exceção. Se isso virou "
        "o default, `hide_parameters=True` pode ser reavaliada — mas confirme "
        "antes de removê-la."
    )
    assert SENTINELA_NOME_REGRA in log


def test_com_a_defesa_os_valores_da_linha_nao_chegam_ao_log() -> None:
    log = _provoca_e_loga(_engine(hide_parameters=True))

    assert SENTINELA_GROUP_BY not in log, (
        "o valor de group_by vazou para o log. É PII de cliente (usuário/host) "
        "saindo pelo tratamento de erro."
    )
    assert SENTINELA_NOME_REGRA not in log


def test_o_erro_continua_diagnosticavel() -> None:
    """O PAR POSITIVO, e o motivo de ele existir: um engine que engolisse a
    exceção inteira passaria no teste acima e seria muito pior — trocaria
    vazamento de PII por cegueira operacional."""
    log = _provoca_e_loga(_engine(hide_parameters=True))

    assert "IntegrityError" in log, "o tipo da exceção sumiu do log"
    assert "FOREIGN KEY" in log.upper(), "a constraint violada sumiu do log"
    assert "INSERT INTO detections" in log, (
        "o statement sumiu do log — sem ele não dá para saber nem qual tabela "
        "recusou a escrita"
    )


def test_o_engine_de_producao_esconde_os_parametros() -> None:
    """Os testes acima usam engines locais; este afirma sobre o engine REAL.

    Sem ele, alguém poderia remover a flag de `database.py` e os três primeiros
    continuariam verdes, porque provam uma propriedade do SQLAlchemy — não a
    configuração deste projeto."""
    assert database.engine.hide_parameters is True, (
        "o engine de produção voltou a expor os valores da linha nas exceções "
        "(db/database.py). Ver o comentário no create_engine."
    )
