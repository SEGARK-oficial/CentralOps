"""Receptor syslog nativo (W3.2) + classificação por conteúdo (W3.3).

Serviço próprio, fora da API (preserva o API-stateless da ADR-0004): escuta
UDP/TCP/TLS, parseia RFC3164/RFC5424/CEF/LEEF para JSON e escreve no MESMO
buffer de ingestão que ``POST /api/ingest/<stream>`` — o resto do pipeline
(dreno, normalize, dedupe, routing) não sabe que o evento entrou por syslog.
"""
