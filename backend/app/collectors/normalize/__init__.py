"""Pacote de normalização.

Componentes:

- ``envelope``: substitui ``BaseCollector.enrich`` e produz o
  envelope canônico ``{_centralops, normalized, raw}``.
- ``engine``: interpretador da DSL de mapping (JMESPath +
  operadores nomeados).
- ``operators``: ``type_cast``, ``value_map``, ``default``,
  ``required``.
- ``drift``: detecção de campos desconhecidos por sampling.
- ``sample_reservoir``: ring buffer Redis para dry-run.
- ``ocsf``: constantes e validators do schema
  unificado OCSF v1.3.0.
"""

from __future__ import annotations

# RF3.1 / RF3.2 — versão do envelope `{_centralops, normalized, raw}`.
# Bump major em mudança incompatível; consumidores Wazuh leem via
# ``_centralops.schema_version`` para roteamento de decoder.
#
# 1.1.0: adiciona ``organization_id`` (id interno do tenant) e
# expõe ``severity_id`` em ``_centralops`` como labels de roteamento de 1ª
# classe. Bump MINOR — mudança aditiva e retrocompatível (consumidores
# ignoram chaves desconhecidas); não altera ``normalized``/``raw`` nem o PRI.
ENVELOPE_SCHEMA_VERSION = "1.1.0"

# Versão do schema OCSF que o bloco ``normalized`` segue. Os mappings
# default emitem classes que existem em 1.8.0 (a versão estável atual, 18/03/2026)
# e o validador estrutural valida contra o manifest 1.8.0 vendorado — por isso o
# envelope declara 1.8.0 (era 1.3.0, impreciso). Deve casar com
# settings.OCSF_VALIDATION_VERSION (um teste garante).
OCSF_VERSION = "1.8.0"

__all__ = ["ENVELOPE_SCHEMA_VERSION", "OCSF_VERSION"]
