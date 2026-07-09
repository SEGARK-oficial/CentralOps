# Stack de Observabilidade CentralOps (host `.108`)

Gateway **OTel Collector** + Prometheus + Loki + **Tempo** + Grafana, rodando
**fora do cluster k3s** da aplicação. Implementa o modelo canônico do
[`monitoring.md`](../../docs/observability/monitoring.md): a app só emite OTLP;
o Collector faz fan-out por sinal.

```
 app k3s (workers OTLP-push)
        │  OTLP/HTTP → http://<IP_DESTE_HOST>:4318
        ▼
 ┌─────────────────────── host .108 (esta stack) ───────────────────────┐
 │  otel-collector (gateway)                                            │
 │     ├─ métricas → prometheusremotewrite → prometheus:9090/api/v1/write│
 │     ├─ logs     → otlphttp/loki          → loki:3100/otlp            │
 │     └─ traces   → tail_sampling → otlp    → tempo:4317               │
 │  prometheus   loki   tempo   grafana(+renderer)                      │
 └──────────────────────────────────────────────────────────────────────┘
```

## Por que um Collector (e por que FORA do cluster)

É a best practice do projeto OpenTelemetry para produção (export direto app→backend
é posicionado para dev/small-scale). O Collector dá **backpressure** (a app não
trava no receiver do Prometheus sob carga), **tail-sampling** (retém 100% de
erros/lentos), **desacoplamento de destino** e **WAL em disco para logs/traces**
(file_storage). ⚠️ Honesto: **métricas** usam fila em memória (`remote_write_queue`),
**sem** WAL — em outage longo do Prometheus, métricas podem ser perdidas após
esgotar fila+retry (logs/traces sobrevivem ao restart). Rodá-lo **no host de observabilidade**, junto dos
backends, é a topologia *gateway* oficialmente endorsada ("per cluster/DC/region")
— e mantém o cluster da app sem nenhum serviço novo.

> **Limite consciente:** este gateway remoto **não** coleta métricas host-level/k8s
> dos nós k3s (hostmetrics/resourcedetection só são corretos rodando junto da app).
> Se um dia precisar disso, aí entra um agent DaemonSet no cluster — decisão aparte,
> aditiva.

## Subir

```bash
cp .env.example .env && edite .env          # senha grafana + token do renderer
docker compose up -d
docker compose ps                            # tudo healthy?
```

Arquivos (tudo NESTE diretório → **auto-contido**, copie a pasta inteira):
- `docker-compose.yml` — a stack (Collector pinado `0.155.0`, Tempo `2.7.0`).
- `otel-collector-config.yaml` — config do gateway.
- `prometheus.yml` — remote-write receiver + out-of-order + scrape das self-métricas do Collector.
- `tempo.yaml` — Tempo monolítico (OTLP; `metrics_generator` é add-on opcional comentado).
- `grafana/provisioning/datasources/` — Prometheus/Loki/Tempo com **uids fixos**
  (`prometheus`/`loki`/`tempo`) — anchor dos dashboards + correlação métrica↔log↔trace.

> **Validado** (boot local end-to-end, 2026-06-25): stack sobe `healthy`; uma
> métrica OTLP enviada ao Collector chega ao Prometheus via remote-write com
> `job=centralops-collector` + `instance=<host:pid>` (labels que os dashboards usam).

## Plugar a aplicação (lado k3s) — só variáveis

A app **não muda de código**. Aponte o endpoint base para este Collector e ligue
logs/traces (ver `kubernetes/helm/centralops/values.yaml` → bloco `config`, ou a
ConfigMap viva):

| Variável | Valor | Efeito |
|---|---|---|
| `OTEL_ENABLED` | `true` | liga traces+métricas |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://<IP_DO_HOST_DE_OBS>:4318` (ex.: `http://192.168.3.211:4318`) | base; a app anexa `/v1/{metrics,logs,traces}` |
| `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` | *(vazio)* | remova o push direto ao Prometheus |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | *(vazio)* | idem |
| `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` | *(vazio)* | idem |
| `OTEL_LOGS_ENABLED` | `true` | liga logs → Loki (toggle separado, volume alto) |
| `OTEL_TRACES_SAMPLER_RATIO` | `1` | head 1.0 na app; o tail-sampling decide no Collector |

> **ORDEM IMPORTA:** suba esta stack **antes** de religar `OTEL_TRACES_SAMPLER_RATIO=1`
> e antes de apontar a app para cá. Apontar a app para um `:4318` inexistente faz a
> telemetria parar.

Patch da ConfigMap viva + restart (exemplo):

```bash
kubectl -n centralops patch configmap centralops-config --type merge -p '{"data":{
  "OTEL_EXPORTER_OTLP_ENDPOINT":"http://192.168.3.211:4318",
  "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT":"",
  "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT":"",
  "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT":"",
  "OTEL_LOGS_ENABLED":"true",
  "OTEL_TRACES_SAMPLER_RATIO":"1"
}}'
kubectl -n centralops rollout restart deploy \
  centralops-api centralops-frontend \
  centralops-worker-priority centralops-worker-bulk \
  centralops-worker-dispatcher centralops-worker-maintenance centralops-worker-beat
```

## Validar

```bash
# 1) Collector recebeu e exportou? (self-métricas)
curl -s http://localhost:8888/metrics | grep -E 'otelcol_receiver_accepted|otelcol_exporter_sent'
# 2) métricas no Prometheus (via remote-write)
curl -s 'http://localhost:9090/api/v1/query?query=collector_events_total' | head
# 3) logs no Loki
curl -s 'http://localhost:3100/loki/api/v1/labels'
# 4) traces no Tempo (após OTEL_TRACES_SAMPLER_RATIO=1): Grafana → Explore → Tempo
```

## Notas honestas (custos a aceitar)

- **SPOF:** 1 Collector + toda a cadeia (Prom/Loki/Tempo/Grafana) neste host. Em
  escala pequena, OK (WAL + restart). Se a telemetria virar crítica em resposta a
  incidente (SOC/MSSP), suba 2 réplicas do Collector atrás de um LB e considere
  separar a cadeia. Se a LAN `192.168.3.x` particiona, o SDK da app dropa in-memory.
- **PII:** a redação no Collector (opcional, comentada no config) protege logs de
  **ops**; **não** é a redação fail-closed do **data-plane** do produto.
- **Tempo** precisa existir **antes** de religar traces (senão volta o 404).
