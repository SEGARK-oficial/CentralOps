"""Vocabulário canônico de capability + observabilidade.

Trava que catálogo (PlatformRegistration.capabilities) E runtime
(BaseProvider.capabilities()) são SUBCONJUNTOS do vocabulário canônico
(app/collectors/capabilities.py) — fecha a divergência achada no review. E
smoke-testa a primitiva de observabilidade ``observe_capability``.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import pytest

from backend.app.collectors import registry
from backend.app.collectors.capabilities import (
    EXACT_CAPABILITIES,
    invalid_capabilities,
    is_valid_capability,
    CAP_DISCOVER_CHILDREN,
    CAP_ALERTS_LIST,
    CAP_ALERTS_SEARCH,
    CAP_LICENSING_LIST,
)
from backend.app.db import models


# ── Vocabulário: is_valid_capability ──────────────────────────────────


@pytest.mark.parametrize(
    "key,expected",
    [
        ("catalog", True),
        ("health", True),
        ("health:check", True),
        ("discover:children", True),
        ("alerts:list", True),
        ("licensing:list", True),
        # namespaces dinâmicos
        ("collect:detections", True),
        ("collect:alerts", True),
        ("query:opensearch_dsl", True),
        ("query:kql", True),
        # inválidos
        ("Health", False),           # case
        ("collect:", False),         # sufixo vazio
        ("foo:bar", False),          # namespace não-dinâmico desconhecido
        ("randomkey", False),        # não-namespaced fora do exato
        ("alerts:LIST", False),      # sufixo fora do slug
        ("", False),
    ],
)
def test_is_valid_capability(key, expected):
    assert is_valid_capability(key) is expected


# ── Catálogo: toda registration usa só keys válidas ───────────────────


def test_all_registered_platform_capabilities_are_canonical():
    platforms = registry.all_platforms()
    assert platforms, "registry de plataformas vazio — vendors não importaram?"
    offenders = {}
    for plat in platforms:
        bad = invalid_capabilities(plat.capabilities)
        if bad:
            offenders[plat.platform] = bad
    assert not offenders, f"capabilities de catálogo fora do vocabulário: {offenders}"


# ── Runtime: capabilities() dos providers ricos usam só keys válidas ──


def _integ(**kw):
    base = dict(name="x", organization_id=1, kind="tenant", platform="sophos")
    base.update(kw)
    return models.Integration(**base)


@pytest.mark.parametrize(
    "integration",
    [
        _integ(platform="sophos", kind="partner"),
        _integ(platform="sophos", kind="organization"),
        _integ(platform="sophos", kind="tenant"),
        _integ(platform="sophos", kind="tenant", parent_integration_id=10),
        _integ(platform="wazuh", kind="tenant"),
    ],
    ids=["sophos-partner", "sophos-org", "sophos-tenant", "sophos-child", "wazuh"],
)
def test_runtime_capabilities_are_canonical(integration):
    caps = registry.get_provider(integration).capabilities()
    bad = invalid_capabilities(caps)
    assert not bad, f"capabilities de runtime fora do vocabulário: {bad}"


def test_router_gated_keys_are_canonical():
    """As keys que o router gateia precisam estar no vocabulário exato."""
    for key in ("discover:children", "alerts:list", "licensing:list"):
        assert key in EXACT_CAPABILITIES


# ── Observabilidade: a primitiva observe_capability ───────────────────


def test_observe_capability_ok_path():
    from backend.app.collectors.metrics import observe_capability

    ran = False
    with observe_capability("sophos", "alerts:list"):
        ran = True
    assert ran  # contexto roda sem erro e emite a métrica (no-op em test OTel)


def test_observe_capability_reraises_on_error():
    from backend.app.collectors.metrics import observe_capability

    with pytest.raises(ValueError):
        with observe_capability("wazuh", "alerts:list"):
            raise ValueError("boom")  # outcome=error registrado, exceção propaga


# ── gate validado por constante (anti fail-open) ──


def test_named_constants_are_canonical():
    for const in (CAP_DISCOVER_CHILDREN, CAP_ALERTS_LIST, CAP_ALERTS_SEARCH,
                  CAP_LICENSING_LIST):
        assert is_valid_capability(const), const


def test_validate_capability_raises_on_typo():
    from backend.app.collectors.capabilities import validate_capability

    assert validate_capability("discover:children") == "discover:children"
    with pytest.raises(ValueError):
        validate_capability("discover:childrenn")  # o typo que era fail-open silencioso


def test_no_raw_literal_capability_membership_in_routers():
    """Nenhum gate de router pode checar pertinência de uma
    capability via LITERAL CRU (``"discover:children" in ...capabilities``) — um
    typo viraria um ``in`` que nunca casa (fail-OPEN). Use CAP_* + helper validado."""
    import re
    from pathlib import Path

    routers = Path(__file__).resolve().parents[1] / "app" / "routers"
    # literal de capability de GATE seguido (na mesma linha) de um membership em
    # algum *capabilities (integration_capabilities / provider.capabilities()).
    gate_caps = (
        "discover:children", "alerts:list", "alerts:search", "alerts:detail",
        "licensing:list",
    )
    pat = re.compile(
        r"""["'](?:%s)["']\s+(?:not\s+)?in\s+[\w.]*capabilities""" % "|".join(map(re.escape, gate_caps))
    )
    offenders = []
    for path in sorted(routers.glob("*.py")):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if pat.search(line):
                offenders.append(f"{path.name}:{i}: {line.strip()}")
    assert not offenders, (
        "gate de capability por LITERAL CRU (use CAP_* + integration_has_capability):\n"
        + "\n".join(offenders)
    )


def test_integration_has_capability_validates_then_checks():
    """O gate VALIDA a key (typo → ValueError) antes de checar a pertinência —
    sem isto um literal com typo virava um ``in`` que nunca casa (fail-open)."""
    from backend.app.collectors.capabilities import CAP_DISCOVER_CHILDREN
    from backend.app.collectors.registry import integration_has_capability

    partner = _integ(platform="sophos", kind="partner")
    standalone = _integ(platform="sophos", kind="tenant")
    assert integration_has_capability(partner, CAP_DISCOVER_CHILDREN) is True
    assert integration_has_capability(standalone, CAP_DISCOVER_CHILDREN) is False
    with pytest.raises(ValueError):
        integration_has_capability(partner, "discover:childrenn")
