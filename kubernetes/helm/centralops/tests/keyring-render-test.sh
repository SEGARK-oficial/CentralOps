#!/usr/bin/env bash
# Helm render guard  — the CE→EE upgrade BLOCKER regression test.
#
# The public license keyring (<kid>.pem) MUST reach every pod that resolves the
# edition (api, workers, kafka-dispatcher) as a /licensing mount + the paired
# CENTRALOPS_LICENSE_KEYS_DIR env — otherwise a valid token fail-closes to
# Community on Kubernetes (the bug this fixes: the chart injected only the token).
#
# Pure bash + helm, no cluster. Run: bash tests/keyring-render-test.sh
set -euo pipefail

CHART_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# The chart's configmap guard rejects the in-cluster broker unless devBroker is on;
# irrelevant to the keyring, so enable it to get a clean render.
BASE=(--set devBroker.enabled=true)
fail=0
note() { printf '  %s %s\n' "$1" "$2"; }
check() { # check <description> <actual> <expected>
  if [ "$2" = "$3" ]; then note "✓" "$1"; else note "✗" "$1 (got '$2', want '$3')"; fail=1; fi
}

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

echo "==> helm lint"
helm lint "$CHART_DIR" "${BASE[@]}" >/dev/null && note "✓" "chart lints"

echo "==> Case 1: DEFAULT (no keyring) — must render NOTHING keyring-related"
helm template t "$CHART_DIR" "${BASE[@]}" > "$tmp/default.yaml"
check "no license-keyring ConfigMap"      "$(grep -c 'name: t-centralops-license-keyring' "$tmp/default.yaml" || true)" "0"
check "no CENTRALOPS_LICENSE_KEYS_DIR env" "$(grep -c 'CENTRALOPS_LICENSE_KEYS_DIR'        "$tmp/default.yaml" || true)" "0"
check "no /licensing mount"                "$(grep -c 'mountPath: /licensing'              "$tmp/default.yaml" || true)" "0"

echo "==> Case 2: INLINE keyring — ConfigMap + KEYS_DIR + mount on EVERY edition-resolving pod"
cat > "$tmp/inline.yaml" <<'YAML'
secrets:
  licenseKeyring:
    "billing-2026.pem": |
      -----BEGIN PUBLIC KEY-----
      MCowBQYDK2VwAyEAtesttesttesttesttesttesttesttesttest0123456789=
      -----END PUBLIC KEY-----
YAML
helm template t "$CHART_DIR" "${BASE[@]}" -f "$tmp/inline.yaml" > "$tmp/inline_out.yaml"
check "license-keyring ConfigMap rendered"        "$(grep -c 'name: t-centralops-license-keyring' "$tmp/inline_out.yaml" | head -1)" "8"
check "PEM embedded in ConfigMap"                 "$(grep -c 'BEGIN PUBLIC KEY' "$tmp/inline_out.yaml")" "1"
check "KEYS_DIR=/licensing in -config"            "$(grep -c 'CENTRALOPS_LICENSE_KEYS_DIR: /licensing' "$tmp/inline_out.yaml")" "1"
# Every app pod (api + kafka-dispatcher + 5 workers = 7) must mount /licensing.
check "/licensing mounted on all 7 app pods"      "$(grep -c 'mountPath: /licensing' "$tmp/inline_out.yaml")" "7"

echo "==> Case 3: existingLicenseKeyring — mount operator's ConfigMap, render none of our own"
helm template t "$CHART_DIR" "${BASE[@]}" --set secrets.existingLicenseKeyring=my-ee-keyring > "$tmp/existing.yaml"
check "our -license-keyring ConfigMap NOT rendered" "$(grep -c 'name: t-centralops-license-keyring' "$tmp/existing.yaml" || true)" "0"
check "volumes reference operator's ConfigMap"      "$(grep -c 'name: my-ee-keyring' "$tmp/existing.yaml")" "7"
check "KEYS_DIR still set"                           "$(grep -c 'CENTRALOPS_LICENSE_KEYS_DIR: /licensing' "$tmp/existing.yaml")" "1"

echo
if [ "$fail" = "0" ]; then echo "✅ keyring render guard PASSED"; else echo "❌ keyring render guard FAILED"; fi
exit "$fail"
