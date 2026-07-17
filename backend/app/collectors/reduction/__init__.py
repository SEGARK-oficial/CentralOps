"""Volume reduction + cost metering.

Ships ``metering`` only — pure measurement (events/bytes IN vs OUT), gated by
``settings.COST_METERING_ENABLED`` (default ON — core feature, batched IN-path;
flag off → no-op, byte-identical hot path).
Reduction LEVERS (drop/sample/trim/suppress) and the ``Route.protect_detection``
precondition build on this measurement; nothing here drops an event.
"""
