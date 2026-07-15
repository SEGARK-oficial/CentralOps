---
sidebar_position: 1
title: Deploy com Docker Compose
description: Suba o CentralOps em um único host com Docker Compose — API, workers, banco e frontend em minutos.
---

# Deploy com Docker Compose

O jeito mais rápido de rodar o CentralOps é com **Docker Compose**. Ele sobe tudo o que a
plataforma precisa — API, workers de coleta (Celery), agendador, Redis, Postgres e o
frontend (Nginx) — em um único host. Ideal para **avaliação, desenvolvimento e produção
em single-host**.

:::tip[Quando usar]

- **Docker Compose** (esta página): 1 servidor, começar rápido.
- **[Kubernetes / Helm](./kubernetes.md)**: produção em escala, multi-node, HA.

:::

## Pré-requisitos

- **Docker** 24+ e o plugin **Docker Compose** v2 (`docker compose version`).
- ~2 vCPU / 4 GB de RAM para começar.
- Portas livres no host: **3000** (HTTP) e **3443** (HTTPS).

## Passo a passo

### 1. Obtenha o código

```bash
git clone https://github.com/SEGARK-oficial/CentralOps.git
cd CentralOps
```

### 2. Configure o ambiente

O `docker compose` lê o `.env` **do diretório do arquivo compose** (`compose/`) — não da
raiz do repositório. Copie o exemplo para lá e ajuste os segredos:

```bash
cp compose/.env.example compose/.env
```

Defina em `compose/.env` (o compose **recusa subir** sem os dois primeiros):

- **`POSTGRES_PASSWORD`** — senha do Postgres (obrigatória).
- **`REDIS_PASSWORD`** — senha do Redis; o AUTH do Redis é sempre exigido (obrigatória).
- **`APP_MASTER_KEY`** — chave mestra de criptografia (≥ 32 caracteres). Deixe **em
  branco** para o container **gerar uma na primeira subida** (persistida em
  `/app/data/app_master_key` — guarde esse arquivo), ou defina a sua.

Gere segredos fortes:

```bash
openssl rand -base64 24   # POSTGRES_PASSWORD e REDIS_PASSWORD
openssl rand -hex 32      # APP_MASTER_KEY
```

Para produção com HTTPS, mantenha `APP_ENV=production` (padrão do exemplo — força
`SESSION_SECURE_COOKIE=true`). A referência completa está em
**[Configuração](./configuration.md)**.

### 3. Suba a stack

A partir da **raiz do repositório**, apontando para o arquivo em `compose/`:

```bash
docker compose -f compose/docker-compose.yml up --build -d
```

O primeiro build compila o backend e o frontend — as próximas subidas são quase
instantâneas.

### 4. Acesse

- **HTTP:** `http://localhost:3000`
- **HTTPS:** `https://localhost:3443`

Se `certs/tls.crt` e `certs/tls.key` não existirem, o container **gera um certificado
autoassinado** automaticamente (o navegador vai avisar; aceite para testar). Para um
certificado próprio, monte os arquivos em `certs/`.

### 5. Verifique a saúde

A prontidão real (Postgres + Redis) é verificada pelo **healthcheck do container** — o
`/readyz` da API não é publicado na borda. Confira o estado dos serviços:

```bash
docker compose -f compose/docker-compose.yml ps
```

Os serviços `centralops` (API) e `frontend` devem aparecer como **`healthy`**. Para ler
o JSON de prontidão direto na API:

```bash
docker compose -f compose/docker-compose.yml exec centralops \
  curl -fsS http://127.0.0.1:8000/readyz
# {"status":"ready","checks":{"db":"ok","redis":"ok"}}
```

Com tudo `healthy`, siga para o
**[Primeiro Login](../getting-started/first-login.md)** para criar a conta de
administrador.

## Rodar imagens prontas (sem build)

As imagens oficiais são publicadas no GitHub Container Registry a cada release:
`ghcr.io/segark-oficial/centralops` (API) e `ghcr.io/segark-oficial/centralops-frontend`
(frontend). Para subir **sem compilar localmente**, aponte o compose para elas em
`compose/.env`:

```dotenv
IMAGE_NAME=ghcr.io/segark-oficial/centralops
IMAGE_TAG=v1.0.0   # fixe uma tag de release; evite `latest` em produção
```

E suba puxando as imagens em vez de buildar:

```bash
docker compose -f compose/docker-compose.yml pull
docker compose -f compose/docker-compose.yml up -d
```

:::note

A stack precisa de vários serviços (API, frontend, workers, Postgres, Redis) — não há
imagem única que rode tudo em um só container. O `compose/docker-compose.yml` é o que
orquestra o conjunto, seja buildando (passo 3) ou puxando as imagens prontas.

:::

## Operação básica

Rodando da raiz do repositório (todos os comandos apontam para `compose/docker-compose.yml`):

| Ação | Comando |
|---|---|
| Ver logs da API | `docker compose -f compose/docker-compose.yml logs -f centralops` |
| Ver logs do frontend | `docker compose -f compose/docker-compose.yml logs -f frontend` |
| Parar | `docker compose -f compose/docker-compose.yml down` |
| Atualizar imagens | `docker compose -f compose/docker-compose.yml pull && docker compose -f compose/docker-compose.yml up -d` |
| Backup do banco | `docker compose -f compose/docker-compose.yml exec postgres pg_dump -U centralops centralops > backup.sql` |

## Próximos passos

- **[Configuração](./configuration.md)** — todas as variáveis de ambiente.
- **[Primeiro Login](../getting-started/first-login.md)** — criar o admin e a equipe.
- **[Quickstart](../getting-started/quickstart.md)** — conectar a primeira fonte.
