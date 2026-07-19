"""Capability anunciada = capability executável (spec_kinds).

ADR-0015, Fase 0. ``QueryCapability.spec_kinds`` é declaração ESTÁTICA do catálogo
do vendor: ``wazuh`` e ``defender`` anunciam ``sigma`` porque o dialeto tem backend
pySigma oficial — não porque pySigma esteja instalado. pySigma é dependência
OPCIONAL (``requirements-query-abstractions.txt``) e não entra na imagem
(``compose/Dockerfile`` instala apenas requirements.lock|txt + otel).

Sem o filtro de runtime, ``GET /providers/query-capabilities`` anuncia ``sigma``,
a UI oferece a opção (``frontend/src/components/queries/CreateQueryForm.tsx:35``),
o analista escreve a regra e só descobre no submit que o tradutor não existe
(HTTP 501 de ``SigmaUnavailableError``, ``centralops_ee/query/service.py:167``).

Este teste trava as duas metades do contrato:
  * ``passthrough`` NUNCA some (é o caminho nativo — sumir seria pior que degradar);
  * um spec_kind com dependência ausente NÃO é anunciado.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import pytest

from backend.app.collectors import capabilities
from backend.app.collectors.capabilities import (
    SPEC_OCSF_QUERYSPEC,
    SPEC_PASSTHROUGH,
    SPEC_SIGMA,
    available_spec_kinds,
)


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    """``_spec_kind_available`` é ``lru_cache``d — isolar entre casos."""
    capabilities._spec_kind_available.cache_clear()
    yield
    capabilities._spec_kind_available.cache_clear()


def test_passthrough_is_never_filtered(monkeypatch):
    """Mesmo com TODA dependência ausente, passthrough sobrevive."""
    monkeypatch.setattr(capabilities.importlib.util, "find_spec", lambda _m: None)
    assert available_spec_kinds((SPEC_PASSTHROUGH, SPEC_SIGMA)) == [SPEC_PASSTHROUGH]


def test_sigma_advertised_when_pysigma_present(monkeypatch):
    monkeypatch.setattr(
        capabilities.importlib.util, "find_spec", lambda _m: object()
    )
    assert available_spec_kinds((SPEC_PASSTHROUGH, SPEC_SIGMA)) == [
        SPEC_PASSTHROUGH,
        SPEC_SIGMA,
    ]


def test_sigma_hidden_when_pysigma_absent(monkeypatch):
    """O caso que motivou a correção: anunciar o que não se entrega."""
    monkeypatch.setattr(
        capabilities.importlib.util,
        "find_spec",
        lambda m: None if m == "sigma" else object(),
    )
    assert SPEC_SIGMA not in available_spec_kinds((SPEC_PASSTHROUGH, SPEC_SIGMA))


def test_order_is_preserved():
    """A ordem declarada pelo vendor é contrato de UI (1º = default do formulário)."""
    declared = (SPEC_PASSTHROUGH, SPEC_OCSF_QUERYSPEC)
    assert available_spec_kinds(declared) == list(declared)


def test_spec_kind_without_probe_is_always_available():
    """spec_kind sem dependência externa registrada não é filtrado por engano."""
    assert SPEC_OCSF_QUERYSPEC not in capabilities._SPEC_KIND_RUNTIME_PROBE
    assert available_spec_kinds((SPEC_OCSF_QUERYSPEC,)) == [SPEC_OCSF_QUERYSPEC]


def test_declared_catalog_matches_reality():
    """Guard de coerência: todo spec_kind com probe registrado existe no vocabulário.

    Um typo em ``_SPEC_KIND_RUNTIME_PROBE`` viraria um filtro que nunca casa —
    fail-open silencioso, exatamente o padrão que este repo já combate em
    ``validate_capability``.
    """
    known = {SPEC_PASSTHROUGH, SPEC_SIGMA, SPEC_OCSF_QUERYSPEC}
    unknown = set(capabilities._SPEC_KIND_RUNTIME_PROBE) - known
    assert not unknown, f"probe registrado para spec_kind inexistente: {unknown}"
