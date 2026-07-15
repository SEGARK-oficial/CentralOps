---
sidebar_position: 2
title: Deploy com Kubernetes (Helm)
description: Rode o CentralOps em produção multi-node com o Helm chart — API, workers, HPA, PDB e NetworkPolicies.
---

# Deploy com Kubernetes (Helm)

Para **produção em escala** (múltiplos nós, alta disponibilidade, autoscaling), o
CentralOps traz um **Helm chart** que provisiona a API, o frontend, os workers de coleta
(Celery), o agendador, além de **HPA** (autoscaling), **PDB** (disponibilidade durante
manutenção) e **NetworkPolicies**. É o mesmo artefato Docker do
[Docker Compose](./docker-compose.md) — só a orquestração muda.

:::tip[Quando usar]
Escolha Kubernetes quando precisar de **escala horizontal, resiliência multi-node ou
integração com um cluster existente**. Para 1 servidor, o [Docker Compose](./docker-compose.md)
é mais simples.
:::

## Pré-requisitos

- Um cluster **Kubernetes** 1.27+ com acesso `kubectl`.
- **Helm** 3.9+.
- Um **Postgres** e um **Redis** — geridos (recomendado) ou dentro do cluster.

## Passo a passo

### 1. Prepare os valores

Crie um `values.override.yaml` com o mínimo para o seu ambiente:

```yaml
image:
  repository: ghcr.io/segark-oficial/centralops
  tag: "v1.0.0"          # fixe uma tag imutável em produção

frontendImage:
  repository: ghcr.io/segark-oficial/centralops-frontend
  tag: "v1.0.0"

# Segredos essenciais (use um Secret existente em produção)
config:
  appEnv: production
  # APP_MASTER_KEY e a URL do banco devem vir de um Secret — ver "Segredos" abaixo.

postgres:
  # aponte para o seu Postgres gerido
  host: postgres.exemplo.internal
  database: centralops

autoscaling:
  enabled: true
  minReplicas: 2
  maxReplicas: 6
```

### 2. Instale

```bash
kubectl create namespace centralops
helm install centralops kubernetes/helm/centralops \
  --namespace centralops \
  -f values.override.yaml
```

### 3. Verifique

```bash
kubectl -n centralops rollout status deploy/centralops-api
kubectl -n centralops get pods
kubectl -n centralops port-forward svc/centralops-api 8080:80 &
curl -fsS http://localhost:8080/readyz
```

## Segredos essenciais

Nunca coloque segredos no `values.yaml`. Crie um Secret e referencie-o:

```bash
kubectl -n centralops create secret generic centralops-secrets \
  --from-literal=APP_MASTER_KEY="<chave-de-32+-caracteres>" \
  --from-literal=DATABASE_URL="postgresql+psycopg2://user:senha@host:5432/centralops"
```

| Chave | Para que serve |
|---|---|
| `APP_MASTER_KEY` | Chave mestra de criptografia dos segredos de integração (≥ 32 chars). |
| `DATABASE_URL` | Conexão com o Postgres de produção. |
| `POSTGRES_PASSWORD` | Se usar o Postgres in-cluster do chart. |

A referência completa de variáveis está em **[Configuração](./configuration.md)**.

## Recursos que o chart entrega

- **API + frontend + workers Celery + beat** como Deployments separados (release
  desacoplado da UI).
- **HPA** para escalar a API/workers por CPU.
- **PodDisruptionBudget** para não derrubar todas as réplicas em manutenção.
- **NetworkPolicies** restringindo o tráfego entre componentes.
- **License keyring** (ConfigMap com chaves **públicas**) para verificar a licença
  Enterprise **offline** — ver **[Edições & Upgrade](../editions/community-vs-enterprise.md)**.

## Próximos passos

- **[Configuração](./configuration.md)** — todas as variáveis de ambiente.
- **[Primeiro Login](../getting-started/first-login.md)** — criar o admin.
- **[Upgrade para Enterprise](../editions/upgrade.md)** — habilitar os recursos MSSP.
