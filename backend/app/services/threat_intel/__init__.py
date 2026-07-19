"""Threat Intel Middleware — orquestração de cache e providers de reputação de IP.

⚠️  SUBSISTEMA DORMENTE — NÃO ESTÁ LIGADO A NADA.

Nenhum router expõe este pacote, nenhuma UI o consome, e nada no pipeline de
ingestão o chama. Isso é ESTADO CONHECIDO, não bug: o código foi escrito com um
modelo de acesso — lookup por IP, on-demand, contra API externa — que é
inviável no hot path. A 15k EPS seriam 15k requests/s ao AbuseIPDB, cuja cota
free é de 1.000 por DIA.

Mantido deliberadamente (ADR-0015) em vez de deletado: são ~1.8k linhas com 4
tabelas e FKs, e o desenho de enriquecimento previsto reaproveita duas peças
prontas — ``clients/abuseipdb.py::download_blacklist`` (o feed em LOTE, que é a
chamada certa: 1 request/hora consome 24 das 1.000 cotas diárias) e
``key_manager.py`` (pool de chaves cifradas com rotação LRU e cooldown).
Deletar agora significaria reescrever depois.

Se/quando for religado, o desenho correto é: feed materializado por task de
beat (o intervalo já está modelado em
``ThreatIntelConfig.blacklist_update_interval_seconds``), carregado como
estrutura in-process por worker, lookup O(1) sem I/O, fail-open, e
enriquecimento ASSIMÉTRICO — anexa campos só no hit, porque enriquecer 100% dos
eventos aumentaria o volume entregue e contradiria o metering de
``bytes_saved``.

Importabilidade é garantida por
``backend/tests/test_adr0015_service_importability.py`` (este pacote já esteve
quebrado no import por reexportar um módulo inexistente).

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
