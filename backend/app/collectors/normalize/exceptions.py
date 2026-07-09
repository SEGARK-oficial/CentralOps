"""Exceptions for the normalize DSL engine.

Separated into their own module to allow predicates.py and engine.py to
share the same exception types without creating a circular import.
"""

from __future__ import annotations


class MappingError(Exception):
    """Falha genérica ao aplicar um mapping."""


class MappingDefinitionError(MappingError):
    """A DSL em si está malformada (sintaxe/forma inválida)."""


class MappingRequiredFieldError(MappingError):
    """Regra com ``required: true`` resolveu para ``None``.

    O pipeline trata como erro de mapping e envia o evento para
    ``QuarantineEvent`` com ``error_kind="map"``.
    """

    def __init__(self, target: str, detail: str = "") -> None:
        self.target = target
        self.detail = detail
        super().__init__(
            f"required field {target!r} resolved to None"
            + (f": {detail}" if detail else "")
        )
