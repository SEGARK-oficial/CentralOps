"""Prontidão do enriquecimento por organização — ``GET .../readiness``.

Responde ao chamado nº 1 de suporte desta feature ("liguei e não faz nada"), que
antes exigia percorrer três abas e cruzar informação de duas.

A propriedade central que estes testes protegem não é "o endpoint responde": é
que ele **só alarma quando o problema afeta ESTA organização**. Um aviso que
aparece para quem não é afetado ensina o operador a ignorar avisos, e aí o aviso
verdadeiro também morre. Por isso metade dos testes aqui verifica SILÊNCIO.
"""

from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

from backend.app.collectors.enrich import config_loader
from backend.app.db.database import Base, get_session
from backend.app.main import app

_BASE = "/api/collectors/enrichment"


@pytest.fixture(autouse=True)
def _clean_memo():
    config_loader.reset_process_memo()
    yield
    config_loader.reset_process_memo()


@pytest.fixture()
def client_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    def override_get_session():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_session] = override_get_session
    clients: list[TestClient] = []

    def factory() -> TestClient:
        c = TestClient(app)
        clients.append(c)
        return c

    yield factory, TestingSessionLocal
    for c in clients:
        c.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


def _bootstrap_admin(client: TestClient) -> None:
    r = client.post(
        "/api/auth/bootstrap",
        json={
            "username": "admin",
            "password": "AdminPassword123!",
            "display_name": "Admin",
        },
    )
    assert r.status_code == 200, r.text


def _org(client: TestClient, name: str) -> int:
    r = client.post("/api/organizations", json={"name": name, "slug": name.lower()})
    assert r.status_code in (200, 201), r.text
    return int(r.json()["id"])


def _steps(client: TestClient, org: int) -> dict:
    r = client.get(f"{_BASE}/readiness", params={"organization_id": org})
    assert r.status_code == 200, r.text
    body = r.json()
    return {s["key"]: s for s in body["steps"]} | {"_ready": body["ready"]}


def _publish_policy(client: TestClient, org: int, rules: list, name="p1") -> str:
    r = client.post(
        f"{_BASE}/policies", json={"name": name, "organization_id": org}
    )
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    r = client.post(
        f"{_BASE}/policies/{pid}/versions",
        json={"rules": rules, "commit_message": "inicial"},
    )
    assert r.status_code in (200, 201), r.text
    r = client.post(f"{_BASE}/policies/{pid}/enable", params={"enabled": True})
    assert r.status_code == 200, r.text
    return pid


def _table(client: TestClient, org: int, name: str, publish: bool) -> str:
    r = client.post(
        f"{_BASE}/tables",
        json={"name": name, "organization_id": org, "match_mode": "cidr"},
    )
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    if publish:
        r = client.post(
            f"{_BASE}/tables/{tid}/versions",
            json={
                "rows": {"10.0.5.0/24": {"site": "filial"}},
                "commit_message": "inicial",
            },
        )
        assert r.status_code in (200, 201), r.text
    return tid


def _table_rule(table: str, rid="regra-site") -> dict:
    return {
        "id": rid,
        "enricher": "table_cidr",
        "table": table,
        "key": {"source": "normalized.src_endpoint.ip", "kind": "ip"},
        "outputs": [{"from": "site", "target": "_centralops.enrichment.src.site"}],
    }


# ── o caminho feliz e o caminho vazio ───────────────────────────────────────


def test_org_sem_nada_bloqueia_na_politica_e_nao_alarma_o_resto(client_factory) -> None:
    """Sem política, os outros passos não têm o que dizer — e não dizem.

    É a diferença entre uma lista de quatro alarmes vermelhos (que não orienta
    ninguém) e um passo bloqueante com ação.
    """
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    org = _org(client, "Vazia")

    st = _steps(client, org)
    assert st["_ready"] is False
    assert st["policy"]["status"] == "blocked"
    assert st["policy"]["action"]["label"] == "Criar política"
    # Nada mais bloqueia: não há regra citando fonte nem tabela.
    assert st["sources"]["status"] == "not_applicable"
    assert st["tables"]["status"] == "not_applicable"
    assert st["cache_l2"]["status"] == "not_applicable"


def test_politica_com_tabela_publicada_fica_pronta(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    org = _org(client, "Ok")
    _table(client, org, "rede", publish=True)
    _publish_policy(client, org, [_table_rule("rede")])

    st = _steps(client, org)
    assert st["_ready"] is True, st
    assert st["policy"]["status"] == "ok"
    assert st["tables"]["status"] == "ok"


# ── o que o endpoint existe para pegar ──────────────────────────────────────


def test_tabela_citada_sem_versao_publicada_bloqueia_com_o_nome(client_factory) -> None:
    """Caso de suporte nº 2: a carga falha a cada ciclo, sem erro na tela.

    Este teste também é o que prova o defeito que encontrei ao escrevê-lo:
    ``_rules_of_org`` não devolvia o campo ``table``, então o passo de tabelas
    lia uma lista sempre vazia e aprovava qualquer configuração. Um teste que
    só checasse o caminho feliz teria passado com o bug dentro.
    """
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    org = _org(client, "SemVersao")
    _table(client, org, "rede", publish=False)
    _publish_policy(client, org, [_table_rule("rede")])

    st = _steps(client, org)
    assert st["_ready"] is False
    assert st["tables"]["status"] == "blocked"
    assert "rede" in st["tables"]["detail"]
    assert "sem versão publicada" in st["tables"]["detail"]


def test_segunda_politica_habilitada_vira_aviso_com_o_nome_da_ignorada(
    client_factory,
) -> None:
    """"Editei e não mudou nada": vale UMA por org, a mais antiga."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    org = _org(client, "Duas")
    _table(client, org, "rede", publish=True)
    _publish_policy(client, org, [_table_rule("rede")], name="antiga")

    # A API recusa habilitar a segunda; o cenário que a prontidão cobre é o de
    # uma base onde as duas já estão habilitadas (anterior a essa recusa).
    from backend.app.db import models

    _, SessionLocal = client_factory
    with SessionLocal() as db:
        p2 = models.EnrichmentPolicy(
            organization_id=org, name="nova", enabled=True
        )
        db.add(p2)
        db.commit()

    st = _steps(client, org)
    assert st["policy"]["status"] == "warning"
    assert "nova" in st["policy"]["detail"]
    # Aviso, não bloqueio: a política antiga SEGUE funcionando.
    assert st["policy"]["blocking"] is False
    assert st["_ready"] is True


def test_cache_l2_ausente_so_bloqueia_quem_usa_enricher_por_lote(
    client_factory,
) -> None:
    """O silêncio é a parte testada.

    Uma organização que só usa tabela do cliente não é afetada pelo L2 ausente.
    Alarmá-la seria treinar o operador a ignorar justamente o aviso que importa
    para a organização vizinha.
    """
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    so_tabela = _org(client, "SoTabela")
    _table(client, so_tabela, "rede", publish=True)
    _publish_policy(client, so_tabela, [_table_rule("rede")])

    st = _steps(client, so_tabela)
    assert st["cache_l2"]["status"] == "not_applicable"
    assert st["cache_l2"]["blocking"] is False
    assert st["_ready"] is True


def test_cache_l2_ausente_bloqueia_quem_usa_e_diz_qual_enricher(
    client_factory,
) -> None:
    factory, SessionLocal = client_factory
    client = factory()
    _bootstrap_admin(client)
    org = _org(client, "ComVT")

    r = client.post(
        f"{_BASE}/sources",
        json={
            "name": "vt-prod",
            "enricher": "virustotal",
            "organization_id": org,
            "secret": "chave-vt",
        },
    )
    assert r.status_code == 201, r.text

    _publish_policy(
        client,
        org,
        [
            {
                "id": "vt-ip",
                "enricher": "virustotal",
                "source": "vt-prod",
                "key": {"source": "normalized.src_endpoint.ip", "kind": "ip"},
                "outputs": [
                    {
                        "from": "malicious",
                        "target": "_centralops.enrichment.src.vt_malicious",
                    }
                ],
            }
        ],
    )

    st = _steps(client, org)
    assert st["cache_l2"]["status"] == "blocked"
    assert "virustotal" in st["cache_l2"]["detail"]
    # E manda para a tela certa, marcada como fora do alcance do admin de org.
    assert st["cache_l2"]["action"]["scope"] == "global"
    assert st["cache_l2"]["action"]["route"] == "/config?tab=enrichment"
    assert st["_ready"] is False


def test_fonte_nunca_testada_e_aviso_e_fonte_que_falhou_traz_a_mensagem(
    client_factory,
) -> None:
    """Nunca testada e testada-com-falha pedem ações diferentes.

    Antes as duas apareciam iguais (só existia ``secret_configured``), e a
    mensagem do provedor ficava no log do worker.
    """
    factory, SessionLocal = client_factory
    client = factory()
    _bootstrap_admin(client)
    org = _org(client, "Fontes")

    r = client.post(
        f"{_BASE}/sources",
        json={
            "name": "cti",
            "enricher": "opencti",
            "organization_id": org,
            "config": {"url": "https://cti.example"},
            "secret": "token",
        },
    )
    assert r.status_code == 201, r.text

    _publish_policy(
        client,
        org,
        [
            {
                "id": "cti-hash",
                "enricher": "opencti",
                "source": "cti",
                "key": {"source": "normalized.file.hashes[0].value", "kind": "file_hash"},
                "outputs": [
                    {"from": "score", "target": "_centralops.enrichment.file.score"}
                ],
            }
        ],
    )

    st = _steps(client, org)
    assert st["sources"]["status"] == "warning"
    assert "nunca testada" in st["sources"]["detail"]

    from backend.app.db import models
    from datetime import datetime

    with SessionLocal() as db:
        src = db.query(models.EnrichmentSource).filter_by(name="cti").first()
        src.last_test_at = datetime.utcnow()
        src.last_test_ok = False
        src.last_test_message = "401 Unauthorized: token inválido"
        db.commit()

    st = _steps(client, org)
    assert st["sources"]["status"] == "warning"
    assert "401 Unauthorized" in st["sources"]["detail"], (
        "a mensagem do provedor não chegou à tela — é o que permite agir sem "
        "abrir log de worker"
    )


def test_fonte_citada_mas_nao_cadastrada_bloqueia(client_factory) -> None:
    """A regra cita por NOME; o nome pode não existir na org."""
    factory, SessionLocal = client_factory
    client = factory()
    _bootstrap_admin(client)
    org = _org(client, "Orfa")

    r = client.post(
        f"{_BASE}/sources",
        json={
            "name": "cti",
            "enricher": "opencti",
            "organization_id": org,
            "config": {"url": "https://cti.example"},
            "secret": "token",
        },
    )
    assert r.status_code == 201, r.text
    _publish_policy(
        client,
        org,
        [
            {
                "id": "cti-hash",
                "enricher": "opencti",
                "source": "cti",
                "key": {"source": "normalized.file.hashes[0].value", "kind": "file_hash"},
                "outputs": [
                    {"from": "score", "target": "_centralops.enrichment.file.score"}
                ],
            }
        ],
    )

    from backend.app.db import models

    with SessionLocal() as db:
        db.query(models.EnrichmentSource).filter_by(name="cti").delete()
        db.commit()

    st = _steps(client, org)
    assert st["sources"]["status"] == "blocked"
    assert "não cadastrada" in st["sources"]["detail"]
    assert "cti" in st["sources"]["detail"]


def test_subsistema_desligado_bloqueia_tudo_e_aponta_para_o_admin_global(
    client_factory,
) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    org = _org(client, "Desligada")
    client.put("/api/collectors/enrichment/config", json={"enabled": False})

    st = _steps(client, org)
    assert st["subsystem"]["status"] == "blocked"
    assert st["subsystem"]["action"]["scope"] == "global"
    assert st["_ready"] is False


# ── isolamento entre organizações ───────────────────────────────────────────


def test_uma_org_nao_alcanca_a_tabela_da_outra_nem_para_publicar(
    client_factory,
) -> None:
    """A regra cita a tabela por NOME, e o nome é resolvido POR ORGANIZAÇÃO.

    Escrevi este teste esperando que a prontidão fosse quem pegaria o caso.
    Não é: a API recusa a publicação antes disso (``enrichment.table_missing``),
    que é o lugar mais forte para o gate estar — o erro chega a quem escreveu a
    regra, no ato, em vez de virar carga falhando a cada ciclo.

    Fica registrado aqui porque é a propriedade de isolamento que sustenta a
    proposta de política herdada da matriz: se o nome resolvesse globalmente, o
    CMDB de um cliente serviria contexto para outro.
    """
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    a = _org(client, "OrgA")
    b = _org(client, "OrgB")
    _table(client, a, "rede", publish=True)
    _publish_policy(client, a, [_table_rule("rede")], name="pa")

    r = client.post(
        f"{_BASE}/policies", json={"name": "pb", "organization_id": b}
    )
    assert r.status_code == 201, r.text
    r = client.post(
        f"{_BASE}/policies/{r.json()['id']}/versions",
        json={"rules": [_table_rule("rede")], "commit_message": "tentativa"},
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "enrichment.table_missing"

    # E a Org A segue pronta — o isolamento não é "nada funciona".
    assert _steps(client, a)["tables"]["status"] == "ok"


# ── duplicar política para outra organização ────────────────────────────────
#
# É a versão Community — e manual — do mecanismo que a matriz usaria para
# aplicar um modelo às filhas. Funciona pela mesma razão estrutural: a regra
# cita tabela e fonte POR NOME, nunca por id, então o mesmo documento vale em
# qualquer organização que tenha os nomes correspondentes.


def test_duplicar_recusa_quando_falta_a_tabela_no_destino(client_factory) -> None:
    """O modo de falha que este endpoint existe para evitar é MUDO.

    Sem a checagem, a política nasceria válida no destino e a carga da tabela
    falharia a cada ciclo — num log de worker que ninguém lê, com os eventos
    saindo sem contexto e sem erro em tela nenhuma.
    """
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    origem = _org(client, "Matriz1")
    destino = _org(client, "Filial1")

    _table(client, origem, "plano-de-rede", publish=True)
    pid = _publish_policy(client, origem, [_table_rule("plano-de-rede")])

    r = client.post(
        f"{_BASE}/policies/{pid}/duplicate-preflight",
        json={"target_organization_id": destino},
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is False
    assert r.json()["missing_tables"] == ["plano-de-rede"]

    r = client.post(
        f"{_BASE}/policies/{pid}/duplicate",
        json={"target_organization_id": destino},
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "enrichment.duplicate_blocked"
    assert "plano-de-rede" in r.json()["detail"]


def test_duplicar_cria_politica_DESABILITADA_com_a_primeira_versao(
    client_factory,
) -> None:
    """Copiar não pode colocar regra no caminho quente de outro tenant sozinho.

    Vale uma política por organização; habilitar a cópia enquanto outra já roda
    seria uma surpresa cara, e a decisão é de quem opera aquela organização.
    """
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    origem = _org(client, "Matriz2")
    destino = _org(client, "Filial2")

    _table(client, origem, "plano-de-rede", publish=True)
    _table(client, destino, "plano-de-rede", publish=True)
    pid = _publish_policy(client, origem, [_table_rule("plano-de-rede")], name="padrao")

    r = client.post(
        f"{_BASE}/policies/{pid}/duplicate-preflight",
        json={"target_organization_id": destino},
    )
    assert r.json()["ok"] is True, r.text

    r = client.post(
        f"{_BASE}/policies/{pid}/duplicate",
        json={"target_organization_id": destino, "commit_message": "padrão do SOC"},
    )
    assert r.status_code == 201, r.text
    nova = r.json()
    assert nova["organization_id"] == destino
    assert nova["enabled"] is False, "a cópia entrou em vigor sozinha"
    assert nova["current_version_id"], "a cópia nasceu sem versão publicada"
    assert nova["rule_count"] == 1

    # A original segue intocada e valendo.
    assert _steps(client, origem)["policy"]["status"] == "ok"


def test_duplicar_recusa_nome_ja_usado_no_destino(client_factory) -> None:
    """Nome é único por organização; o 409 do banco viraria erro opaco."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    origem = _org(client, "Matriz3")
    destino = _org(client, "Filial3")

    _table(client, origem, "rede", publish=True)
    _table(client, destino, "rede", publish=True)
    pid = _publish_policy(client, origem, [_table_rule("rede")], name="padrao")

    r = client.post(f"{_BASE}/policies", json={"name": "padrao", "organization_id": destino})
    assert r.status_code == 201, r.text

    r = client.post(
        f"{_BASE}/policies/{pid}/duplicate-preflight",
        json={"target_organization_id": destino},
    )
    assert r.json()["name_conflict"] is True
    assert r.json()["ok"] is False

    # Com outro nome, passa.
    r = client.post(
        f"{_BASE}/policies/{pid}/duplicate",
        json={"target_organization_id": destino, "name": "padrao-copia"},
    )
    assert r.status_code == 201, r.text


def test_tabela_sem_versao_no_destino_e_AVISO_e_nao_bloqueio(client_factory) -> None:
    """Existir e estar publicada são coisas diferentes.

    A tabela existir basta para a política ser válida; não ter versão publicada
    impede o FUNCIONAMENTO, e é o operador do destino quem resolve isso. Barrar
    a cópia aqui obrigaria a ordem inversa — publicar a tabela antes de saber
    quais campos a política usa.
    """
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    origem = _org(client, "Matriz4")
    destino = _org(client, "Filial4")

    _table(client, origem, "rede", publish=True)
    _table(client, destino, "rede", publish=False)
    pid = _publish_policy(client, origem, [_table_rule("rede")], name="p4")

    r = client.post(
        f"{_BASE}/policies/{pid}/duplicate-preflight",
        json={"target_organization_id": destino},
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert r.json()["tables_without_version"] == ["rede"]

    r = client.post(
        f"{_BASE}/policies/{pid}/duplicate", json={"target_organization_id": destino}
    )
    assert r.status_code == 201, r.text
    # E a prontidão do destino já aponta o que falta lá.
    st = _steps(client, destino)
    assert st["policy"]["status"] == "blocked"  # cópia nasce desabilitada


def test_politica_sem_versao_publicada_nao_pode_ser_duplicada(client_factory) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)
    origem = _org(client, "Matriz5")
    destino = _org(client, "Filial5")

    r = client.post(f"{_BASE}/policies", json={"name": "vazia", "organization_id": origem})
    assert r.status_code == 201, r.text
    pid = r.json()["id"]

    r = client.post(
        f"{_BASE}/policies/{pid}/duplicate", json={"target_organization_id": destino}
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "enrichment.policy_without_version"
