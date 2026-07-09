# CentralOps — Helm Chart

Deploy do CentralOps em Kubernetes (Modo C do [deployment guide](../../../)),
seguindo o (build-once / run-many: a mesma imagem de backend sobe como
`api` e como os workers Celery; o papel é definido pelo `command`).

> **Pré-requisitos honrados pelo chart:** a API é stateless (init de schema saiu
> do import → `python -m app.db.migrate` sob `pg_advisory_lock` roda no boot de
> cada pod), e a `APP_MASTER_KEY` vem de Secret (não mais de arquivo em volume
> compartilhado). Isso é o que destrava o k8s no (share-nothing,
> cross-node).

## Instalação

```bash
helm upgrade --install centralops ./kubernetes/helm/centralops \
  --namespace centralops --create-namespace \
  -f my-values.yaml
```

Para um ambiente de teste rápido (Postgres/Redis in-cluster):

```bash
helm upgrade --install centralops ./kubernetes/helm/centralops \
  --namespace centralops --create-namespace \
  --set devDatabase.enabled=true \
  --set config.appEnv=development
```

## Componentes

| Workload | Réplicas (default) | Observação |
|---|---|---|
| `*-api` | 2 | uvicorn puro; probes `/readyz` (readiness/startup) + `/livez` (liveness) |
| `*-frontend` | 2 | nginx-unprivileged; serve SPA + reverse-proxy `/api` |
| `*-worker-priority/bulk/maintenance/dispatcher` | 1 | filas Celery `-Q` por papel; HPA + liveness `inspect ping` |
| `*-worker-beat` | 1 | **singleton** (lock RedBeat); sem HPA, sem liveness ping |

## Sizing & Requisitos Mínimos

Números **medidos em produção** (k3s 2 vCPU / 4GB por nó, `kubectl top`, Sophos em volume baixo):

| Componente | concurrency | Uso real | request | limit | Notas |
|---|---|---|---|---|---|
| worker (priority/bulk/maintenance) | **2** | ~200–300Mi | 384Mi / 150m | **640Mi** / 800m | cada fork Celery ~150–200Mi; conc=4 estoura 512Mi |
| **worker-dispatcher** | **2** | ~554Mi | 512Mi / 150m | **1Gi** / 1000m | mais pesado (libs de sink S3/Sentinel/Kafka por fork) — OOMKilled em 640Mi |
| worker-beat | — (singleton) | ~98Mi | 128Mi / 50m | 256Mi / 300m | só agenda; nunca autoescala |
| api (uvicorn) | — | ~140Mi | 256Mi / 100m | 512Mi / 500m | |
| frontend (nginx) | — | ~5Mi | 64Mi / 50m | 128Mi / 200m | |
| postgres | — | ~30–60Mi | 256Mi / 100m | 512Mi / 1000m | `devDatabase` only; em prod use gerenciado |
| redis | — | ~205Mi | 256Mi / 100m | 768Mi / 1000m | `maxmemory 512mb` + `volatile-lru` + `auto-aof-rewrite` |

**Consumo total real ≈ 1.3GB** (app) + ~1.5GB (control-plane k3s no master).

| Perfil | CPU | RAM | Veredito |
|---|---|---|---|
| **Mínimo** | 2× 2 vCPU | **2× 4GB (8GB)** | funciona com 1 réplica/worker, conc=2, postgres/redis limitados — **pouca folga** |
| **Recomendado** | 2× 2–4 vCPU | **2× 8GB** ou **3× 4GB** | folga p/ picos, rolling updates, crescimento |
| **Ideal (MSSP)** | master dedicado + 2 workers | master 4GB + workers 2× 8GB | control-plane isolado da carga (taint) → não cai junto sob OOM |

> ### ⚠️ Lições do incidente OOM (2026-06-25) — encodadas neste chart
> 1. **NUNCA autoescale por memória.** O HPA-por-memória dos workers entrou em
>    *death-spiral* (memória é overhead fixo dos forks → nunca converge → escala
>    até o máximo → OOM → escala mais → derruba o control-plane). Este chart
>    autoescala **só por CPU** e **exclui o beat** (singleton).
> 2. **`concurrency=2`** nos nós de 4GB (conc=4 exige ~1Gi/worker e nós de 8GB).
> 3. **Postgres/Redis SEMPRE com limits.** Sem limite, o Redis cresceu o AOF até
>    6GB → OOM no boot. Mitigado: `maxmemory` + `volatile-lru` (preserva o broker)
>    + `auto-aof-rewrite`. Para mais vazão, escale **réplicas** (horizontal),
>    nunca concurrency/memória.

## Valores que você PRECISA revisar

| Caminho | Por quê |
|---|---|
| `secrets.existingSecret` | Em produção, aponte para um Secret gerenciado (ExternalSecrets/Vault/SOPS) em vez dos placeholders `CHANGE_ME*` |
| `secrets.appMasterKey` | ≥32 chars; **a MESMA** em api e workers (senão os workers não decifram segredos). O boot aborta se inválida |
| `secrets.databaseUrl` / `redisUrl` / `celery*Url` | Apontam para o Postgres/Redis reais. Sem `redisUrl` o app cai em `localhost:6379` |
| `image.digest` | Pine por `sha256:` em produção (reprodutível; evita `latest`) |
| `config.otelExporterOtlpEndpoint` | Endpoint OTLP por ambiente (vazio = sem push) |
| `config.corsOrigins` | Restrinja às origens reais (evite `*`) |
| `ingress.tls` | TLS de borda — sem isto o tráfego externo é HTTP puro |
| `networkPolicies.ingressControllerNamespace` | Restringe o ingress do frontend ao namespace do seu ingress controller |

## Postura de segurança (DevSecOps)

- **Pod Security Standards: restricted** — `runAsNonRoot`, `seccompProfile: RuntimeDefault`,
  `allowPrivilegeEscalation: false`, `capabilities: drop [ALL]`, `readOnlyRootFilesystem`
  (paths graváveis via `emptyDir`).
- **ServiceAccount dedicado** sem `automountServiceAccountToken` (nenhum workload fala com o api-server).
- **NetworkPolicies default-deny** (Ingress **e** Egress) com allow-list: DNS, frontend→api,
  api/workers→Postgres/Redis, e egress de internet 443 para os coletores (bloqueando o
  range privado do cluster → anti-movimento-lateral).
- **PodDisruptionBudget** para api e frontend.

## Produção: o que NÃO vem no chart (use serviço gerenciado)

Postgres e Redis. O `devDatabase` é apenas dev/teste (single-replica, sem HA/backup).
Em produção use RDS/Cloud SQL/ElastiCache e aponte os `secrets`.

## Validação local

```bash
helm lint kubernetes/helm/centralops
helm template centralops kubernetes/helm/centralops | kubectl apply --dry-run=client -f -
```
