"""Pacote de destinos de saída plugáveis.

Importar este pacote dispara o ``registry._register_builtins()`` (via
import de ``registry``), que por sua vez importa cada módulo de ``kind``
para registrá-lo. Consumidores fazem::

    from .output.destinations import registry
    dest = registry.build(config, secrets)

Adicionar um destino novo = criar ``destinations/<kind>.py`` com um
``register(...)`` no fim e adicioná-lo a ``registry._register_builtins``.
Nenhuma outra mudança (espelha o registry de collectors).
"""

from __future__ import annotations

from . import registry  # noqa: F401  (side-effect: registra os kinds built-in)

__all__ = ["registry"]
