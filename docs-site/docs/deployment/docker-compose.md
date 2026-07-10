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

:::tip Quando usar
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

Copie o exemplo e defina os valores obrigatórios:

```bash
cp .env.example .env
```

No mínimo, defina no `.env`:

- **`POSTGRES_PASSWORD`** — senha do banco (obrigatória; sem ela o compose recusa subir).
- **`APP_MASTER_KEY`** — chave mestra de criptografia (≥ 32 caracteres). Se você deixar
  em branco, o container **gera uma na primeira subida** e a persiste em
  `/app/data/app_master_key` — guarde esse arquivo.

Para produção com HTTPS, mantenha `APP_ENV=production` e `SESSION_SECURE_COOKIE=true`
(padrão do exemplo). A referência completa está em **[Configuração](./configuration.md)**.

### 3. Suba a stack

```bash
cd compose
docker compose up --build -d
```

O primeiro build baixa as imagens e compila o frontend — os próximos são quase
instantâneos.

### 4. Acesse

- **HTTP:** `http://localhost:3000`
- **HTTPS:** `https://localhost:3443`

Se `certs/tls.crt` e `certs/tls.key` não existirem, o container **gera um certificado
autoassinado** automaticamente (o navegador vai avisar; aceite para testar). Para um
certificado próprio, monte os arquivos em `certs/`.

### 5. Verifique a saúde

```bash
curl -fsS http://localhost:3000/readyz
# {"status":"ready","checks":{"db":"ok","redis":"ok"}}
```

`ready` significa que API, banco e Redis estão no ar. Agora siga para o
**[Primeiro Login](../getting-started/first-login.md)** para criar a conta de
administrador.

## Imagem única (sem clonar o repo)

As imagens são publicadas no GitHub Container Registry. Para rodar sem clonar:

```bash
docker run -d --name centralops \
  -p 3000:80 -p 3443:443 \
  -e APP_MASTER_KEY="defina-uma-chave-de-pelo-menos-32-caracteres" \
  -e ENABLE_HTTPS=1 \
  -v centralops-data:/app/data \
  ghcr.io/segark-oficial/centralops:latest
```

Configurações podem vir por `--env-file .env` ou por um arquivo `/app/.env` montado no
container. Fixe uma **tag imutável** (ex.: `v1.0.0`) em produção — evite `latest`.

## Operação básica

| Ação | Comando |
|---|---|
| Ver logs | `docker compose logs -f api` |
| Parar | `docker compose down` |
| Atualizar versão | `docker compose pull && docker compose up -d` |
| Backup do banco | `docker compose exec postgres pg_dump -U centralops centralops > backup.sql` |

## Próximos passos

- **[Configuração](./configuration.md)** — todas as variáveis de ambiente.
- **[Primeiro Login](../getting-started/first-login.md)** — criar o admin e a equipe.
- **[Quickstart](../getting-started/quickstart.md)** — conectar a primeira fonte.
