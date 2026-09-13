from __future__ import annotations

import asyncio
from typing import Any

from ..loopback import CentralOpsAPIError
from ._base import (
    CentralOpsClient,
    ToolSpec,
    _integer,
    _object,
    _string,
)


_TERMINAL_STATES = {"completed", "failed", "cancelled"}
_DEFAULT_POLL_INTERVAL = 5
_MIN_POLL_INTERVAL = 2
# Embutido no core, a chamada da ferramenta é UMA requisição HTTP aberta em
# ``/api/mcp``; proxies reversos costumam cortar em 60-120 s. O teto de espera
# fica abaixo disso — o agente chama de novo se precisar continuar esperando.
_MAX_WAIT_TIMEOUT = 120


async def _list_backfill_jobs(
    client: CentralOpsClient,
    *,
    integration_id: int,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> Any:
    return await client.get(
        f"/integrations/{integration_id}/backfill-jobs",
        params={"status": status, "limit": limit, "offset": offset},
    )


async def _request_backfill(
    client: CentralOpsClient,
    *,
    integration_id: int,
    streams: list[str],
    from_ts: str,
    to_ts: str,
) -> Any:
    if not streams:
        raise ValueError("streams must contain at least one stream name")
    job = await client.post(
        f"/integrations/{integration_id}/backfill",
        json={"streams": streams, "from_ts": from_ts, "to_ts": to_ts},
    )
    if isinstance(job, dict):
        job_id = job.get("id")
        return {
            "job": job,
            "status_hint": (
                "Use wait_for_backfill_job to await completion, or get_backfill_job "
                "for a single status snapshot."
            ),
            "poll_interval_recommended_s": _DEFAULT_POLL_INTERVAL,
            "wait_for_backfill_job_input": {"job_id": job_id} if job_id else None,
        }
    return job


async def _get_backfill_job(
    client: CentralOpsClient,
    *,
    job_id: str,
) -> Any:
    return await client.get(f"/backfill-jobs/{job_id}")


async def _cancel_backfill_job(
    client: CentralOpsClient,
    *,
    job_id: str,
) -> Any:
    return await client.post(f"/backfill-jobs/{job_id}/cancel")


async def _backfill_diagnostics(client: CentralOpsClient) -> Any:
    return await client.get("/backfill-jobs/diagnostics")


async def _wait_for_backfill_job(
    client: CentralOpsClient,
    *,
    job_id: str,
    timeout_s: int = 60,
    poll_interval_s: int = _DEFAULT_POLL_INTERVAL,
) -> Any:
    if timeout_s < 1:
        raise ValueError("timeout_s must be >= 1")
    if timeout_s > _MAX_WAIT_TIMEOUT:
        raise ValueError(
            f"timeout_s must be <= {_MAX_WAIT_TIMEOUT} to avoid blocking the LLM session"
        )
    if poll_interval_s < _MIN_POLL_INTERVAL:
        poll_interval_s = _MIN_POLL_INTERVAL

    deadline = asyncio.get_event_loop().time() + timeout_s
    last: Any = None
    while True:
        try:
            last = await client.get(f"/backfill-jobs/{job_id}")
        except CentralOpsAPIError as exc:
            return {
                "terminal": False,
                "timed_out": False,
                "error": {"http_status": exc.status_code, "message": str(exc)},
            }
        status = last.get("status") if isinstance(last, dict) else None
        if status in _TERMINAL_STATES:
            return {"terminal": True, "timed_out": False, "job": last}
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            return {"terminal": False, "timed_out": True, "job": last}
        await asyncio.sleep(min(poll_interval_s, max(remaining, 0.5)))


def specs() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="list_backfill_jobs",
            description=(
                "List backfill jobs for a CentralOps integration. Filter by status to "
                "find pending or running jobs."
            ),
            input_schema=_object(
                properties={
                    "integration_id": _integer("Integration id.", minimum=1),
                    "status": {
                        "type": "string",
                        "description": "Optional status filter.",
                        "enum": ["pending", "running", "completed", "failed", "cancelled"],
                    },
                    "limit": _integer(
                        "Maximum results (1-500, default 50).",
                        minimum=1,
                        maximum=500,
                    ),
                    "offset": _integer("Pagination offset (default 0).", minimum=0),
                },
                required=["integration_id"],
            ),
            handler=_list_backfill_jobs,
        ),
        ToolSpec(
            name="request_backfill",
            description=(
                "Create a backfill job for a window of historical events. Window must be "
                "<= 90 days and from_ts cannot be more than 90 days in the past. Streams "
                "must be supported by the integration's vendor. Returns the created job "
                "(status starts as 'pending'). Use wait_for_backfill_job to follow it.\n\n"
                "COSTS REAL RESOURCES: this replays vendor API calls and consumes "
                "the tenant's API quota and rate-limit budget. NOT idempotent — "
                "each call enqueues another job. Check list_backfill_jobs for an "
                "equivalent job before creating one, and only run it on explicit "
                "human request."
            ),
            read_only=False,
            destructive=True,
            idempotent=False,
            input_schema=_object(
                properties={
                    "integration_id": _integer("Integration id.", minimum=1),
                    "streams": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "description": "Stream names to backfill (e.g. ['alerts', 'cases']).",
                    },
                    "from_ts": _string(
                        "Window start, ISO 8601 (e.g. '2026-04-01T00:00:00Z')."
                    ),
                    "to_ts": _string(
                        "Window end, ISO 8601. Must be greater than from_ts; window <= 90 days."
                    ),
                },
                required=["integration_id", "streams", "from_ts", "to_ts"],
            ),
            handler=_request_backfill,
        ),
        ToolSpec(
            name="get_backfill_job",
            description=(
                "Fetch a single backfill job by id, including progress_pct (0-100), "
                "events_collected, events_dispatched, last_error, and the derived "
                "stalled/stall_reason fields."
            ),
            input_schema=_object(
                properties={
                    "job_id": _string("Backfill job id (uuid)."),
                },
                required=["job_id"],
            ),
            handler=_get_backfill_job,
        ),
        ToolSpec(
            name="cancel_backfill_job",
            description=(
                "Cancel a pending or running backfill job (the Celery task is revoked "
                "cooperatively; the worker exits cleanly at the next page boundary). "
                "Jobs already completed/failed/cancelled return 400. Requires the "
                "integration.write permission. Returns the updated job.\n\n"
                "Stops work in progress: pages not yet collected when the worker "
                "exits are simply not collected, and the job cannot be resumed — "
                "a new backfill must be requested. Only run on explicit human "
                "request."
            ),
            read_only=False,
            destructive=True,
            input_schema=_object(
                properties={
                    "job_id": _string("Backfill job id (uuid)."),
                },
                required=["job_id"],
            ),
            handler=_cancel_backfill_job,
        ),
        ToolSpec(
            name="backfill_diagnostics",
            description=(
                "Diagnose 'why does backfill never run?': live Celery workers, "
                "consumers of the collect.backfill queue, and the pending-job "
                "backlog. Requires a global-admin token; returns 403 otherwise. "
                "Use when a job reports stalled=true."
            ),
            input_schema=_object(properties={}),
            handler=_backfill_diagnostics,
        ),
        ToolSpec(
            name="wait_for_backfill_job",
            description=(
                "Poll a backfill job until it reaches a terminal state "
                "('completed', 'failed', 'cancelled') or timeout_s elapses. Polls "
                "server-side so the LLM does not loop. Default timeout 60s, max 600s."
            ),
            input_schema=_object(
                properties={
                    "job_id": _string("Backfill job id (uuid)."),
                    "timeout_s": _integer(
                        "Maximum seconds to wait (1-600, default 60).",
                        minimum=1,
                        maximum=_MAX_WAIT_TIMEOUT,
                    ),
                    "poll_interval_s": _integer(
                        f"Seconds between polls (>= {_MIN_POLL_INTERVAL}, default {_DEFAULT_POLL_INTERVAL}).",
                        minimum=_MIN_POLL_INTERVAL,
                    ),
                },
                required=["job_id"],
            ),
            handler=_wait_for_backfill_job,
        ),
    ]
