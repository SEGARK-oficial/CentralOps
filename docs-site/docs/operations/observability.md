---
sidebar_position: 9
title: Observabilidade (OTel)
description: "As duas superfícies de observabilidade do CentralOps — a in-app (sem configuração) e a de ops/SRE via OpenTelemetry. Passo a passo para ligar o OTel, apontar para um Collector e consumir métricas, traces e logs em Prometheus/Grafana/Loki/Tempo/Datadog."
---

# Observabilidade

O CentralOps expõe observabilidade em **duas superfícies independentes**:

- **Superfície A — in-app (cliente).** Já vem pronta, **sem nenhuma configuração**. É a
  visão de operação dentro do produto (fluxo de dados, saúde do pipeline, dashboard).
- **Superfície B — ops/SRE (OpenTelemetry).** **Opt-in.** Instrumentação OTel-native
  (métricas + traces + logs) exportada via **OTLP-push**, vendor-neutro: você pluga o seu
  backend de observabilidade (Prometheus/Grafana, Loki, Tempo/Jaeger, Datadog, Zabbix…).

As duas são desacopladas: a Superfície A funciona sempre; a B você liga quando o time de
SRE quiser correlacionar tudo no stack de monitoramento da empresa.

---

## Superfície A — o que já vem pronto (sem config)

Nada a instalar. Disponível na UI e nos endpoints de saúde:

- **Operação → [Fluxo de dados](./dashboard.md)** (`/flow`) — mapa Fontes → Roteamento →
  Destinos com throughput, saúde por destino e o card de **redução de volume & custo**.
- **Normalização → [Saúde do Pipeline](./pipeline-health.md)** (`/pipeline-health`) —
  taxa de mapeamento, quarentena, conformidade OCSF.
- **Health/liveness:** `GET /readyz` (prontidão de db/redis — **não** reporta versão nem
  edição) e `GET /healthz`.

Esse breakdown rico por vendor/stream vive no **Redis** com TTL de ~3h (série de curta
duração), separado da Superfície B — por isso não depende de OTel.

---

## Superfície B — OpenTelemetry (ops/SRE)

### O que você ganha

- **Métricas OTLP-push.** Cada processo filho (worker/dispatcher/beat) **empurra** suas
  séries a cada intervalo — não há scrape. Funciona atrás de NAT/egress-only.
- **Traces distribuídos.** O fluxo inteiro de um evento — `collect → normalize → dispatch`
  → por-destino — com `traceparent` (W3C) propagado através do boundary Celery.
- **Logs correlacionados** por `trace_id`/`span_id` (toggle **separado**, `OTEL_LOGS_ENABLED`,
  porque o volume é alto).
- **Vendor-neutro.** Você exporta para **um** OTel Collector; o Collector faz o *fan-out*
  para os backends que quiser. Nenhum acoplamento a fornecedor no CentralOps.

### Passo a passo para ligar

:::danger[O endpoint é OBRIGATÓRIO quando `OTEL_ENABLED=true`]

Ligar `OTEL_ENABLED=true` **sem** um `OTEL_EXPORTER_OTLP_ENDPOINT` com scheme
(`http://` ou `https://`) faz o OTel se **autodesligar** com **um** warning:

```
OTEL_ENABLED=true mas nenhum endpoint OTLP com scheme
(OTEL_EXPORTER_OTLP_ENDPOINT vazio/sem http[s]://) — métricas OTel DESLIGADAS neste processo
```

Isso é **proposital** (fail-safe): sem endpoint absoluto, o SDK montaria uma URL relativa
`/v1/metrics` e o exporter falharia a **cada** ciclo (`No scheme supplied`), poluindo o log.
Se o SEU compose/Helm define `OTEL_EXPORTER_OTLP_ENDPOINT=` **vazio**, isso conta como
"presente porém vazio" → OTel desliga. **Sempre sete um endpoint real** ao ligar o OTel.

:::

**1) Tenha os extras OTel na imagem.** As imagens publicadas já são buildadas com
`INSTALL_OTEL=true` (default). Numa build própria mínima que passou `--build-arg
INSTALL_OTEL=false`, reinstale: `pip install -r backend/requirements-otel.txt`. Sem os
pacotes, o OTel degrada para no-op (um warning único, o pipeline segue).

**2) Suba um OTel Collector.** O repositório traz um **stack de referência** pronto em
[`compose/observability/`](https://github.com/SEGARK-oficial/CentralOps/tree/main/compose/observability)
(Collector + Prometheus + Loki + Tempo + Grafana). Ver [Consumir a telemetria](#consumir-a-telemetria-collector--backends).

**3) Sete o env dos serviços `collector-*`** (workers, dispatcher, beat). No Compose, em
`compose/.env`:

```dotenv
OTEL_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318   # OBRIGATÓRIO (com scheme)
# opcionais:
OTEL_LOGS_ENABLED=true                # liga também o sinal de LOGS (volume alto)
OTEL_TRACES_SAMPLER_RATIO=1.0         # head-sampling 0..1 (1.0 = tudo; tail-sampling no Collector)
OTEL_SERVICE_NAME=centralops-collector
```

Se o Collector estiver em **outra rede/host**, use o IP do host + a porta publicada
(ex.: `http://192.168.3.108:4318`), não o DNS de serviço.

**4) Recrie os serviços:**

```bash
docker compose -f compose/docker-compose.yml up -d
```

No **Helm**, os mesmos valores viram config do chart (`config.otelEnabled`,
`config.otelExporterOtlpEndpoint`, `config.otelLogsEnabled`).

**5) Valide** — ver [Validação](#validação).

### Variáveis de ambiente

Setadas no env dos serviços `collector-*`. Defaults em `backend/app/core/config.py`.

| Variável | Default (código) | O que faz |
|---|---|---|
| `OTEL_ENABLED` | `false` | Liga os sinais OTel. OFF ⇒ **no-op total**, zero overhead. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `""` | Endpoint OTLP/HTTP do Collector (ex.: `http://otel-collector:4318`). **Obrigatório com scheme** quando `OTEL_ENABLED=true` (ver fail-safe acima). |
| `OTEL_SERVICE_NAME` | `centralops-collector` | `service.name` no Resource (correlação no backend de ops). |
| `OTEL_METRIC_EXPORT_INTERVAL_MS` | `60000` | Intervalo do push de métricas (por processo). |
| `OTEL_TRACES_SAMPLER_RATIO` | `1.0` | Head-sampling (0..1). 1.0 emite tudo; deixe o **tail-sampling** no Collector decidir o que reter (100% de erro/DLQ, amostra o sucesso). |
| `OTEL_LOGS_ENABLED` | `false` | Toggle **separado** do sinal de LOGS. Requer `OTEL_ENABLED=true` também. |

:::note[Os defaults do `docker-compose.yml` diferem do código]

Para dev, o compose sobrescreve dois: `OTEL_METRIC_EXPORT_INTERVAL_MS=15000` (push a cada
15 s, mais responsivo) e `OTEL_TRACES_SAMPLER_RATIO=0` (não emite trace por padrão — ligue
subindo o ratio). Ajuste no seu `compose/.env` conforme a necessidade.

:::

### Modelo push — o que saber (gotchas)

- **Descarte silencioso.** Se o Collector (`:4318`) estiver fora do ar, o SDK **descarta**
  métricas/traces silenciosamente. O export é assíncrono — **não quebra a coleta**, e o log
  `OTel ... ativo` aparece mesmo assim (só confirma que a instrumentação subiu, não que o
  Collector está recebendo).
- **Não existe a série `up`.** No modelo push não há scrape → não há `up{job=...}`. Use
  **`collector_up`** (heartbeat re-exportado a cada ciclo, com label `role` =
  `worker`/`dispatcher`/`beat`) ou `target_info` como sinal de *liveness* em dashboards e
  alertas. É também como você acompanha a **saúde do Beat** (`collector_up{role="beat"}`).
- **Janela do `rate()`.** Com push a cada 15–60 s, use `rate(...[5m])` — `rate(...[1m])`
  pode retornar zero (menos de 2 amostras na janela).
- **Prometheus precisa das flags de remote-write** (ver abaixo) — sem elas, recusa o push.

---

## Consumir a telemetria (Collector → backends)

O CentralOps empurra **tudo** via OTLP para **um** Collector, que faz o *fan-out*:

```
collector-* (OTLP/HTTP :4318) → otel-collector → fan-out:
  métricas : prometheusremotewrite → Prometheus  :9090/api/v1/write
  logs     : otlphttp/loki         → Loki         :3100/otlp
  traces   : tail_sampling → batch → Tempo/Jaeger/Datadog
```

### Stack de referência (pronto para subir)

O repo traz Collector + Prometheus + Loki + Tempo + Grafana em
[`compose/observability/`](https://github.com/SEGARK-oficial/CentralOps/tree/main/compose/observability),
com a config do Collector em `otel-collector-config.yaml` e dashboards Grafana já
provisionados:

```bash
cd compose/observability
cp .env.example .env    # ajuste hosts/portas se necessário
docker compose up -d
```

Depois, aponte `OTEL_EXPORTER_OTLP_ENDPOINT` dos serviços `collector-*` para esse
Collector (`http://otel-collector:4318` na mesma rede Docker, ou `http://<host>:4318`).

### Snippets por destino

Trechos para colar nos `exporters:` + `service.pipelines:` da config do Collector. Troque
os `<PLACEHOLDER>`.

**Prometheus — métricas (remote-write, produção):**

```yaml
exporters:
  prometheusremotewrite:
    endpoint: http://<prometheus-host>:9090/api/v1/write   # <PLACEHOLDER>
    tls: { insecure: true }
service:
  pipelines:
    metrics:
      exporters: [prometheusremotewrite]
```

Flags **obrigatórias** no Prometheus (senão recusa o push com 404):

```
--web.enable-remote-write-receiver
--enable-feature=otlp-write-receiver
--storage.tsdb.retention.time=15d
```

**Grafana Loki — logs (Loki 3.x, OTLP nativo):**

```yaml
exporters:
  otlphttp/loki:
    endpoint: http://<loki-host>:3100/otlp     # <PLACEHOLDER>
    tls: { insecure: true }
service:
  pipelines:
    logs:
      exporters: [otlphttp/loki]
```

**Grafana Tempo / Jaeger — traces:**

```yaml
exporters:
  otlphttp/tempo:
    endpoint: http://<tempo-host>:4318          # <PLACEHOLDER>
    tls: { insecure: true }
service:
  pipelines:
    traces:
      exporters: [otlphttp/tempo]               # mantenha tail_sampling + batch nos processors
```

**Datadog** (métricas + traces + logs num só exporter):

```yaml
exporters:
  datadog:
    api:
      site: datadoghq.com
      key: <DD_API_KEY>                          # <PLACEHOLDER> (use um secret)
service:
  pipelines:
    metrics: { exporters: [datadog] }
    traces:  { exporters: [datadog] }
    logs:    { exporters: [datadog] }
```

:::tip[Redes/projetos Docker separados]

Se as stacks CentralOps e de monitoramento ficam em **projetos distintos** (redes Docker
separadas), o Collector **não** alcança `prometheus`/`loki` por DNS de serviço — use o
**IP do host** + portas publicadas (ex.: `http://192.168.3.108:9090/api/v1/write`).

:::

---

## Validação

1. **Instrumentação subiu** — no log dos `collector-*`:

   ```bash
   docker compose -f compose/docker-compose.yml logs collector-worker-priority | grep -i "OTel"
   # OTel metrics ativo (OTLP-push, N instrumentos, interval=..., endpoint=http://otel-collector:4318)
   # OTel tracing ativo (resource=centralops-collector, endpoint=...)
   ```

   Se em vez disso aparecer `... DESLIGADO ... OTEL_EXPORTER_OTLP_ENDPOINT vazio/sem
   http[s]://`, você caiu no fail-safe — **sete o endpoint** (seção acima).

2. **Métricas chegando** — no Prometheus/Grafana, consulte
   `sum by (role) (collector_up)` (deve haver 1+ por papel) e
   `rate(collector_events_in_total[5m])`.

3. **Traces** — dispare uma coleta e procure um trace `collect.cycle` → `dispatch.*` no
   Tempo/Jaeger.

4. **Logs** (se ligou `OTEL_LOGS_ENABLED`) — filtre por `service.name="centralops-collector"`
   no Loki/Datadog.

### Métricas principais

Alguns instrumentos úteis para dashboards e alertas (catálogo completo em
`backend/app/collectors/otel_metrics.py`):

| Métrica | Tipo | Para quê |
|---|---|---|
| `collector_up{role}` | gauge | Liveness por papel (worker/dispatcher/**beat**). Ausência ⇒ processo caiu. |
| `collector_events_in_total{org_id,integration_id}` | counter | Volume de ingestão (eventos IN). |
| `collector_bytes_in_total` | counter | Volume de ingestão (bytes IN). |
| `collector_events_sent_total{destination_id,kind}` | counter | Entregues por destino. |
| `collector_dispatch_failures_total{target,reason}` | counter | Falhas de entrega. |
| `collector_dlq_total{destination_id,error_kind}` | counter | Eventos que foram para a DLQ. |
| `collector_destination_breaker_state{destination_id}` | gauge | Circuit-breaker por destino (1 = aberto). Agregue com `max by (destination_id)`. |
| `collector_dataplane_consumer_lag{partition}` | gauge | Lag do consumer Kafka (data-plane). |
| `collector_quarantine_total` | counter | Eventos em quarentena. |

---

## Próximos passos

- **[Fluxo de dados](./dashboard.md)** e **[Saúde do Pipeline](./pipeline-health.md)** — a
  Superfície A (in-app), sem configuração.
- **[Runbooks](../runbooks/migration-and-boot.md)** — diagnóstico quando algo trava no boot
  ou na entrega.
- `compose/observability/README.md` (no repositório) — detalhes do stack de referência.
