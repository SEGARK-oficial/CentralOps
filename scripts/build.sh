#!/bin/bash

# Script de build otimizado para aproveitamento de cache Docker

set -euo pipefail

IMAGE_NAME="${IMAGE_NAME:-centralops}"
DOCKERFILE="compose/Dockerfile"

build_main_image() {
  docker build \
    --target final \
    --cache-from "${IMAGE_NAME}:latest" \
    --cache-from "${IMAGE_NAME}:frontend-deps" \
    --cache-from "${IMAGE_NAME}:backend-deps" \
    -t "${IMAGE_NAME}:latest" \
    -f "$DOCKERFILE" \
    .
}

echo "Iniciando build otimizado com cache..."

build_main_image
echo "Build concluido com sucesso."

echo "Criando tags auxiliares de cache..."

docker build \
  --target frontend-deps \
  -t "${IMAGE_NAME}:frontend-deps" \
  -f "$DOCKERFILE" \
  . || echo "Cache do frontend nao criado"

docker build \
  --target backend-deps \
  -t "${IMAGE_NAME}:backend-deps" \
  -f "$DOCKERFILE" \
  . || echo "Cache do backend nao criado"

echo "Build completo finalizado."
