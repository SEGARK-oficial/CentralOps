"""AWS CloudWatch Logs collector — FilterLogEvents.

Cobre: coleta + enriquecimento + cursor, paginação por ``nextToken``,
``extract_message_id`` (eventId), TETO por ciclo (cursor resumível, watermark
NÃO avança) e o registro zero-core. ``aioboto3`` NÃO é importado — o client é
mockado no seam ``_client`` (modelo do ``test_aws_cloudtrail``).
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from ..base import CollectorContext
from ..normalize import engine as E
from ..normalize.defaults import load_default_rules
from ..vendors import aws_cloudwatch as cw
from ..vendors.aws_cloudwatch import AWSCloudWatchCollector

_CONN = {
    "access_key_id": "AKIA_TEST",
    "secret": "secret-test",
    "log_group": "/aws/lambda/minha-funcao",
    "region": "us-east-1",
    "account_id": "888",
}

# 2026-06-21T10:00:00Z em ms epoch.
_TS = 1782036000000


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

    async def backoff(self, vendor, retry_after):
        return None


def _ctx(cursor: Dict[str, Any] | None = None, bounded: bool = True) -> CollectorContext:
    return CollectorContext(
        integration_id=99,
        organization_id=5,
        platform="aws_cloudwatch",
        headers={},
        session=MagicMock(),
        cursor=cursor,
        domain_limiter=_NoopDomainLimiter(),
        rate_limiter=_NoopRateLimiter(),
        redis=MagicMock(),
        bounded_per_cycle=bounded,
    )


def _event(eid: str, message: str = "hello", ts: int = _TS) -> Dict[str, Any]:
    """Shape do FilteredLogEvent (API_FilteredLogEvent)."""
    return {
        "eventId": eid,
        "timestamp": ts,
        "ingestionTime": ts + 500,
        "message": message,
        "logStreamName": "2026/06/21/[$LATEST]abc123",
    }


class _FakeLogs:
    """Mock do client aioboto3 ``logs``: async CM + ``filter_log_events``.

    ``pages`` é a lista de respostas a devolver em ordem; cada chamada consome
    uma. Grava os kwargs recebidos para asserção de paginação/janela.
    """

    def __init__(self, pages: List[Dict[str, Any]]) -> None:
        self._pages = list(pages)
        self.calls: List[Dict[str, Any]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def filter_log_events(self, **kw):
        self.calls.append(kw)
        if not self._pages:
            return {"events": []}
        return self._pages.pop(0)


def _patched(fake: _FakeLogs):
    return (
        patch.object(AWSCloudWatchCollector, "_load_conn", return_value=dict(_CONN)),
        patch.object(AWSCloudWatchCollector, "_client", return_value=fake),
    )


async def _run(ctx: CollectorContext, fake: _FakeLogs) -> List[Dict[str, Any]]:
    load_conn, client = _patched(fake)
    with load_conn, client:
        return [ev async for ev in AWSCloudWatchCollector(ctx).collect()]


@pytest.mark.asyncio
async def test_collects_enriches_and_advances_cursor() -> None:
    fake = _FakeLogs([{"events": [_event("ev-1"), _event("ev-2", message="bye")]}])
    ctx = _ctx(cursor=None)

    collected = await _run(ctx, fake)

    assert [e["eventId"] for e in collected] == ["ev-1", "ev-2"]
    # Enriquecimento: a resposta da API não traz log group / região / conta / ISO.
    assert collected[0]["logGroupName"] == "/aws/lambda/minha-funcao"
    assert collected[0]["awsRegion"] == "us-east-1"
    assert collected[0]["awsAccountId"] == "888"
    assert collected[0]["eventTime"].endswith("Z")

    # Janela fechada e explícita na chamada.
    call = fake.calls[0]
    assert call["logGroupName"] == "/aws/lambda/minha-funcao"
    assert call["startFromHead"] is True
    assert call["startTime"] < call["endTime"]
    assert "nextToken" not in call

    # Drenou tudo → watermark avança para endTime+1 e o token some.
    assert ctx.cursor["start_time_ms"] == call["endTime"] + 1
    assert "next_token" not in ctx.cursor

    assert AWSCloudWatchCollector(ctx).domain == "logs.amazonaws.com"


@pytest.mark.asyncio
async def test_paginates_until_next_token_absent() -> None:
    fake = _FakeLogs(
        [
            {"events": [_event("ev-1")], "nextToken": "tok-2"},
            # Página VAZIA com nextToken: a doc é explícita que isso NÃO é fim.
            {"events": [], "nextToken": "tok-3"},
            {"events": [_event("ev-3")]},
        ]
    )
    ctx = _ctx(cursor=None)

    collected = await _run(ctx, fake)

    assert [e["eventId"] for e in collected] == ["ev-1", "ev-3"]
    assert len(fake.calls) == 3
    assert fake.calls[1]["nextToken"] == "tok-2"
    assert fake.calls[2]["nextToken"] == "tok-3"
    # Janela IDÊNTICA em todas as páginas (o token é colado à janela).
    assert {c["startTime"] for c in fake.calls} == {fake.calls[0]["startTime"]}
    assert {c["endTime"] for c in fake.calls} == {fake.calls[0]["endTime"]}


@pytest.mark.asyncio
async def test_per_cycle_cap_saves_resumable_cursor_without_advancing_watermark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cw, "_MAX_PAGES_PER_CYCLE", 2)
    fake = _FakeLogs(
        [
            {"events": [_event("ev-1")], "nextToken": "tok-2"},
            {"events": [_event("ev-2")], "nextToken": "tok-3"},
            {"events": [_event("ev-3")]},  # não deve ser alcançada neste ciclo
        ]
    )
    ctx = _ctx(cursor=None)

    collected = await _run(ctx, fake)

    # Parou no teto: 2 páginas, 2 eventos.
    assert [e["eventId"] for e in collected] == ["ev-1", "ev-2"]
    assert len(fake.calls) == 2

    # Cursor RESUMÍVEL: token da PRÓXIMA página + a janela que o originou.
    assert ctx.cursor["next_token"] == "tok-3"
    assert ctx.cursor["start_time_ms"] == fake.calls[0]["startTime"]
    assert ctx.cursor["end_time_ms"] == fake.calls[0]["endTime"]
    # ARMADILHA: o watermark NÃO pode avançar no cap-hit (descartaria tok-3).
    assert ctx.cursor["start_time_ms"] != fake.calls[0]["endTime"] + 1


@pytest.mark.asyncio
async def test_resumes_from_saved_token_reusing_same_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cw, "_MAX_PAGES_PER_CYCLE", 2)
    fake = _FakeLogs([{"events": [_event("ev-3")]}])
    ctx = _ctx(cursor={"start_time_ms": 1000, "end_time_ms": 9000, "next_token": "tok-3"})

    collected = await _run(ctx, fake)

    assert [e["eventId"] for e in collected] == ["ev-3"]
    call = fake.calls[0]
    # Retoma exatamente a janela persistida — sem overlap, sem janela nova.
    assert (call["startTime"], call["endTime"], call["nextToken"]) == (1000, 9000, "tok-3")
    # Agora sim drenou → watermark avança e o token some.
    assert ctx.cursor == {"start_time_ms": 9001}


@pytest.mark.asyncio
async def test_backfill_ignores_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """``bounded_per_cycle=False`` (backfill one-shot) drena a janela inteira."""
    monkeypatch.setattr(cw, "_MAX_PAGES_PER_CYCLE", 1)
    fake = _FakeLogs(
        [
            {"events": [_event("ev-1")], "nextToken": "t2"},
            {"events": [_event("ev-2")], "nextToken": "t3"},
            {"events": [_event("ev-3")]},
        ]
    )
    ctx = _ctx(cursor=None, bounded=False)

    collected = await _run(ctx, fake)

    assert [e["eventId"] for e in collected] == ["ev-1", "ev-2", "ev-3"]
    assert "next_token" not in ctx.cursor


@pytest.mark.asyncio
async def test_extract_message_id_uses_event_id() -> None:
    collector = AWSCloudWatchCollector(_ctx())
    assert collector.extract_message_id(_event("31132629274945519779805")) == (
        "31132629274945519779805"
    )
    assert collector.extract_message_id({}) == ""


def test_normalizes_json_message_to_ocsf() -> None:
    raw = cw._enrich(
        _event(
            "ev-1",
            message=(
                '{"level":"ERROR","eventName":"PutItem","user":"mary",'
                '"sourceIPAddress":"192.0.2.10","errorCode":"Throttled"}'
            ),
        ),
        dict(_CONN),
    )
    norm = E.apply_compiled(
        E.compile_rules(load_default_rules("aws_cloudwatch", "aws_cloudwatch.event")), raw
    ).output["normalized"]

    # CloudWatch Logs é TRANSPORTE (payload heterogêneo: VPC Flow, stdout de Lambda,
    # log de aplicação) — o OCSF não tem classe de "log genérico", então o fallback
    # oficial é Base Event (0). Carimbar tudo como 6003 API Activity seria falso.
    assert norm["class_uid"] == 0
    assert norm["category_uid"] == 0
    assert norm["type_uid"] == 0
    assert norm["metadata"]["uid"] == "ev-1"
    assert norm["metadata"]["log_name"] == "/aws/lambda/minha-funcao"
    assert norm["cloud"]["region"] == "us-east-1"
    assert norm["cloud"]["account"]["uid"] == "888"
    # extração oportunista do payload vai p/ ``unmapped`` (uso sancionado pelo OCSF
    # para dado vendor-specific), não p/ objetos de uma classe que não é a nossa.
    assert norm["unmapped"]["operation"] == "PutItem"
    assert norm["unmapped"]["user_name"] == "mary"
    assert norm["unmapped"]["src_ip"] == "192.0.2.10"
    assert norm["severity_id"] == 4  # "ERROR" → pre_cast lowercase → value_map
    # timestamp_t do OCSF é em MILISSEGUNDOS — o CloudWatch já entrega ms
    # e ``_enrich`` gera o ``eventTime`` ISO com precisão de ms, então o
    # round-trip devolve exatamente o ``_TS`` original.
    assert norm["time"] == _TS


def test_normalizes_plain_text_message_without_quarantine() -> None:
    """Log não-JSON: ``json_parse`` tolerante → ``_msg`` None → defaults."""
    raw = cw._enrich(_event("ev-2", message="START RequestId: abc"), dict(_CONN))
    norm = E.apply_compiled(
        E.compile_rules(load_default_rules("aws_cloudwatch", "aws_cloudwatch.event")), raw
    ).output["normalized"]

    assert norm["message"] == "START RequestId: abc"
    # GOTCHA do engine: ``default`` NÃO passa por pre_cast/value_map — o 1 é literal.
    assert norm["severity_id"] == 1
    assert norm["unmapped"]["operation"] is None


def test_registered_zero_core_exotic_creds() -> None:
    from ..capabilities import invalid_capabilities
    from ..registry import get, get_platform, has

    assert has("aws_cloudwatch", "events")
    plat = get_platform("aws_cloudwatch")
    assert plat is not None and plat.test_fn is not None
    assert invalid_capabilities(plat.capabilities) == []
    assert "secret_access_key" in {f.key for f in plat.auth_fields if f.type == "secret"}
    keys = {f.key for f in plat.auth_fields}
    # Reuso GENÉRICO de colunas — zero coluna nova.
    assert {"client_id", "base_url", "region", "tenant_id"} <= keys
    reg = get("aws_cloudwatch", "events")
    assert reg.refresh_fn.__name__ == "_cloudwatch_refresher"
    assert reg.queue == "collect.bulk"


def test_ocsf_class_uid_is_in_core_allowlist() -> None:
    """O mapping default só pode emitir class_uid já conhecido pelo core
    (``ocsf/classes.py``) — emitir uma classe nova exigiria editar core."""
    from ..normalize.ocsf.classes import is_valid_class_uid

    rules = load_default_rules("aws_cloudwatch", "aws_cloudwatch.event")["rules"]
    class_uid = next(r for r in rules if r["target"] == "normalized.class_uid")["const"]
    assert is_valid_class_uid(class_uid)


@pytest.mark.asyncio
async def test_probe_fails_fast_without_creds() -> None:
    result = await cw._cloudwatch_probe({"base_url": "", "client_id": "", "secret_access_key": ""})
    assert result.ok is False
