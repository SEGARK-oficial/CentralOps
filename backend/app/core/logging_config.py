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


# ── Redação por HANDLER (cobre loggers filhos e o export OTLP) ────────
#
# Filtro instalado num LOGGER só é consultado para registros criados NAQUELE
# logger: um ``logging.getLogger("app.collectors.x").warning(...)`` propaga até os
# handlers do raiz SEM passar pelos filtros do raiz. Como quase todo log da
# aplicação nasce num logger filho, a redação que só vive no raiz não protege
# nada fora do raiz. O stdout/JSONL escapava porque o formatter JSON também
# redige — mas qualquer handler que NÃO usa esse formatter (o ``LoggingHandler``
# do OpenTelemetry, o ``basicConfig`` do dispatcher Kafka, o handler de debug)
# recebia o registro cru. Por isso a redação é instalada em CADA handler.
#
# A redação aqui é CIRÚRGICA: mascara o VALOR (``token=[REDACTED]``) e preserva
# o resto da mensagem, para não apagar logs operacionais legítimos como
# "faltou o header Authorization".

#: Atributos que todo ``LogRecord`` tem; o que sobra veio de ``extra=``.
_STD_RECORD_ATTRS: frozenset[str] = frozenset(
    vars(logging.LogRecord("", logging.INFO, "", 0, "", (), None))
) | {"message", "asctime", "taskName"}

_REDACTED = "[REDACTED]"

# ``scheme://usuario:senha@host`` → ``scheme://[REDACTED]@host`` (Redis, AMQP,
# Postgres, URLs de webhook com credencial embutida).
_URL_USERINFO_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9+.\-]*://)[^\s/@:]*:[^\s/@]+@")

# ``Bearer <token>`` sempre; ``Basic <base64>`` só quando parece base64 (dígito,
# ``+``, ``/`` ou ``=``) para não mascarar prosa como "Basic authentication".
_AUTH_SCHEME_RE = re.compile(
    r"\b(Bearer)\s+[A-Za-z0-9._~+/=\-]{8,}"
    r"|\b(Basic)\s+(?=[A-Za-z0-9+/]*[0-9+/=])[A-Za-z0-9+/]{8,}={0,2}",
    re.IGNORECASE,
)

# ``chave=valor``, ``chave: valor``, ``"chave": "valor"``, ``'chave': 'valor'`` e
# query string (``?token=...&``) para as chaves sensíveis conhecidas. Exige o
# separador: "senha inválida para o usuário" não casa.
_SENSITIVE_KEYS_ALT = "|".join(
    sorted(
        (re.escape(k) for k in SENSITIVE_FIELD_NAMES | {"id_token", "client_assertion"}),
        key=len,
        reverse=True,
    )
)
_SENSITIVE_KV_RE = re.compile(
    r"(?<![\w-])(?P<k>" + _SENSITIVE_KEYS_ALT + r")(?![\w-])"
    r"(?P<sep>[\"']?\s*[:=]\s*[\"']?)"
    # ``Authorization: Bearer x`` fica para ``_AUTH_SCHEME_RE`` (preserva o esquema),
    # e valor já redigido não é tocado de novo (idempotência: ``[REDACTED]]``).
    r"(?!(?:bearer|basic)\s)(?!\[REDACTED)"
    r"(?P<v>[^\s\"',;&}\]]+)",
    re.IGNORECASE,
)


def _scrub_auth_scheme(match: re.Match[str]) -> str:
    return f"{match.group(1) or match.group(2)} {_REDACTED}"


def scrub_sensitive_text(value: str) -> str:
    """Remove segredos de um texto livre, preservando o resto da mensagem.

    Superconjunto de :func:`scrub_secrets_in_value`: além de PAT e token do
    Vault, mascara credencial embutida em URL, ``Bearer``/``Basic`` e o valor de
    pares ``chave=valor`` cujas chaves estão em :data:`SENSITIVE_FIELD_NAMES`.
    Usado em todo texto que sai do processo: stdout/JSONL, logs e traces OTLP."""
    if not value:
        return value
    value = _URL_USERINFO_RE.sub(r"\1" + _REDACTED + "@", value)
    value = _AUTH_SCHEME_RE.sub(_scrub_auth_scheme, value)
    value = _SENSITIVE_KV_RE.sub(r"\g<k>\g<sep>" + _REDACTED, value)
    # Por último: PAT/Vault em qualquer posição (fora de ``chave=valor``).
    return _scrub_pat(value)


def _scrub_value(value: Any, _depth: int = 0) -> Any:
    """Redige um valor de ``extra`` sem mutar o objeto do chamador."""
    if isinstance(value, str):
        return scrub_sensitive_text(value)
    if _depth >= 6:
        return value
    if isinstance(value, dict):
        return {
            k: (
                _REDACTED
                if isinstance(k, str) and k.lower() in SENSITIVE_FIELD_NAMES
                else _scrub_value(v, _depth + 1)
            )
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        scrubbed = [_scrub_value(v, _depth + 1) for v in value]
        return scrubbed if isinstance(value, list) else tuple(scrubbed)
    return value


def scrub_log_record(record: logging.LogRecord) -> None:
    """Redige um ``LogRecord`` IN-PLACE: mensagem, args, traceback em cache e extras.

    A mensagem é pré-formatada (``getMessage()``) antes de redigir: um argumento
    que não é ``str`` (dict, exceção, objeto) só vira texto na formatação, e é
    nesse texto que o segredo aparece. Idempotente."""
    if record.args:
        try:
            record.msg = record.getMessage()
            record.args = ()
        except Exception:  # noqa: BLE001 — args incompatíveis: o logging reporta
            pass
    if isinstance(record.msg, str):
        record.msg = scrub_sensitive_text(record.msg)
    if record.exc_text:
        record.exc_text = scrub_sensitive_text(record.exc_text)
    for key, value in list(vars(record).items()):
        if key in _STD_RECORD_ATTRS:
            continue
        if key.lower() in SENSITIVE_FIELD_NAMES:
            setattr(record, key, _REDACTED)
        else:
            setattr(record, key, _scrub_value(value))


class RedactingFormatter(logging.Formatter):
    """Envolve o formatter de um handler e redige o TEXTO que ele produz.

    Redige na saída de CADA handler, sem tocar no ``LogRecord``: o registro é
    compartilhado por todos os handlers, e alterá-lo (pré-formatar a mensagem,
    zerar ``args``) tiraria os argumentos estruturados de quem vem depois — um
    handler de captura, um agregador. Instalar com :func:`attach_redaction`."""

    def __init__(self, inner: logging.Formatter | None = None) -> None:
        super().__init__()
        self.inner = inner or logging.Formatter()

    def format(self, record: logging.LogRecord) -> str:
        text = self.inner.format(record)
        try:
            return scrub_sensitive_text(text)
        except Exception:  # pragma: no cover — redação nunca derruba um log
            return text

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return self.inner.formatTime(record, datefmt)


def attach_redaction(handler: logging.Handler) -> logging.Handler:
    """Redige toda saída do handler (envolve o formatter atual). Idempotente.

    Chamar DEPOIS de ``setFormatter``: um ``setFormatter`` posterior troca o
    formatter e desfaz a redação."""
    if not isinstance(handler.formatter, RedactingFormatter):
        handler.setFormatter(RedactingFormatter(handler.formatter))
    return handler


def attach_redaction_to_root_handlers() -> None:
    """Instala a redação em todos os handlers ATUAIS do raiz.

    Para processos que configuram o logging por fora de :func:`configure_logging`
    (``logging.basicConfig`` do dispatcher Kafka, logging do Celery beat)."""
    for handler in logging.getLogger().handlers:
        attach_redaction(handler)


def redacted_record_copy(record: logging.LogRecord) -> logging.LogRecord:
    """Cópia redigida de um registro, para exportadores que não usam formatter.

    O ``LoggingHandler`` do OpenTelemetry monta ``exception.message`` e
    ``exception.stacktrace`` direto de ``record.exc_info`` — por fora de qualquer
    filtro. Na cópia, a exceção vira atributos ``exception.*`` já redigidos e
    ``exc_info`` é removido. O registro original (visto pelos demais handlers)
    não é tocado."""
    import copy
    import traceback

    clone = copy.copy(record)
    if clone.exc_info and clone.exc_info[0] is not None:
        exc_type, exc_value, _tb = clone.exc_info
        stacktrace = clone.exc_text or "".join(traceback.format_exception(*clone.exc_info))
        setattr(clone, "exception.type", getattr(exc_type, "__name__", str(exc_type)))
        setattr(clone, "exception.message", scrub_sensitive_text(str(exc_value)))
        setattr(clone, "exception.stacktrace", scrub_sensitive_text(stacktrace))
    clone.exc_info = None
    clone.exc_text = None
    scrub_log_record(clone)
    return clone


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

        # Scrub secrets em values string — PAT copsk_*, tokens do Vault,
        # credencial em URL, Bearer/Basic e ``chave=valor`` sensível. Defesa em
        # profundidade contra extras não-nominativos (ex.: "url"="...?token=...")
        # e contra o traceback, que o formatter re-formata a partir de exc_info.
        for key, value in log_data.items():
            if isinstance(value, str):
                log_data[key] = scrub_sensitive_text(value)


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

    # Redação em CADA handler: filtro de logger não vale para os loggers filhos
    # (ver ``RedactingFormatter``). Depois do ``setFormatter`` acima.
    for handler in handlers:
        attach_redaction(handler)

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
