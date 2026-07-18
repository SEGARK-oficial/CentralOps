"""Threat Intel Middleware — orquestração de cache e providers de reputação de IP.

ADR-0015 (Fase 0): este ``__init__`` reexportava ``BlacklistManager`` e
``get_blacklist_manager`` de um módulo ``.blacklist`` que NUNCA EXISTIU — nem no
working tree, nem em ponto algum do histórico do git, e nenhum dos dois símbolos
aparece em qualquer outro lugar do código. Consequência: ``import
backend.app.services.threat_intel`` levantava ``ModuleNotFoundError``, tornando o
subsistema inteiro (~1.8k linhas: 3 tiers de cache, AbuseIPDB, OTX, consensus,
rotação de chaves) INALCANÇÁVEL. Os zero consumidores não eram uma escolha de
desenho; eram a consequência.

As reexportações mortas foram removidas. Nada foi perdido: as operações de
blacklist que de fato existem vivem em ``cache.py``
(``is_blacklisted`` / ``blacklist_size`` / ``replace_blacklist``, com swap atômico
via staging + RENAME) e o download do feed em lote em
``clients/abuseipdb.py::download_blacklist``.

Guard de regressão: ``backend/tests/test_adr0015_service_importability.py``
importa todo módulo sob ``app/services`` e reprova o CI nesta classe de falha.
"""

from .service import ThreatIntelService, get_threat_intel_service

__all__ = [
    "ThreatIntelService",
    "get_threat_intel_service",
]
