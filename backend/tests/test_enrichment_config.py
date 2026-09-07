"""Configuração de infraestrutura do enriquecimento em BANCO, editável pela UI.

O que estes testes protegem, em ordem de custo do defeito:

1. **A senha do cache L2 nunca sai pela API nem entra no cache compartilhado.**
   É a invariante que justifica ter trazido a credencial para o banco. Um campo
   novo no schema que vaze o texto claro é a regressão silenciosa clássica: a
   tela continua funcionando e ninguém percebe.
2. **A instância do L2 é recusada quando é a mesma do Redis principal.** Era uma
   decisão fail-closed que só existia em comentário, e virou o modo de falha mais
   caro do produto: evicção silenciosa no principal reaparece como reentrega no
   SIEM. Agora a API recusa.
3. **O subsistema desligado não abre conexão de banco.** Estava travado por um
   teste que espionava ``SessionLocal``; mover a flag para o banco podia
   destruí-la sem nenhum sinal, porque resolver a flag passaria a ser, ele
   próprio, uma consulta.
4. **``config_version`` é estável** no caminho de fallback. Instável, o worker
   concluiria "a config mudou" a cada ciclo e reconstruiria o cliente Redis para
   sempre.
"""

from __future__ import annotations

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

_BASE = "/api/collectors/enrichment/config"
_SENHA = "senha-do-redis-dedicado-987"


@pytest.fixture(autouse=True)
def _clean_memo():
    """O memo é módulo-global: sem limpar, um teste herda o snapshot do outro."""
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


# ── 1. o segredo não sai ────────────────────────────────────────────────────


def test_api_nunca_devolve_a_senha_do_cache_l2(client_factory) -> None:
    """Varre a resposta INTEIRA, não campos nomeados.

    Checar ``"redis_password" not in body`` só protegeria do campo que eu me
    lembrei de checar; o risco real é o campo que alguém acrescentar depois. A
    busca pelo VALOR pega qualquer nome novo.
    """
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.put(
        _BASE,
        json={
            "redis_host": "redis-enrich",
            "redis_port": 6379,
            "redis_db": 0,
            "redis_password": _SENHA,
        },
    )
    assert r.status_code == 200, r.text
    assert _SENHA not in r.text, "a senha voltou no corpo do PUT"
    assert r.json()["redis_secret_configured"] is True
    assert r.json()["redis_url_masked"] == "redis://:***@redis-enrich:6379/0"

    got = client.get(_BASE)
    assert got.status_code == 200, got.text
    assert _SENHA not in got.text, "a senha voltou no corpo do GET"
    assert got.json()["redis_configured"] is True


def test_snapshot_serializado_para_o_cache_nao_carrega_texto_claro(
    client_factory,
) -> None:
    """``to_dict`` alimenta o Redis PRINCIPAL, que é compartilhado.

    Escrever ali a senha em claro trocaria um segredo em arquivo por um segredo
    em cache de rede — pior, porque o arquivo ao menos tem permissão de disco.
    """
    factory, SessionLocal = client_factory
    client = factory()
    _bootstrap_admin(client)
    client.put(_BASE, json={"redis_host": "redis-enrich", "redis_password": _SENHA})

    with SessionLocal() as db:
        snapshot = config_loader.load_from_db_session(db)

    payload = snapshot.to_dict()
    import json as _json

    assert _SENHA not in _json.dumps(payload), "senha em claro no cache compartilhado"
    # O ciphertext viaja; é o que permite decifrar em processo sem reabrir o DB.
    assert payload["redis_secret_ref"]
    # E o texto claro só existe ao montar a URL de conexão, em processo.
    assert _SENHA in (snapshot.redis_url() or "")


def test_senha_vazia_remove_e_ausente_preserva(client_factory) -> None:
    """Três estados, e a diferença entre eles é dado destruído por engano."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    client.put(_BASE, json={"redis_host": "redis-enrich", "redis_password": _SENHA})
    assert client.get(_BASE).json()["redis_secret_configured"] is True

    # Ausente ⇒ preserva (o operador editou só a porta).
    r = client.put(_BASE, json={"redis_port": 6380})
    assert r.json()["redis_secret_configured"] is True
    assert r.json()["redis_port"] == 6380

    # Vazia ⇒ remove.
    r = client.put(_BASE, json={"redis_password": ""})
    assert r.json()["redis_secret_configured"] is False


# ── 2. instância dedicada ───────────────────────────────────────────────────


def test_recusa_o_mesmo_endereco_do_redis_principal(client_factory, monkeypatch) -> None:
    """409 com a razão escrita, não um sucesso que quebra semanas depois."""
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "REDIS_URL", "redis://cache-principal:6379/0")
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.put(_BASE, json={"redis_host": "cache-principal", "redis_port": 6379, "redis_db": 0})
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "enrichment.config_redis_same_as_main"
    assert "volatile-lru" in r.json()["detail"]


def test_loopback_alias_nao_burla_o_guard(client_factory, monkeypatch) -> None:
    """``localhost`` e ``127.0.0.1`` são a mesma máquina.

    Sem normalizar, o guard vira teatro: o operador contorna sem querer, só por
    escrever o endereço da outra forma.
    """
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "REDIS_URL", "redis://localhost:6379/0")
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.put(_BASE, json={"redis_host": "127.0.0.1", "redis_port": 6379, "redis_db": 0})
    assert r.status_code == 409, r.text


def test_db_logico_diferente_e_permitido_mas_porta_igual_no_mesmo_host_nao(
    client_factory, monkeypatch
) -> None:
    """DB lógico distinto NÃO isola memória, mas é o que o operador pode querer
    conscientemente num ambiente de laboratório. O guard mira a colisão exata
    (host, porta, db) — que é a que reproduz o incidente de evicção."""
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "REDIS_URL", "redis://cache-principal:6379/0")
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.put(_BASE, json={"redis_host": "cache-principal", "redis_port": 6379, "redis_db": 3})
    assert r.status_code == 200, r.text


def test_guard_roda_sobre_o_estado_final_e_nao_sobre_o_payload(
    client_factory, monkeypatch
) -> None:
    """Mudar só a PORTA pode criar a colisão, e o payload não tem o host.

    Um check sobre o corpo da requisição não veria isto: o host colidente já
    estava gravado, e a requisição só carrega o número da porta.
    """
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "REDIS_URL", "redis://cache-principal:6390/0")
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.put(_BASE, json={"redis_host": "cache-principal", "redis_port": 6379, "redis_db": 0})
    assert r.status_code == 200, r.text

    r = client.put(_BASE, json={"redis_port": 6390})
    assert r.status_code == 409, "colisão criada por edição parcial passou"


# ── 3. faixas e escopo ──────────────────────────────────────────────────────


def test_valor_fora_da_faixa_e_recusado_com_o_intervalo_na_mensagem(
    client_factory,
) -> None:
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    r = client.put(_BASE, json={"remote_batch_budget_ms": 600_000})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "enrichment.config_out_of_range"
    assert "60000" in r.json()["detail"]


def test_config_e_de_instalacao_entao_exige_admin_global(client_factory) -> None:
    """Admin escopado editando isto mudaria o comportamento do vizinho."""
    factory, SessionLocal = client_factory
    client = factory()
    _bootstrap_admin(client)

    from backend.app.db import models

    with SessionLocal() as db:
        org = models.Organization(name="Filial", slug="filial")
        db.add(org)
        db.commit()
        user = db.query(models.AppUser).filter_by(username="admin").first()
        user.organization_id = org.id
        db.commit()

    r = client.get(_BASE)
    assert r.status_code == 403, r.text


# ── 4. estabilidade do snapshot e propagação ────────────────────────────────


def test_config_version_estavel_no_fallback_de_env(monkeypatch) -> None:
    """Sem memo da cifragem, cada snapshot teria versão nova.

    O Fernet usa nonce: cifrar o mesmo texto duas vezes dá bytes diferentes, e
    ``redis_secret_ref`` entra no ``config_version``. O runtime compara essa
    versão para decidir se reconstrói o cliente do L2 — instável, ele
    reconectaria a cada ciclo, para sempre. Encontrado escrevendo este teste.
    """
    from backend.app.core.config import settings

    monkeypatch.setattr(
        settings, "ENRICH_REDIS_URL", "redis://:hunter2@redis-enrich:6379/0"
    )
    config_loader._env_secret_memo.clear()

    a = config_loader._snapshot_from_env()
    b = config_loader._snapshot_from_env()

    assert a.config_version == b.config_version
    assert a.redis_host == "redis-enrich"
    assert a.redis_secret_ref and "hunter2" not in a.redis_secret_ref
    assert a.redis_url() == "redis://:hunter2@redis-enrich:6379/0"


def test_env_sem_url_mantem_o_remoto_desligado() -> None:
    """Fail-closed preservado: mudar ONDE se configura não muda o default."""
    snapshot = config_loader.EnrichmentConfigSnapshot()
    assert snapshot.redis_configured is False
    assert snapshot.redis_url() is None
    assert snapshot.redis_url_masked() is None


def test_senha_indecifravel_desliga_o_remoto_em_vez_de_derrubar_a_coleta(
    monkeypatch,
) -> None:
    """``APP_MASTER_KEY`` trocada não pode parar a COLETA, que é o produto."""
    snapshot = config_loader.EnrichmentConfigSnapshot(
        redis_host="redis-enrich", redis_secret_ref="enc::lixo-que-nao-decifra"
    )

    class _Quebrado:
        def decrypt(self, _ref):
            raise ValueError("chave errada")

    monkeypatch.setattr(
        "backend.app.core.secrets.get_default_backend", lambda: _Quebrado()
    )
    assert snapshot.redis_url() is None


def test_put_invalida_o_memo_de_processo(client_factory) -> None:
    """Sem isto, a própria API responderia com o valor velho por segundos."""
    factory, _ = client_factory
    client = factory()
    _bootstrap_admin(client)

    config_loader._memo_put(config_loader.EnrichmentConfigSnapshot(enabled=True))
    assert config_loader._memo_get() is not None

    client.put(_BASE, json={"enabled": False})
    assert config_loader._memo_get() is None, "o memo sobreviveu ao PUT"


# ── 5. a propriedade que a migração para banco podia destruir ───────────────


def test_flag_desligada_continua_sem_abrir_sessao_de_banco(monkeypatch) -> None:
    """Resolver a flag NÃO pode ser, ele próprio, uma consulta.

    Este é o teste que impede a regressão mais fácil desta mudança: ler o
    ``enabled`` do banco dentro de ``load_policy_for_org`` abriria a sessão
    ANTES de descobrir que não havia nada a fazer — destruindo em silêncio a
    propriedade "flag off custa zero" que o gate existe para garantir.

    Conta as chamadas em vez de levantar: o ``except Exception`` amplo da função
    engoliria um sentinela que levantasse.
    """
    from backend.app.collectors.enrich import runtime as enrich_runtime
    from backend.app.db import database as db_module

    chamadas: list[int] = []

    def _espiao(*_a, **_k):
        chamadas.append(1)
        raise RuntimeError("não deveria chegar aqui")

    monkeypatch.setattr(db_module, "SessionLocal", _espiao)

    assert enrich_runtime.load_policy_for_org(1, enabled=False) is None
    assert chamadas == [], "abriu sessão de banco com o enriquecimento desligado"


def test_enabled_none_cai_em_settings_preservando_chamadores_antigos(
    monkeypatch,
) -> None:
    """O parâmetro é opcional de propósito: quem chamava antes segue igual."""
    from backend.app.collectors.enrich import runtime as enrich_runtime
    from backend.app.core.config import settings
    from backend.app.db import database as db_module

    chamadas: list[int] = []

    def _espiao(*_a, **_k):
        chamadas.append(1)
        raise RuntimeError("não deveria chegar aqui")

    monkeypatch.setattr(db_module, "SessionLocal", _espiao)
    monkeypatch.setattr(settings, "ENRICHMENT_ENABLED", False, raising=False)

    assert enrich_runtime.load_policy_for_org(1) is None
    assert chamadas == []


# ── 6. rotação de credencial chega no worker ────────────────────────────────


def test_rotacionar_a_senha_derruba_o_cliente_l2_do_fork(monkeypatch) -> None:
    """Sem comparar ``config_version``, o worker seguiria com a credencial velha.

    O ``_kv_cache`` é memória de PROCESSO e sobrevive a ciclos. Rotacionar a
    senha no console sem invalidar esse objeto deixaria o fork autenticando com
    a chave revogada até o próximo restart — falha que aparece como queda de
    acerto, não como erro de credencial.
    """
    from backend.app.collectors.enrich import runtime as enrich_runtime

    construidos: list[str] = []

    monkeypatch.setattr(
        enrich_runtime,
        "build_redis_client",
        lambda url: construidos.append(url) or object(),
    )

    cfg_a = config_loader.EnrichmentConfigSnapshot(redis_host="redis-enrich")
    rt = enrich_runtime.EnrichRuntime(
        max_table_bytes=1024, lru_bytes=2048, config=cfg_a
    )
    ctx = object()

    assert rt._cache_for(ctx) is not None
    assert rt._cache_for(ctx) is not None
    assert len(construidos) == 1, "reconstruiu o cliente sem a config ter mudado"

    # Mesma URL, senha nova ⇒ versão nova ⇒ cliente novo. O ciphertext precisa
    # ser REAL: um valor falso não decifra, ``redis_url()`` devolve None pelo
    # fail-safe e o teste passaria a medir a coisa errada — foi o que aconteceu
    # na primeira escrita deste teste.
    from backend.app.core import secrets as secrets_mod

    cfg_b = config_loader.EnrichmentConfigSnapshot(
        redis_host="redis-enrich",
        redis_secret_ref=secrets_mod.get_default_backend().encrypt("senha-nova"),
    )
    rt._config = cfg_b
    assert rt._cache_for(ctx) is not None
    assert len(construidos) == 2, "a rotação de senha não derrubou o cliente antigo"


def test_runtime_sem_snapshot_usa_settings_como_antes(monkeypatch) -> None:
    """Compatibilidade: os testes que constroem o runtime direto não mudam."""
    from backend.app.collectors.enrich import runtime as enrich_runtime
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "ENRICH_REDIS_URL", "", raising=False)
    rt = enrich_runtime.EnrichRuntime(max_table_bytes=1024, lru_bytes=2048)
    assert rt._cache_for(object()) is None
    assert rt.remote_batch_budget_s == float(settings.ENRICH_REMOTE_BATCH_BUDGET_S)

    cfg = config_loader.EnrichmentConfigSnapshot(remote_batch_budget_ms=1234)
    rt2 = enrich_runtime.EnrichRuntime(
        max_table_bytes=1024, lru_bytes=2048, config=cfg
    )
    assert rt2.remote_batch_budget_s == pytest.approx(1.234)
    assert rt2._breaker_settings() == (3, 600, 120, 1920)
