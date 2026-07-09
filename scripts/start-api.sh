#!/bin/sh
# ============================================================================
# Entrypoint da API  — uvicorn PURO, sem nginx.
#
# Antes, o serviço subia nginx + uvicorn no mesmo container (start.sh). Com o
# P0, o nginx passou para a imagem `frontend`; aqui roda só o app-server.
# uvicorn é o processo PID 1 (via ``exec``) → recebe SIGTERM diretamente e
# drena as requisições em voo nativamente (O4), sem precisar de supervisor.
# Bind 0.0.0.0:8000 para ser alcançável pelo serviço `frontend` na rede do
# compose (antes era 127.0.0.1, acessível só pelo nginx co-locado).
# ============================================================================
set -eu

APP_MASTER_KEY_FILE="${APP_MASTER_KEY_FILE:-/app/data/app_master_key}"

# Master key — precedência: env (OBRIGATÓRIO em prod, O6) > arquivo no
# volume (SÓ dev/test). Em PRODUÇÃO a chave gerada-em-arquivo é insegura e saiu
# do default: (1) se o volume reseta, perde-se a chave e portanto TODOS os
# segredos cifrados (integrations/destinations) ficam irrecuperáveis; (2) sob
# non-root (O5) a API não escreve o arquivo; (3) sob réplicas>1 cada réplica
# geraria uma chave DIFERENTE → cifra inconsistente. Logo prod EXIGE env/secret
# do orquestrador; dev/test mantém o arquivo por conveniência.
if [ -z "${APP_MASTER_KEY:-}" ]; then
    if [ "${APP_ENV:-production}" = "production" ]; then
        echo "ERROR: APP_MASTER_KEY não definida. Em produção ela é OBRIGATÓRIA via" >&2
        echo "       env/secret do orquestrador  — não geramos chave" >&2
        echo "       efêmera em arquivo (perda de chave = perda dos segredos). Abortando." >&2
        exit 1
    fi
    # dev/test: conveniência — arquivo no volume (ou gera efêmera local).
    mkdir -p "$(dirname "${APP_MASTER_KEY_FILE}")"
    if [ -s "${APP_MASTER_KEY_FILE}" ]; then
        APP_MASTER_KEY=$(tr -d '\r\n' < "${APP_MASTER_KEY_FILE}")
        echo "start-api: APP_MASTER_KEY lida de ${APP_MASTER_KEY_FILE} (dev/test)."
    else
        APP_MASTER_KEY=$(openssl rand -hex 32)
        umask 077
        printf '%s\n' "${APP_MASTER_KEY}" > "${APP_MASTER_KEY_FILE}"
        chmod 600 "${APP_MASTER_KEY_FILE}"
        echo "start-api: APP_MASTER_KEY gerada (dev/test) em ${APP_MASTER_KEY_FILE}."
    fi
    export APP_MASTER_KEY
fi

if [ "${#APP_MASTER_KEY}" -lt 32 ]; then
    echo "ERROR: APP_MASTER_KEY inválida (${#APP_MASTER_KEY} chars; mínimo 32)." >&2
    exit 1
fi

# migração de schema como ETAPA explícita (saiu do import de
# app.main). Idempotente + serializada por advisory lock no Postgres → segura
# rodar em TODAS as réplicas no boot (a 1ª aplica/carimba, as demais no-op).
# Aborta o boot se a migração falhar (não sobe a API com schema inconsistente).
echo "start-api: migração de schema (python -m app.db.migrate)..."
python -m app.db.migrate || { echo "start-api: migração FALHOU — abortando." >&2; exit 1; }

exec uvicorn app.main:app --host 0.0.0.0 --port "${API_PORT:-8000}"
