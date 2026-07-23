"""Route.drop_raw — descarte do bloco ``raw`` por-destino.

É a decisão que o ``raw_reduction`` do mapping NÃO pode tomar: o mesmo evento
vai íntegro ao lago (DFIR precisa do bruto) e enxuto ao SIEM (que cobra por
volume). Um mapping serve N destinos; a escolha keep/drop é por-rota.
"""
from __future__ import annotations

from backend.app.collectors.routing.engine import (
    CompiledRoute,
    SamplingConfig,
    route_batch,
)


def _env(event_id: str = "e1", org: int = 7) -> dict:
    return {
        "_centralops": {"event_id": event_id, "organization_id": org, "vendor": "sophos"},
        "normalized": {"severity_id": 3},
        "raw": {"rawData": {"blob": "x" * 4000, "meta": "m"}},
    }


def _route(rid: str, dest: str, **kw) -> CompiledRoute:
    base = dict(
        id=rid, name=rid, priority=100, condition={}, action="route",
        destination_ids=(dest,), is_final=False, protect_detection=False,
    )
    base.update(kw)
    return CompiledRoute(**base)


def test_drop_raw_removes_the_block_for_that_destination_only():
    """O ponto central: lago recebe o bruto, SIEM não — do MESMO evento."""
    lake = _route("r-lake", "d-lake", drop_raw=False)
    siem = _route("r-siem", "d-siem", drop_raw=True)

    result = route_batch([_env()], [lake, siem])

    (to_lake,) = result.sub_batches["d-lake"]
    (to_siem,) = result.sub_batches["d-siem"]
    assert "raw" in to_lake, "o lago precisa do bruto para forense"
    assert "raw" not in to_siem, "o SIEM não deve pagar pelo bruto"


def test_drop_raw_marks_provenance():
    """O destino tem que distinguir 'descartamos' de 'o vendor não mandou'."""
    result = route_batch([_env()], [_route("r", "d", drop_raw=True)])
    (out,) = result.sub_batches["d"]
    assert out["_centralops"]["raw_dropped"] is True


def test_drop_raw_preserves_normalized_and_labels():
    result = route_batch([_env()], [_route("r", "d", drop_raw=True)])
    (out,) = result.sub_batches["d"]
    assert out["normalized"] == {"severity_id": 3}
    assert out["_centralops"]["event_id"] == "e1"


def test_drop_raw_never_mutates_the_shared_envelope():
    env = _env()
    route_batch([env], [_route("r", "d", drop_raw=True)])
    assert "raw" in env, "o envelope compartilhado do fan-out não pode ser mutado"


def test_protect_detection_blocks_drop_raw():
    """Fail-safe idêntico ao do sampling: quem alimenta detecção não perde
    fidelidade sem opt-out consciente."""
    protegida = _route("r", "d", drop_raw=True, protect_detection=True)
    result = route_batch([_env()], [protegida])
    (out,) = result.sub_batches["d"]
    assert "raw" in out


def test_drop_raw_off_is_byte_identical():
    env = _env()
    result = route_batch([env], [_route("r", "d", drop_raw=False)])
    (out,) = result.sub_batches["d"]
    assert out is env  # mesma referência: zero cópia


def test_drop_raw_credits_bytes_saved_when_measuring():
    result = route_batch(
        [_env()], [_route("r", "d", drop_raw=True)], measure_drop_bytes=True
    )
    assert result.raw_dropped_bytes_per_org.get(7, 0) > 0


def test_drop_raw_does_not_measure_when_metering_off():
    """measure_drop_bytes=False (COST_METERING off) ⇒ nenhuma serialização."""
    result = route_batch(
        [_env()], [_route("r", "d", drop_raw=True)], measure_drop_bytes=False
    )
    assert not result.raw_dropped_bytes_per_org


def test_drop_raw_composes_with_sampling():
    """Uma rota amostrada que também dropa raw: o evento mantido sai sem raw e
    com o rótulo de sample_rate."""
    r = _route("r", "d", drop_raw=True, sample_percent=100)
    result = route_batch(
        [_env()], [r], sampling=SamplingConfig(enabled=True, protect_detection_enforced=True)
    )
    (out,) = result.sub_batches["d"]
    assert "raw" not in out


def test_event_without_raw_is_a_noop():
    env = {"_centralops": {"event_id": "e", "organization_id": 7}, "normalized": {}}
    result = route_batch([env], [_route("r", "d", drop_raw=True)])
    (out,) = result.sub_batches["d"]
    assert out is env  # nada a remover → sem cópia
