"""O evento de scheduled query precisa ser OCSF 1.8 de verdade.

Ele não era. O bloco ``normalized`` tinha três chaves (``severity_id``,
``message`` e um ``metadata`` com nomes próprios) e faltava toda a identidade da
classe: sem ``class_uid``, sem ``time``, sem ``type_uid``.

Isso não era cosmético. Os destinos que entregam SÓ o ``normalized`` (Chronicle,
Security Lake, Datadog, webhook em modo OCSF) recebiam um objeto que nenhum
consumidor de OCSF sabe classificar, e o Security Lake ainda deriva a partição
do campo ``time``, que não existia.

Junto ia um segundo defeito, de roteamento: o envelope saía sem
``organization_id``, então casava somente rotas GLOBAIS. Uma rota criada pelo
próprio tenant nunca recebia o resultado da scheduled query dele.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import pytest

from backend.app.collectors import scheduler_tasks
from backend.app.collectors.normalize.ocsf import get_registry, structural_gate


@pytest.fixture()
def evento(monkeypatch):
    """Dispara a função real e captura o envelope que iria para os destinos."""
    capturado: list = []
    monkeypatch.setattr(
        "backend.app.collectors.pipeline._enqueue_dispatch",
        lambda batch, *a, **kw: capturado.extend(batch),
    )

    integration = SimpleNamespace(
        id=42,
        name="EDR corporativo",
        platform="sophos",
        organization_id=7,
        data_geography="eu",
        organization=SimpleNamespace(id=7, name="Org Sete"),
    )
    sched = SimpleNamespace(id=3)
    query_def = SimpleNamespace(
        id=9,
        title="Logins fora do horário",
        description="Detecta autenticação madrugada",
        statement="SELECT * FROM xdr_data",
        table="xdr_data",
    )
    record = SimpleNamespace(id=101, language="sql", ocsf_mapping_version="1.8.0")

    scheduler_tasks._dispatch_scheduled_query_alert(
        integration=integration,
        sched=sched,
        query_def=query_def,
        items=[{"a": 1}, {"a": 2}],
        from_ts="2026-08-01T00:00:00Z",
        to_ts="2026-08-02T00:00:00Z",
        record=record,
    )
    assert capturado, "nenhum envelope foi despachado"
    return capturado[0]


def test_o_evento_passa_no_gate_estrutural_do_ocsf(evento) -> None:
    """A prova que interessa: um validador OCSF aceita o que sai daqui."""
    resultado = structural_gate(evento["normalized"], get_registry("1.8.0"))

    assert resultado.valid, f"OCSF inválido: {resultado.reason} {resultado.missing_required}"
    assert resultado.class_uid == 1006
    assert resultado.class_name == "Scheduled Job Activity"


def test_carrega_a_identidade_da_classe(evento) -> None:
    n = evento["normalized"]

    assert n["class_uid"] == 1006
    assert n["category_uid"] == 1
    # type_uid = class_uid * 100 + activity_id, como manda a spec.
    assert n["type_uid"] == n["class_uid"] * 100 + n["activity_id"]


def test_o_tempo_esta_em_milissegundos(evento) -> None:
    """Segundos aqui seria erro de 1000x, que este repo já pagou em 16 mappings."""
    t = evento["normalized"]["time"]

    # ~1.7e12 em ms contra ~1.7e9 em segundos: a ordem de grandeza denuncia.
    assert t > 1_000_000_000_000, f"time={t} parece estar em segundos"


def test_leva_o_contexto_que_antes_ficava_para_tras(evento) -> None:
    """Sem isto o destino recebe "uma query rodou" e nada mais."""
    extra = evento["normalized"]["unmapped"]

    assert extra["query_title"] == "Logins fora do horário"
    assert extra["items_count"] == 2
    assert extra["integration_name"] == "EDR corporativo"
    assert extra["platform"] == "sophos"
    assert extra["organization_id"] == 7
    assert extra["dialect"] == "sql"
    assert extra["search_result_id"] == 101
    assert extra["from"] == "2026-08-01T00:00:00Z"
    # Mesma chave da Detection durável, para o destino correlacionar.
    assert extra["dedup_key"] == "sched:3:integ:42"


def test_o_contexto_do_produto_fica_em_unmapped(evento) -> None:
    """O OCSF reserva ``unmapped`` para o que é específico do produto.

    Inventar campo de primeiro nível produziria um evento que passa no
    validador e que nenhum consumidor sabe ler.
    """
    n = evento["normalized"]

    assert "schedule_id" not in n
    assert "query_title" not in n
    assert n["unmapped"]["schedule_id"] == 3


def test_o_envelope_carrega_a_organizacao(evento) -> None:
    """Sem isto o evento casa SOMENTE rota global.

    A rota que o tenant cria para os próprios resultados nunca recebia nada, e
    o sintoma é o pior possível: entrega silenciosamente vazia.
    """
    meta = evento["_centralops"]

    assert meta.get("organization_id") == 7, (
        "organization_id ausente: o roteador só vai casar rotas globais."
    )


def test_severidade_preservada(evento) -> None:
    """Ela vira o PRI do syslog; mexer mudaria o alerta de quem já depende."""
    assert evento["normalized"]["severity_id"] == 5


def test_o_raw_continua_com_teto_de_itens(evento) -> None:
    """O teto existe para não estourar o limite de payload do Wazuh."""
    assert evento["raw"]["items_truncated"] is False
    assert len(evento["raw"]["items"]) == 2
