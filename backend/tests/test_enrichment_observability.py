"""Observabilidade do enriquecimento: contadores por regra + log de atividade.

O que estes testes protegem é uma propriedade de CUSTO, não de correção
funcional: a feature responde "isso está funcionando?" e a maneira óbvia de
implementá-la (gravar por evento) transformaria observabilidade em gargalo. Os
testes de cardinalidade abaixo falham se alguém mover uma escrita para o laço
por evento.

O ring vive no Redis. Aqui ele é substituído por um fake em memória: o teste é
sobre O QUE se grava e QUANDO, não sobre o cliente Redis, que tem cobertura
própria.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

from backend.app.collectors.enrich import activity as enrich_activity
from backend.app.db.database import Base, get_session
from backend.app.main import app

_BASE = "/api/collectors/enrichment"


class _FakeRedis:
    """Só o que ``activity`` usa: LPUSH, LTRIM, EXPIRE e LRANGE."""

    def __init__(self) -> None:
        self.lists: Dict[str, List[str]] = {}
        self.expires: Dict[str, int] = {}

    # pipeline() devolve a si mesmo; execute() é no-op porque as operações já
    # foram aplicadas. Suficiente: o código sob teste não lê retorno de pipeline.
    def pipeline(self) -> "_FakeRedis":
        return self

    def lpush(self, key: str, *values: str) -> None:
        self.lists.setdefault(key, [])
        for v in values:
            self.lists[key].insert(0, v)

    def ltrim(self, key: str, start: int, end: int) -> None:
        if key in self.lists:
            self.lists[key] = self.lists[key][start : end + 1]

    def expire(self, key: str, ttl: int) -> None:
        self.expires[key] = ttl

    def execute(self) -> None:
        return None

    def lrange(self, key: str, start: int, end: int) -> List[str]:
        return self.lists.get(key, [])[start : end + 1]


@pytest.fixture()
def fake_redis(monkeypatch) -> _FakeRedis:
    fake = _FakeRedis()
    monkeypatch.setattr(
        "backend.app.collectors.observability_store._redis", lambda: fake
    )
    return fake


# ── o ring em si ────────────────────────────────────────────────────────────

def test_record_grava_no_ring_da_org_com_ttl(fake_redis) -> None:
    enrich_activity.record(
        7, "remote_batch", ok=False, rule_id="ti", enricher="opencti",
        reason="http_401", detail="401 Unauthorized", keys=12,
    )

    key = "obs:enrichlog:7"
    assert key in fake_redis.lists
    assert fake_redis.expires[key] == 24 * 3600, "sem TTL o ring vaza para sempre"

    entry = json.loads(fake_redis.lists[key][0])
    assert entry["ok"] is False
    assert entry["reason"] == "http_401"
    # `detail` é o que o operador lê para agir. Se sumir, o log vira "falhou".
    assert entry["detail"] == "401 Unauthorized"
    assert entry["keys"] == 12
    assert "ts" in entry


def test_ring_e_limitado_e_escopado_por_org(fake_redis) -> None:
    for i in range(250):
        enrich_activity.record(1, "table_load", ok=True, rule_id=f"r{i}")
    enrich_activity.record(2, "table_load", ok=True, rule_id="outra-org")

    assert len(fake_redis.lists["obs:enrichlog:1"]) == 200, "LTRIM não aplicado"

    da_org_1 = enrich_activity.read_recent(1, limit=200)
    assert all(e["rule_id"] != "outra-org" for e in da_org_1)
    assert da_org_1[0]["rule_id"] == "r249", "mais recente primeiro"


def test_record_nunca_levanta_com_redis_fora(monkeypatch) -> None:
    """Observabilidade caindo não pode derrubar o ciclo de coleta."""
    def _boom():
        raise ConnectionError("redis fora do ar")

    monkeypatch.setattr(
        "backend.app.collectors.observability_store._redis", _boom
    )
    enrich_activity.record(1, "table_load", ok=True)  # não levanta
    assert enrich_activity.read_recent(1) == []


def test_detail_e_truncado(fake_redis) -> None:
    """Mensagem de erro de biblioteca às vezes carrega o payload inteiro."""
    enrich_activity.record(1, "remote_batch", ok=False, detail="x" * 5000)
    entry = json.loads(fake_redis.lists["obs:enrichlog:1"][0])
    assert len(entry["detail"]) == 400


def test_org_none_nao_grava(fake_redis) -> None:
    """Sem org não há a quem mostrar, e `obs:enrichlog:None` seria lixo eterno."""
    enrich_activity.record(None, "table_load", ok=True)
    assert fake_redis.lists == {}


# ── cardinalidade: a propriedade que justifica o desenho ─────────────────────

def test_carga_de_tabela_grava_uma_vez_por_regra_por_ciclo(fake_redis) -> None:
    """1 registro por regra por CICLO, independente do tamanho do lote.

    Este teste é a trava contra a implementação ingênua. Se alguém mover a
    escrita para o applier, o número aqui vira "por evento" e o Redis recebe
    duas escritas por evento por regra.
    """
    from backend.app.collectors.enrich import runtime as rt
    from backend.app.collectors.enrich.contract import EnrichContext
    from backend.app.collectors.enrich.dsl import compile_policy

    policy = compile_policy(
        [
            {
                "id": "ti",
                "enricher": "table_exact",
                "table": "hosts",
                "key": {"source": "normalized.actor.user.name", "kind": "user"},
                "outputs": [{"from": "dono", "target": "_centralops.enrichment.dono"}],
            }
        ]
    )
    runtime = rt.EnrichRuntime(max_table_bytes=10_000_000, lru_bytes=50_000_000)
    ctx = EnrichContext(organization_id=42, config={}, secret_ref=None)

    async def _fake_load_one(reg, rule, ctx):
        return rt.DictLookupTable({"a": {"dono": "ti"}, "b": {"dono": "rh"}})

    runtime._load_one = _fake_load_one  # type: ignore[assignment]
    asyncio.run(runtime.load_tables(policy, ctx))

    entries = enrich_activity.read_recent(42)
    assert len(entries) == 1, f"esperado 1 registro por ciclo, veio {len(entries)}"
    assert entries[0]["kind"] == "table_load"
    assert entries[0]["ok"] is True
    assert entries[0]["rule_id"] == "ti"
    assert entries[0]["entries"] == 2, "contagem de linhas carregadas"
    assert "latency_ms" in entries[0]


def test_falha_de_carga_registra_o_motivo_real(fake_redis) -> None:
    """O 401 tem que chegar em quem cadastrou a fonte, não só no log do worker."""
    from backend.app.collectors.enrich import runtime as rt
    from backend.app.collectors.enrich.contract import EnrichContext
    from backend.app.collectors.enrich.dsl import compile_policy

    policy = compile_policy(
        [
            {
                "id": "ti",
                "enricher": "table_exact",
                "table": "hosts",
                "key": {"source": "normalized.actor.user.name", "kind": "user"},
                "outputs": [{"from": "d", "target": "_centralops.enrichment.d"}],
            }
        ]
    )
    runtime = rt.EnrichRuntime(max_table_bytes=10_000_000, lru_bytes=50_000_000)
    ctx = EnrichContext(organization_id=42, config={}, secret_ref=None)

    async def _boom(reg, rule, ctx):
        raise PermissionError("401 Unauthorized: token inválido")

    runtime._load_one = _boom  # type: ignore[assignment]
    tables = asyncio.run(runtime.load_tables(policy, ctx))

    assert tables == {}, "carga falhou, a coleta segue sem a tabela"
    entries = enrich_activity.read_recent(42)
    assert len(entries) == 1
    assert entries[0]["ok"] is False
    assert "401 Unauthorized" in entries[0]["detail"]


# ── endpoints ───────────────────────────────────────────────────────────────

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

    yield factory
    for c in clients:
        c.close()
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


def _bootstrap_admin(client: TestClient) -> None:
    r = client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "AdminPassword123!", "display_name": "Admin"},
    )
    assert r.status_code == 200, r.text


def _org(client: TestClient, name: str) -> int:
    r = client.post("/api/organizations", json={"name": name, "slug": name.lower()})
    assert r.status_code in (200, 201), r.text
    return int(r.json()["id"])


def test_activity_endpoint_filtra_e_escopa(client_factory, fake_redis) -> None:
    client = client_factory()
    _bootstrap_admin(client)
    org_a = _org(client, "AlfaObs")
    org_b = _org(client, "BetaObs")

    enrich_activity.record(org_a, "table_load", ok=True, rule_id="ok")
    enrich_activity.record(org_a, "remote_batch", ok=False, rule_id="ruim", reason="http_401")
    enrich_activity.record(org_b, "table_load", ok=True, rule_id="da-beta")

    r = client.get(f"{_BASE}/activity", params={"organization_id": org_a})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["organization_id"] == org_a
    assert {e["rule_id"] for e in body["entries"]} == {"ok", "ruim"}, "vazou outra org"

    r = client.get(
        f"{_BASE}/activity", params={"organization_id": org_a, "only_failures": True}
    )
    assert [e["rule_id"] for e in r.json()["entries"]] == ["ruim"]

    r = client.get(
        f"{_BASE}/activity", params={"organization_id": org_a, "kind": "remote_batch"}
    )
    assert [e["rule_id"] for e in r.json()["entries"]] == ["ruim"]


def test_metrics_endpoint_lista_regras_da_versao_vigente(client_factory, monkeypatch) -> None:
    client = client_factory()
    _bootstrap_admin(client)
    org = _org(client, "GamaObs")

    r = client.post(
        f"{_BASE}/policies", json={"name": "p1", "organization_id": org}
    )
    assert r.status_code == 201, r.text
    policy_id = r.json()["id"]

    r = client.post(
        f"{_BASE}/tables",
        json={"name": "hosts", "kind": "exact", "organization_id": org},
    )
    assert r.status_code == 201, r.text
    r = client.post(
        f"{_BASE}/tables/{r.json()['id']}/versions",
        json={"rows": {"a": {"dono": "ti"}}, "commit_message": "v1"},
    )
    assert r.status_code in (200, 201), r.text

    r = client.post(
        f"{_BASE}/policies/{policy_id}/versions",
        json={
            "rules": [
                {
                    "id": "regra-viva",
                    "enricher": "table_exact",
                    "table": "hosts",
                    "key": {"source": "normalized.actor.user.name", "kind": "user"},
                    "outputs": [{"from": "dono", "target": "_centralops.enrichment.dono"}],
                }
            ],
            "commit_message": "v1",
        },
    )
    assert r.status_code in (200, 201), r.text

    # Habilitar importa: o painel espelha o que o worker APLICA, e o worker só
    # carrega política habilitada (`runtime.load_policy_for_org`). Publicar sem
    # habilitar e ver números seria o painel confirmando algo que não acontece.
    assert client.post(f"{_BASE}/policies/{policy_id}/enable?enabled=true").status_code == 200

    seen: List[tuple] = []

    def _fake_total(kind, oid, metric, *, minutes, **kw):
        seen.append((kind, oid, metric, minutes))
        return {"hit": 90.0, "miss": 10.0}.get(metric, 0.0)

    monkeypatch.setattr(
        "backend.app.collectors.observability_store.read_window_total", _fake_total
    )

    r = client.get(f"{_BASE}/metrics", params={"organization_id": org})
    assert r.status_code == 200, r.text
    rules = r.json()["rules"]
    assert len(rules) == 1
    assert rules[0]["rule_id"] == "regra-viva"
    assert rules[0]["enricher"] == "table_exact"
    assert rules[0]["hit"] == 90.0 and rules[0]["miss"] == 10.0

    # `oid` = org:regra. Agregar por org perderia QUAL regra parou de casar, que
    # é justamente a pergunta que o painel responde.
    assert all(oid == f"{org}:regra-viva" for _, oid, _, _ in seen)


def test_metrics_recusa_janela_maior_que_a_retencao(client_factory) -> None:
    """As séries vivem 3h no store; aceitar 24h devolveria zero como se fosse dado."""
    client = client_factory()
    _bootstrap_admin(client)
    org = _org(client, "DeltaObs")

    r = client.get(
        f"{_BASE}/metrics", params={"organization_id": org, "range_minutes": 1440}
    )
    assert r.status_code == 422
def test_metrics_reflete_so_a_politica_que_o_worker_aplica(client_factory, monkeypatch) -> None:
    """Duas políticas habilitadas: o worker aplica UMA (``runtime.py:718``).

    Listar as duas seria mentir duas vezes. Regras que nunca rodam apareceriam
    com números, e ``rule_id`` só é único DENTRO de uma versão: duas políticas
    com ``regra-1`` leriam o MESMO contador e mostrariam o resultado uma da
    outra, sem forma de saber qual disparou.
    """
    client = client_factory()
    _bootstrap_admin(client)
    org = _org(client, "EpsilonObs")

    r = client.post(f"{_BASE}/tables", json={"name": "hosts", "kind": "exact", "organization_id": org})
    assert r.status_code == 201, r.text
    table_id = r.json()["id"]
    assert client.post(
        f"{_BASE}/tables/{table_id}/versions",
        json={"rows": {"a": {"dono": "ti"}}, "commit_message": "v1"},
    ).status_code in (200, 201)

    def _policy_with_rule(name: str) -> str:
        r = client.post(f"{_BASE}/policies", json={"name": name, "organization_id": org})
        assert r.status_code == 201, r.text
        pid = r.json()["id"]
        # MESMO rule_id nas duas: é o default que a própria UI sugere.
        assert client.post(
            f"{_BASE}/policies/{pid}/versions",
            json={
                "rules": [
                    {
                        "id": "regra-1",
                        "enricher": "table_exact",
                        "table": "hosts",
                        "key": {"source": "normalized.actor.user.name", "kind": "user"},
                        "outputs": [{"from": "dono", "target": "_centralops.enrichment.dono"}],
                    }
                ],
                "commit_message": name,
            },
        ).status_code in (200, 201)
        assert client.post(f"{_BASE}/policies/{pid}/enable?enabled=true").status_code == 200
        return pid

    _policy_with_rule("primeira")
    _policy_with_rule("segunda")

    monkeypatch.setattr(
        "backend.app.collectors.observability_store.read_window_total",
        lambda kind, oid, metric, **kw: 1.0,
    )

    r = client.get(f"{_BASE}/metrics", params={"organization_id": org})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["rules"]) == 1, "só a política aplicada entra no painel"
    assert body["policy_name"] == "primeira", "a primeira habilitada, por criação"


def test_activity_ignora_entrada_ilegivel(client_factory, fake_redis) -> None:
    """Schema do ring pode evoluir; linha antiga não derruba a aba inteira."""
    client = client_factory()
    _bootstrap_admin(client)
    org = _org(client, "ZetaObs")

    enrich_activity.record(org, "table_load", ok=True, rule_id="boa")
    # Sem `ts`/`kind`/`ok`: o que uma versão futura poderia ter gravado.
    fake_redis.lists[f"obs:enrichlog:{org}"].insert(0, json.dumps({"campo_novo": 1}))

    r = client.get(f"{_BASE}/activity", params={"organization_id": org})
    assert r.status_code == 200, r.text
    assert [e["rule_id"] for e in r.json()["entries"]] == ["boa"]
def test_metrics_nao_inventa_numeros_para_politica_desabilitada(client_factory) -> None:
    """Publicada mas não habilitada: nada roda, então nada é reportado.

    ``policy_name`` volta ``None``, que é o sinal para a UI dizer "nenhuma
    política ativa" em vez de "0% de acerto" — as duas pedem ações opostas.
    """
    client = client_factory()
    _bootstrap_admin(client)
    org = _org(client, "EtaObs")

    r = client.post(f"{_BASE}/policies", json={"name": "dormindo", "organization_id": org})
    assert r.status_code == 201, r.text

    r = client.get(f"{_BASE}/metrics", params={"organization_id": org})
    assert r.status_code == 200, r.text
    assert r.json()["rules"] == []
    assert r.json()["policy_name"] is None
def _remote_policy():
    from backend.app.collectors.enrich.dsl import compile_policy

    return compile_policy(
        [
            {
                "id": "ti-remota",
                "enricher": "virustotal",
                "source": "vt",
                "mode": "remote",
                "key": {"source": "normalized.src_endpoint.ip", "kind": "ip"},
                "outputs": [{"from": "malicious", "target": "_centralops.enrichment.vt.m"}],
            }
        ]
    )


def test_remoto_desligado_por_falta_de_l2_aparece_no_log(fake_redis, monkeypatch) -> None:
    """Sem ``ENRICH_REDIS_URL`` o remoto NÃO roda, e isso precisa ser visível.

    O default da variável é vazio, então esta é a configuração de fábrica. Sem
    registro, a aba de Consultas diz "nenhuma falha, a consulta está de pé"
    enquanto nada remoto acontece, e o operador vai procurar o problema no dado.
    """
    from backend.app.collectors.enrich import runtime as rt
    from backend.app.collectors.enrich.contract import EnrichContext

    runtime = rt.EnrichRuntime(max_table_bytes=10_000_000, lru_bytes=50_000_000)
    monkeypatch.setattr(runtime, "_cache_for", lambda ctx: None)
    ctx = EnrichContext(organization_id=51, config={}, secret_ref=None)

    res = asyncio.run(
        runtime.resolve_remote(
            _remote_policy(),
            [{"normalized": {"src_endpoint": {"ip": "10.0.0.1"}}}],
            ctx,
            budget_s=5.0,
        )
    )
    assert res.get("ti-remota", "10.0.0.1") == (False, None), "nada foi resolvido"

    entries = enrich_activity.read_recent(51)
    assert len(entries) == 1, "o desligamento do remoto tem que aparecer"
    assert entries[0]["ok"] is False
    assert entries[0]["reason"] == "no_l2_cache"
    assert "ENRICH_REDIS_URL" in entries[0]["detail"]


def test_orcamento_esgotado_aparece_no_log(fake_redis, monkeypatch) -> None:
    """Gate binário: o lote inteiro segue sem remoto. Silenciar isso engana."""
    from backend.app.collectors.enrich import runtime as rt
    from backend.app.collectors.enrich.contract import EnrichContext

    class _CacheVazio:
        async def get_many(self, keys):
            return {}, {}, list(keys)

    runtime = rt.EnrichRuntime(max_table_bytes=10_000_000, lru_bytes=50_000_000)
    monkeypatch.setattr(runtime, "_cache_for", lambda ctx: _CacheVazio())
    ctx = EnrichContext(organization_id=52, config={}, secret_ref=None)

    asyncio.run(
        runtime.resolve_remote(
            _remote_policy(),
            [{"normalized": {"src_endpoint": {"ip": "10.0.0.2"}}}],
            ctx,
            budget_s=0.0,  # já estourado ao entrar no laço
        )
    )

    entries = enrich_activity.read_recent(52)
    assert len(entries) == 1
    assert entries[0]["reason"] == "budget_exhausted"
    assert "ENRICH_REMOTE_BATCH_BUDGET_S" in entries[0]["detail"]
