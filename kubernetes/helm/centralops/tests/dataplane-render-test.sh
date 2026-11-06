#!/usr/bin/env bash
# Helm render guard — ADR-0018 Onda 1 (deploy de produção do data-plane).
#
# Trava, sem cluster (helm template), o que a auditoria achou quebrado no chart:
#   * toda fila Celery roteada no código tem um worker consumindo (collect.query
#     ficava sem consumidor: scheduled query pendente para sempre);
#   * o worker sobe pelo cops-celery da IMAGEM (no EE, centralops_ee.celery_app);
#     o app fixo deixava a imagem EE em k8s sem as próprias tasks;
#   * os workers têm tempo de drain ≥ task_time_limit (900 s), não os 30 s default;
#   * o data-plane recebe TLS e RF 3 em broker externo, RF 1 no devBroker;
#   * PgBouncer opcional: app pelo pooler, migração direta; PrometheusRule opcional;
#   * o processor do tópico raw (Onda 2) sobe com retenção e PDB.
#
# Run: bash tests/dataplane-render-test.sh
set -euo pipefail

CHART_DIR="$(cd "$(dirname "$0")/.." && pwd)"
fail=0
note() { printf '  %s %s\n' "$1" "$2"; }
check() { # check <description> <actual> <expected>
  if [ "$2" = "$3" ]; then note "✓" "$1"; else note "✗" "$1 (got '$2', want '$3')"; fail=1; fi
}

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

echo "==> Case 1: devBroker (dev/test)"
helm template t "$CHART_DIR" --set devBroker.enabled=true > "$tmp/dev.yaml"
workers="$(helm template t "$CHART_DIR" --set devBroker.enabled=true --show-only templates/workers-deployment.yaml)"
check "worker de collect.query renderizado" "$(grep -c '"collect.query"' <<<"$workers" || true)" "1"
check "nenhum worker fixa o app Celery do Core" "$(grep -c 'app.collectors.celery_app"' <<<"$workers" || true)" "0"
check "todo worker sobe pelo cops-celery" \
  "$(grep -c '"/usr/local/bin/cops-celery"' <<<"$workers")" \
  "$(grep -c '^kind: Deployment' <<<"$workers")"
low_grace="$(grep -E 'terminationGracePeriodSeconds: ' <<<"$workers" | awk '$2 < 120' | wc -l | tr -d ' ')"
check "nenhum worker com drain < 120 s" "$low_grace" "0"
check "bulk com drain ≥ task_time_limit" \
  "$(grep -A30 'name: t-centralops-worker-bulk$' <<<"$workers" | grep -m1 -oE 'terminationGracePeriodSeconds: [0-9]+' | awk '{print ($2>=900)}')" "1"
check "devBroker força RF 1" "$(grep -c 'KAFKA_DELIVER_REPLICATION: "1"' "$tmp/dev.yaml")" "1"
check "processor do tópico raw renderizado (Onda 2)" \
  "$(grep -c 'from app.collectors.dataplane.processor import main; main()' "$tmp/dev.yaml")" "1"
check "retenção do raw no configmap" "$(grep -c 'KAFKA_RAW_RETENTION_MS: "86400000"' "$tmp/dev.yaml")" "1"
check "PDB do processor" "$(grep -A3 '^kind: PodDisruptionBudget' "$tmp/dev.yaml" | grep -c 'name: t-centralops-raw-processor$')" "1"

echo "==> Case 2: broker gerenciado SASL_SSL"
helm template t "$CHART_DIR" \
  --set dataPlane.kafka.bootstrapServers=b-1.example.internal:9096 \
  --set dataPlane.kafka.securityProtocol=SASL_SSL \
  --set dataPlane.kafka.sasl.mechanism=SCRAM-SHA-512 \
  --set dataPlane.kafka.tls.caFile=/etc/kafka/ca.pem > "$tmp/managed.yaml"
check "RF 3 em broker externo" "$(grep -c 'KAFKA_DELIVER_REPLICATION: "3"' "$tmp/managed.yaml")" "1"
check "CA do broker no configmap" "$(grep -c 'KAFKA_SSL_CAFILE: "/etc/kafka/ca.pem"' "$tmp/managed.yaml")" "1"
check "verificação TLS ligada" "$(grep -c 'KAFKA_SSL_VERIFY: "true"' "$tmp/managed.yaml")" "1"

echo "==> Case 3: celeryApp explícito"
explicit="$(helm template t "$CHART_DIR" --set devBroker.enabled=true \
  --set workers.celeryApp=custom.celery_app --show-only templates/workers-deployment.yaml)"
check "app explícito em todos os workers" \
  "$(grep -c '"custom.celery_app"' <<<"$explicit")" \
  "$(grep -c '^kind: Deployment' <<<"$explicit")"

echo "==> Case 4: default — sem PgBouncer nem PrometheusRule"
check "sem PgBouncer por padrão" "$(grep -c 'name: t-centralops-pgbouncer$' "$tmp/dev.yaml" || true)" "0"
check "sem PrometheusRule por padrão" "$(grep -c '^kind: PrometheusRule' "$tmp/dev.yaml" || true)" "0"
check "sem DATABASE_MIGRATION_URL por padrão" "$(grep -c 'DATABASE_MIGRATION_URL' "$tmp/dev.yaml" || true)" "0"

echo "==> Case 5: PgBouncer ligado"
helm template t "$CHART_DIR" --set devBroker.enabled=true --set networkPolicies.enabled=true \
  --set pgbouncer.enabled=true \
  --set 'secrets.databaseUrl=postgresql+psycopg2://cops:pw@db.example.internal:5432/centralops?sslmode=require' \
  > "$tmp/pgb.yaml"
check "Deployment do PgBouncer" "$(grep -A3 '^kind: Deployment' "$tmp/pgb.yaml" | grep -c 'name: t-centralops-pgbouncer$')" "1"
check "Service do PgBouncer" "$(grep -A3 '^kind: Service$' "$tmp/pgb.yaml" | grep -c 'name: t-centralops-pgbouncer$')" "1"
check "app aponta para o PgBouncer, sem query string" \
  "$(grep -c 'DATABASE_URL: "postgresql+psycopg2://cops:pw@t-centralops-pgbouncer:5432/centralops"' "$tmp/pgb.yaml")" "1"
check "migração com a URL direta (com sslmode)" \
  "$(grep -c 'DATABASE_MIGRATION_URL: "postgresql+psycopg2://cops:pw@db.example.internal:5432/centralops?sslmode=require"' "$tmp/pgb.yaml")" "1"
check "PgBouncer lê a URL direta" "$(grep -c 'key: DATABASE_MIGRATION_URL' "$tmp/pgb.yaml")" "1"
check "pooling por transação" "$(grep -c 'name: POOL_MODE, value: "transaction"' "$tmp/pgb.yaml")" "1"
check "NetworkPolicy de ingress do PgBouncer" "$(grep -c 'name: t-centralops-allow-pgbouncer-from-app' "$tmp/pgb.yaml")" "1"

echo "==> Case 6: PrometheusRule ligado"
helm template t "$CHART_DIR" --set devBroker.enabled=true \
  --set monitoring.prometheusRule.enabled=true --set monitoring.prometheusRule.labels.release=kps \
  --show-only templates/prometheusrule.yaml > "$tmp/rules.yaml"
check "PrometheusRule renderizado" "$(grep -c '^kind: PrometheusRule' "$tmp/rules.yaml")" "1"
check "label do seletor aplicada" "$(grep -c 'release: kps' "$tmp/rules.yaml")" "1"
check "as 9 regras do data-plane" "$(grep -c -- '- alert: CentralOps' "$tmp/rules.yaml")" "9"
check "template de label preservado" "$(grep -c 'labels.destination_id' "$tmp/rules.yaml")" "2"

if [ "$fail" -ne 0 ]; then echo "FALHOU"; exit 1; fi
echo "OK"
