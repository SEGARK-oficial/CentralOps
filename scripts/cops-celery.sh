#!/bin/sh
# ============================================================================
# Wrapper para ``celery`` dentro dos containers do Collector.
#
# Uso esperado a partir do host:
#
#   docker compose exec collector-worker-priority cops-celery inspect active
#   docker compose exec collector-worker-priority cops-celery inspect registered
#
# Por que existe: ``docker compose exec`` inicia um novo processo sem passar
# pelo ``ENTRYPOINT`` do container, entao o ``start-collector.sh`` nao e
# executado e ``APP_MASTER_KEY`` fica ausente — a aplicacao falha na
# validacao do pydantic-settings antes mesmo de inicializar o Celery.
#
# Este wrapper delega ao ``start-collector.sh`` (que carrega a chave do
# volume persistido) e depois invoca o Celery com o app correto.
# ============================================================================

exec /usr/local/bin/start-collector.sh \
    celery -A app.collectors.celery_app "$@"
