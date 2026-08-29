#!/usr/bin/env bash
# runner-docker-prune.sh — manutenção de disco dos runners self-hosted (LXC).
#
# POR QUÊ: runners self-hosted PERSISTEM estado entre runs (≠ GitHub-hosted
# ephemeral). Builds Cython pesados + E2E multi-shard várias vezes/dia enchem
# /var/lib/docker (camadas dangling, build cache do BuildKit, volumes/redes
# órfãos) até "no space left on device" — que para TODO o CI sem aviso.
#
# SEGURANÇA: a poda é ESCOPADA, não um `prune -a --volumes` cego:
#   - imagens dangling + containers parados + redes não-usadas (system prune -f)
#   - imagens NÃO-usadas com mais de IMAGE_AGE (default 72h) — preserva as recentes
#   - build cache do BuildKit com mais de CACHE_AGE (default 168h = 7d)
#   - SOMENTE os volumes nomeados do E2E (centralops-e2e-*) que NÃO estejam em uso
#     (docker volume rm falha se em uso → seguro)
# NÃO remove volumes arbitrários nem imagens recentes/tagueadas em uso.
#
# USO RECOMENDADO (cobertura garantida no pool): instalar como cron em CADA LXC:
#   sudo cp scripts/runner-docker-prune.sh /usr/local/bin/runner-docker-prune.sh
#   sudo chmod +x /usr/local/bin/runner-docker-prune.sh
#   # /etc/cron.d/runner-docker-prune  (diário 02:00, antes do nightly E2E):
#   0 2 * * * root /usr/local/bin/runner-docker-prune.sh >> /var/log/runner-prune.log 2>&1
# (O workflow runner-maintenance.yml roda isto on-demand/best-effort, mas só
#  alcança o runner em que o job cair — o cron de host cobre as duas máquinas.)

# CALIBRAGEM (ago/2026): os defaults antigos eram só por IDADE (72h/7d), o que
# não limita NADA quando o disco é pequeno — 7 dias de cache do BuildKit não cabem
# em 50GB, por mais novo que o cache seja. O que torna o consumo previsível é o
# teto por TAMANHO (CACHE_KEEP), com a idade servindo só de desempate. Referência
# medida: imagem da API 1.63GB, node:20 1.59GB, um run limpo do CE ocupa ~23-26GB
# e o EE ~16-19GB; como os dois repos caem no mesmo pool, os working sets somam.
set -uo pipefail

IMAGE_AGE="${IMAGE_AGE:-48h}"
CACHE_AGE="${CACHE_AGE:-72h}"
# Teto de tamanho do cache do BuildKit. É ESTE número que dá previsibilidade:
# sem ele o cache cresce até o disco acabar, só que mais devagar.
CACHE_KEEP="${CACHE_KEEP:-10GB}"
# Piso de alerta: abaixo disto o script grita, porque o próximo job vai recusar
# rodar (ver o preflight nos workflows) e é melhor saber pelo cron às 02:00 do
# que por um CI vermelho às 09:00.
MIN_FREE_GB="${MIN_FREE_GB:-12}"

log() { printf '[%s] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"; }

log "==== disco ANTES ===="
df -h / 2>/dev/null || true
docker system df 2>/dev/null || true

log "removendo volumes E2E órfãos (só os não-usados)…"
for v in centralops-e2e-postgres-data centralops-e2e-redis-data centralops-e2e-app-data; do
  docker volume rm "$v" >/dev/null 2>&1 && log "  volume removido: $v" || true
done

log "system prune (dangling images + containers parados + redes não-usadas)…"
docker system prune -f >/dev/null 2>&1 || true

log "image prune (imagens não-usadas > ${IMAGE_AGE})…"
docker image prune -a -f --filter "until=${IMAGE_AGE}" >/dev/null 2>&1 || true

log "buildx cache prune (idade > ${CACHE_AGE})…"
docker buildx prune -f --filter "until=${CACHE_AGE}" >/dev/null 2>&1 || true

log "buildx cache prune (teto de tamanho: ${CACHE_KEEP})…"
docker buildx prune -f --keep-storage="${CACHE_KEEP}" >/dev/null 2>&1 || true

log "==== disco DEPOIS ===="
df -h / 2>/dev/null || true
docker system df 2>/dev/null || true

# Alerta operacional: a poda rodou e MESMO ASSIM o disco está no vermelho. Isso
# não é higiene, é capacidade — e o CI vai começar a recusar jobs. Sai com código
# 1 para que o cron/monitoramento consiga distinguir "limpei" de "não coube".
FREE_GB="$(df -BG --output=avail / | tail -1 | tr -dc '0-9')"
if [ "${FREE_GB:-0}" -lt "$MIN_FREE_GB" ]; then
  log "ALERTA: $(hostname) com ${FREE_GB}GB livres, abaixo do piso de ${MIN_FREE_GB}GB."
  log "A poda não resolve: esta máquina precisa de mais disco."
  exit 1
fi
log "manutenção concluída — ${FREE_GB}GB livres."
