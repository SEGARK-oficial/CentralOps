#!/usr/bin/env bash
# seed-redis-e2e.sh — Popula reservoir Redis com eventos sintéticos para E2E.
#
# Chamado pelo workflow e2e.yml após o seed.ts.
# Localmente (com o stack E2E já no ar): bash scripts/seed-redis-e2e.sh
#
# O CentralOps lê samples de normalize:sample:<vendor>:<event_type>
# para executar dry-run no Mapping Editor.
# Sem esses samples, o dry-run retorna envelope vazio e o teste 02 falha.
#
# Conexão: usa o redis-cli DENTRO do container do compose (redis:7-alpine já
# traz o binário) em vez de um redis-cli no host. Dois motivos:
# 1. O serviço `redis` do compose não publica porta para o host — só é
# acessível pela rede interna do Docker (host=redis dentro da rede).
# 2. O runner ubuntu-latest do GitHub Actions não traz mais `redis-cli`
# pré-instalado (causava `exit 127` — command not found).
# Rodar via `docker compose exec` resolve ambos e usa a MESMA versão do Redis.

set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-compose/docker-compose.e2e.yml}"
REDIS_SERVICE="${REDIS_SERVICE:-redis}"
REDIS_PASSWORD="${REDIS_PASSWORD:-e2e-redis-pass}"

# Executa o redis-cli dentro do container. -T desabilita TTY (necessário em CI).
# REDISCLI_AUTH autentica sem expor a senha em texto plano nem emitir warning.
redis_cli() {
  docker compose -f "$COMPOSE_FILE" exec -T \
    -e REDISCLI_AUTH="$REDIS_PASSWORD" \
    "$REDIS_SERVICE" redis-cli "$@"
}

echo "[seed-redis] Populando reservoir para sophos/sophos.alert..."

# Os campos espelham o schema REAL de alertas do Sophos Central, que o mapping
# default sophos.alert consome: `id` (finding_info.uid), `createdAt`/`raisedAt`
# (normalized.time, required), `severity` (severity_id via value_map), `description`
# (finding_info.title), `type`. Antes usávamos `when` → normalized.time resolvia
# None e o dry-run falhava a regra required. Agora normaliza 100% (envelope OCSF OK).

# o reservoir é particionado por organization_id. O seed.ts
# persiste o orgId da org de teste em e2e/.e2e-org-id — usamos na chave Redis.
ORG_ID_FILE="${ORG_ID_FILE:-e2e/.e2e-org-id}"
ORG_ID="$(tr -dc '0-9' < "$ORG_ID_FILE" 2>/dev/null || true)"
if [ -z "${ORG_ID:-}" ]; then
  echo "[seed-redis] ERRO: $ORG_ID_FILE não encontrado/vazio. O seed.ts rodou antes?"
  exit 1
fi
KEY="normalize:sample:${ORG_ID}:sophos:sophos.alert"
echo "[seed-redis] Reservoir key (org-scoped): $KEY"

# Evento 1: alerta de malware simples
redis_cli LPUSH "$KEY" \
  '{"id":"e2e-001","type":"Event::Endpoint::Threat::Detected","description":"Malware detectado: Eicar-Test-Virus","severity":"high","createdAt":"2026-01-15T10:00:00.000Z","raisedAt":"2026-01-15T10:00:00.000Z","source":"Sophos","location":"C:\\test\\eicar.com","sha256":"275a021bbfb6489e54d471899f7db9d1663fc695ec2fe2a2c4538aabf651fd0f"}' \
  > /dev/null

# Evento 2: alerta de ransomware
redis_cli LPUSH "$KEY" \
  '{"id":"e2e-002","type":"Event::Endpoint::Ransomware::Detected","description":"Ransomware detectado: Ransomware.Gen","severity":"critical","createdAt":"2026-01-15T11:00:00.000Z","raisedAt":"2026-01-15T11:00:00.000Z","source":"Sophos","location":"D:\\shares\\documents"}' \
  > /dev/null

# Evento 3: alerta de acesso suspeito
redis_cli LPUSH "$KEY" \
  '{"id":"e2e-003","type":"Event::Endpoint::SuspiciousActivity","description":"Atividade suspeita: login incomum","severity":"medium","createdAt":"2026-01-15T12:00:00.000Z","raisedAt":"2026-01-15T12:00:00.000Z","source":"Sophos","username":"suspicious_user"}' \
  > /dev/null

# Confirmar que os eventos foram inseridos (extrai só dígitos — robusto a CR/espaços).
COUNT=$(redis_cli LLEN "$KEY" 2>/dev/null | tr -dc '0-9')
COUNT="${COUNT:-0}"
echo "[seed-redis] Reservoir $KEY: $COUNT eventos"

if [ "$COUNT" -lt 1 ]; then
  echo "[seed-redis] ERRO: Nenhum evento foi inserido no reservoir."
  exit 1
fi

echo "[seed-redis] Seed Redis concluído."
