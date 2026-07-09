"""Routing subsystem (motor de roteamento label-driven).

Public surface re-exported for ergonomic imports:

    from .routing import route_batch, evaluate_event, validate_condition
"""

from .engine import (  # noqa: F401
    ACTION_DROP,
    ACTION_ROUTE,
    ALLOWED_FIELDS,
    ALLOWED_OPS,
    BatchRouting,
    CompiledRoute,
    EventRouting,
    evaluate_event,
    event_labels,
    find_unreachable,
    matches,
    order_routes,
    route_batch,
    SamplingConfig,
    validate_condition,
)
from .pii_redaction import (  # noqa: F401
    PiiRedactionError,
    apply_pii_redaction,
    compile_pii_redaction,
    validate_pii_redaction,
)

__all__ = [
    "ACTION_DROP",
    "ACTION_ROUTE",
    "ALLOWED_FIELDS",
    "ALLOWED_OPS",
    "BatchRouting",
    "CompiledRoute",
    "EventRouting",
    "evaluate_event",
    "event_labels",
    "find_unreachable",
    "matches",
    "order_routes",
    "route_batch",
    "SamplingConfig",
    "validate_condition",
    "PiiRedactionError",
    "apply_pii_redaction",
    "compile_pii_redaction",
    "validate_pii_redaction",
]
