"""Regression: every Celery queue declared via ``@shared_task(queue=...)`` must
have at least one consumer in ``docker-compose.yml``.

Without this guard, tasks dispatch to Redis successfully (producer side
returns 200 OK) but stay encarcerated in the queue forever because no
worker is reading it. Silent failure mode — exactly what masked
``sync_sophos_partner`` returning 200 with 0 children created during the
2026-05-06 incident: the queue ``maintenance`` was declared in code but
no worker consumed it.

This test parses both sides and fails if any queue is orphaned.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO_ROOT / "compose" / "docker-compose.yml"
APP_PATH = REPO_ROOT / "backend" / "app"

# Two patterns cover both ways tasks declare their queue:
#   1. @shared_task(...queue="<name>"...)   — decorator-level
#   2. "queue": "<name>" inside celery_app.task_routes dict
# We scan for both because Celery resolves the queue at dispatch time using
# either the decorator or task_routes (whichever is present). Missing a worker
# for either kind produces the same silent-drop bug.
_QUEUE_DECORATOR_RE = re.compile(
    r'@(?:shared_task|\w+\.task|celery_app\.task)\s*\([^)]*?queue\s*=\s*"([^"]+)"',
    re.DOTALL,
)
_QUEUE_ROUTE_RE = re.compile(r'"queue"\s*:\s*"([^"]+)"')


def _collect_queues_used() -> set[str]:
    """Every queue named in a task decorator OR task_routes across the backend."""
    queues: set[str] = set()
    for py_file in APP_PATH.rglob("*.py"):
        try:
            text = py_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        queues.update(_QUEUE_DECORATOR_RE.findall(text))
        queues.update(_QUEUE_ROUTE_RE.findall(text))
    return queues


def _collect_queues_consumed() -> set[str]:
    """Every queue named in a worker's ``-Q`` argument in docker-compose.yml.

    Avoids a YAML dependency by scanning the file as text and picking up the
    ``- -Q`` followed by ``- <csv>`` lines. The compose file uses list-form
    ``command:`` blocks for every worker, so this is robust enough.
    """
    consumed: set[str] = set()
    text = COMPOSE_PATH.read_text(encoding="utf-8")
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        if line.strip() == "- -Q":
            # Next non-comment, non-empty line is the queue CSV value.
            # Comment block before the value can be long — keep scanning
            # until we find the actual ``- <value>`` (or hit EOF).
            for j in range(idx + 1, len(lines)):
                next_line = lines[j].strip()
                if not next_line or next_line.startswith("#"):
                    continue
                if next_line.startswith("- "):
                    csv_value = next_line[2:].strip().strip('"').strip("'")
                    consumed.update(q.strip() for q in csv_value.split(","))
                break
    return consumed


def test_every_used_queue_has_consumer() -> None:
    used = _collect_queues_used()
    consumed = _collect_queues_consumed()
    orphan = used - consumed
    assert not orphan, (
        "Celery queues declared via @shared_task(queue=...) but NOT consumed "
        f"by any worker in compose/docker-compose.yml: {sorted(orphan)}\n\n"
        "Tasks dispatched to these queues silently disappear into Redis. "
        "Add the queue(s) to a worker's '-Q' list in docker-compose.yml.\n"
        f"Used by code: {sorted(used)}\n"
        f"Consumed by workers: {sorted(consumed)}"
    )


def test_parser_finds_known_queues() -> None:
    """Sanity: the parser actually picks up the queues we know exist.

    Guards against a regex regression that returns empty and makes the
    main test trivially green.
    """
    used = _collect_queues_used()
    expected_subset = {
        "maintenance",
        "maintenance.high",
        "collect.priority",
        "collect.bulk",
        "collect.backfill",
    }
    missing = expected_subset - used
    assert not missing, (
        f"Parser failed to find expected queues: {missing}. "
        "Did the @shared_task regex break? "
        f"Found: {sorted(used)}"
    )
