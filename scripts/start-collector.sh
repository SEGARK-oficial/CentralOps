#!/bin/sh
# ============================================================================
# Entrypoint dos containers de Collector (workers, beat, dispatcher).
#
# Diferente do ``start.sh`` do servico principal, este script NAO sobe nginx
# nem uvicorn — apenas garante que ``APP_MASTER_KEY`` esteja disponivel
# (necessaria para descriptografar secrets da tabela ``integrations``) e
# depois faz ``exec`` do ``command:`` recebido (geralmente ``celery worker``
# ou ``celery beat``).
#
# A chave pode chegar de 3 formas, em ordem de precedencia:
# 1. Variavel de ambiente ``APP_MASTER_KEY`` (explicita no .env).
# 2. Arquivo persistido em ``APP_MASTER_KEY_FILE`` (default:
# /app/data/app_master_key). O servico principal ``centralops`` gera
# essa chave na primeira subida — por isso os workers usam
# ``depends_on: centralops`` e montam o mesmo volume ``app-data``.
# 3. Falha fatal com mensagem clara se ambos ausentes.
#
# Aguarda ate ~60s pela chave, dando tempo do ``centralops`` inicializar
# e criar o arquivo na primeira subida da stack.
# ============================================================================

set -eu

APP_MASTER_KEY_FILE="${APP_MASTER_KEY_FILE:-/app/data/app_master_key}"

if [ -z "${APP_MASTER_KEY:-}" ]; then
    # em PRODUÇÃO a chave vem do env/secret do
    # orquestrador — NÃO esperamos o arquivo escrito pelo 'centralops' (esse
    # acoplamento por volume compartilhado não funciona cross-node/k8s nem sob
    # non-root, e atrasa o boot). dev/test mantém a espera pelo arquivo.
    if [ "${APP_ENV:-production}" = "production" ]; then
        echo "ERROR: APP_MASTER_KEY não definida. Em produção ela é OBRIGATÓRIA via" >&2
        echo "       env/secret do orquestrador  — a MESMA usada pela" >&2
        echo "       API (senão os workers não decifram os segredos). Abortando." >&2
        exit 1
    fi
    echo "start-collector: APP_MASTER_KEY ausente; aguardando ${APP_MASTER_KEY_FILE} (dev/test)..."
    tries=0
    while [ ! -s "${APP_MASTER_KEY_FILE}" ] && [ "${tries}" -lt 60 ]; do
        sleep 1
        tries=$((tries + 1))
    done

    if [ ! -s "${APP_MASTER_KEY_FILE}" ]; then
        echo "ERROR: APP_MASTER_KEY nao configurado e ${APP_MASTER_KEY_FILE}" >&2
        echo "       nao foi encontrado apos 60s de espera." >&2
        echo "       Opcoes:" >&2
        echo "         - Defina APP_MASTER_KEY no .env (min 32 chars), OU" >&2
        echo "         - Garanta que o servico 'centralops' esteja rodando" >&2
        echo "           e montando o volume 'app-data' em /app/data." >&2
        exit 1
    fi

    APP_MASTER_KEY=$(tr -d '\r\n' < "${APP_MASTER_KEY_FILE}")
    if [ "${#APP_MASTER_KEY}" -lt 32 ]; then
        echo "ERROR: APP_MASTER_KEY persistido em ${APP_MASTER_KEY_FILE} e invalido" >&2
        echo "       (${#APP_MASTER_KEY} caracteres; minimo 32)." >&2
        exit 1
    fi

    export APP_MASTER_KEY
    echo "start-collector: APP_MASTER_KEY carregado de ${APP_MASTER_KEY_FILE}"
fi

# Métricas: instrumentação é OTel-native (OTLP-push) — ver app/collectors/
# otel_metrics.py. Não há mais PROMETHEUS_MULTIPROC_DIR nem /metrics por
# processo . O endpoint Prometheus de compat, se desejado, é
# exposto pelo OTel Collector — ver compose/otel-collector-config.yaml.

# ── Garante o schema via SELF-INIT idempotente  ────────
# Antes este worker ESPERAVA o arquivo /app/data/.db_ready escrito pela API —
# acoplamento por filesystem que não funciona cross-node (k8s) e travava
# réplicas/multi-node. Agora cada container GARANTE o schema ele mesmo:
# initialize_database() faz _wait_for_db() (absorve rede/DNS atrasados do
# orquestrador) + DDL idempotente sob pg_advisory_lock — concorrência-segura
# entre todos os serviços que sobem juntos (verificado: 2+ inits concorrentes
# sem corrida). Qualquer container pode bootstrapar; sem ordering, sem volume
# compartilhado. O Beat importa beat_schedule depois disto, com a tabela já
# existente. Falha aqui (DB inalcançável após o backoff) aborta o worker — o
# restart:unless-stopped do compose re-tenta.
echo "start-collector: migração de schema (python -m app.db.migrate)..."
python -m app.db.migrate || { echo "start-collector: migração FALHOU — abortando." >&2; exit 1; }
echo "start-collector: schema pronto."

# Log amigavel antes de handoff para o Celery.
echo "start-collector: SERVICE_ROLE=${SERVICE_ROLE:-unset} PID=$$ executando: $*"
exec "$@"
