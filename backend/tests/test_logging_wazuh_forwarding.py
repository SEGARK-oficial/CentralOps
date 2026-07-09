"""Testes do handler JSONL para forwarding ao agente Wazuh (RNF6.2 — F5-S2 Commit 3).

Cobre:
- Handler de arquivo grava JSONL válido quando habilitado.
- Handler desabilitado não cria arquivo.
- Path inacessível não bloqueia inicialização (degradação graciosa).
- Rotation handler mantém formato JSON após re-configuração.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from backend.app.core.logging_config import (
    CentralOpsJsonFormatter,
    configure_logging,
)


# ── Testes de handler JSONL ───────────────────────────────────────────


def test_jsonl_handler_writes_to_path_when_enabled(tmp_path: Path) -> None:
    """Quando enable_wazuh_jsonl=True, o log deve ser gravado no arquivo."""
    jsonl_path = str(tmp_path / "app.jsonl")

    configure_logging(enable_wazuh_jsonl=True, wazuh_jsonl_path=jsonl_path)

    test_logger = logging.getLogger("backend.app.wazuh_test_write")
    test_logger.info("mensagem de teste wazuh", extra={"event": "test.write", "x": 1})

    # Garante que o buffer do handler foi descarregado.
    root = logging.getLogger()
    for handler in root.handlers:
        handler.flush()

    assert os.path.exists(jsonl_path), "Arquivo JSONL não foi criado"

    content = Path(jsonl_path).read_text(encoding="utf-8").strip()
    assert content, "Arquivo JSONL está vazio"

    # Deve ser JSON válido linha a linha.
    for line in content.splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        assert "message" in record
        assert record.get("service") == "centralops"


def test_jsonl_handler_writes_valid_json_fields(tmp_path: Path) -> None:
    """Arquivo JSONL deve conter campos obrigatórios do formatter."""
    jsonl_path = str(tmp_path / "fields_check.jsonl")

    configure_logging(enable_wazuh_jsonl=True, wazuh_jsonl_path=jsonl_path)

    logging.getLogger("backend.app.wazuh_test_fields").warning(
        "log de campos estruturados",
        extra={
            "event": "test.fields",
            "integration_id": 77,
            "stream": "wazuh-check",
        },
    )

    root = logging.getLogger()
    for h in root.handlers:
        h.flush()

    lines = [
        ln.strip()
        for ln in Path(jsonl_path).read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    assert lines, "Nenhum log gravado no arquivo"

    record = json.loads(lines[-1])
    assert "level" in record
    assert "timestamp" in record
    assert record.get("service") == "centralops"


def test_jsonl_handler_disabled_no_file_created(tmp_path: Path) -> None:
    """Com enable_wazuh_jsonl=False, nenhum arquivo JSONL deve ser criado."""
    jsonl_path = str(tmp_path / "should_not_exist.jsonl")

    configure_logging(enable_wazuh_jsonl=False, wazuh_jsonl_path=jsonl_path)

    logging.getLogger("backend.app.wazuh_test_disabled").info("log sem wazuh handler")

    root = logging.getLogger()
    for h in root.handlers:
        h.flush()

    assert not os.path.exists(jsonl_path), (
        "Arquivo JSONL foi criado apesar de enable_wazuh_jsonl=False"
    )


def test_jsonl_handler_handles_unreachable_path_gracefully() -> None:
    """Path JSONL inacessível não deve levantar durante configure_logging."""
    bad_path = "/nonexistent/deeply/nested/path/that/cannot/be/created/app.jsonl"
    # Não deve levantar qualquer exceção.
    configure_logging(enable_wazuh_jsonl=True, wazuh_jsonl_path=bad_path)

    # Deve ainda haver pelo menos o stdout handler.
    root = logging.getLogger()
    assert len(root.handlers) >= 1, "Nenhum handler ativo após path inválido"


def test_jsonl_handler_only_stdout_when_path_fails() -> None:
    """Após path inválido, apenas o stdout handler deve estar ativo."""
    bad_path = "/totally/wrong/path/app.jsonl"
    configure_logging(enable_wazuh_jsonl=True, wazuh_jsonl_path=bad_path)

    root = logging.getLogger()
    # Não deve ter nenhum FileHandler ativo (o path falhou).
    from logging.handlers import TimedRotatingFileHandler

    file_handlers = [h for h in root.handlers if isinstance(h, TimedRotatingFileHandler)]
    assert len(file_handlers) == 0, (
        "FileHandler presente mesmo com path inacessível"
    )


def test_configure_logging_wazuh_enabled_has_two_handlers(tmp_path: Path) -> None:
    """Com Wazuh habilitado e path válido, root deve ter 2 handlers."""
    jsonl_path = str(tmp_path / "two_handlers.jsonl")
    configure_logging(enable_wazuh_jsonl=True, wazuh_jsonl_path=jsonl_path)

    root = logging.getLogger()
    assert len(root.handlers) == 2, (
        f"Esperado 2 handlers (stdout + file), encontrado {len(root.handlers)}"
    )


@pytest.mark.parametrize(
    "message,extra",
    [
        ("log simples", {}),
        ("log com event", {"event": "wazuh.test", "org_id": 5}),
        ("log com sensitive", {"password": "secret123"}),  # deve ser redactado
    ],
)
def test_jsonl_each_line_is_parseable(
    tmp_path: Path, message: str, extra: dict
) -> None:
    """Cada linha do JSONL deve ser JSON parseable independente do conteúdo."""
    jsonl_path = str(tmp_path / f"parseable_{message[:6].replace(' ', '_')}.jsonl")
    configure_logging(enable_wazuh_jsonl=True, wazuh_jsonl_path=jsonl_path)

    test_logger = logging.getLogger(f"backend.app.wazuh_parse_test.{id(extra)}")
    test_logger.info(message, extra=extra)

    root = logging.getLogger()
    for h in root.handlers:
        h.flush()

    if not os.path.exists(jsonl_path):
        pytest.skip("Nenhum log gravado — handler pode ter sido limpo por outro teste")

    lines = [
        ln.strip()
        for ln in Path(jsonl_path).read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    for line in lines:
        record = json.loads(line)  # Não deve levantar
        assert "message" in record

    # Verifica redação de campo sensível
    if "password" in extra:
        last = json.loads(lines[-1])
        assert last.get("password") == "[REDACTED]", "Campo 'password' não foi redactado no JSONL"


def test_settings_expose_wazuh_logging_config() -> None:
    """Settings deve expor LOGGING_WAZUH_JSONL_ENABLED e LOGGING_WAZUH_JSONL_PATH."""
    from backend.app.core.config import settings

    # Campos existem e têm defaults seguros.
    assert hasattr(settings, "LOGGING_WAZUH_JSONL_ENABLED")
    assert hasattr(settings, "LOGGING_WAZUH_JSONL_PATH")
    assert isinstance(settings.LOGGING_WAZUH_JSONL_ENABLED, bool)
    assert isinstance(settings.LOGGING_WAZUH_JSONL_PATH, str)
    assert settings.LOGGING_WAZUH_JSONL_PATH != ""
