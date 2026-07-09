"""CentralOps Collector Multi-Tenant.

Subsistema assíncrono (Celery + Redis + aiohttp) que coleta logs de APIs
de vendors de segurança de terceiros (Sophos, Microsoft Defender,
NinjaOne, …), enriquece com ``customer_id`` e despacha para o Wazuh via
Syslog TCP/TLS (RFC 5424) ou JSONL.

Arquitetura stateless (RNF01): toda a inteligência de estado (cursor,
token cache, dedupe, rate window, domain semaphore) vive em Redis; o
Postgres mantém a fonte da verdade do cursor (``collection_state``).

Entry points:
- ``celery -A backend.app.collectors.celery_app worker …``
- ``celery -A backend.app.collectors.celery_app beat …``
- ``python -m backend.app.collectors.cli smoke --integration <id> --stream alerts``
"""
