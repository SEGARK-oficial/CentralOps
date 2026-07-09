"""Registry de fonte ÚNICO.

Trava a unificação: o registry legado ``app/providers/registry.py`` (dict
``_PROVIDERS`` paralelo) foi ELIMINADO; a resolução do BaseProvider rico vive
agora no ``collectors.registry`` via ``PlatformRegistration.provider_factory``.

Se alguém reintroduzir o registry paralelo, hardcodar a lista de plataformas, ou
quebrar a factory de sophos/wazuh, estes testes falham.
"""
from __future__ import annotations

import importlib

import pytest

from backend.app.collectors import registry as reg
from backend.app.db.models import Integration


def test_legacy_providers_registry_is_gone():
    """O registry paralelo não pode voltar a existir."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("backend.app.providers.registry")


def test_get_provider_resolves_via_collectors_registry():
    """sophos/wazuh resolvem para o BaseProvider rico via factory tardia."""
    sophos = reg.get_provider(Integration(platform="sophos"))
    wazuh = reg.get_provider(Integration(platform="wazuh"))
    assert type(sophos).__name__ == "SophosProvider"
    assert type(wazuh).__name__ == "WazuhProvider"


def test_get_provider_unknown_platform_raises_valueerror():
    """Plataforma só-catálogo/coleta (sem factory) preserva o erro do registry legado."""
    with pytest.raises(ValueError):
        reg.get_provider(Integration(platform="ninjaone"))


def test_provider_supported_platforms_is_registry_derived():
    """A lista vem das registrations COM provider_factory — nunca hardcode.

    Base: {sophos, wazuh}. Há providers ricos de QUERY a
    crowdstrike (FQL) e microsoft_defender (KQL) → entram aqui. Vendors só-coleta
    sem provider rico (ninjaone) NÃO entram."""
    platforms = set(reg.provider_supported_platforms())
    assert {"sophos", "wazuh", "crowdstrike", "microsoft_defender"} <= platforms
    assert "ninjaone" not in platforms


def test_platform_registration_has_capability_model_fields():
    """O capability model existe na registration de cada vendor."""
    sophos = reg.get_platform("sophos")
    assert sophos is not None
    # campos novos presentes + tipados
    assert isinstance(sophos.capabilities, frozenset)
    assert isinstance(sophos.required_secrets, tuple)
    assert sophos.provider_factory is not None  # sophos tem provider rico
    assert sophos.variant == ""  # variante única (split vem na F1)
    # capabilities declaradas refletem a coleta + ações do Sophos
    assert "collect:alerts" in sophos.capabilities
    assert "discover:children" in sophos.capabilities


def test_collect_only_vendor_has_no_provider_factory():
    """ninjaone é catálogo+coleta — sem provider_factory (sem query). (defender
    ganhou provider rico de query KQL, então saiu deste grupo.)"""
    ninja = reg.get_platform("ninjaone")
    assert ninja is not None
    assert ninja.provider_factory is None
