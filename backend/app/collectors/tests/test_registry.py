"""Registry OOP — garante extensibilidade sem tocar em pipeline/beat/refreshers."""

from __future__ import annotations

from datetime import timedelta

import pytest

from ..base import BaseCollector
from ..registry import (
    CollectorRegistration,
    all_registrations,
    get,
    has,
    iter_for_platform,
    register,
    supported_platforms,
    supported_streams,
)


class _NoopCollector(BaseCollector):
    platform = "test-vendor"
    stream = "test-stream"

    @property
    def domain(self) -> str:
        return "api.test.example"

    async def collect(self):  # pragma: no cover
        if False:
            yield {}

    def extract_message_id(self, event):
        return str(event.get("id", ""))


async def _noop_refresher(integration_id: int) -> dict:
    return {"access_token": "t", "expires_in": 3600}


def _reg(platform: str = "test-vendor", stream: str = "test-stream") -> CollectorRegistration:
    return CollectorRegistration(
        platform=platform,
        stream=stream,
        collector_cls=_NoopCollector,
        refresh_fn=_noop_refresher,
        schedule=timedelta(minutes=10),
        queue="collect.bulk",
        task_name="collectors.collect_vendor_logs_bulk",
    )


def test_builtins_registered_on_import() -> None:
    """Sophos/Defender/NinjaOne estão registrados por side-effect do __init__."""
    platforms = set(supported_platforms())
    assert {"sophos", "microsoft_defender", "ninjaone"}.issubset(platforms)


def test_get_raises_keyerror_for_unknown() -> None:
    with pytest.raises(KeyError, match="inexistente"):
        get("inexistente", "whatever")


def test_register_and_lookup() -> None:
    reg = _reg()
    register(reg)
    assert has("test-vendor", "test-stream") is True
    assert get("test-vendor", "test-stream").collector_cls is _NoopCollector
    assert "test-stream" in supported_streams("test-vendor")


def test_iter_for_platform_filters_by_platform() -> None:
    register(_reg(stream="a"))
    register(_reg(stream="b"))
    streams = {r.stream for r in iter_for_platform("test-vendor")}
    assert {"a", "b"}.issubset(streams)


def test_register_is_idempotent_but_overwrites() -> None:
    register(_reg(platform="overwrite"))
    first = get("overwrite", "test-stream")
    # Re-registrar com mesma key sobrescreve (comportamento esperado + warn).
    new_reg = _reg(platform="overwrite")
    register(new_reg)
    second = get("overwrite", "test-stream")
    assert first is not second  # dataclass novo
    assert second.collector_cls is _NoopCollector


def test_beat_key_is_stable() -> None:
    reg = get("sophos", "alerts")
    assert reg.beat_key == "sophos-alerts"


def test_registry_drives_pipeline_resolution() -> None:
    """O pipeline e os refreshers leem o mesmo registry — smoke check."""
    from ..auth.refreshers import refresher_for

    # Se o registry funciona, refresher_for resolve só com o nome da platform.
    fn = refresher_for("sophos")
    assert callable(fn)


def test_adding_new_vendor_requires_only_registry_entry() -> None:
    """Contrato de extensibilidade: após registrar, pipeline/beat veem o vendor."""
    reg = _reg(platform="new-edr", stream="threats")
    register(reg)

    # beat_schedule não precisa ser editado — iter_for_platform acha.
    entries = list(iter_for_platform("new-edr"))
    assert len(entries) == 1
    assert entries[0].task_name == "collectors.collect_vendor_logs_bulk"
    assert entries[0].queue == "collect.bulk"

    # pipeline usaria registry_get (sem map hardcoded).
    assert has("new-edr", "threats") is True
    assert get("new-edr", "threats").schedule == timedelta(minutes=10)
