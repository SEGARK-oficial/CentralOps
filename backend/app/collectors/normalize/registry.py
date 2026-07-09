"""Registro de tipo-casts da DSL de mapping (Fase 1.1).

Expõe um decorador ``@register_type_cast`` que associa uma função a um nome
canônico e seus metadados documentais. O dict resultante ``TYPE_CASTS`` é
importado por ``operators.py`` substituindo o antigo literal ``_TYPE_CAST``.

``OperatorError`` é definido aqui (e não em ``operators.py``) para evitar
dependência circular: ``operators.py`` importa ``register_type_cast`` deste
módulo e, ao mesmo tempo, as funções registradas precisam levantar
``OperatorError``. Colocar a exceção aqui quebra o ciclo.

Uso:
    from .registry import register_type_cast, OperatorError

    @register_type_cast(
        "meu_cast",
        description="Transforma X em Y.",
        signature="X → Y",
    )
    def meu_cast(value):
        ...

Invariantes:
- Nomes são únicos — registrar o mesmo nome duas vezes levanta ``KeyError``.
- ``TYPE_CASTS`` e ``TYPE_CAST_DESCRIPTORS`` são dicts em memória; nenhuma
  persistência é necessária porque os casts são built-in da aplicação.
"""

from __future__ import annotations

from typing import Any, Callable, TypeVar

__all__ = [
    "OperatorError",
    "OperatorSizeError",
    "TYPE_CASTS",
    "TYPE_CAST_DESCRIPTORS",
    "register_type_cast",
]


class OperatorError(ValueError):
    """Falha ao aplicar um operador (ex: cast inválido)."""


class OperatorSizeError(OperatorError):
    """Levantada quando o input excede um limite defensivo de tamanho/profundidade.

    NÃO é silenciada por ``tolerant=True`` — proteção contra DoS tem
    precedência sobre tolerância a erros de parsing.  O engine propaga esta
    exceção incondicionalmente, mesmo quando ``op.tolerant is True``.
    """


# name → callable
TYPE_CASTS: dict[str, Callable[..., Any]] = {}

# name → {"description": str, "signature": str}
TYPE_CAST_DESCRIPTORS: dict[str, dict[str, str]] = {}

_F = TypeVar("_F", bound=Callable[..., Any])


def register_type_cast(
    name: str,
    *,
    description: str,
    signature: str,
) -> Callable[[_F], _F]:
    """Decorador que registra uma função como cast nomeado.

    Args:
        name: Nome canônico usado na DSL (``type_cast: "<name>"``).
        description: Uma frase descrevendo o que o cast faz.
        signature: Notação curta do tipo de entrada/saída,
                   ex. ``"str → str"`` ou ``"float[0..1] → int[0..100]"``.

    Raises:
        KeyError: Se ``name`` já estiver registrado (previne colisões silenciosas).
    """
    if name in TYPE_CASTS:
        raise KeyError(
            f"register_type_cast: nome {name!r} já registrado. "
            "Use um nome diferente ou remova o registro anterior."
        )

    def decorator(fn: _F) -> _F:
        TYPE_CASTS[name] = fn
        TYPE_CAST_DESCRIPTORS[name] = {
            "description": description,
            "signature": signature,
        }
        return fn

    return decorator
