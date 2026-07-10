---
sidebar_position: 3
title: Configuração (variáveis de ambiente)
description: Referência das variáveis de ambiente do CentralOps — o que é obrigatório, o que tem padrão seguro e o que ajustar por ambiente.
---

# Configuração

O CentralOps é configurado por **variáveis de ambiente**. Em Docker Compose elas vêm do
arquivo `.env`; em Kubernetes, de um `Secret`/`ConfigMap`. Esta página é a referência —
comece pelo `.env.example`, que traz todas com comentários.

:::info Mínimo obrigatório
Para subir em produção você precisa apenas de **`POSTGRES_PASSWORD`** e, idealmente, de
uma **`APP_MASTER_KEY`** definida por você. O resto tem padrão seguro.
:::

## Essenciais

| Variável | Padrão | Descrição |
|---|---|---|
| `APP_MASTER_KEY` | *(gerada e persistida em `/app/data/app_master_key`)* | Chave mestra de criptografia dos segredos (≥ 32 caracteres). **Defina-a você** em produção e guarde com segurança — perdê-la torna os segredos ilegíveis. |
| `APP_ENV` | `production` | `production` exige HTTPS/cookie seguro; use `development` para dev local sem TLS. |
| `APP_COMPANY_NAME` | `Sua Empresa` | Nome exibido na interface. |
| `APP_COMPANY_PORTAL_NAME` | `Portal de Login` | Subtítulo da tela de login. |

## Banco de dados (Postgres)

| Variável | Padrão | Descrição |
|---|---|---|
| `POSTGRES_PASSWORD` | *(vazio — **obrigatório**)* | Senha do Postgres. Sem valor, o compose recusa subir. |
| `POSTGRES_USER` | `centralops` | Usuário do banco. |
| `POSTGRES_DB` | `centralops` | Nome do banco. |
| `DATABASE_URL` | *(derivada das vars acima)* | Sobrescreva só para usar um **Postgres externo/gerido** (RDS, Neon…) ou voltar a SQLite em dev: `sqlite:////app/data/app.db`. |

O Docker Compose sobe um **Postgres 16** com volume nomeado por padrão. Em produção
séria, prefira um Postgres gerido e aponte `DATABASE_URL` para ele.

## HTTPS e rede (Nginx)

| Variável | Padrão | Descrição |
|---|---|---|
| `ENABLE_HTTPS` | `0` | `1` habilita o Nginx com TLS. Sem certificado fornecido em `certs/`, um autoassinado é gerado. |
| `NGINX_SERVER_NAME` | `_` | Valor de `server_name` no Nginx (use seu domínio em produção, ex.: `centralops.suaempresa.com`). |

## Sessão e segurança

| Variável | Padrão | Descrição |
|---|---|---|
| `SESSION_SECURE_COOKIE` | `true` | Use `true` quando o acesso principal for HTTPS (obrigatório com `APP_ENV=production`). |
| `DEBUG_REQUESTS` | `0` | `1` grava `debug_requests.log` com as chamadas a APIs externas (diagnóstico; desligue em produção). |

## Segredos das integrações

As credenciais de cada integração são cifradas em repouso. O provedor de cifra padrão é o
**`local_fernet`** (AES derivado da `APP_MASTER_KEY`). Detalhes e rotação em
**[Segredos e chave mestra](../administration/secrets-and-master-key.md)**.

## Boas práticas

- **Fixe versões:** use uma tag imutável de imagem (ex.: `v1.0.0`) em produção, não `latest`.
- **APP_MASTER_KEY externa:** defina-a por Secret e faça backup — é a chave de tudo.
- **HTTPS sempre:** `ENABLE_HTTPS=1` + `SESSION_SECURE_COOKIE=true` + `NGINX_SERVER_NAME` com o seu domínio.
- **Postgres gerido** em produção (backup, HA e patching por conta do provedor).
