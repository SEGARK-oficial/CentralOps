"""Gate do carve-out + contrato de capability-gating.

A "definition of done" do carve-out é binária: deve ser possível
deletar o ``if integration.kind in (...)`` dos routers SEM perder função. Este
teste TRAVA isso — falha no PR se alguém readicionar gating por kind/parent-type
no router em vez de gatear por capability (``integration_capabilities``).

NB: ``integration.platform ==`` NÃO é proibido aqui porque há usos legítimos
(serialização de campos vendor-specific no read-model, shape de config no
update, filtros SQL). A execução de query/ação ainda Sophos-hardcoded
(search/blocks/scheduled_queries) é dívida rastreada, não
neste gate.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROUTERS_DIR = Path(__file__).resolve().parents[1] / "app" / "routers"

# Anti-pattern: gating por TIPO de integração (parent MSSP) no core via kind.
# O gate cego a ``not in`` dava DoD-verde falsa — havia
# 3 ``integration.kind not in (...)`` VIVOS passando. Cobre agora: ``in``/``not in``
# com ``(`` | ``[`` | ``{`` (tupla/lista/set), e ``==``/``!=`` p/ partner|organization.
_KIND_IN = re.compile(r"\.kind\s+(?:not\s+)?in\s*[\(\[{]")
_KIND_EQ_PARENT = re.compile(r"""\.kind\s*(?:==|!=)\s*['"](partner|organization)['"]""")


def _router_sources():
    return sorted(_ROUTERS_DIR.glob("*.py"))


def test_routers_dir_found():
    assert _ROUTERS_DIR.is_dir(), f"routers dir não encontrado: {_ROUTERS_DIR}"
    assert _router_sources(), "nenhum router .py encontrado"


@pytest.mark.parametrize("path", _router_sources(), ids=lambda p: p.name)
def test_no_kind_based_gating_in_router(path: Path):
    """Nenhum router pode ramificar por ``integration.kind in (...)`` /
    ``.kind == "partner"|"organization"`` — use a capability ``discover:children``
    via ``integration_capabilities(integration)``."""
    src = path.read_text(encoding="utf-8")
    offenders = []
    for i, line in enumerate(src.splitlines(), start=1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue  # comentário citando o anti-pattern é permitido
        if _KIND_IN.search(line) or _KIND_EQ_PARENT.search(line):
            offenders.append(f"{path.name}:{i}: {stripped}")
    assert not offenders, (
        "Gating por kind reintroduzido no router (use capability discover:children "
        "via integration_capabilities):\n" + "\n".join(offenders)
    )


# ── Red-test do próprio gate ───────────────────


@pytest.mark.parametrize(
    "snippet",
    [
        'if integration.kind in ("partner", "organization"):',
        'if integration.kind not in ("partner", "organization"):',   # era o furo
        'if integration.kind in ["partner"]:',
        'if integration.kind in {"partner"}:',
        'if integration.kind == "partner":',
        'if integration.kind != "organization":',
    ],
)
def test_gate_regex_catches_kind_gating(snippet: str):
    """O gate DEVE pegar todas as formas de ramificar por kind — inclusive a
    negada (``not in`` / ``!=``) que passava batido antes."""
    assert _KIND_IN.search(snippet) or _KIND_EQ_PARENT.search(snippet), (
        f"gate cego ao anti-pattern: {snippet!r}"
    )


def test_gate_regex_allows_legitimate_platform_eq():
    """``platform ==`` (serialização/config) NÃO é proibido — não pode dar match."""
    ok = 'if integration.platform == "sophos":'
    assert not _KIND_IN.search(ok) and not _KIND_EQ_PARENT.search(ok)


# ── Contrato do capability-gating (trava o que o router gateia) ────────


def _integ(**kw):
    from backend.app.db import models

    base = dict(name="x", organization_id=1, kind="tenant", platform="sophos")
    base.update(kw)
    return models.Integration(**base)


def test_runtime_capabilities_drive_gating():
    """Trava as capabilities de runtime que substituíram os if-vendor."""
    from backend.app.collectors.registry import integration_capabilities

    partner = _integ(kind="partner")
    child = _integ(kind="tenant", parent_integration_id=10)
    standalone = _integ(kind="tenant")
    wazuh = _integ(platform="wazuh")
    ninjaone = _integ(platform="ninjaone")

    # children_count / bulk / delete / backfill gateiam por discover:children
    assert "discover:children" in integration_capabilities(partner)
    assert "discover:children" not in integration_capabilities(standalone)
    assert "discover:children" not in integration_capabilities(ninjaone)

    # licensing preview gateia por licensing:list (só child tenant Sophos)
    assert "licensing:list" in integration_capabilities(child)
    assert "licensing:list" not in integration_capabilities(standalone)

    # a superfície de alerts foi REMOVIDA — wazuh não emite mais alerts:*
    # (busca federada usa query:opensearch_dsl)
    assert not any(cap.startswith("alerts:") for cap in integration_capabilities(wazuh))
    assert "query:opensearch_dsl" in integration_capabilities(wazuh)

    # plataforma sem provider rico ⇒ conjunto vazio (nunca parent/licensing)
    assert integration_capabilities(ninjaone) == frozenset()
