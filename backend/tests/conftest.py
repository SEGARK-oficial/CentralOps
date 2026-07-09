import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
# TestClient roda sobre http://testserver — cookies Secure seriam filtrados
# pelo httpx. Em produção o validator F4-S4 força True (APP_ENV=production).
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _reset_global_state():
    """Reset module-level singletons that accumulate state across tests.

    Resets:
    - db_module.SessionLocal: some fixtures patch it to a test-specific
      sessionmaker; restore so subsequent tests use the canonical default.
    - IntegrationRateLimiter in-memory windows: the singleton accumulates
      POST /integrations requests from admin (user_id=1) across tests.
      Without a reset, tests that run after many other integration-creation
      tests see 429 when they expect 400 or 200.
    - app.dependency_overrides: cleared by individual fixtures, but a
      hard reset here ensures no cross-contamination in edge cases.
    """
    from backend.app.db import database as db_module
    from backend.app.core import rate_limiter as rl_module

    original_session_local = db_module.SessionLocal
    yield
    db_module.SessionLocal = original_session_local

    # Clear in-memory rate limiter windows (Redis state is not used in tests
    # because REDIS_URL is not set in the test environment).
    limiter = rl_module.integration_rate_limiter
    with limiter._lock:
        limiter._create_windows.clear()
        limiter._delete_windows.clear()

    # Reset open-core extension-point slots: any test that
    # registers a scope resolver or quota guard must not leak into tests that
    # assume the in-Core defaults (e.g. test_partner_program_h3a, test_*subtree*).
    from backend.app.core import ee_hooks as _ee_hooks

    _ee_hooks.reset_scope_resolver()
    _ee_hooks.reset_quota_guard()
    _ee_hooks.reset_extra_task_modules()
    _ee_hooks.reset_beat_entries()
    _ee_hooks.reset_partner_sync_dispatcher()
    _ee_hooks.reset_tenant_selection_applier()

    # Reset the cached edition FeatureSet: a test that sets a license token
    # via env must not leak an Enterprise cache into tests that expect Community.
    from backend.app.core import edition as _edition

    _edition.reset_cache()
