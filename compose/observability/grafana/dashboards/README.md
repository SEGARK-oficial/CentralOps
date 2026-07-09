# Dashboards Grafana — CentralOps

Quatro dashboards auto-provisionados (Superfície B, OTel-native). Fonte de dados:
**Prometheus** que faz scrape do OTel Collector em `:8889` (ver
`../otel-collector-config.yaml`). As séries são as do catálogo
`backend/app/collectors/otel_metrics.py` (`collector_*`).

| Arquivo | UID | Descrição |
|---|---|---|
| `centralops-executive.json` | `centralops-executive` | **Executivo & SLO** — visão de uma linha: workers ativos, ingestão vs entrega (EPS), taxa de rejeição/DLQ e erros críticos (quarentena/rejeições). Para decisão rápida. |
| `centralops-pipeline-k8s.json` | `centralops-pipeline-k8s` | **Pipeline & Processing** — estágios de normalização e roteamento: throughput de eventos normalizados, dedupe/drop, latência de processamento, quarentena por vendor & error_kind, decisões de roteamento por outcome e top rotas. |
| `centralops-delivery-k8s.json` | `centralops-delivery-k8s` | **Integrations & Delivery** — ingress (coleta por vendor: latência API p95, rate-limit backoffs, OAuth token TTL) e egress (entrega por destino: EPS, latência p95, circuit breaker). |
| `centralops-dataplane-k8s.json` | `centralops-dataplane-k8s` | **Data-plane (Kafka)** — transporte durável Kafka/Redpanda: produce rate/latência p95 (por destino & outcome), consume rate/latência p95 (por outcome), **consumer lag por partição** e entrega via data-plane (sent vs DLQ). Só popula com `EVENT_DATAPLANE=kafka`. Ver runbook `data-plane-kafka.md`. |

## Pré-requisitos

1. OTel ligado nos workers: `OTEL_ENABLED=true` (+ `OTEL_EXPORTER_OTLP_ENDPOINT`).
   Ver `docs-site/docs/outputs/observability.md`.
2. OTel Collector de pé com `add_metric_suffixes: false` no exporter `prometheus`
   (já configurado em `../otel-collector-config.yaml`) — **obrigatório** para os
   nomes de série baterem com as queries dos dashboards.
3. Prometheus fazendo scrape de `otel-collector:8889`.

## Importar

- **Provisionado (recomendado):** monte este diretório em
  `/var/lib/grafana/dashboards` e aponte um provider em
  `grafana/provisioning/dashboards/*.yaml`.
- **Manual:** Grafana → Dashboards → Import → cole o JSON → selecione o
  datasource Prometheus na variável `DS_PROMETHEUS`.

## Variáveis

`DS_PROMETHEUS` (datasource) em todos. Filtros adicionais por dashboard:
`job` / `instance` (data-plane), `vendor`, `destination_id`, `tenant` — multi + All.
