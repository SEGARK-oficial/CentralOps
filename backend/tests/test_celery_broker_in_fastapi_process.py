"""Regression for Erro A — FastAPI process producers using Celery default broker.

Before this fix, ``backend/app/main.py`` did not import
``backend.app.collectors.celery_app``. As a result, calling
``sync_sophos_partner.delay(...)`` from a FastAPI router resolved against
Celery's default app — broker ``amqp://guest@localhost:5672`` — and the
task was either silently dropped (no broker reachable) or routed to a
RabbitMQ instance no worker was reading. The collector worker entrypoint
imports ``celery_app`` directly, so it always had the right configuration;
the bug only manifested in the API process.

This test imports ``backend.app.main`` and asserts that ``current_app``
(Celery's per-process singleton) carries the configured broker, NOT the
default amqp URL. If the import in ``main.py`` is removed, this fails.
"""

from __future__ import annotations

import os

# These need to be set BEFORE importing backend.app.main (config validation).
os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")
# Force a non-default broker so the test would fail loud if main.py forgot
# to import celery_app. We use a fake redis URL — celery only constructs
# the URL string at import time; no connection happens here.
os.environ.setdefault("CELERY_BROKER_URL", "redis://localhost:6379/9")
os.environ.setdefault("CELERY_RESULT_BACKEND", "redis://localhost:6379/10")


def test_main_import_binds_celery_singleton_to_configured_broker():
    """Importing ``backend.app.main`` must trigger ``celery_app`` import,
    which in turn registers the configured broker on Celery's ``current_app``.

    Without the fix in ``main.py``, ``current_app.conf.broker_url`` would
    fall back to ``"amqp://guest@localhost:5672//"`` and ``.delay()`` calls
    in routers would silently target a broker no one listens on.
    """
    # Lazy import — we want the side-effects of importing main.
    import backend.app.main  # noqa: F401  — side-effect import under test
    from celery import current_app

    broker_url = current_app.conf.broker_url
    # Either our explicit env override, or whatever the app resolved (Redis-based).
    # Crucially: NOT the amqp default.
    assert broker_url is not None and broker_url != "", (
        "current_app.conf.broker_url is empty — celery_app was never instantiated"
    )
    assert not broker_url.startswith("amqp://"), (
        f"Celery still on default amqp broker ({broker_url!r}) — "
        "backend.app.main is not importing collectors.celery_app, so any "
        "@shared_task .delay() from a router targets the wrong broker. "
        "See diagnóstico Erro A."
    )
    # Specifically should match what we set (or at least be a redis URL).
    assert broker_url.startswith("redis://"), (
        f"Expected Redis broker, got {broker_url!r}"
    )


def test_celery_app_singleton_is_the_same_instance_in_api_and_collectors_imports():
    """Defense-in-depth: importing ``celery_app`` from collectors and from
    main both resolve to the same Celery instance — guaranteeing that any
    config change at boot is visible everywhere."""
    import backend.app.main  # noqa: F401
    from backend.app.collectors.celery_app import celery_app as ca_collectors
    from celery import current_app

    # current_app is a proxy — _get_current_object resolves it.
    assert current_app._get_current_object() is ca_collectors, (
        "Celery current_app diverges from collectors.celery_app — multiple "
        "Celery() instances were created. Producers may target the wrong one."
    )


def test_current_app_resolves_in_other_threads_too():
    """Production reproduction: FastAPI dispatches sync ``def`` handlers to
    the asyncio threadpool. The handler thread is NOT the import thread, so
    ``current_app`` cannot rely on the LocalStack populated by ``Celery.__init__``
    (which calls ``set_current()`` — thread-local).

    Without ``celery_app.set_default()``, a request handler running in the
    threadpool would see ``current_app == amqp://localhost`` lazy default
    and ``.delay()`` would silently target the wrong broker. This was Erro A
    in the production incident.

    This test fails if ``set_default()`` is removed from ``celery_app.py``.
    """
    import threading

    import backend.app.main  # noqa: F401
    from backend.app.collectors.celery_app import celery_app as ca_collectors

    result: dict = {}

    def _worker() -> None:
        # Re-import inside the thread — same module cached, but ``current_app``
        # resolution happens in this thread's context.
        from celery import current_app

        result["broker_url"] = current_app.conf.broker_url
        result["app_id"] = id(current_app._get_current_object())

    t = threading.Thread(target=_worker)
    t.start()
    t.join(timeout=5)

    assert "broker_url" in result, "thread did not finish — possible deadlock"
    assert not result["broker_url"].startswith("amqp://"), (
        f"Threaded handler resolves to amqp default ({result['broker_url']!r}). "
        "celery_app.set_default() was not called — current_app falls back to "
        "the lazy default app in threads other than the import thread. "
        "Without the fix, FastAPI sync handlers (which run in threadpool) "
        "would have .delay() target the wrong broker."
    )
    assert result["app_id"] == id(ca_collectors), (
        "current_app in worker thread is NOT the collectors.celery_app instance. "
        "set_default() must register the app globally so all threads see it."
    )
