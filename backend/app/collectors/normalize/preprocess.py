"""Operadores de pré-processamento da DSL v2.

Executados UMA vez por evento, antes do loop de regras, populando o
dicionário ``extracted_fields: Dict[str, Any]``.  Nomes de target DEVEM
começar com ``_`` (namespace reservado).

Operadores cobertos:

- ``json_parse``: parseia uma string JSON extraída do raw em objeto Python.
  Caso de uso canônico: ``processedData`` do Sophos Detection Event.

Adicionando um novo operador
-----------------------------
Basta usar ``@register_preprocess_op`` com nome único, ``description`` e
``signature``.  O operador fica disponível imediatamente na DSL v2 e no
endpoint ``GET /api/mappings/normalize/preprocess-ops``.

Notas de segurança
------------------
- ``max_bytes`` em ``json_parse`` é **sempre** respeitado, mesmo quando
  ``tolerant=True``.  Limite de DoS não pode ser suprimido por configuração
  de tolerância de erro de parsing.
- ``PREPROCESS_JSON_MAX_BYTES`` é lido do ambiente uma única vez na
  importação do módulo (cache de processo).  Default: 1 MiB (1_048_576).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Optional, TypeVar

from jmespath.parser import ParsedResult

from .registry import OperatorError  # noqa: F401 — re-export para callers
from .registry import OperatorSizeError  # noqa: F401 — re-export para callers

logger = logging.getLogger(__name__)

__all__ = [
    "CompiledPreprocessOp",
    "PREPROCESS_OPS",
    "PREPROCESS_OP_DESCRIPTORS",
    "register_preprocess_op",
    "apply_preprocess_op",
]

# ── Registros ─────────────────────────────────────────────────────────

# name → callable
PREPROCESS_OPS: dict[str, Callable[..., Any]] = {}

# name → {"description": str, "signature": str}
PREPROCESS_OP_DESCRIPTORS: dict[str, dict[str, str]] = {}

_F = TypeVar("_F", bound=Callable[..., Any])


def register_preprocess_op(
    name: str,
    *,
    description: str,
    signature: str,
) -> Callable[[_F], _F]:
    """Decorador que registra uma função como operador de pré-processamento.

    Args:
        name: Nome canônico usado na DSL (``op: "<name>"``).
        description: Uma frase descrevendo o que o operador faz.
        signature: Notação curta do tipo de entrada/saída.

    Raises:
        KeyError: Se ``name`` já estiver registrado.
    """
    if name in PREPROCESS_OPS:
        raise KeyError(
            f"register_preprocess_op: nome {name!r} já registrado. "
            "Use um nome diferente ou remova o registro anterior."
        )

    def decorator(fn: _F) -> _F:
        PREPROCESS_OPS[name] = fn
        PREPROCESS_OP_DESCRIPTORS[name] = {
            "description": description,
            "signature": signature,
        }
        return fn

    return decorator


# ── Compiled dataclass ─────────────────────────────────────────────────

@dataclass(frozen=True)
class CompiledPreprocessOp:
    """Forma pré-validada e pré-compilada de um operador de pré-processamento."""

    op: str                        # "json_parse" por ora; extensível
    compiled_source: ParsedResult  # JMESPath compilado (aponta para campo do raw)
    source_str: str                # expressão JMESPath original (para drift)
    target: str                    # ex: "_processed" — DEVE começar com "_"
    tolerant: bool                 # True → erros de parse viram None silenciosamente


# ── Configuração: max_bytes lido ONCE por processo ─────────────────────

def _read_max_bytes() -> int:
    raw = os.environ.get("PREPROCESS_JSON_MAX_BYTES", "1048576")
    try:
        v = int(raw)
        if v <= 0:
            raise ValueError("must be positive")
        return v
    except (ValueError, TypeError):
        logger.warning(
            "PREPROCESS_JSON_MAX_BYTES inválido (%r); usando 1048576 (1 MiB)", raw
        )
        return 1_048_576


_JSON_MAX_BYTES: int = _read_max_bytes()


# ── Operadores ─────────────────────────────────────────────────────────

@register_preprocess_op(
    "json_parse",
    description=(
        "Parseia uma string JSON e retorna o objeto Python resultante. "
        "None passa direto. Non-string levanta OperatorError. "
        "max_bytes é sempre respeitado (proteção DoS) independente de tolerant."
    ),
    signature="str → object | None → None",
)
def json_parse(value: Any, *, tolerant: bool = False, max_bytes: Optional[int] = None) -> Any:
    """Parseia ``value`` como JSON.

    Ordem de checagens:

    1. ``None`` → retorna ``None`` (sem considerar tolerant).
    2. ``not isinstance(value, str)`` → OperatorError (ou None se tolerant).
    3. ``len(value.encode("utf-8")) > max_bytes`` → OperatorError SEMPRE
       (proteção DoS — tolerant não suprime esta checagem).
    4. ``json.loads(value)`` → None se tolerant e JSONDecodeError, else OperatorError.

    Args:
        value: Valor a parsear (esperado str, mas aceita None).
        tolerant: Se True, erros de parse devolvem None em vez de levantar.
        max_bytes: Limite de tamanho em bytes UTF-8. Se None, usa
            ``_JSON_MAX_BYTES`` (lido do env na importação do módulo).

    Returns:
        Objeto Python desserializado, ou None.

    Raises:
        OperatorError: Se value não for str (e tolerant=False), ou se
            o payload exceder max_bytes, ou se o JSON for inválido
            (e tolerant=False).
    """
    effective_max = max_bytes if max_bytes is not None else _JSON_MAX_BYTES

    # Checagem 1: None passthrough
    if value is None:
        return None

    # Checagem 2: tipo não-string
    if not isinstance(value, str):
        if tolerant:
            return None
        raise OperatorError(
            f"json_parse: espera str, recebeu {type(value).__name__}"
        )

    # Checagem 3: tamanho (DoS) — SEMPRE, independente de tolerant.
    # Usa OperatorSizeError para que o engine propague mesmo com tolerant=True.
    byte_len = len(value.encode("utf-8"))
    if byte_len > effective_max:
        raise OperatorSizeError(
            f"json_parse: payload excede {effective_max} bytes ({byte_len} bytes)"
        )

    # Checagem 4: parse JSON.
    # RecursionError: payloads deeply-nested como '[' * 500_000 + ']' * 500_000
    # (dentro do limite de bytes mas com profundidade absurda) causam stack
    # overflow no parser C do Python.
    # ValueError: emitido pelo parser C em alguns inputs malformados específicos.
    # Ambos são tratados como erro de parsing (não de tamanho).
    try:
        return json.loads(value)
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        if tolerant:
            logger.debug("json_parse: JSON inválido (tolerant=True), retornando None: %s", exc)
            return None
        raise OperatorError(f"json_parse: JSON inválido: {exc}") from exc


# ── apply_preprocess_op ────────────────────────────────────────────────

def apply_preprocess_op(value: Any, op_name: str, **kwargs: Any) -> Any:
    """Aplica o operador de pré-processamento nomeado.

    Levanta ``OperatorError`` se o operador for desconhecido.
    """
    fn = PREPROCESS_OPS.get(op_name)
    if fn is None:
        raise OperatorError(
            f"preprocess op desconhecido: {op_name!r}. "
            f"Suportados: {sorted(PREPROCESS_OPS.keys())}"
        )
    return fn(value, **kwargs)
