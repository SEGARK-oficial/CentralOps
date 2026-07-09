"""Routing engine (pure). Covers the condition language,
per-event evaluation (first-match + is_final clone/stop, drop), batch → sub-batch
split, zero-loss fallback, validation, and unreachable detection — including the
acceptance scenario (severity_id>=4 → SIEM, else → S3)."""

from __future__ import annotations

import pytest

from backend.app.collectors.routing import (
    CompiledRoute,
    evaluate_event,
    find_unreachable,
    matches,
    order_routes,
    route_batch,
    validate_condition,
)
from backend.app.collectors.routing.engine import event_labels


def _route(rid, *, priority=100, condition=None, action="route", dests=(), final=True, enabled=True, canary=100):
    return CompiledRoute(
        id=rid,
        name=rid,
        priority=priority,
        condition=condition or {},
        action=action,
        destination_ids=tuple(dests),
        is_final=final,
        enabled=enabled,
        canary_percent=canary,
    )


def _ev(severity_id=1, vendor="sophos", org=1, event_type="alert"):
    return {
        "_centralops": {
            "severity_id": severity_id,
            "vendor": vendor,
            "organization_id": org,
            "event_type": event_type,
            "event_id": f"e-{severity_id}-{vendor}-{event_type}",
        }
    }


# ── Condition language ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "cond,labels,expected",
    [
        ({}, {"severity_id": 5}, True),  # catch-all
        ({"severity_id": 5}, {"severity_id": 5}, True),  # scalar eq
        ({"severity_id": 5}, {"severity_id": 4}, False),
        ({"severity_id": {"gte": 4}}, {"severity_id": 4}, True),
        ({"severity_id": {"gte": 4}}, {"severity_id": 3}, False),
        ({"severity_id": {"gt": 4}}, {"severity_id": 4}, False),
        ({"severity_id": {"lt": 2}}, {"severity_id": 1}, True),
        ({"severity_id": {"lte": 2}}, {"severity_id": 2}, True),
        ({"severity_id": {"ne": 5}}, {"severity_id": 4}, True),
        ({"vendor": {"in": ["sophos", "defender"]}}, {"vendor": "sophos"}, True),
        ({"vendor": {"in": ["defender"]}}, {"vendor": "sophos"}, False),
        ({"vendor": {"nin": ["defender"]}}, {"vendor": "sophos"}, True),
        ({"vendor": {"exists": True}}, {"vendor": "sophos"}, True),
        ({"vendor": {"exists": False}}, {"severity_id": 1}, True),  # absent
        ({"vendor": {"exists": True}}, {"severity_id": 1}, False),  # absent
        # multi-field AND
        ({"severity_id": {"gte": 4}, "vendor": "sophos"}, {"severity_id": 5, "vendor": "sophos"}, True),
        ({"severity_id": {"gte": 4}, "vendor": "sophos"}, {"severity_id": 5, "vendor": "defender"}, False),
        # missing field, positive op → no match (not a crash)
        ({"severity_id": {"gte": 4}}, {}, False),
        # incomparable types → no match, no crash
        ({"severity_id": {"gte": 4}}, {"severity_id": "high"}, False),
    ],
)
def test_matches(cond, labels, expected) -> None:
    assert matches(cond, labels) is expected


def test_event_labels_extracts_centralops() -> None:
    assert event_labels(_ev(severity_id=7))["severity_id"] == 7
    assert event_labels({"normalized": {}})  == {}


# ── Per-event evaluation ───────────────────────────────────────────────


def test_first_match_is_final_is_exclusive() -> None:
    routes = order_routes([
        _route("a", priority=10, condition={"severity_id": {"gte": 4}}, dests=["siem"], final=True),
        _route("b", priority=20, condition={}, dests=["s3"], final=True),
    ])
    d = evaluate_event(event_labels(_ev(severity_id=5)), routes)
    assert d.destinations == frozenset({"siem"})  # stopped at first final match
    assert d.matched and not d.dropped


def test_non_final_clones_and_continues() -> None:
    routes = order_routes([
        _route("a", priority=10, condition={"severity_id": {"gte": 4}}, dests=["siem"], final=False),
        _route("b", priority=20, condition={}, dests=["s3"], final=True),
    ])
    d = evaluate_event(event_labels(_ev(severity_id=5)), routes)
    # high-sev event fans out to BOTH siem (non-final) and s3 (catch-all)
    assert d.destinations == frozenset({"siem", "s3"})


def test_drop_is_terminal() -> None:
    routes = order_routes([
        _route("noise", priority=10, condition={"severity_id": {"lt": 1}}, action="drop", final=True),
        _route("all", priority=100, condition={}, dests=["s3"], final=True),
    ])
    assert evaluate_event(event_labels(_ev(severity_id=0)), routes).dropped is True
    assert evaluate_event(event_labels(_ev(severity_id=3)), routes).destinations == frozenset({"s3"})


def test_disabled_route_skipped() -> None:
    routes = order_routes([
        _route("a", priority=10, condition={}, dests=["siem"], final=True, enabled=False),
        _route("b", priority=20, condition={}, dests=["s3"], final=True),
    ])
    assert evaluate_event(event_labels(_ev()), routes).destinations == frozenset({"s3"})


def test_unmatched_event_is_not_matched() -> None:
    routes = order_routes([_route("a", priority=10, condition={"vendor": "defender"}, dests=["siem"])])
    d = evaluate_event(event_labels(_ev(vendor="sophos")), routes)
    assert not d.matched and d.destinations == frozenset()


# ── Batch routing (acceptance scenario) ────────────────────────────


def test_route_batch_severity_split_adr_acceptance() -> None:
    """severity_id>=4 → SIEM, else → S3 (acceptance scenario)."""
    routes = [
        _route("hi", priority=10, condition={"severity_id": {"gte": 4}}, dests=["siem"], final=True),
        _route("rest", priority=100, condition={}, dests=["s3"], final=True),
    ]
    batch = [_ev(severity_id=5), _ev(severity_id=2), _ev(severity_id=4), _ev(severity_id=1)]
    r = route_batch(batch, routes)
    assert len(r.sub_batches["siem"]) == 2  # sev 5, 4
    assert len(r.sub_batches["s3"]) == 2  # sev 2, 1
    assert r.routed == 4
    assert r.dropped == 0
    assert r.fallback == 0


def test_route_batch_drop_reduces_volume() -> None:
    routes = [
        _route("noise", priority=10, condition={"severity_id": {"lt": 1}}, action="drop", final=True),
        _route("rest", priority=100, condition={}, dests=["s3"], final=True),
    ]
    batch = [_ev(severity_id=0), _ev(severity_id=0), _ev(severity_id=5)]
    r = route_batch(batch, routes)
    assert r.dropped == 2
    assert len(r.sub_batches["s3"]) == 1
    assert r.routed == 1


def test_route_batch_unmatched_goes_to_configured_fallback() -> None:
    """vendor-neutro: com fallback CONFIGURADO, evento sem match vai pra ele."""
    routes = [_route("hi", priority=10, condition={"severity_id": {"gte": 9}}, dests=["siem"], final=True)]
    batch = [_ev(severity_id=9), _ev(severity_id=1)]
    r = route_batch(batch, routes, fallback_destination_id="fallback-dest")
    assert len(r.sub_batches["siem"]) == 1
    assert len(r.sub_batches["fallback-dest"]) == 1  # the unmatched sev-1 event
    assert r.fallback == 1
    assert r.unrouted == 0


def test_route_batch_unmatched_no_fallback_goes_unrouted() -> None:
    """vendor-neutro: SEM fallback, evento sem match → unrouted (DLQ),
    NUNCA um sink hardcoded (ex.: wazuh-default)."""
    routes = [_route("hi", priority=10, condition={"severity_id": {"gte": 9}}, dests=["siem"], final=True)]
    r = route_batch([_ev(severity_id=9), _ev(severity_id=1)], routes)
    assert len(r.sub_batches["siem"]) == 1
    assert "wazuh-default" not in r.sub_batches  # nenhum sink inventado
    assert r.unrouted == 1 and len(r.unrouted_events) == 1
    assert r.fallback == 0


def test_route_batch_no_routes_no_fallback_all_unrouted() -> None:
    r = route_batch([_ev(), _ev(severity_id=5)], [])
    assert not r.sub_batches  # nada entregue (sem sink hardcoded)
    assert r.unrouted == 2 and len(r.unrouted_events) == 2


def test_route_batch_fanout_event_in_multiple_subbatches() -> None:
    routes = [
        _route("a", priority=10, condition={"severity_id": {"gte": 4}}, dests=["siem"], final=False),
        _route("b", priority=20, condition={}, dests=["s3"], final=True),
    ]
    r = route_batch([_ev(severity_id=5)], routes)
    assert len(r.sub_batches["siem"]) == 1
    assert len(r.sub_batches["s3"]) == 1  # same event cloned to both


# ── Validation ─────────────────────────────────────────────────────────


def test_validate_condition_ok() -> None:
    validate_condition({})
    validate_condition({"severity_id": {"gte": 4}, "vendor": "sophos"})
    validate_condition({"vendor": {"in": ["a", "b"]}})


@pytest.mark.parametrize(
    "cond",
    [
        {"not_a_field": 1},  # unknown field
        {"severity_id": {"bogus": 4}},  # unknown op
        {"vendor": {"in": "notalist"}},  # in needs list
        {"vendor": {"exists": "yes"}},  # exists needs bool
        {"severity_id": {}},  # empty op map
        "notadict",  # not an object
    ],
)
def test_validate_condition_rejects(cond) -> None:
    with pytest.raises(ValueError):
        validate_condition(cond)


# ── Unreachable detection ──────────────────────────────────────────────


def test_unreachable_catch_all_shadows_rest() -> None:
    routes = order_routes([
        _route("catchall", priority=10, condition={}, dests=["s3"], final=True),
        _route("hi", priority=20, condition={"severity_id": {"gte": 4}}, dests=["siem"], final=True),
    ])
    assert find_unreachable(routes) == ["hi"]


def test_unreachable_duplicate_condition() -> None:
    routes = order_routes([
        _route("a", priority=10, condition={"vendor": "sophos"}, dests=["siem"], final=True),
        _route("b", priority=20, condition={"vendor": "sophos"}, dests=["s3"], final=True),
    ])
    assert find_unreachable(routes) == ["b"]


def test_non_final_does_not_shadow() -> None:
    routes = order_routes([
        _route("a", priority=10, condition={}, dests=["siem"], final=False),  # clone+continue
        _route("b", priority=20, condition={"severity_id": {"gte": 4}}, dests=["s3"], final=True),
    ])
    assert find_unreachable(routes) == []


def test_disabled_route_does_not_shadow_and_is_not_flagged() -> None:
    routes = order_routes([
        _route("a", priority=10, condition={}, dests=["siem"], final=True, enabled=False),
        _route("b", priority=20, condition={"severity_id": {"gte": 4}}, dests=["s3"], final=True),
    ])
    assert find_unreachable(routes) == []


def test_reachable_routes_not_flagged() -> None:
    routes = order_routes([
        _route("hi", priority=10, condition={"severity_id": {"gte": 4}}, dests=["siem"], final=True),
        _route("rest", priority=100, condition={}, dests=["s3"], final=True),
    ])
    assert find_unreachable(routes) == []


# ── Canary (% rollout) ─────────────────────────────────────────────────


def test_canary_full_100_is_no_op() -> None:
    routes = order_routes([_route("a", condition={}, dests=["siem"], canary=100)])
    assert evaluate_event(event_labels(_ev()), routes).destinations == frozenset({"siem"})


def test_canary_zero_never_matches_falls_through() -> None:
    routes = order_routes([
        _route("canary", priority=10, condition={}, dests=["new"], final=True, canary=0),
        _route("rest", priority=100, condition={}, dests=["old"], final=True),
    ])
    # 0% canary → no event takes 'canary'; all fall through to 'old'.
    for i in range(20):
        d = evaluate_event(event_labels(_ev(severity_id=i)), routes)
        assert d.destinations == frozenset({"old"})


def test_canary_is_deterministic_per_event_id() -> None:
    routes = order_routes([
        _route("canary", priority=10, condition={}, dests=["new"], final=True, canary=50),
        _route("rest", priority=100, condition={}, dests=["old"], final=True),
    ])
    ev = {"_centralops": {"event_id": "stable-id-123", "severity_id": 5}}
    first = evaluate_event(event_labels(ev), routes).destinations
    # Same event_id → same path every time (idempotent across retries).
    for _ in range(10):
        assert evaluate_event(event_labels(ev), routes).destinations == first


def test_canary_splits_population_roughly_by_percent() -> None:
    routes = order_routes([
        _route("canary", priority=10, condition={}, dests=["new"], final=True, canary=30),
        _route("rest", priority=100, condition={}, dests=["old"], final=True),
    ])
    new_count = 0
    n = 1000
    for i in range(n):
        ev = {"_centralops": {"event_id": f"evt-{i}", "severity_id": 5}}
        if "new" in evaluate_event(event_labels(ev), routes).destinations:
            new_count += 1
    # ~30% ± tolerance (deterministic hash distribution).
    assert 0.22 * n < new_count < 0.38 * n


def test_route_batch_per_route_counts() -> None:
    """per-route counts (fan-out events count toward every route hit)."""
    routes = [
        _route("hi", priority=10, condition={"severity_id": {"gte": 4}}, dests=["siem"], final=False),
        _route("rest", priority=100, condition={}, dests=["s3"], final=True),
    ]
    r = route_batch([_ev(severity_id=5), _ev(severity_id=2), _ev(severity_id=4)], routes)
    assert r.per_route["hi"] == 2  # sev 5 + 4 (clone, non-final)
    assert r.per_route["rest"] == 3  # all 3 reach the catch-all


def test_canary_route_does_not_shadow_later_routes() -> None:
    routes = order_routes([
        _route("canary", priority=10, condition={}, dests=["new"], final=True, canary=50),
        _route("rest", priority=100, condition={}, dests=["old"], final=True),
    ])
    # A <100% canary route never fully shadows (lets the rest through).
    assert find_unreachable(routes) == []


# ── data residency enforcement ────────────────────────────────────────────


def _ev_geo(geography: str | None, event_id: str = "e-1") -> dict:
    """Build an event envelope with a data_geography label."""
    meta: dict = {
        "severity_id": 3,
        "vendor": "sophos",
        "organization_id": 1,
        "event_id": event_id,
    }
    if geography is not None:
        meta["data_geography"] = geography
    return {"_centralops": meta}


def test_residency_no_conflict_passes_through() -> None:
    """Destination residency matches event geography → event is NOT blocked."""
    routes = [_route("r1", dests=["dest-eu"], final=True)]
    # dest-eu declared for EU; event is EU → passes.
    result = route_batch(
        [_ev_geo("EU")],
        routes,
        destination_residency={"dest-eu": "EU"},
    )
    assert "dest-eu" in result.sub_batches
    assert len(result.sub_batches["dest-eu"]) == 1
    assert result.residency_blocked == 0
    assert result.fallback == 0


def test_residency_conflict_excludes_destination() -> None:
    """Destination EU constraint + US event → destination excluded; sem fallback
    configurado, o evento vai p/ unrouted (DLQ), não p/ um sink hardcoded."""
    routes = [_route("r1", dests=["dest-eu"], final=True)]
    result = route_batch(
        [_ev_geo("US")],
        routes,
        destination_residency={"dest-eu": "EU"},
    )
    # dest-eu must be excluded.
    assert "dest-eu" not in result.sub_batches
    # Vendor-neutro: sem fallback → unrouted (zero-loss via DLQ).
    assert result.unrouted == 1 and len(result.unrouted_events) == 1
    assert "wazuh-default" not in result.sub_batches
    assert result.residency_blocked == 1
    assert result.fallback == 0
    assert result.routed == 0


def test_residency_global_destination_accepts_any_geography() -> None:
    """data_residency=global → accepts events from any geography."""
    routes = [_route("r1", dests=["dest-global"], final=True)]
    for geo in ("EU", "US", "BR", "APAC"):
        result = route_batch(
            [_ev_geo(geo)],
            routes,
            destination_residency={"dest-global": "global"},
        )
        assert "dest-global" in result.sub_batches, f"global dest should accept geo={geo}"
        assert result.residency_blocked == 0


def test_residency_null_destination_accepts_any_geography() -> None:
    """data_residency=None (no restriction) → destination accepts everything."""
    routes = [_route("r1", dests=["dest-any"], final=True)]
    for geo in ("EU", "US", "BR"):
        result = route_batch(
            [_ev_geo(geo)],
            routes,
            destination_residency={"dest-any": None},
        )
        assert "dest-any" in result.sub_batches
        assert result.residency_blocked == 0


def test_residency_unknown_geography_not_blocked() -> None:
    """Event with no data_geography → residency enforcement is NOT applied (conservative)."""
    routes = [_route("r1", dests=["dest-eu"], final=True)]
    # Event has no geography → passes regardless of destination residency.
    result = route_batch(
        [_ev_geo(None)],
        routes,
        destination_residency={"dest-eu": "EU"},
    )
    assert "dest-eu" in result.sub_batches
    assert result.residency_blocked == 0


def test_residency_no_map_provided_no_enforcement() -> None:
    """When destination_residency is not passed at all → no enforcement (backward-compat)."""
    routes = [_route("r1", dests=["dest-eu"], final=True)]
    result = route_batch([_ev_geo("US")], routes)  # no destination_residency kwarg
    assert "dest-eu" in result.sub_batches
    assert result.residency_blocked == 0


def test_residency_fan_out_partial_block() -> None:
    """Fan-out: one destination matches residency, another is blocked → partial fan-out."""
    # Route with two destinations: dest-eu (EU) and dest-us (US).
    routes = [_route("r1", dests=["dest-eu", "dest-us"], final=True)]
    result = route_batch(
        [_ev_geo("US")],
        routes,
        destination_residency={"dest-eu": "EU", "dest-us": "US"},
    )
    # dest-us passes; dest-eu is blocked.
    assert "dest-us" in result.sub_batches
    assert "dest-eu" not in result.sub_batches
    assert result.residency_blocked == 1
    assert result.routed == 1  # event WAS routed (to dest-us)
    assert result.fallback == 0


def test_residency_all_blocked_goes_unrouted() -> None:
    """Quando TODOS os destinos do fan-out são bloqueados (residency) e não há
    fallback → unrouted (DLQ), zero-loss vendor-neutro."""
    routes = [_route("r1", dests=["dest-eu", "dest-br"], final=True)]
    result = route_batch(
        [_ev_geo("US")],
        routes,
        destination_residency={"dest-eu": "EU", "dest-br": "BR"},
    )
    assert "dest-eu" not in result.sub_batches
    assert "dest-br" not in result.sub_batches
    assert result.unrouted == 1 and len(result.unrouted_events) == 1
    assert result.residency_blocked == 2
    assert result.fallback == 0


def test_residency_data_geography_as_routing_label() -> None:
    """data_geography can also be used as a routing CONDITION (ALLOWED_FIELDS check)."""
    from backend.app.collectors.routing import validate_condition

    # Should not raise — data_geography is an allowed routing field.
    validate_condition({"data_geography": {"in": ["EU", "US"]}})
    validate_condition({"data_geography": "EU"})


# ── platform label ───────────────────────────────────────────────────────


def _ev_platform(platform: str, *, severity_id: int = 3, event_id: str = "e-1") -> dict:
    return {
        "_centralops": {
            "platform": platform,
            "severity_id": severity_id,
            "event_id": event_id,
        }
    }


def test_platform_is_an_allowed_routing_field() -> None:
    validate_condition({"platform": "microsoft_defender"})
    validate_condition({"platform": {"in": ["sophos", "microsoft_defender"]}})


def test_route_condition_on_platform() -> None:
    """A route can condition on the ``platform`` label."""
    routes = order_routes([
        _route("defender", priority=10, condition={"platform": "microsoft_defender"}, dests=["siem"], final=True),
        _route("rest", priority=100, condition={}, dests=["s3"], final=True),
    ])
    d_def = evaluate_event(event_labels(_ev_platform("microsoft_defender")), routes)
    assert d_def.destinations == frozenset({"siem"})
    d_sophos = evaluate_event(event_labels(_ev_platform("sophos")), routes)
    assert d_sophos.destinations == frozenset({"s3"})


def test_route_batch_platform_split() -> None:
    routes = [
        _route("def", priority=10, condition={"platform": {"in": ["microsoft_defender"]}}, dests=["siem"], final=True),
        _route("rest", priority=100, condition={}, dests=["s3"], final=True),
    ]
    batch = [
        _ev_platform("microsoft_defender", event_id="a"),
        _ev_platform("sophos", event_id="b"),
        _ev_platform("microsoft_defender", event_id="c"),
    ]
    r = route_batch(batch, routes)
    assert len(r.sub_batches["siem"]) == 2
    assert len(r.sub_batches["s3"]) == 1


# ── org_id alias == organization_id ──────────────────────────────────────


def test_org_id_alias_matches_organization_id() -> None:
    """A route authored with ``org_id`` matches against the canonical
    ``organization_id`` envelope label."""
    routes = order_routes([
        _route("tenant42", priority=10, condition={"org_id": 42}, dests=["siem"], final=True),
        _route("rest", priority=100, condition={}, dests=["s3"], final=True),
    ])
    ev_match = {"_centralops": {"organization_id": 42, "event_id": "x"}}
    ev_other = {"_centralops": {"organization_id": 7, "event_id": "y"}}
    assert evaluate_event(event_labels(ev_match), routes).destinations == frozenset({"siem"})
    assert evaluate_event(event_labels(ev_other), routes).destinations == frozenset({"s3"})


def test_org_id_alias_validates() -> None:
    validate_condition({"org_id": 42})
    validate_condition({"org_id": {"in": [1, 2, 3]}})


def test_org_id_and_organization_id_are_interchangeable_in_matches() -> None:
    labels = {"organization_id": 99}
    assert matches({"org_id": 99}, labels) is True
    assert matches({"organization_id": 99}, labels) is True
    assert matches({"org_id": 100}, labels) is False


# ── Unreachable: predicate subsumption (find_unreachable extension) ────────────


def test_unreachable_superset_in_shadows_narrower_eq() -> None:
    """An earlier is_final route whose condition SUBSUMES a later one shadows it:
    ``{vendor in [sophos, defender]}`` (A) ⊇ ``{vendor: sophos}`` (B)."""
    routes = order_routes([
        _route("broad", priority=10, condition={"vendor": {"in": ["sophos", "defender"]}}, dests=["siem"], final=True),
        _route("narrow", priority=20, condition={"vendor": "sophos"}, dests=["s3"], final=True),
    ])
    assert find_unreachable(routes) == ["narrow"]


def test_unreachable_disjoint_predicates_do_not_shadow() -> None:
    """Disjoint conditions never shadow each other (no false positive)."""
    routes = order_routes([
        _route("a", priority=10, condition={"vendor": "sophos"}, dests=["siem"], final=True),
        _route("b", priority=20, condition={"vendor": "defender"}, dests=["s3"], final=True),
    ])
    assert find_unreachable(routes) == []


def test_unreachable_narrower_does_not_shadow_broader() -> None:
    """A narrower earlier route does NOT shadow a broader later one (B leaks
    through): ``{vendor: sophos}`` does not subsume ``{vendor in [sophos, x]}``."""
    routes = order_routes([
        _route("narrow", priority=10, condition={"vendor": "sophos"}, dests=["siem"], final=True),
        _route("broad", priority=20, condition={"vendor": {"in": ["sophos", "defender"]}}, dests=["s3"], final=True),
    ])
    assert find_unreachable(routes) == []


def test_unreachable_subsumption_respects_org_id_alias() -> None:
    """Alias normalization: A on ``organization_id`` subsumes B on ``org_id``."""
    routes = order_routes([
        _route("a", priority=10, condition={"organization_id": {"in": [1, 2]}}, dests=["siem"], final=True),
        _route("b", priority=20, condition={"org_id": 1}, dests=["s3"], final=True),
    ])
    assert find_unreachable(routes) == ["b"]


def test_unreachable_multi_field_subsumption() -> None:
    """A constrains a subset of B's fields with broader sets → A subsumes B."""
    routes = order_routes([
        # A: vendor in {sophos, defender}  (no severity constraint)
        _route("a", priority=10, condition={"vendor": {"in": ["sophos", "defender"]}}, dests=["siem"], final=True),
        # B: vendor=sophos AND severity_id=5  → strictly inside A
        _route("b", priority=20, condition={"vendor": "sophos", "severity_id": 5}, dests=["s3"], final=True),
    ])
    assert find_unreachable(routes) == ["b"]


def test_unreachable_a_constrains_field_b_does_not_no_shadow() -> None:
    """A restricts a field B leaves open → B leaks through, not shadowed."""
    routes = order_routes([
        _route("a", priority=10, condition={"vendor": "sophos"}, dests=["siem"], final=True),
        _route("b", priority=20, condition={"severity_id": 5}, dests=["s3"], final=True),
    ])
    assert find_unreachable(routes) == []


def test_unreachable_range_condition_is_conservative() -> None:
    """Non-enumerable clauses (ranges) are not proven to subsume → no false
    positive, even if mathematically one contains the other."""
    routes = order_routes([
        _route("a", priority=10, condition={"severity_id": {"gte": 3}}, dests=["siem"], final=True),
        _route("b", priority=20, condition={"severity_id": 5}, dests=["s3"], final=True),
    ])
    # Conservative: we do NOT flag b (gte ranges are not enumerated).
    assert find_unreachable(routes) == []


def test_unreachable_route_after_catch_all_is_final() -> None:
    """Any route after an earlier catch-all is_final route is unreachable."""
    routes = order_routes([
        _route("catchall", priority=10, condition={}, dests=["s3"], final=True),
        _route("x", priority=20, condition={"vendor": "sophos"}, dests=["siem"], final=True),
        _route("y", priority=30, condition={"platform": "microsoft_defender"}, dests=["lake"], final=True),
    ])
    assert set(find_unreachable(routes)) == {"x", "y"}


def test_unreachable_canary_superset_does_not_shadow() -> None:
    """A canary (<100%) is_final route never shadows a later route, even if its
    condition would subsume — the non-canary fraction falls through."""
    routes = order_routes([
        _route("broad-canary", priority=10, condition={"vendor": {"in": ["sophos", "defender"]}}, dests=["new"], final=True, canary=50),
        _route("narrow", priority=20, condition={"vendor": "sophos"}, dests=["old"], final=True),
    ])
    assert find_unreachable(routes) == []


# ── Canary-by-label: canary is scoped to the route's matched population ────────


def test_canary_scoped_by_route_condition() -> None:
    """A route with condition severity>=4 + canary 25% applies the canary fraction
    ONLY among matching (sev>=4) events; sev<4 events are untouched by the gate
    and fall through to the catch-all. Confirms canary-by-label already works."""
    routes = order_routes([
        _route("canary", priority=10, condition={"severity_id": {"gte": 4}}, dests=["new-siem"], final=True, canary=25),
        _route("rest", priority=100, condition={}, dests=["old-siem"], final=True),
    ])

    high_to_new = 0
    high_total = 0
    low_to_new = 0
    n = 800
    for i in range(n):
        # Half high-sev (5), half low-sev (1) — distinct event_ids for bucketing.
        sev = 5 if i % 2 == 0 else 1
        ev = {"_centralops": {"severity_id": sev, "event_id": f"evt-{i}"}}
        dests = evaluate_event(event_labels(ev), routes).destinations
        if sev >= 4:
            high_total += 1
            if "new-siem" in dests:
                high_to_new += 1
        else:
            if "new-siem" in dests:
                low_to_new += 1

    # No low-sev event ever reaches the canary destination (scoped by label).
    assert low_to_new == 0
    # Among high-sev events, roughly 25% take the canary (deterministic hash).
    frac = high_to_new / high_total
    assert 0.18 < frac < 0.32, f"canary fraction among matched events was {frac:.3f}"


def test_canary_by_label_batch_only_matched_events_sampled() -> None:
    """Batch-level: canary on a label-scoped route never diverts non-matching
    events to the canary destination."""
    routes = [
        _route("canary", priority=10, condition={"severity_id": {"gte": 4}}, dests=["new"], final=True, canary=50),
        _route("rest", priority=100, condition={}, dests=["old"], final=True),
    ]
    # 4 low-sev events: none may ever land in "new".
    batch = [
        {"_centralops": {"severity_id": 1, "event_id": f"low-{i}"}} for i in range(4)
    ]
    r = route_batch(batch, routes)
    assert "new" not in r.sub_batches  # zero low-sev events diverted to canary
    assert len(r.sub_batches["old"]) == 4


# ── Wazuh-source loop protection ──────────────────────
# Wazuh é FONTE (pull do Indexer); NÃO é tipo de destino — o destino é syslog, e
# ``wazuh-default`` entrega via syslog ao próprio Wazuh. Um evento cuja fonte é
# integração wazuh NUNCA pode cair no catch-all/sink ``wazuh-default`` (loop
# fonte↔destino). Exclusão GLOBAL no route_batch.


def _ev_wazuh(severity_id=5, org=1, event_type="wazuh.detection"):
    return {
        "_centralops": {
            "severity_id": severity_id,
            "vendor": "wazuh",
            "platform": "wazuh",
            "organization_id": org,
            "event_type": event_type,
            "event_id": f"w-{severity_id}-{event_type}",
        }
    }


def _ev_sophos(severity_id=5, org=1):
    return {
        "_centralops": {
            "severity_id": severity_id,
            "vendor": "sophos",
            "platform": "sophos",
            "organization_id": org,
            "event_type": "sophos.alert",
            "event_id": f"s-{severity_id}",
        }
    }


def test_wazuh_source_unmatched_is_loop_blocked_not_fallback():
    """Fonte wazuh sem rota: NÃO cai no catch-all wazuh-default (loop) — é descartada."""
    res = route_batch([_ev_wazuh()], [])
    assert "wazuh-default" not in res.sub_batches
    assert res.loop_blocked == 1
    assert res.fallback == 0


def test_non_wazuh_source_unmatched_goes_unrouted():
    """Vendor-neutro: fonte não-wazuh sem rota e SEM fallback → unrouted (DLQ)."""
    res = route_batch([_ev_sophos()], [])
    assert res.unrouted == 1 and len(res.unrouted_events) == 1
    assert "wazuh-default" not in res.sub_batches
    assert res.fallback == 0
    assert res.loop_blocked == 0


def test_wazuh_source_explicit_loop_dest_is_suppressed():
    """Rota EXPLÍCITA p/ um destino-LOOP (host-based) é suprimida p/ fonte wazuh.
    Loop é por-host (via wazuh_loop_destination_ids), não por sentinela hardcoded."""
    routes = [_route("r1", condition={"platform": "wazuh"}, dests=("loopdest",))]
    res = route_batch([_ev_wazuh()], routes, wazuh_loop_destination_ids=frozenset({"loopdest"}))
    assert "loopdest" not in res.sub_batches
    assert res.loop_blocked == 1
    assert res.routed == 0


def test_wazuh_source_to_other_destination_is_routed():
    """Fonte wazuh roteada a um destino real (s3) entrega normal; sem wazuh-default."""
    routes = [_route("r1", condition={"platform": "wazuh"}, dests=("s3",))]
    res = route_batch([_ev_wazuh()], routes)
    assert len(res.sub_batches.get("s3", [])) == 1
    assert "wazuh-default" not in res.sub_batches
    assert res.routed == 1
    assert res.loop_blocked == 0


def test_wazuh_source_mixed_dests_strips_only_loop_dest():
    """Rota wazuh→{s3, loopdest}: entrega só ao s3; o destino-loop é removido."""
    routes = [_route("r1", condition={"platform": "wazuh"}, dests=("s3", "loopdest"))]
    res = route_batch([_ev_wazuh()], routes, wazuh_loop_destination_ids=frozenset({"loopdest"}))
    assert len(res.sub_batches.get("s3", [])) == 1
    assert "loopdest" not in res.sub_batches
    assert res.routed == 1
    assert res.loop_blocked == 0


# ── anti-loop GENÉRICO (não só o sentinela) ────


def test_wazuh_loop_set_suppresses_generic_syslog_dest():
    """Um syslog dest genérico (UUID) que aponta ao manager Wazuh — passado em
    wazuh_loop_destination_ids — é suprimido p/ fonte wazuh (não só wazuh-default)."""
    loop_id = "syslog-uuid-aaa"
    routes = [_route("r1", condition={"platform": "wazuh"}, dests=(loop_id,))]
    res = route_batch([_ev_wazuh()], routes, wazuh_loop_destination_ids=frozenset({loop_id}))
    assert loop_id not in res.sub_batches
    assert res.loop_blocked == 1
    assert res.routed == 0


def test_wazuh_loop_set_mixed_keeps_non_loop_dest():
    """wazuh→{s3, syslog-loop}: com o syslog no loop set, entrega só ao s3."""
    loop_id = "syslog-uuid-bbb"
    routes = [_route("r1", condition={"platform": "wazuh"}, dests=("s3", loop_id))]
    res = route_batch([_ev_wazuh()], routes, wazuh_loop_destination_ids=frozenset({loop_id}))
    assert len(res.sub_batches.get("s3", [])) == 1
    assert loop_id not in res.sub_batches
    assert res.routed == 1
    assert res.loop_blocked == 0


def test_loop_set_does_not_affect_non_wazuh_source():
    """O loop guard só vale p/ FONTE wazuh — sophos→syslog-loop entrega normal."""
    loop_id = "syslog-uuid-ccc"
    routes = [_route("r1", condition={"platform": "sophos"}, dests=(loop_id,))]
    res = route_batch([_ev_sophos()], routes, wazuh_loop_destination_ids=frozenset({loop_id}))
    assert len(res.sub_batches.get(loop_id, [])) == 1
    assert res.routed == 1
    assert res.loop_blocked == 0
