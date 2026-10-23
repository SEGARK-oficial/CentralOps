"""Google Cloud via Pub/Sub — coletor, credencial, teste de conexão e mappings.

O que mais importa aqui é QUANDO o ack acontece: só em ``on_cycle_committed``
(cursor gravado). Um ack dentro de ``collect()`` apagaria da fila um evento que o
ciclo ainda podia perder. Os testes afirmam a ordem pelas chamadas HTTP que o
mock registra, nunca pelo texto do fonte.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import aiohttp
import pytest
from yarl import URL

from . import _gcp_fixtures as F
from ._aiohttp_mock import aioresponses
from ..base import CollectorContext
from ..normalize import engine as E
from ..normalize.defaults import load_default_rules
from ..normalize.ocsf import validator as V
from ..registry import get, get_platform, supported_streams
from ..vendors import gcp_pubsub as G

_SUB = f"https://pubsub.googleapis.com/v1/projects/{F.PROJECT}/subscriptions/centralops-audit"
_PULL = f"{_SUB}:pull"
_LEASE = f"{_SUB}:modifyAckDeadline"
_ACK = f"{_SUB}:acknowledge"


class _NoopDomainLimiter:
    def slot(self, domain):
        class _Ctx:
            async def __aenter__(self_inner):
                return None

            async def __aexit__(self_inner, *a):
                return False

        return _Ctx()


class _NoopRateLimiter:
    async def acquire(self, tenant_id, vendor):
        return None


def _ctx(session, cursor: Optional[Dict[str, Any]] = None, *, bounded: bool = True) -> CollectorContext:
    return CollectorContext(
        integration_id=77,
        organization_id=3,
        platform="gcp_pubsub",
        headers={"Authorization": "Bearer ya29.teste"},
        session=session,
        cursor=cursor,
        domain_limiter=_NoopDomainLimiter(),
        rate_limiter=_NoopRateLimiter(),
        redis=MagicMock(),
        bounded_per_cycle=bounded,
    )


@pytest.fixture(autouse=True)
def _target(monkeypatch):
    """O alvo (projeto + prefixo) viria do banco; aqui é fixo."""
    monkeypatch.setattr(
        G.GcpPubSubAuditCollector, "_load_target", lambda self: (F.PROJECT, "centralops")
    )


def _calls(m, method: str, url: str) -> List[Dict[str, Any]]:
    return [c.kwargs.get("json") for c in m.requests.get((method, URL(url)), [])]


async def _collect(collector) -> List[Dict[str, Any]]:
    return [ev async for ev in collector.collect()]


# ── Entrega: ack só depois do commit ─────────────────────────────────────


@pytest.mark.asyncio
async def test_ack_happens_only_after_the_cycle_is_committed() -> None:
    page = [
        F.pubsub_message(F.SET_IAM_POLICY, ack_id="a1", published="2026-10-23T12:00:02Z"),
        F.pubsub_message(F.DENIED_SA_KEY, ack_id="a2", published="2026-10-23T12:05:02Z"),
    ]
    with aioresponses() as m:
        m.post(_PULL, payload={"receivedMessages": page})
        m.post(_PULL, exception=aiohttp.SocketTimeoutError())  # fila vazia
        m.post(_LEASE, payload={})
        m.post(_ACK, payload={})
        async with aiohttp.ClientSession() as session:
            collector = G.GcpPubSubAuditCollector(_ctx(session))
            events = await _collect(collector)
            # Fim do collect(): eventos entregues ao pipeline, NADA confirmado.
            assert _calls(m, "POST", _ACK) == []
        await collector.on_cycle_committed()

    assert [e["insertId"] for e in events] == ["-a1b2c3d4e5", "-f6g7h8"]
    assert _calls(m, "POST", _LEASE) == [{"ackIds": ["a1", "a2"], "ackDeadlineSeconds": 600}]
    assert _calls(m, "POST", _ACK) == [{"ackIds": ["a1", "a2"]}]


@pytest.mark.asyncio
async def test_aborted_cycle_hands_the_messages_back_without_ack() -> None:
    page = [F.pubsub_message(F.SET_IAM_POLICY, ack_id="a1", published="2026-10-23T12:00:02Z")]
    with aioresponses() as m:
        m.post(_PULL, payload={"receivedMessages": page})
        m.post(_PULL, exception=aiohttp.SocketTimeoutError())
        m.post(_LEASE, payload={})  # o prazo esticado no pull
        m.post(_LEASE, payload={})  # o nack (prazo 0) no abort
        async with aiohttp.ClientSession() as session:
            collector = G.GcpPubSubAuditCollector(_ctx(session))
            await _collect(collector)
        await collector.on_cycle_aborted()

    assert _calls(m, "POST", _ACK) == [], "ciclo com erro não pode confirmar nada"
    assert _calls(m, "POST", _LEASE)[-1] == {"ackIds": ["a1"], "ackDeadlineSeconds": 0}


@pytest.mark.asyncio
async def test_settle_is_a_noop_when_nothing_was_read() -> None:
    with aioresponses() as m:
        m.post(_PULL, exception=aiohttp.SocketTimeoutError())
        async with aiohttp.ClientSession() as session:
            collector = G.GcpPubSubAuditCollector(_ctx(session))
            assert await _collect(collector) == []
        await collector.on_cycle_committed()
        await collector.on_cycle_aborted()
    assert _calls(m, "POST", _ACK) == [] and _calls(m, "POST", _LEASE) == []


@pytest.mark.asyncio
async def test_failed_ack_is_reported_not_swallowed() -> None:
    """O pipeline loga a falha; aqui ela tem de chegar até ele."""
    page = [F.pubsub_message(F.SET_IAM_POLICY, ack_id="a1", published="2026-10-23T12:00:02Z")]
    with aioresponses() as m:
        m.post(_PULL, payload={"receivedMessages": page})
        m.post(_PULL, exception=aiohttp.SocketTimeoutError())
        m.post(_LEASE, payload={})
        m.post(_ACK, status=503, payload={"error": {"message": "unavailable"}})
        async with aiohttp.ClientSession() as session:
            collector = G.GcpPubSubAuditCollector(_ctx(session))
            await _collect(collector)
        with pytest.raises(RuntimeError, match="1 de 1"):
            await collector.on_cycle_committed()


# ── Cursor, teto e streams desligados ────────────────────────────────────


@pytest.mark.asyncio
async def test_drained_queue_moves_the_watermark_to_now() -> None:
    """Fila vazia = em dia. Watermark parado no último evento faria um stream
    quieto parecer atrasado para sempre."""
    before = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    with aioresponses() as m:
        m.post(_PULL, exception=aiohttp.SocketTimeoutError())
        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session, cursor={"watermark": "2026-10-01T00:00:00Z"})
            await _collect(G.GcpPubSubAuditCollector(ctx))
    wm = G.GcpPubSubAuditCollector.watermark_at(ctx.cursor)
    assert wm is not None and wm >= before
    assert ctx.hit_cycle_cap is False, "fila vazia não é backlog"


@pytest.mark.asyncio
async def test_cap_stops_the_cycle_and_signals_backlog(monkeypatch) -> None:
    monkeypatch.setattr(G, "_MAX_PULLS_PER_CYCLE", 2)
    pages = [
        [F.pubsub_message(F.SET_IAM_POLICY, ack_id=f"a{i}", published=f"2026-10-23T12:0{i}:00Z")]
        for i in range(3)
    ]
    with aioresponses() as m:
        for page in pages:
            m.post(_PULL, payload={"receivedMessages": page})
        m.post(_LEASE, payload={}, repeat=True)
        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session)
            collector = G.GcpPubSubAuditCollector(ctx)
            events = await _collect(collector)

    assert len(events) == 2, "parou no teto"
    assert ctx.hit_cycle_cap is True
    # Watermark = publishTime mais novo LIDO, não "agora": sobrou fila.
    assert G.GcpPubSubAuditCollector.watermark_at(ctx.cursor) == datetime(2026, 10, 23, 12, 1)
    assert collector._ack_ids == ["a0", "a1"], "só o que foi lido entra no ack"


@pytest.mark.asyncio
async def test_unbounded_run_drains_past_the_cap(monkeypatch) -> None:
    monkeypatch.setattr(G, "_MAX_PULLS_PER_CYCLE", 1)
    with aioresponses() as m:
        for i in range(2):
            m.post(_PULL, payload={"receivedMessages": [
                F.pubsub_message(F.SET_IAM_POLICY, ack_id=f"a{i}", published="2026-10-23T12:00:00Z")
            ]})
        m.post(_PULL, exception=aiohttp.SocketTimeoutError())
        m.post(_LEASE, payload={}, repeat=True)
        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session, bounded=False)
            events = await _collect(G.GcpPubSubAuditCollector(ctx))
    assert len(events) == 2 and ctx.hit_cycle_cap is False


@pytest.mark.asyncio
async def test_missing_subscription_turns_the_stream_off() -> None:
    with aioresponses() as m:
        m.post(_PULL, status=404, payload={"error": {"message": "Resource not found"}})
        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session, cursor=None)
            assert await _collect(G.GcpPubSubAuditCollector(ctx)) == []
    assert ctx.cursor is None, "stream desligado não inventa watermark"


@pytest.mark.asyncio
async def test_backfill_window_never_touches_the_queue() -> None:
    with aioresponses() as m:
        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session, cursor={"backfill_from_ts": "2026-10-01T00:00:00", "backfill_to_ts": "x"})
            assert await _collect(G.GcpPubSubAuditCollector(ctx)) == []
    assert m.requests == {}, "backfill consumiria a fila do ciclo normal sem ack"


@pytest.mark.asyncio
async def test_connection_timeout_is_an_error_not_an_empty_queue() -> None:
    """Google inalcançável não pode virar ciclo vazio 'verde' com watermark = agora."""
    with aioresponses() as m:
        m.post(_PULL, exception=aiohttp.ConnectionTimeoutError())
        async with aiohttp.ClientSession() as session:
            ctx = _ctx(session, cursor={"watermark": "2026-10-01T00:00:00Z"})
            with pytest.raises(aiohttp.ConnectionTimeoutError):
                await _collect(G.GcpPubSubAuditCollector(ctx))
    assert ctx.cursor == {"watermark": "2026-10-01T00:00:00Z"}


@pytest.mark.asyncio
async def test_unauthorized_surfaces_as_401_for_the_pipeline() -> None:
    with aioresponses() as m:
        m.post(_PULL, status=401, payload={"error": {"message": "invalid token"}})
        async with aiohttp.ClientSession() as session:
            with pytest.raises(aiohttp.ClientResponseError) as exc:
                await _collect(G.GcpPubSubAuditCollector(_ctx(session)))
    assert exc.value.status == 401


@pytest.mark.asyncio
async def test_rate_limited_pull_raises_the_vendor_429() -> None:
    with aioresponses() as m:
        m.post(_PULL, status=429, headers={"Retry-After": "12"}, payload={})
        async with aiohttp.ClientSession() as session:
            with pytest.raises(G.GcpPubSubRateLimitedError) as exc:
                await _collect(G.GcpPubSubAuditCollector(_ctx(session)))
    assert exc.value.retry_after == 12


@pytest.mark.asyncio
async def test_each_stream_reads_its_own_subscription() -> None:
    sub = f"https://pubsub.googleapis.com/v1/projects/{F.PROJECT}/subscriptions/centralops-gke-audit:pull"
    with aioresponses() as m:
        m.post(sub, exception=aiohttp.SocketTimeoutError())
        async with aiohttp.ClientSession() as session:
            await _collect(G.GcpPubSubGkeAuditCollector(_ctx(session)))
    assert list(m.requests) == [("POST", URL(sub))]


# ── Mensagem → evento e dedupe ───────────────────────────────────────────


def test_non_json_payload_is_kept_as_text() -> None:
    import base64

    msg = {"data": base64.b64encode(b"linha solta").decode(), "messageId": "m"}
    assert G._decode_log_entry(msg) == {"textPayload": "linha solta"}
    assert G._decode_log_entry({"data": "@@não-base64@@"}) is None
    assert G._decode_log_entry({}) is None


def test_message_id_scopes_insert_id_by_log_and_instant() -> None:
    c = G.GcpPubSubAuditCollector(MagicMock())
    assert c.extract_message_id(F.SET_IAM_POLICY) == (
        f"projects/{F.PROJECT}/logs/cloudaudit.googleapis.com%2Factivity"
        "|2026-10-23T12:00:00.123456789Z|-a1b2c3d4e5"
    )
    assert c.extract_message_id({"textPayload": "x"}) == "", "sem insertId → hash do pipeline"


@pytest.mark.parametrize(
    "project,prefix",
    [("projeto/../outro", "centralops"), ("ab", "centralops"), (F.PROJECT, "pre/fixo"), (F.PROJECT, "")],
)
def test_target_names_cannot_escape_the_subscription_path(project, prefix) -> None:
    with pytest.raises(ValueError):
        G._validated_target(project, prefix)


# ── Credencial ───────────────────────────────────────────────────────────

_SA = {
    "type": "service_account",
    "client_email": "coletor@projeto-exemplo.iam.gserviceaccount.com",
    # Só precisa ser não-vazia: o parse nunca roda (o mint é mockado). Um PEM aqui,
    # mesmo falso, reprova o openness-gate (detector de chave privada commitada).
    "private_key": "chave-falsa-de-teste",
    "token_uri": "https://oauth2.googleapis.com/token",
}
_WIF_AZURE = {
    "type": "external_account",
    "audience": "//iam.googleapis.com/projects/1/locations/global/workloadIdentityPools/p/providers/az",
    "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
    "token_url": "https://sts.googleapis.com/v1/token",
    "service_account_impersonation_url": (
        "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
        "coletor@projeto-exemplo.iam.gserviceaccount.com:generateAccessToken"
    ),
    "credential_source": {
        "url": "http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01",
        "headers": {"Metadata": "True"},
        "format": {"type": "json", "subject_token_field_name": "access_token"},
    },
}


def test_accepts_service_account_and_cloud_wif() -> None:
    assert G.parse_credential_info(json.dumps(_SA))["type"] == "service_account"
    assert G.parse_credential_info(json.dumps(_WIF_AZURE))["type"] == "external_account"


@pytest.mark.parametrize(
    "info,motivo",
    [
        ("{nao é json", "JSON válido"),
        ({**_SA, "token_uri": "https://coletor.example.org/token"}, "token_uri"),
        ({**_SA, "private_key": ""}, "private_key"),
        ({"type": "authorized_user", "refresh_token": "x"}, "não suportado"),
        ({**_WIF_AZURE, "token_url": "https://example.org/token"}, "token_url"),
        ({**_WIF_AZURE, "credential_source": {"file": "/etc/passwd"}}, "arquivo/executável"),
        ({**_WIF_AZURE, "credential_source": {"executable": {"command": "id"}}}, "arquivo/executável"),
        ({**_WIF_AZURE, "credential_source": {"url": "http://10.0.0.8/segredo"}}, "metadata"),
        ({**_WIF_AZURE, "service_account_impersonation_url": "https://example.org/x"}, "fora do Google"),
    ],
)
def test_rejects_credentials_that_point_outside_google(info, motivo) -> None:
    text = info if isinstance(info, str) else json.dumps(info)
    with pytest.raises(G.GcpCredentialError, match=motivo):
        G.parse_credential_info(text)


@pytest.mark.parametrize(
    "url,metadata,ok",
    [
        ("https://oauth2.googleapis.com/token", False, True),
        ("https://sts.googleapis.com/v1/token", False, True),
        ("http://sts.googleapis.com/v1/token", False, False),  # Google só em HTTPS
        ("https://sts.googleapis.com.example.org/v1/token", False, False),
        ("http://169.254.169.254/latest/api/token", True, True),
        ("http://169.254.169.254/latest/api/token", False, False),
        ("http://[fd00:ec2::254]/latest/meta-data/", True, True),
        ("file:///etc/passwd", True, False),
        ("http://10.0.0.8/", True, False),
    ],
)
def test_auth_url_allowlist(url, metadata, ok) -> None:
    assert G._auth_url_allowed(url, allow_metadata=metadata) is ok


def test_guarded_transport_refuses_hosts_outside_the_allowlist() -> None:
    """A barreira vale no transporte também — não só na validação do JSON."""
    pytest.importorskip("google.auth")
    from google.auth import exceptions as ga_exceptions

    request = G._guarded_request()
    with pytest.raises(ga_exceptions.TransportError, match="não permitido"):
        request("https://coletor.example.org/token", method="POST", body=b"x")


def test_mint_token_uses_the_guarded_transport(monkeypatch) -> None:
    google_auth = pytest.importorskip("google.auth")
    seen: Dict[str, Any] = {}

    class _Creds:
        token = "ya29.novo"
        expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=30)

        def refresh(self, request):
            seen["request"] = request

    def _load(info, scopes=None):
        seen["scopes"] = scopes
        return _Creds(), None

    monkeypatch.setattr(google_auth, "load_credentials_from_dict", _load)
    token, expires_in = G._mint_token(_SA)

    assert token == "ya29.novo"
    assert 1700 < expires_in <= 1800
    assert seen["scopes"] == ["https://www.googleapis.com/auth/pubsub"]
    assert type(seen["request"]).__name__ == "_Request", "o token tem de passar pelo transporte guardado"


@pytest.mark.asyncio
async def test_refresher_returns_the_token_for_the_cache(monkeypatch) -> None:
    monkeypatch.setattr(G, "_load_credential_text", lambda integration_id: json.dumps(_SA))
    monkeypatch.setattr(G, "_mint_token", lambda info: ("ya29.cache", 3599))
    assert await G._gcp_refresher(77) == {"access_token": "ya29.cache", "expires_in": 3599}


@pytest.mark.asyncio
async def test_refresher_refuses_a_forged_credential_before_any_network(monkeypatch) -> None:
    forjada = {**_WIF_AZURE, "credential_source": {"file": "/run/secrets/kms.key"}}
    monkeypatch.setattr(G, "_load_credential_text", lambda integration_id: json.dumps(forjada))
    chamadas: List[Any] = []
    monkeypatch.setattr(G, "_mint_token", lambda info: chamadas.append(info))
    with pytest.raises(G.GcpCredentialError):
        await G._gcp_refresher(77)
    assert chamadas == []


# ── Teste de conexão ─────────────────────────────────────────────────────


def _tip(suffix: str) -> str:
    return (
        f"https://pubsub.googleapis.com/v1/projects/{F.PROJECT}/subscriptions/"
        f"centralops-{suffix}:testIamPermissions"
    )


@pytest.mark.asyncio
async def test_probe_reports_each_stream(monkeypatch) -> None:
    monkeypatch.setattr(G, "_mint_token", lambda info: ("ya29.probe", 3600))
    with aioresponses() as m:
        m.post(_tip("audit"), payload={"permissions": ["pubsub.subscriptions.consume"]})
        m.post(_tip("gke-audit"), status=404, payload={})
        m.post(_tip("cloudsql-mysql"), status=403, payload={})
        result = await G._gcp_pubsub_probe(
            {"tenant_id": F.PROJECT, "client_secret": json.dumps(_SA)}
        )
    assert result.ok is True
    assert "audit (centralops-audit): ok" in result.detail
    assert "gke_audit (centralops-gke-audit): não existe" in result.detail
    assert "cloudsql_mysql (centralops-cloudsql-mysql): sem permissão" in result.detail


@pytest.mark.asyncio
async def test_probe_fails_when_no_subscription_is_usable(monkeypatch) -> None:
    monkeypatch.setattr(G, "_mint_token", lambda info: ("ya29.probe", 3600))
    with aioresponses() as m:
        m.post(_tip("audit"), payload={"permissions": []})
        m.post(_tip("gke-audit"), status=404, payload={})
        m.post(_tip("cloudsql-mysql"), status=404, payload={})
        result = await G._gcp_pubsub_probe(
            {"tenant_id": F.PROJECT, "client_secret": json.dumps(_SA)}
        )
    assert result.ok is False
    assert "sem pubsub.subscriptions.consume" in result.detail


@pytest.mark.asyncio
async def test_probe_does_not_echo_the_auth_error_text(monkeypatch) -> None:
    def _boom(info):
        raise ValueError("resposta do endpoint com dado sensível")

    monkeypatch.setattr(G, "_mint_token", _boom)
    result = await G._gcp_pubsub_probe({"tenant_id": F.PROJECT, "client_secret": json.dumps(_SA)})
    assert result.ok is False
    assert "sensível" not in result.detail and "ValueError" in result.detail


@pytest.mark.asyncio
async def test_probe_validates_input_before_the_network() -> None:
    assert (await G._gcp_pubsub_probe({"tenant_id": "", "client_secret": "{}"})).ok is False
    bad = await G._gcp_pubsub_probe({"tenant_id": F.PROJECT, "client_secret": "{nao json"})
    assert bad.ok is False and "JSON" in bad.detail


# ── Registro (vendor novo = zero core) ───────────────────────────────────


def test_platform_and_streams_are_self_registered() -> None:
    plat = get_platform("gcp_pubsub")
    assert plat is not None and plat.category == "Cloud-audit" and plat.test_fn is not None
    assert {f.key for f in plat.auth_fields} == {"tenant_id", "base_url", "client_secret"}
    assert next(f for f in plat.auth_fields if f.key == "client_secret").type == "secret"
    assert supported_streams("gcp_pubsub") == ["audit", "cloudsql_mysql", "gke_audit"]
    assert {"collect:audit", "collect:gke_audit", "collect:cloudsql_mysql"} <= plat.capabilities
    for stream in ("audit", "gke_audit", "cloudsql_mysql"):
        reg = get("gcp_pubsub", stream)
        assert reg.refresh_fn is G._gcp_refresher
        assert reg.collector_cls.event_type == f"gcp.{stream}"


# ── Mappings ─────────────────────────────────────────────────────────────


def _normalize(event_type: str, raw: Dict[str, Any]) -> Dict[str, Any]:
    compiled = E.compile_rules(load_default_rules("gcp_pubsub", event_type))
    return E.apply_compiled(compiled, raw).output["normalized"]


def _assert_valid_ocsf(n: Dict[str, Any]) -> None:
    r = V.validate_normalized(n)
    assert r.valid, (r.reason, n)


def test_set_iam_policy_is_an_update_api_activity() -> None:
    n = _normalize("gcp.audit", F.SET_IAM_POLICY)
    _assert_valid_ocsf(n)
    assert (n["class_uid"], n["activity_id"], n["type_uid"]) == (6003, 3, 600303)
    assert n["status_id"] == 1 and n["status"] == "Success"
    assert n["actor"]["user"]["email_addr"] == "ana.silva@example.com"
    assert n["src_endpoint"]["ip"] == "203.0.113.10"
    assert n["api"]["operation"] == "SetIamPolicy"
    assert n["cloud"] == {
        "provider": "GCP", "project_uid": F.PROJECT, "account": {"uid": F.PROJECT}, "region": None,
    }
    # 9 casas de fração truncam para o milissegundo certo.
    assert n["time"] == 1792756800123
    assert n["resources"] == [{"name": f"projects/{F.PROJECT}", "type": "project"}]


def test_policy_denied_is_a_failure_and_private_ip_is_not_an_ip() -> None:
    n = _normalize("gcp.audit", F.DENIED_SA_KEY)
    _assert_valid_ocsf(n)
    assert (n["activity_id"], n["type_uid"]) == (1, 600301)  # CreateServiceAccountKey
    assert n["status_id"] == 2 and n["status_code"] == "7"
    assert n["severity_id"] == 3
    assert n["src_endpoint"]["ip"] is None, "'private' não é endereço IP"
    assert not any(o["type_id"] == 2 for o in n.get("observables") or [])


def test_workspace_login_failure_is_a_failed_logon() -> None:
    n = _normalize("gcp.audit", F.WORKSPACE_LOGIN_FAILURE)
    _assert_valid_ocsf(n)
    assert (n["class_uid"], n["category_uid"], n["activity_id"], n["type_uid"]) == (3002, 3, 1, 300201)
    assert n["status_id"] == 2 and n["status"] == "Failure"
    assert n["user"]["email_addr"] == "bruno.costa@example.com"


def test_kubectl_exec_on_gke() -> None:
    n = _normalize("gcp.gke_audit", F.GKE_EXEC)
    _assert_valid_ocsf(n)
    assert (n["class_uid"], n["activity_id"], n["type_uid"]) == (6003, 1, 600301)
    assert n["api"]["operation"] == "io.k8s.core.v1.pods.exec.create"
    assert n["resources"] == [{"name": "core/v1/namespaces/producao/pods/api-7d9f/exec", "type": "k8s_cluster"}]
    assert n["cloud"]["region"] == "southamerica-east1"
    assert n["metadata"]["product"]["name"] == "GKE Audit Logs"


def test_gke_service_account_is_not_an_email_observable() -> None:
    n = _normalize("gcp.gke_audit", F.GKE_SECRET_READ_BY_SA)
    _assert_valid_ocsf(n)
    assert (n["activity_id"], n["type_uid"]) == (2, 600302)
    assert n["actor"]["user"]["name"] == "system:serviceaccount:default:app"
    emails = [o for o in n.get("observables") or [] if o["type_id"] == 5]
    assert emails == []


def test_mysql_failed_login_is_a_failed_authentication() -> None:
    n = _normalize("gcp.cloudsql_mysql", F.MYSQL_LOGIN_FAILURE)
    _assert_valid_ocsf(n)
    assert (n["class_uid"], n["category_uid"], n["activity_id"], n["type_uid"]) == (3002, 3, 1, 300201)
    assert n["status_id"] == 2 and n["status_code"] == "1045"
    assert n["user"]["name"] == "root"
    assert n["src_endpoint"]["ip"] == "198.51.100.7"
    assert n["service"] == {"name": "Cloud SQL for MySQL", "uid": f"{F.PROJECT}:mysql-prod"}


def test_mysql_grant_is_a_datastore_update() -> None:
    n = _normalize("gcp.cloudsql_mysql", F.MYSQL_GRANT)
    _assert_valid_ocsf(n)
    assert (n["class_uid"], n["category_uid"], n["activity_id"], n["type_uid"]) == (6005, 6, 2, 600502)
    assert n["database"] == {"uid": f"{F.PROJECT}:mysql-prod", "name": "vendas"}
    assert n["table"] == {"name": "pedidos"}
    assert n["query_info"]["query_string"].startswith("GRANT SELECT")
    assert "user" not in n, "'user' é do Authentication; na 6005 quem age é o actor"


@pytest.mark.parametrize("cmd", [
    "select", "insert", "update", "delete", "truncate", "create_user", "drop_user", "alter_user",
    "grant", "revoke", "show_grants", "call_procedure", "rename_table", "load", "comando_novo",
])
def test_every_mysql_command_yields_a_coherent_type_uid(cmd) -> None:
    raw = json.loads(json.dumps(F.MYSQL_GRANT))
    raw["protoPayload"]["request"]["cmd"] = cmd
    n = _normalize("gcp.cloudsql_mysql", raw)
    _assert_valid_ocsf(n)
    assert n["type_uid"] == n["class_uid"] * 100 + n["activity_id"]
