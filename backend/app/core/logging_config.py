"""Configuração de logging JSON estruturado.

Substitui o ``logging.basicConfig`` text-based por JSON formatter, com:
- correlation_id propagado via contextvar entre requests e workers.
- Redação automática de campos sensíveis nos extras do log record.
- Saída em stdout (compatível com fluentbit/promtail/Wazuh log forwarder).
- Handler de arquivo JSONL opcional para consumo direto pelo agente Wazuh.
"""

from __future__ import annotations

import logging
import re
import sys
from contextvars import ContextVar
from typing import Any

from pythonjsonlogger import json as pythonjson_logger

# ── Correlation ID ────────────────────────────────────────────────────

# Contextvar para correlation_id; propagado entre requests HTTP e workers.
_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def get_correlation_id() -> str | None:
    """Retorna o correlation_id ativo no contexto atual, ou None."""
    return _correlation_id.get()


def set_correlation_id(value: str | None) -> None:
    """Define o correlation_id no contexto atual."""
    _correlation_id.set(value)


# ── Redação de campos sensíveis ──────────────────────────────────────

# Campos cujo valor deve ser redactado nos log records (defesa em profundidade).
SENSITIVE_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "password",
        "client_secret",
        "api_password",
        "api_username",
        "manager_api_password",
        "manager_api_username",
        "indexer_password",
        "indexer_username",
        "smtp_password",
        "access_token",
        "refresh_token",
        "authorization",
        "token",
        "secret",
        "master_key",
        "app_master_key",
        # Credenciais do Vault (KMS).
        "secret_id",
        "role_id",
        "vault_token",
        # Nomes de segredo comuns em payloads de evento de segurança (não PII —
        # segredos, cuja redação é sempre segura). Fecham buracos no ring de
        # captura/auditoria, que grava payload de cliente.
        "api_key",
        "apikey",
        "x-api-key",
        "private_key",
        "secret_key",
        "session_key",
        "credentials",
        "passwd",
        "pwd",
        "cookie",
        "set-cookie",
        "bearer",
    }
)


# ── Scrubber de Personal Access Tokens (PAT) ────────────────


# Regex: copsk_ seguido de 20+ chars urlsafe base64 (-, _, alphanumerics).
# Token gerado por secrets.token_urlsafe(32) tem ~43 chars; usamos {20,80}
# pra cobrir variacoes futuras sem capturar palavras curtas como "copsk_x".
_PAT_PATTERN = re.compile(r"copsk_[A-Za-z0-9_\-]{20,80}")
_PAT_REDACTION = "copsk_[REDACTED]"

# Regex: tokens do HashiCorp Vault (KMS). Modernos com prefixo
# hv[s|b|r]. (service/batch/recovery, prefixo inequívoco → 8+ chars). Legado
# s./b. exige corpo longo (24+) p/ não capturar abreviações em prosa.
_VAULT_TOKEN_PATTERN = re.compile(
    r"\bhv[sbr]\.[A-Za-z0-9._\-]{8,}|\b[sb]\.[A-Za-z0-9]{24,}"
)
_VAULT_TOKEN_REDACTION = "[REDACTED_VAULT_TOKEN]"


def _scrub_pat(value: str) -> str:
    """Substitui secrets conhecidos em texto: PAT (copsk_*) e tokens do Vault."""
    value = _PAT_PATTERN.sub(_PAT_REDACTION, value)
    value = _VAULT_TOKEN_PATTERN.sub(_VAULT_TOKEN_REDACTION, value)
    return value


def scrub_secrets_in_value(value: str) -> str:
    """Público: remove secrets embutidos EM UM VALOR de string (PAT, token Vault).

    Diferente da redação por NOME de campo, isto pega o segredo mesmo quando ele
    cai num campo de nome inocente — ex.: uma URL ``https://.../?token=copsk_...``
    num campo ``url``. Usado pela redação do ring de captura/auditoria."""
    return _scrub_pat(value)


class TokenScrubFilter(logging.Filter):
    """Filter que apaga PATs (copsk_*) de mensagens e args de log records.

    Defesa em profundidade: mesmo que algum logger imprima Authorization
    header, request body, ou exception com PAT no texto, o token nunca
    chega ao stdout/JSONL.

    Aplicado no root logger junto com ``CentralOpsJsonFormatter`` (que
    cobre ``extra``s nominativos).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # 1. Mensagem principal — pode ser str pre-formatado ou format string.
        if isinstance(record.msg, str):
            record.msg = _scrub_pat(record.msg)

        # 2. Args (positional) — usados em record.getMessage() pelos formatters.
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(
                    _scrub_pat(a) if isinstance(a, str) else a for a in record.args
                )
            elif isinstance(record.args, dict):
                record.args = {
                    k: (_scrub_pat(v) if isinstance(v, str) else v)
                    for k, v in record.args.items()
                }

        # 3. exc_text — quando logger.exception() cacheou o traceback formatado.
        if record.exc_text:
            record.exc_text = _scrub_pat(record.exc_text)

        return True


# ── Formatter customizado ─────────────────────────────────────────────


class CentralOpsJsonFormatter(pythonjson_logger.JsonFormatter):
    """Formatter JSON com correlation_id, service tag e redação de secrets.

    Estende :class:`pythonjsonlogger.jsonlogger.JsonFormatter` (v4) para:
    - Injetar ``level``, ``logger``, ``service`` com nomes padronizados.
    - Incluir ``correlation_id`` quando disponível no contextvar.
    - Redactar chaves sensíveis nos campos extras do log record.
    """

    def add_fields(
        self,
        log_data: dict[str, Any],
        record: logging.LogRecord,
        message_dict: dict[str, Any],
    ) -> None:
        # Popula campos base via implementação da classe pai.
        super().add_fields(log_data, record, message_dict)

        # Campos padronizados — substituem keys legacy do stdlib.
        log_data["level"] = record.levelname
        log_data["logger"] = record.name
        log_data["service"] = "centralops"

        # Correlation ID injetado quando presente no contextvar.
        cid = get_correlation_id()
        if cid:
            log_data["correlation_id"] = cid

        # Redação de campos sensíveis nos extras.
        for key in list(log_data.keys()):
            if isinstance(key, str) and key.lower() in SENSITIVE_FIELD_NAMES:
                log_data[key] = "[REDACTED]"

        # Scrub secrets (PAT copsk_* + tokens do Vault) em values string —
        # defesa em profundidade contra extras nao-nominativos (ex:
        # "url"="...?token=copsk_xxx", ou um token Vault num corpo de exceção).
        for key, value in log_data.items():
            if isinstance(value, str):
                log_data[key] = _scrub_pat(value)


# ── Configuração global ───────────────────────────────────────────────


def configure_logging(
    level: int = logging.INFO,
    enable_wazuh_jsonl: bool = False,
    wazuh_jsonl_path: str = "/var/log/centralops/app.jsonl",
) -> None:
    """Configura o root logger para emissão JSON. Idempotente.

    Parâmetros
    ----------
    level:
        Nível mínimo de logging (padrão: ``logging.INFO``).
    enable_wazuh_jsonl:
        Se ``True``, adiciona ``TimedRotatingFileHandler`` escrevendo JSONL
        no caminho ``wazuh_jsonl_path`` para consumo pelo agente Wazuh.
        Em caso de falha de acesso ao caminho, emite aviso e continua só
        com stdout — nunca bloqueia a inicialização.
    wazuh_jsonl_path:
        Caminho do arquivo JSONL para forwarding ao Wazuh.
        Rotation diária, retenção de 7 arquivos.
    """
    formatter = CentralOpsJsonFormatter(
        fmt=["timestamp", "level", "logger", "service", "message"],
        timestamp=True,  # injeta campo "timestamp" com datetime UTC
    )

    # Handler stdout — sempre presente.
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(formatter)

    handlers: list[logging.Handler] = [stdout_handler]

    # Handler de arquivo opcional para forwarding ao agente Wazuh.
    if enable_wazuh_jsonl:
        from logging.handlers import TimedRotatingFileHandler

        try:
            file_handler = TimedRotatingFileHandler(
                wazuh_jsonl_path,
                when="midnight",
                interval=1,
                backupCount=7,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            handlers.append(file_handler)
        except (OSError, PermissionError) as exc:
            # Não bloqueia boot se path não existir ou sem permissão.
            # Aviso emitido antes de configurar o root handler definitivo.
            logging.warning("Wazuh JSONL path inacessível: %s", exc)

    root = logging.getLogger()
    # Remove handlers existentes para evitar duplicação em re-chamadas.
    root.handlers = handlers
    root.setLevel(level)

    # Filter de scrub de PATs (copsk_*) — defesa em profundidade
    # contra leak via msg/args/exc_text de qualquer logger.
    # Remove instâncias antigas (idempotência) antes de adicionar.
    root.filters = [f for f in root.filters if not isinstance(f, TokenScrubFilter)]
    root.addFilter(TokenScrubFilter())

    # Reduz verbosidade de bibliotecas ruidosas mas sem interesse operacional.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
