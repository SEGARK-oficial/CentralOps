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

## Data-plane: broker Kafka gerenciado

Em produção, aponte o data-plane para um broker gerenciado (MSK, Confluent Cloud, Redpanda Cloud) em vez do `devBroker` de testes:

```yaml
dataPlane:
  kafka:
    bootstrapServers: "b-1.seu-broker.internal:9096,b-2.seu-broker.internal:9096"
    securityProtocol: SASL_SSL
    sasl:
      mechanism: SCRAM-SHA-512
      username: centralops
      password: ""            # prefira secrets.existingSecret
    tls:
      caFile: ""              # caminho da CA privada montada no pod; vazio = CAs do sistema
      verify: true
    replicationFactor: 3      # com minInsyncReplicas derivado (2), um broker pode cair
    partitions: 8
```

O tópico de entrega é criado na primeira subida com essas partições, réplicas e `min.insync.replicas`. Aumentar partições depois exige repartição manual no broker.

## PgBouncer (recomendado em produção)

Cada processo da aplicação abre até 40 conexões com o Postgres (`DB_POOL_SIZE` + `DB_MAX_OVERFLOW`). Com vários workers, a soma passa facilmente do `max_connections` padrão do Postgres (100). Ligue o PgBouncer do chart:

```yaml
pgbouncer:
  enabled: true
  serverTlsSslmode: require   # TLS do PgBouncer até o Postgres gerenciado
```

- Com o Secret do chart, `secrets.databaseUrl` continua sendo a URL **direta** do Postgres. O chart aponta a aplicação para o PgBouncer e guarda a URL direta em `DATABASE_MIGRATION_URL`.
- Com `secrets.existingSecret`, o Secret precisa das duas chaves: `DATABASE_URL` (host `<release>-centralops-pgbouncer`, porta 5432, sem `sslmode`) e `DATABASE_MIGRATION_URL` (Postgres direto).
- A migração de boot sempre vai direto ao Postgres: ela usa um lock de sessão que o pooling por transação não suporta.
- A senha do banco não pode depender de URL-encoding, porque o PgBouncer a lê como está escrita na URL.

## Alertas com Prometheus Operator

Se o cluster usa o Prometheus Operator, o chart cria um `PrometheusRule` com as regras do data-plane (DLQ que não grava, lag, breaker aberto, DLQ crescendo, coleta atrasada, evicção do Redis):

```yaml
monitoring:
  prometheusRule:
    enabled: true
    labels:
      release: kube-prometheus-stack   # o rótulo que o seu Prometheus seleciona
```

## Recursos que o chart entrega

- **API + frontend + workers Celery + beat** como Deployments separados (release
  desacoplado da UI). Há um worker por papel: coleta prioritária, coleta em lote,
  manutenção, busca federada e scheduled queries (`query`) e reenvio da fila de
  reenvio (`dispatcher`). Quando o pod é
  encerrado, cada worker tem até 15 minutos e meio para terminar a tarefa em
  andamento (2 minutos na coleta prioritária).
- **HPA** para escalar a API/workers por CPU.
- **PodDisruptionBudget** (ligado por padrão nos workers e no consumidor do
  data-plane) para não derrubar todas as réplicas em manutenção, e réplicas do
  mesmo papel espalhadas por nós quando possível.
- **NetworkPolicies** restringindo o tráfego entre componentes.
- **License keyring** (ConfigMap com chaves **públicas**) para verificar a licença
  Enterprise **offline** — ver **[Edições & Upgrade](../editions/community-vs-enterprise.md)**.

- **Data-plane**: `raw-processor` (processa) e `kafka-dispatcher` (entrega), que
  escalam por réplicas até o número de partições dos tópicos `raw` e `deliver`. O
  evento só passa por eles: Celery e Redis ficam no controle (agenda, tokens,
  disjuntor).

## Dimensionamento

Números medidos no gate de carga do repositório (`scripts/loadtest/dataplane_gate.py`),
com tudo num nó só. Use como ponto de partida e meça no seu hardware.

| Papel | Medido | Escala com |
| --- | --- | --- |
| API (ingestão push) | 30–34 mil eventos/s por processo | réplicas da API |
| `raw-processor` | 6–6,6 mil eventos/s por processo num notebook recente; 1,1–1,5 mil por núcleo num Xeon de 2016 | réplicas, até as partições do `raw` (16) |
| `kafka-dispatcher` | 12 mil eventos/s para um destino (4 sub-partições) | réplicas, até as partições do `deliver` |
| Destino com **Entrega em ordem** | ~6 mil eventos/s | não escala: uma partição, um lote por vez |

Um destino fora do ar por 30–40 s não gerou nenhuma linha no Postgres: os lotes
esperaram no tópico de estacionamento e foram entregues quando ele voltou. A meta
de 10 mil eventos/s sustentados em 3 nós ainda não foi medida num cluster.

## Próximos passos

- **[Configuração](./configuration.md)** — todas as variáveis de ambiente.
- **[Primeiro Login](../getting-started/first-login.md)** — criar o admin.
- **[Upgrade para Enterprise](../editions/upgrade.md)** — habilitar os recursos MSSP.
