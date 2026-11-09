"""ADR-0018, Onda 1 — o deploy de produção do data-plane funciona como documentado.

* O data-plane fala com broker gerenciado: com SSL/SASL_SSL o cliente ganha um
  ``ssl_context`` (o aiokafka RECUSA esses protocolos sem ele — o caminho de HA
  documentado falhava no boot).
* O tópico de entrega nasce com ``min.insync.replicas`` coerente com o RF.
* Toda fila Celery roteada no código tem consumidor no chart Helm (a
  ``collect.query`` não tinha) e o worker sobe pelo app Celery da IMAGEM.
* No compose, TODOS os serviços da app usam o MESMO data-plane (a API e os
  workers de maintenance/query caíam no default ``celery`` do código).

O render do chart com ``helm template`` está em
``kubernetes/helm/centralops/tests/dataplane-render-test.sh`` (CI: helm guard).
"""

from __future__ import annotations

import os
import re
import ssl
from pathlib import Path

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import pytest

from backend.app.collectors.dataplane import kafka_transport as kt

REPO_ROOT = Path(__file__).resolve().parents[2]
CHART = REPO_ROOT / "kubernetes" / "helm" / "centralops"


# ── TLS do data-plane ────────────────────────────────────────────────────────


def test_kafka_client_kwargs_tls(monkeypatch):
    monkeypatch.setattr(kt.settings, "KAFKA_SECURITY_PROTOCOL", "SASL_SSL")
    monkeypatch.setattr(kt.settings, "KAFKA_SASL_MECHANISM", "SCRAM-SHA-512")
    monkeypatch.setattr(kt.settings, "KAFKA_SASL_USERNAME", "u")
    monkeypatch.setattr(kt.settings, "KAFKA_SASL_PASSWORD", "p")
    monkeypatch.setattr(kt.settings, "KAFKA_SSL_CAFILE", None)
    monkeypatch.setattr(kt.settings, "KAFKA_SSL_VERIFY", True)

    kwargs = kt._client_kwargs()

    assert isinstance(kwargs["ssl_context"], ssl.SSLContext)
    assert kwargs["ssl_context"].verify_mode == ssl.CERT_REQUIRED
    assert kwargs["sasl_mechanism"] == "SCRAM-SHA-512"


def test_aiokafka_accepts_the_sasl_ssl_kwargs(monkeypatch):
    """O que estava quebrado de fato: o construtor do aiokafka levantava
    "ssl_context is mandatory" com SASL_SSL e sem contexto."""
    aiokafka = pytest.importorskip("aiokafka")
    monkeypatch.setattr(kt.settings, "KAFKA_SECURITY_PROTOCOL", "SASL_SSL")
    monkeypatch.setattr(kt.settings, "KAFKA_SASL_MECHANISM", "PLAIN")
    monkeypatch.setattr(kt.settings, "KAFKA_SASL_USERNAME", "u")
    monkeypatch.setattr(kt.settings, "KAFKA_SASL_PASSWORD", "p")

    import asyncio

    async def build():
        consumer = aiokafka.AIOKafkaConsumer("t", **kt._client_kwargs(), group_id="g")
        return consumer  # sem start(): só o construtor valida o ssl_context

    assert asyncio.run(build()) is not None


def test_plaintext_has_no_ssl_context(monkeypatch):
    monkeypatch.setattr(kt.settings, "KAFKA_SECURITY_PROTOCOL", "PLAINTEXT")
    assert "ssl_context" not in kt._client_kwargs()


def test_tls_verify_off_only_when_asked(monkeypatch):
    monkeypatch.setattr(kt.settings, "KAFKA_SECURITY_PROTOCOL", "SSL")
    monkeypatch.setattr(kt.settings, "KAFKA_SSL_CAFILE", None)
    monkeypatch.setattr(kt.settings, "KAFKA_SSL_VERIFY", False)
    ctx = kt._ssl_context()
    assert ctx.verify_mode == ssl.CERT_NONE and ctx.check_hostname is False


@pytest.mark.parametrize(
    "rf, explicit, expected",
    [
        (1, None, {}),
        (2, None, {"min.insync.replicas": "1"}),
        (3, None, {"min.insync.replicas": "2"}),
        (3, 1, {"min.insync.replicas": "1"}),
        (3, 5, {"min.insync.replicas": "3"}),  # nunca acima do RF
    ],
)
def test_deliver_topic_min_insync(monkeypatch, rf, explicit, expected):
    monkeypatch.setattr(kt.settings, "KAFKA_DELIVER_REPLICATION", rf)
    monkeypatch.setattr(kt.settings, "KAFKA_DELIVER_MIN_INSYNC_REPLICAS", explicit)
    assert kt._deliver_topic_configs() == expected


# ── Chart Helm: filas consumidas e app da imagem ────────────────────────────


def _helm_queues_consumed() -> set[str]:
    """Filas do ``-Q`` de cada papel no template, só para papéis declarados em
    ``values.yaml`` com réplicas (os que o chart realmente sobe)."""
    yaml = pytest.importorskip("yaml")
    values = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    deployed = {
        name
        for name, w in (values.get("workers") or {}).items()
        if isinstance(w, dict) and int(w.get("replicaCount") or 0) > 0
    }
    tpl = (CHART / "templates" / "workers-deployment.yaml").read_text(encoding="utf-8")
    consumed: set[str] = set()
    branch = None
    for line in tpl.splitlines():
        m = re.search(r'eq \$name "([a-z]+)"', line)
        if m and ("if eq" in line):
            branch = m.group(1)
            continue
        v = re.match(r'\s*- "([a-z0-9.,_]+)"\s*$', line)
        if v and branch in deployed and "." in v.group(1):
            consumed.update(q.strip() for q in v.group(1).split(","))
    return consumed


@pytest.mark.source_only  # lê o chart; a imagem compilada não leva kubernetes/
def test_helm_routed_queues_are_consumed():
    from test_compose_celery_queues_consumed import _collect_queues_used

    used = _collect_queues_used()
    consumed = _helm_queues_consumed()
    assert {"collect.priority", "collect.query", "maintenance"} <= consumed, consumed
    orphan = used - consumed
    assert not orphan, f"filas roteadas sem worker no chart Helm: {sorted(orphan)}"


@pytest.mark.source_only
def test_helm_ee_registers_ee_tasks():
    """O worker sobe pelo ``cops-celery`` da imagem (que a imagem EE reescreve
    para o app EE) — nunca com o app do Core fixo no template."""
    yaml = pytest.importorskip("yaml")
    tpl = (CHART / "templates" / "workers-deployment.yaml").read_text(encoding="utf-8")
    values = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    dockerfile = (REPO_ROOT / "compose" / "Dockerfile").read_text(encoding="utf-8")

    assert values["workers"]["celeryApp"] == ""
    assert '"/usr/local/bin/cops-celery"' in tpl
    command_block = tpl.split("command:", 1)[1].split("envFrom:", 1)[0]
    assert '"app.collectors.celery_app"' not in command_block
    assert "/usr/local/bin/cops-celery" in dockerfile


@pytest.mark.source_only
def test_helm_workers_drain_at_least_task_time_limit():
    yaml = pytest.importorskip("yaml")
    from backend.app.collectors import celery_app

    values = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    limit = int(celery_app.celery_app.conf.task_time_limit)
    assert int(values["workers"]["terminationGracePeriodSeconds"]) > limit


# ── Compose: o MESMO data-plane em todos os serviços da app ─────────────────

_KAFKA_VARS = (
    # EVENT_DATAPLANE saiu na Onda 5: o Kafka é o único data-plane.
    "KAFKA_BOOTSTRAP_SERVERS",
    "KAFKA_SECURITY_PROTOCOL",
    "KAFKA_SASL_MECHANISM",
    "KAFKA_SASL_USERNAME",
    "KAFKA_SASL_PASSWORD",
    "KAFKA_SSL_CAFILE",
    "KAFKA_DELIVER_REPLICATION",
    # ADR-0018 Onda 2 — quem cria o tópico raw primeiro (API, syslog ou processor)
    # tem de usar os mesmos valores.
    "KAFKA_RAW_PARTITIONS",
    "KAFKA_RAW_RETENTION_MS",
    "KAFKA_RAW_SHARDS_PER_STREAM",
)


def test_compose_kafka_parity():
    yaml = pytest.importorskip("yaml")
    compose = REPO_ROOT / "compose" / "docker-compose.yml"
    if not compose.exists():  # pragma: no cover — imagem sem o compose montado
        pytest.skip("compose/docker-compose.yml ausente")
    services = yaml.safe_load(compose.read_text(encoding="utf-8"))["services"]
    app = {
        name: svc
        for name, svc in services.items()
        if "${IMAGE_NAME:-centralops}:" in str(svc.get("image", "")) and name != "frontend"
    }
    assert len(app) >= 8, sorted(app)

    def env_of(svc):
        out = {}
        for item in svc.get("environment") or []:
            k, _, v = str(item).partition("=")
            out[k] = v
        return out

    for var in _KAFKA_VARS:
        values = {name: env_of(svc).get(var) for name, svc in app.items()}
        missing = sorted(n for n, v in values.items() if v is None)
        assert not missing, f"{var} ausente em: {missing}"
        assert len(set(values.values())) == 1, f"{var} diverge entre serviços: {values}"


# ── Regras de alerta: métricas e runbooks que existem ───────────────────────


@pytest.mark.source_only  # lê compose/observability e docs-site
def test_alert_rules_reference_real_metrics_and_runbooks():
    yaml = pytest.importorskip("yaml")
    from backend.app.collectors import otel_metrics

    rules_file = REPO_ROOT / "compose" / "observability" / "alerts" / "centralops-dataplane.rules.yml"
    groups = yaml.safe_load(rules_file.read_text(encoding="utf-8"))["groups"]
    rules = [r for g in groups for r in g["rules"]]
    assert len(rules) >= 8

    known = set(otel_metrics._SPEC)
    for rule in rules:
        names = set(re.findall(r"\bcollector_[a-z0-9_]+", rule["expr"]))
        assert names, rule["alert"]
        unknown = names - known
        assert not unknown, f"{rule['alert']}: métrica inexistente {sorted(unknown)}"
        runbook = rule["annotations"].get("runbook")
        assert runbook and (REPO_ROOT / runbook).exists(), f"{rule['alert']}: runbook {runbook}"

    prom = (REPO_ROOT / "compose" / "observability" / "prometheus.yml").read_text(encoding="utf-8")
    assert "/etc/prometheus/rules/*.yml" in prom


# ── PrometheusRule e PgBouncer (Helm) ────────────────────────────────────────


@pytest.mark.source_only
def test_chart_alert_rules_match_compose_rules():
    """O PrometheusRule do chart e o Prometheus do compose avaliam as MESMAS
    regras: o Helm só lê arquivos do próprio chart, então há uma cópia."""
    compose = REPO_ROOT / "compose" / "observability" / "alerts" / "centralops-dataplane.rules.yml"
    chart = CHART / "files" / "prometheus" / "centralops-dataplane.rules.yml"
    assert chart.read_bytes() == compose.read_bytes()


@pytest.mark.source_only  # executa os entrypoints do repositório
@pytest.mark.parametrize("script", ["start-api.sh", "start-collector.sh"])
@pytest.mark.parametrize("migration_url", ["", "postgresql+psycopg2://u:p@direct:5432/db"])
def test_migration_uses_direct_url_when_set(tmp_path, script, migration_url):
    """Com PgBouncer, a migração (advisory lock de SESSÃO) vai direto ao
    Postgres; sem DATABASE_MIGRATION_URL, nada muda."""
    import shutil
    import subprocess

    if shutil.which("sh") is None:  # pragma: no cover
        pytest.skip("sem /bin/sh")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    record = tmp_path / "migrate-url.txt"
    fake = bindir / "python"
    # Registra a DATABASE_URL vista pela migração; ``exec "$@"`` cai no ``true``.
    fake.write_text(f'#!/bin/sh\nprintf "%s" "$DATABASE_URL" > "{record}"\nexit 0\n')
    fake.chmod(0o755)
    for helper in ("nginx", "uvicorn", "gunicorn"):
        stub = bindir / helper
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)

    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "APP_MASTER_KEY": "k" * 40,
        "APP_ENV": "production",
        "DATABASE_URL": "postgresql+psycopg2://u:p@pgbouncer:5432/db",
    }
    if migration_url:
        env["DATABASE_MIGRATION_URL"] = migration_url
    subprocess.run(
        ["sh", str(REPO_ROOT / "scripts" / script), "true"],
        env=env, capture_output=True, timeout=30,
    )
    assert record.read_text() == (migration_url or env["DATABASE_URL"])


def test_compose_pgbouncer_is_opt_in_and_migration_url_reaches_every_service():
    """PgBouncer do compose só sobe com o profile; DATABASE_MIGRATION_URL chega a
    TODOS os serviços da app (os entrypoints migram por ela quando definida)."""
    yaml = pytest.importorskip("yaml")
    compose = REPO_ROOT / "compose" / "docker-compose.yml"
    if not compose.exists():  # pragma: no cover
        pytest.skip("compose/docker-compose.yml ausente")
    services = yaml.safe_load(compose.read_text(encoding="utf-8"))["services"]

    pgb = services["pgbouncer"]
    assert pgb.get("profiles") == ["pgbouncer"]
    env = dict(str(e).partition("=")[::2] for e in pgb["environment"])
    assert env["POOL_MODE"] == "transaction"
    assert env["UPSTREAM_DATABASE_URL"].startswith("${DATABASE_MIGRATION_URL:-")

    app = [
        name for name, svc in services.items()
        if "${IMAGE_NAME:-centralops}:" in str(svc.get("image", "")) and name != "frontend"
    ]
    missing = [
        name for name in app
        if not any(str(e).startswith("DATABASE_MIGRATION_URL=") for e in services[name]["environment"])
    ]
    assert not missing, f"DATABASE_MIGRATION_URL ausente em: {missing}"
