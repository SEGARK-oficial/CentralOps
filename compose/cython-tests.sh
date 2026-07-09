#!/usr/bin/env bash
# Run the backend test suite against the Cython-compiled tree.
#
# Runs in the ``backend-test`` Docker stage. Tree layout is preserved
# as ``/build/backend/...`` (NOT flattened to /app) because the existing
# tests use ``from backend.app.X import Y`` and rely on the sys.path
# manipulation in ``backend/tests/conftest.py``. The test stage matches
# what a developer sees locally — only difference is .so vs .py.
#
# Auto-skip is wired up via /build/backend/conftest.py: it detects the
# .so tree and applies pytest.mark.skip to the source_only nodeids.

set -euo pipefail

cd /build

# PYTHONPATH covers all three import flavors present in the test suite:
#   /build           → 'backend.app.X'      (most tests; see backend/tests/conftest.py)
#   /build/backend   → 'app.X'              (a handful of tests)
#   /build/scripts   → 'reencrypt_secrets'  (one test imports the top-level CLI)
export PYTHONPATH="/build:/build/backend:/build/scripts${PYTHONPATH:+:${PYTHONPATH}}"

export APP_ENV="${APP_ENV:-test}"
export APP_MASTER_KEY="${APP_MASTER_KEY:-test-master-key-for-centralops-suite-12345}"
export SESSION_SECURE_COOKIE="${SESSION_SECURE_COOKIE:-false}"
export DATABASE_URL="${DATABASE_URL:-sqlite:///:memory:}"
export PYTHONDONTWRITEBYTECODE=1

# Sanity probe: critical modules must load from .so.
echo "cython-tests: smoke-check imports of compiled modules…"
python - <<'PY'
import importlib
import sys

sys.path.insert(0, "/build")

critical = [
    "backend.app.collectors.registry",
    "backend.app.collectors.pipeline",
    "backend.app.collectors.vendors.sophos_detections",
    "backend.app.collectors.vendors.sophos",
    "backend.app.services.token_manager",
    "backend.app.services.xdr_query",
    "backend.app.providers.sophos.provider",
    "backend.app.routers.integrations",
]
for mod in critical:
    m = importlib.import_module(mod)
    src = getattr(m, "__file__", "?")
    if not src.endswith(".so"):
        raise SystemExit(
            f"ABORT: {mod} loaded from {src} — expected .so. "
            "Cython compilation likely did not cover this module."
        )
    print(f"  OK  {mod} -> {src}")
print("cython-tests: smoke-check passed.")
PY

echo "cython-tests: running pytest sweep…"
# --ignore: benchmarks/ requires pytest-benchmark (dev-only); they are
# not a regression gate. Performance baselines run in a separate CI job.
# --maxfail=5: short-circuit after a handful of failures. With the source_only
# auto-skip in backend/conftest.py wiring up the known-incompatible tests,
# anything that reaches this threshold is a real regression and worth halting on.
exec python -m pytest \
    -m "not source_only" \
    --maxfail=5 \
    --no-header \
    -q \
    --ignore=backend/app/collectors/tests/benchmarks \
    backend/tests \
    backend/app/collectors/tests
