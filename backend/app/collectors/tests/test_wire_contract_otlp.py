"""WIRE CONTRACT + behaviour for kind ``otlp``.

Three surfaces contracted (mirrors test_wire_contract_elastic_bulk.py):

1. ``format_otlp_log_record`` / ``format_otlp_request`` — estrutura exata do
   ``ExportLogsServiceRequest``: resourceLogs → scopeLogs → logRecords, com
   ``timeUnixNano`` como string numérica em nanossegundos, ``severityNumber``
   mapeado de OCSF severity_id, ``body.stringValue`` com JSON do envelope,
   ``attributes`` como lista de KeyValue.

2. O payload que ``send_batch`` POSTs: JSON compacto, ``Content-Type:
   application/json``, sem trailing newlines.  Capturado via sessão
   totalmente mockada — sem sockets.

3. O mapeamento de resposta OTLP → ``DeliveryResult``: 200/204 → aceito;
   ``partialSuccess.rejectedLogRecords > 0`` → fallback individual;
   401/403 → rejected de lote; 429/5xx → retryable.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

from backend.app.collectors.output.otlp_sender import (  # noqa: E402
    OtlpHttpClient,
    _SCOPE_NAME,
    _SCOPE_VERSION,
    format_otlp_log_record,
    format_otlp_request,
    _severity_number,
)
from backend.app.collectors.output.destinations.registry import (  # noqa: E402
    describe_all,
    all_kinds,
)


# ── Fixtures e helpers ───────────────────────────────────────────────────────


_CANONICAL = {
    "_centralops": {
        "vendor": "sophos",
        "integration_id": 1,
        "customer_id": 7,
        "stream": "alerts",
        "event_type": "sophos.alert",
        "event_id": "evt-abc123",
    },
    "normalized": {
        "class_uid": 2004,
        "severity_id": 4,  # high → SEVERITY_NUMBER_ERROR (17)
        "time": 1_718_640_000_000,  # ms → 1718640000000000000 ns
    },
    "raw": {"id": "evt-1"},
}

_CANONICAL_NS = 1_718_640_000_000 * 1_000_000  # 1718640000000000000


def _mock_response(status: int, body: Any = None) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=body or {})
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=None)
    return resp


def _mock_session(response: MagicMock) -> MagicMock:
    session = MagicMock()
    session.closed = False
    session.post = MagicMock(return_value=response)
    session.get = MagicMock(return_value=response)
    session.close = AsyncMock()
    return session


def _client(endpoint: str = "https://otel.test.local:4318/v1/logs") -> OtlpHttpClient:
    return OtlpHttpClient(endpoint=endpoint, verify_tls=False)


async def _capture_wire(client: OtlpHttpClient, batch: list) -> str:
    """Aciona send_batch com sessão mockada (200 ok) e captura data= do POST."""
    resp = _mock_response(200, {})
    session = _mock_session(resp)
    client._session = session
    result = await client.send_batch(batch)
    assert result.accepted == len(batch), f"esperado {len(batch)} aceitos, got {result}"
    return session.post.call_args.kwargs["data"].decode("utf-8")


# ═══════════════════════════════════════════════════════════════════════════
# PART 1 — format_otlp_log_record: estrutura e semântica (pure, sem I/O)
# ═══════════════════════════════════════════════════════════════════════════


def test_log_record_has_required_keys() -> None:
    """LogRecord deve ter timeUnixNano, severityNumber, severityText, body, attributes."""
    rec = format_otlp_log_record(_CANONICAL)
    assert "timeUnixNano" in rec
    assert "severityNumber" in rec
    assert "severityText" in rec
    assert "body" in rec
    assert "attributes" in rec


def test_time_unix_nano_from_normalized_time() -> None:
    """normalized.time em ms → timeUnixNano em ns como string numérica."""
    rec = format_otlp_log_record(_CANONICAL)
    assert rec["timeUnixNano"] == str(_CANONICAL_NS)
    # Deve ser string numérica (proto3 uint64 em JSON).
    assert isinstance(rec["timeUnixNano"], str)
    assert rec["timeUnixNano"].isdigit()


def test_time_unix_nano_fallback_to_wall_clock() -> None:
    """Sem normalized.time → usa time.time_ns() (wall clock, > 0 e recente)."""
    ev = {"_centralops": {"event_id": "no-time"}, "raw": {}}
    before_ns = time.time_ns()
    rec = format_otlp_log_record(ev)
    after_ns = time.time_ns()
    t_ns = int(rec["timeUnixNano"])
    assert before_ns <= t_ns <= after_ns


@pytest.mark.parametrize(
    "severity_id,expected_sev_num",
    [
        (0,  0),   # unknown      → UNSPECIFIED
        (1,  9),   # informational → INFO
        (2,  5),   # low           → DEBUG
        (3,  13),  # medium        → WARN
        (4,  17),  # high          → ERROR
        (5,  21),  # critical      → FATAL
        (6,  24),  # fatal         → FATAL4
        (99, 9),   # other         → INFO
    ],
)
def test_severity_mapping(severity_id: int, expected_sev_num: int) -> None:
    """Cada OCSF severity_id mapeia para o OTLP SeverityNumber correto."""
    ev = {
        "normalized": {"severity_id": severity_id, "time": 1_000},
        "raw": {},
    }
    rec = format_otlp_log_record(ev)
    assert rec["severityNumber"] == expected_sev_num


def test_severity_unknown_input_returns_unspecified() -> None:
    """severity_id ausente ou inválido → severityNumber=0 (UNSPECIFIED)."""
    assert _severity_number(None) == 0
    assert _severity_number("bad") == 0
    assert _severity_number(-1) == 0
    assert _severity_number(999) == 0


def test_body_is_string_value_with_json_of_envelope() -> None:
    """body.stringValue deve conter o JSON serializado do envelope completo."""
    rec = format_otlp_log_record(_CANONICAL)
    body = rec["body"]
    assert isinstance(body, dict)
    assert "stringValue" in body
    parsed = json.loads(body["stringValue"])
    assert parsed["_centralops"]["event_id"] == "evt-abc123"
    assert parsed["normalized"]["class_uid"] == 2004


def test_attributes_contain_centralops_fields() -> None:
    """attributes deve conter centralops.vendor, centralops.event_id, etc."""
    rec = format_otlp_log_record(_CANONICAL)
    attrs = {a["key"]: a["value"]["stringValue"] for a in rec["attributes"]}
    assert attrs["centralops.vendor"] == "sophos"
    assert attrs["centralops.event_id"] == "evt-abc123"
    assert attrs["centralops.event_type"] == "sophos.alert"
    assert attrs["centralops.stream"] == "alerts"
    assert attrs["centralops.integration_id"] == "1"
    assert attrs["centralops.customer_id"] == "7"
    assert attrs["ocsf.class_uid"] == "2004"
    assert attrs["ocsf.severity_id"] == "4"


def test_attributes_omit_none_values() -> None:
    """Atributos com valor None não devem aparecer na lista."""
    ev = {"_centralops": {"vendor": "test"}, "normalized": {}, "raw": {}}
    rec = format_otlp_log_record(ev)
    keys = {a["key"] for a in rec["attributes"]}
    assert "centralops.event_id" not in keys
    assert "ocsf.class_uid" not in keys


def test_attributes_all_values_are_strings() -> None:
    """Todos os values em attributes devem ser stringValue (não inteiros)."""
    rec = format_otlp_log_record(_CANONICAL)
    for attr in rec["attributes"]:
        val = attr["value"]
        assert "stringValue" in val
        assert isinstance(val["stringValue"], str)


# ═══════════════════════════════════════════════════════════════════════════
# PART 2 — format_otlp_request: estrutura ExportLogsServiceRequest
# ═══════════════════════════════════════════════════════════════════════════


def test_request_structure_resourcelogs_scopelogs_logrecords() -> None:
    """ExportLogsServiceRequest deve ter resourceLogs → scopeLogs → logRecords."""
    req = format_otlp_request([_CANONICAL])
    assert "resourceLogs" in req
    assert len(req["resourceLogs"]) == 1
    rl = req["resourceLogs"][0]
    assert "resource" in rl
    assert "scopeLogs" in rl
    assert len(rl["scopeLogs"]) == 1
    sl = rl["scopeLogs"][0]
    assert "scope" in sl
    assert "logRecords" in sl
    assert len(sl["logRecords"]) == 1


def test_scope_name_and_version() -> None:
    """scope.name e scope.version devem ser os valores canonicos do sender."""
    req = format_otlp_request([_CANONICAL])
    scope = req["resourceLogs"][0]["scopeLogs"][0]["scope"]
    assert scope["name"] == _SCOPE_NAME
    assert scope["version"] == _SCOPE_VERSION


def test_resource_attrs_injected() -> None:
    """resource_attrs custom aparecem em resource.attributes como KeyValue."""
    req = format_otlp_request(
        [_CANONICAL],
        resource_attrs={"service.name": "centralops", "host.name": "node-01"},
    )
    attrs = req["resourceLogs"][0]["resource"]["attributes"]
    kv = {a["key"]: a["value"]["stringValue"] for a in attrs}
    assert kv["service.name"] == "centralops"
    assert kv["host.name"] == "node-01"


def test_empty_resource_attrs_gives_empty_list() -> None:
    """Sem resource_attrs → resource.attributes é lista vazia."""
    req = format_otlp_request([_CANONICAL])
    attrs = req["resourceLogs"][0]["resource"]["attributes"]
    assert attrs == []


def test_request_with_multiple_events() -> None:
    """Batch com N eventos → N logRecords no mesmo scopeLogs."""
    ev2 = {"_centralops": {"event_id": "evt-2"}, "normalized": {"severity_id": 1}, "raw": {}}
    req = format_otlp_request([_CANONICAL, ev2])
    log_records = req["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    assert len(log_records) == 2


# ═══════════════════════════════════════════════════════════════════════════
# PART 3 — on-wire bytes (real send_batch, mocked transport)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_wire_is_valid_json() -> None:
    """send_batch posta JSON válido e parsável."""
    wire = await _capture_wire(_client(), [_CANONICAL])
    parsed = json.loads(wire)
    assert "resourceLogs" in parsed


@pytest.mark.asyncio
async def test_wire_single_event_has_one_log_record() -> None:
    """Um evento → exatamente 1 logRecord no wire."""
    wire = await _capture_wire(_client(), [_CANONICAL])
    parsed = json.loads(wire)
    records = parsed["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    assert len(records) == 1


@pytest.mark.asyncio
async def test_wire_time_unix_nano_in_nanos() -> None:
    """timeUnixNano no wire deve ser string com valor em nanossegundos."""
    wire = await _capture_wire(_client(), [_CANONICAL])
    parsed = json.loads(wire)
    rec = parsed["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    assert rec["timeUnixNano"] == str(_CANONICAL_NS)
    # Comprimento > 15 dígitos confirma que não são ms (13 dígitos)
    assert len(rec["timeUnixNano"]) >= 16


@pytest.mark.asyncio
async def test_wire_severity_number_mapped() -> None:
    """severity_id=4 (high) → severityNumber=17 (ERROR) no wire."""
    wire = await _capture_wire(_client(), [_CANONICAL])
    parsed = json.loads(wire)
    rec = parsed["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    assert rec["severityNumber"] == 17
    assert rec["severityText"] == "ERROR"


@pytest.mark.asyncio
async def test_wire_body_contains_envelope_json() -> None:
    """body.stringValue no wire deve ser o JSON do envelope."""
    wire = await _capture_wire(_client(), [_CANONICAL])
    parsed = json.loads(wire)
    rec = parsed["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    body_str = rec["body"]["stringValue"]
    body_obj = json.loads(body_str)
    assert body_obj["_centralops"]["event_id"] == "evt-abc123"


@pytest.mark.asyncio
async def test_wire_empty_batch_no_post() -> None:
    """Batch vazio → ok(0) sem POST."""
    client = _client()
    session = _mock_session(_mock_response(200, {}))
    client._session = session
    result = await client.send_batch([])
    assert result.accepted == 0
    session.post.assert_not_called()


@pytest.mark.asyncio
async def test_wire_unicode_preserved() -> None:
    """ensure_ascii=False → multibyte chars como UTF-8 bruto no wire."""
    ev = {
        "_centralops": {"event_id": "u1"},
        "normalized": {"severity_id": 1, "time": 1_000},
        "msg": "café—日本語",
        "raw": {},
    }
    wire = await _capture_wire(_client(), [ev])
    assert "café—日本語" in wire
    assert "\\u" not in wire


# ═══════════════════════════════════════════════════════════════════════════
# PART 4 — DeliveryResult mapping (mocked responses)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_200_ok_all_accepted() -> None:
    """HTTP 200 sem partialSuccess → todos aceitos."""
    client = _client()
    client._session = _mock_session(_mock_response(200, {}))
    result = await client.send_batch([_CANONICAL])
    assert result.accepted == 1
    assert result.rejected == []
    assert result.retryable is False


@pytest.mark.asyncio
async def test_204_ok_all_accepted() -> None:
    """HTTP 204 (sem corpo) → todos aceitos."""
    client = _client()
    client._session = _mock_session(_mock_response(204, None))
    result = await client.send_batch([_CANONICAL])
    assert result.accepted == 1
    assert result.rejected == []
    assert result.retryable is False


@pytest.mark.asyncio
async def test_429_retryable() -> None:
    """HTTP 429 → retryable=True, accepted=0."""
    client = _client()
    client._session = _mock_session(_mock_response(429, {}))
    result = await client.send_batch([_CANONICAL])
    assert result.retryable is True
    assert result.accepted == 0


@pytest.mark.asyncio
async def test_503_retryable() -> None:
    """HTTP 503 → retryable=True."""
    client = _client()
    client._session = _mock_session(_mock_response(503, {}))
    result = await client.send_batch([_CANONICAL])
    assert result.retryable is True


@pytest.mark.asyncio
async def test_401_rejects_whole_batch_non_retryable() -> None:
    """HTTP 401 → todos rejeitados com error_kind=auth, retryable=False."""
    client = _client()
    client._session = _mock_session(_mock_response(401, {}))
    result = await client.send_batch([_CANONICAL])
    assert result.retryable is False
    assert len(result.rejected) == 1
    assert result.rejected[0].error_kind == "auth"
    assert result.rejected[0].event_id == "evt-abc123"


@pytest.mark.asyncio
async def test_403_rejects_whole_batch_non_retryable() -> None:
    """HTTP 403 → todos rejeitados com error_kind=auth."""
    client = _client()
    batch = [_CANONICAL, {"_centralops": {"event_id": "evt-2"}, "raw": {}}]
    client._session = _mock_session(_mock_response(403, {}))
    result = await client.send_batch(batch)
    assert result.retryable is False
    assert len(result.rejected) == 2
    assert all(r.error_kind == "auth" for r in result.rejected)


@pytest.mark.asyncio
async def test_400_triggers_individual_fallback() -> None:
    """HTTP 400 (determinístico) → fallback individual."""
    client = _client()

    # Batch de 2 eventos; fallback individual vai enviar 1 a 1.
    ev_good = {"_centralops": {"event_id": "good"}, "normalized": {"severity_id": 1}, "raw": {}}
    ev_bad = {"_centralops": {"event_id": "bad"}, "normalized": {"severity_id": 99}, "raw": {}}

    call_count = 0

    async def _post_side_effect(url: str, *, data: Any) -> MagicMock:
        nonlocal call_count
        call_count += 1
        # 1ª chamada = lote inteiro → 400
        # Chamadas subsequentes (fallback individual):
        #   ev_good → 200; ev_bad → 400
        if call_count == 1:
            return _mock_response(400, {"message": "bad event"}).__enter__() or _mock_response(400, {"message": "bad event"})
        # Não alcançado via __aenter__  — usamos AsyncMock abaixo.
        return _mock_response(200, {}).__enter__() or _mock_response(200, {})

    # Configura session com respostas sequenciais
    batch_resp = _mock_response(400, {"message": "bad event"})
    good_resp = _mock_response(200, {})
    bad_resp = _mock_response(400, {"message": "bad event"})

    responses = iter([batch_resp, good_resp, bad_resp])

    def _post_mock(*args: Any, **kwargs: Any) -> MagicMock:
        return next(responses)

    session = MagicMock()
    session.closed = False
    session.post = MagicMock(side_effect=_post_mock)
    session.close = AsyncMock()

    client._session = session
    result = await client.send_batch([ev_good, ev_bad])

    assert result.accepted == 1
    assert len(result.rejected) == 1
    assert result.rejected[0].event_id == "bad"
    assert result.rejected[0].error_kind == "schema_rejected"
    assert result.retryable is False


@pytest.mark.asyncio
async def test_partial_success_rejected_log_records_triggers_fallback() -> None:
    """partialSuccess.rejectedLogRecords > 0 → fallback individual."""
    client = _client()

    ev_good = {"_centralops": {"event_id": "g1"}, "normalized": {"severity_id": 1}, "raw": {}}
    ev_bad = {"_centralops": {"event_id": "b1"}, "normalized": {"severity_id": 99}, "raw": {}}

    # Resposta inicial do lote: partial_success com 1 rejeitado.
    partial_body = {
        "partialSuccess": {
            "rejectedLogRecords": 1,
            "errorMessage": "campo inválido",
        }
    }
    batch_resp = _mock_response(200, partial_body)
    good_resp = _mock_response(200, {})
    bad_resp = _mock_response(400, {"message": "campo inválido"})

    responses = iter([batch_resp, good_resp, bad_resp])

    def _post_mock(*args: Any, **kwargs: Any) -> MagicMock:
        return next(responses)

    session = MagicMock()
    session.closed = False
    session.post = MagicMock(side_effect=_post_mock)
    session.close = AsyncMock()

    client._session = session
    result = await client.send_batch([ev_good, ev_bad])

    assert result.accepted == 1
    assert len(result.rejected) == 1
    assert result.rejected[0].event_id == "b1"


@pytest.mark.asyncio
async def test_event_id_missing_uses_question_mark() -> None:
    """Evento sem event_id → '?' no rejected (nunca empty string)."""
    client = _client()
    ev = {"raw": {}}  # sem _centralops
    client._session = _mock_session(_mock_response(401, {}))
    result = await client.send_batch([ev])
    assert result.rejected[0].event_id == "?"


# ═══════════════════════════════════════════════════════════════════════════
# PART 5 — test() probe
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_probe_ok_on_200() -> None:
    """HTTP 200 no probe → TestResult.ok=True."""
    client = _client()
    client._session = _mock_session(_mock_response(200, {}))
    result = await client.test()
    assert result.ok is True
    assert "OTLP ok" in result.detail


@pytest.mark.asyncio
async def test_probe_ok_on_204() -> None:
    """HTTP 204 no probe → TestResult.ok=True."""
    client = _client()
    client._session = _mock_session(_mock_response(204, None))
    result = await client.test()
    assert result.ok is True


@pytest.mark.asyncio
async def test_probe_fails_on_401() -> None:
    """HTTP 401 no probe → TestResult.ok=False com 'credencial'."""
    client = _client()
    client._session = _mock_session(_mock_response(401, {}))
    result = await client.test()
    assert result.ok is False
    assert "credencial" in result.detail


@pytest.mark.asyncio
async def test_probe_fails_on_500() -> None:
    """HTTP 500 no probe → TestResult.ok=False."""
    client = _client()
    client._session = _mock_session(_mock_response(500, {}))
    result = await client.test()
    assert result.ok is False


@pytest.mark.asyncio
async def test_probe_fails_on_connection_error() -> None:
    """Erro de conexão no probe → TestResult.ok=False com mensagem de rede."""
    client = _client()
    session = MagicMock()
    session.closed = False
    session.close = AsyncMock()

    error_resp = MagicMock()
    error_resp.__aenter__ = AsyncMock(side_effect=aiohttp.ClientConnectionError("refused"))
    error_resp.__aexit__ = AsyncMock(return_value=None)
    session.post = MagicMock(return_value=error_resp)

    client._session = session
    result = await client.test()
    assert result.ok is False
    assert "erro de conexão" in result.detail


import aiohttp  # noqa: E402  (importado aqui para o teste acima)


# ═══════════════════════════════════════════════════════════════════════════
# PART 6 — registry: otlp aparece em describe_all
# ═══════════════════════════════════════════════════════════════════════════


def test_otlp_registered_in_all_kinds() -> None:
    """kind=otlp deve aparecer em registry.all_kinds() após o import."""
    assert "otlp" in all_kinds()


def test_otlp_in_describe_all() -> None:
    """describe_all() deve incluir o kind=otlp com schema correto."""
    catalog = {c["kind"]: c for c in describe_all()}
    assert "otlp" in catalog
    otlp = catalog["otlp"]
    assert otlp["label"] == "OTLP/HTTP (OpenTelemetry)"
    schema = otlp["config_schema"]
    assert "endpoint" in schema["properties"]
    assert "headers" in schema["properties"]
    assert "resource_attrs" in schema["properties"]
    assert "verify_tls" in schema["properties"]


def test_otlp_capabilities() -> None:
    """Capabilities: tls, batch, test, at_least_once. SEM erasure, SEM idempotent."""
    catalog = {c["kind"]: c for c in describe_all()}
    caps = set(catalog["otlp"]["capabilities"])
    assert "tls" in caps
    assert "batch" in caps
    assert "test" in caps
    assert "at_least_once" in caps
    assert "erasure" not in caps
    assert "idempotent" not in caps


def test_otlp_factory_builds_client() -> None:
    """registry.build() para otlp deve retornar um OtlpHttpClient."""
    from backend.app.collectors.output.destinations.registry import (
        DestinationConfig,
        build,
        compute_config_version,
    )

    config = {"endpoint": "https://otel.test.local:4318/v1/logs"}
    dest_cfg = DestinationConfig(
        destination_id="test-otlp",
        kind="otlp",
        config=config,
        config_version=compute_config_version(config, {}),
    )
    dest = build(dest_cfg)
    assert isinstance(dest, OtlpHttpClient)
    assert dest.kind == "otlp"


def test_otlp_format_returns_log_record() -> None:
    """OtlpHttpClient.format() retorna dict com estrutura LogRecord."""
    client = _client()
    rec = client.format(_CANONICAL)
    assert "timeUnixNano" in rec
    assert "severityNumber" in rec
    assert "body" in rec
    assert "attributes" in rec
