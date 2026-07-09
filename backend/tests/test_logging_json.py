"""Testes do módulo de logging JSON estruturado.

Cobre:
- Saída JSON válida com campos obrigatórios.
- Redação de campos sensíveis nos extras do log record.
- Propagação de correlation_id via contextvar.
- Middleware de correlation_id: geração de UUID e uso de header fornecido.
"""

from __future__ import annotations

import io
import json
import logging
import uuid
from typing import Generator

import pytest
from fastapi.testclient import TestClient

from backend.app.core.logging_config import (  # noqa: E402
    SENSITIVE_FIELD_NAMES,
    CentralOpsJsonFormatter,
    configure_logging,
    get_correlation_id,
    set_correlation_id,
)


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def json_logger() -> Generator[tuple[logging.Logger, io.StringIO], None, None]:
    """Logger isolado com CentralOpsJsonFormatter e buffer StringIO."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    fmt = CentralOpsJsonFormatter(
        fmt=["timestamp", "level", "logger", "service", "message"],
        timestamp=True,
    )
    handler.setFormatter(fmt)

    logger = logging.getLogger(f"test.{uuid.uuid4().hex}")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    # Não propaga para o root logger (evita poluir saída de testes).
    logger.propagate = False

    yield logger, buf

    logger.handlers.clear()


@pytest.fixture(autouse=True)
def clear_correlation_id() -> Generator[None, None, None]:
    """Garante que o contextvar está limpo antes e depois de cada teste."""
    set_correlation_id(None)
    yield
    set_correlation_id(None)


# ── Testes de formato JSON ────────────────────────────────────────────


def test_json_formatter_outputs_valid_json(
    json_logger: tuple[logging.Logger, io.StringIO],
) -> None:
    """Formatter deve emitir JSON válido com campos obrigatórios."""
    logger, buf = json_logger
    logger.info("mensagem de teste")

    raw = buf.getvalue().strip()
    assert raw, "Nenhuma saída produzida pelo formatter"

    record = json.loads(raw)
    assert record["message"] == "mensagem de teste"
    assert record["level"] == "INFO"
    assert "logger" in record
    assert record["service"] == "centralops"
    assert "timestamp" in record


def test_json_formatter_includes_extra_fields(
    json_logger: tuple[logging.Logger, io.StringIO],
) -> None:
    """Campos passados via extra= devem aparecer no JSON."""
    logger, buf = json_logger
    logger.info("evento estruturado", extra={"integration_id": 42, "stream": "alerts"})

    record = json.loads(buf.getvalue().strip())
    assert record["integration_id"] == 42
    assert record["stream"] == "alerts"


@pytest.mark.parametrize("field_name", sorted(SENSITIVE_FIELD_NAMES))
def test_json_formatter_redacts_sensitive_fields(
    json_logger: tuple[logging.Logger, io.StringIO],
    field_name: str,
) -> None:
    """Campos sensíveis devem ser substituídos por '[REDACTED]'."""
    logger, buf = json_logger
    logger.info("log com campo sensível", extra={field_name: "super-secret-value"})

    raw = buf.getvalue().strip()
    assert "super-secret-value" not in raw, (
        f"Valor sensível vazou no campo '{field_name}'"
    )
    record = json.loads(raw)
    assert record[field_name] == "[REDACTED]"


def test_json_formatter_redacts_case_insensitive(
    json_logger: tuple[logging.Logger, io.StringIO],
) -> None:
    """Redação deve ser case-insensitive (PASSWORD, Password, password)."""
    logger, buf = json_logger
    logger.warning("variante maiúscula", extra={"PASSWORD": "dont-leak"})

    raw = buf.getvalue().strip()
    assert "dont-leak" not in raw
    record = json.loads(raw)
    assert record["PASSWORD"] == "[REDACTED]"


def test_json_formatter_scrubs_vault_token_in_message_body(
    json_logger: tuple[logging.Logger, io.StringIO],
) -> None:
    """Token do Vault (hvs.*/ s.*) em CORPO de mensagem deve ser apagado."""
    logger, buf = json_logger
    secret = "hvs.CAESIJ8examplevaulttokenvalue1234567890abcdef"
    logger.error("falha ao falar com o vault usando %s no header", secret)

    raw = buf.getvalue().strip()
    assert secret not in raw, "token do Vault vazou no corpo da mensagem"
    record = json.loads(raw)
    assert "[REDACTED_VAULT_TOKEN]" in record["message"]


# ── Testes de correlation_id ──────────────────────────────────────────


def test_correlation_id_propagated_via_contextvar(
    json_logger: tuple[logging.Logger, io.StringIO],
) -> None:
    """Após set_correlation_id, o CID deve aparecer no log JSON."""
    logger, buf = json_logger
    test_cid = "cid-test-" + uuid.uuid4().hex

    set_correlation_id(test_cid)
    logger.info("log com cid")

    record = json.loads(buf.getvalue().strip())
    assert record.get("correlation_id") == test_cid


def test_correlation_id_absent_when_not_set(
    json_logger: tuple[logging.Logger, io.StringIO],
) -> None:
    """Sem set_correlation_id, o campo não deve aparecer no JSON."""
    logger, buf = json_logger
    # clear_correlation_id fixture garante que está None.
    logger.info("log sem cid")

    record = json.loads(buf.getvalue().strip())
    assert "correlation_id" not in record


def test_correlation_id_cleared_after_reset(
    json_logger: tuple[logging.Logger, io.StringIO],
) -> None:
    """Após set_correlation_id(None), o campo deve sumir do log."""
    logger, buf = json_logger

    set_correlation_id("cid-temporario")
    set_correlation_id(None)

    logger.info("log após limpar cid")
    record = json.loads(buf.getvalue().strip())
    assert "correlation_id" not in record


def test_get_correlation_id_returns_active_value() -> None:
    """get_correlation_id deve retornar o valor definido no contextvar."""
    test_cid = "abc-123"
    set_correlation_id(test_cid)
    assert get_correlation_id() == test_cid


# ── Testes do middleware HTTP ─────────────────────────────────────────


@pytest.fixture()
def test_client() -> TestClient:
    """TestClient do app FastAPI.

    Os middleware tests batem em ``/openapi.json`` — endpoint nativo
    do FastAPI que não toca o banco e nem requer auth. Antes usávamos
    ``/api/auth/status``, mas ele consulta ``app_users``; com SQLite
    in-memory (pool default, não StaticPool) cada connection é um DB
    isolado e a tabela criada via ``Base.metadata.create_all`` não é
    visível na connection do request. ``/openapi.json`` evita essa
    dor: o objetivo dos testes é o middleware HTTP, não o endpoint
    em si.
    """
    from backend.app.main import app

    return TestClient(app, raise_server_exceptions=True)


def test_correlation_id_middleware_generates_uuid(
    test_client: TestClient,
) -> None:
    """Sem X-Correlation-Id no request, response deve conter UUID gerado."""
    response = test_client.get("/openapi.json")
    cid = response.headers.get("X-Correlation-Id")
    assert cid is not None, "Header X-Correlation-Id ausente na response"
    # Valida que é UUID v4 válido.
    parsed = uuid.UUID(cid, version=4)
    assert str(parsed) == cid


def test_correlation_id_middleware_uses_provided_header(
    test_client: TestClient,
) -> None:
    """X-Correlation-Id fornecido pelo cliente deve ser ecoado na response."""
    client_cid = "my-tracing-id-abc123"
    response = test_client.get(
        "/openapi.json", headers={"X-Correlation-Id": client_cid}
    )
    assert response.headers.get("X-Correlation-Id") == client_cid


def test_correlation_id_middleware_echoes_arbitrary_string(
    test_client: TestClient,
) -> None:
    """CID pode ser qualquer string — não precisa ser UUID."""
    cid = "span-id:abcdef1234"
    response = test_client.get("/openapi.json", headers={"X-Correlation-Id": cid})
    assert response.headers.get("X-Correlation-Id") == cid


# ── Testes de configure_logging ───────────────────────────────────────


def test_configure_logging_idempotent() -> None:
    """configure_logging chamado duas vezes não deve duplicar handlers."""
    configure_logging()
    configure_logging()
    root = logging.getLogger()
    # Após configure_logging, root deve ter exatamente 1 handler (stdout).
    stdout_handlers = [
        h
        for h in root.handlers
        if isinstance(h, logging.StreamHandler) and not hasattr(h, "baseFilename")
    ]
    assert len(stdout_handlers) == 1


def test_configure_logging_wazuh_handles_bad_path(tmp_path: "pytest.TempPathFactory") -> None:
    """Path JSONL inacessível não deve levantar exceção na inicialização."""
    bad_path = "/nonexistent/deep/path/app.jsonl"
    # Não deve levantar — o handler de stdout deve permanecer ativo.
    configure_logging(enable_wazuh_jsonl=True, wazuh_jsonl_path=bad_path)
    root = logging.getLogger()
    assert len(root.handlers) >= 1
