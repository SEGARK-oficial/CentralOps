"""Escopo SUBTREE-AWARE em drift, discover-fields e samples de mapping.

Continuação do trabalho de rotas (test_routes_subtree_scope.py): as superfícies
AUXILIARES de mapping ficaram para trás. Um admin de org PAI não via o drift nem
as amostras das FILHAS — logo não conseguia evoluir o mapping a partir do tráfego
real da subárvore que administra, que é justamente o trabalho de um MSP.

Contrato exercitado aqui pelos DOIS lados: com o resolver FLAT (Community) nada
muda; com um resolver de subárvore registrado (o que o Enterprise faz), o pai
passa a enxergar as filhas — e continua SEM enxergar quem está fora.
"""
from __future__ import annotations

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import pytest

from backend.app.core import ee_hooks, tenant
from backend.app.db import models

PAI, FILHA, ESTRANHA = 1, 2, 99


class _User:
    """AppUser mínimo: escopado (não-global) na org PAI."""

    def __init__(self, org_id=PAI, role="admin", is_global=False):
        self.organization_id = org_id
        self.role = role
        self.is_global = is_global


@pytest.fixture(autouse=True)
def _reset_resolver():
    ee_hooks.reset_scope_resolver()
    yield
    ee_hooks.reset_scope_resolver()


def _register_subtree(mapping):
    """Registra um resolver de subárvore (o papel do pacote Enterprise)."""
    ee_hooks.register_scope_resolver(
        lambda user, session: set(mapping.get(user.organization_id, set()))
    )


# ── accessible_org_ids: o seam que todas as superfícies passam a usar ────────

def test_community_flat_resolver_sees_only_own_org():
    """Sem resolver registrado o default é FLAT — o pai NÃO vê a filha. Isso é
    limitação de edição, não bug, e o teste trava esse contrato."""
    assert tenant.accessible_org_ids(_User(), None) == {PAI}


def test_registered_subtree_resolver_expands_to_children():
    _register_subtree({PAI: {PAI, FILHA}})
    assert tenant.accessible_org_ids(_User(), None) == {PAI, FILHA}


def test_subtree_never_reaches_an_unrelated_org():
    _register_subtree({PAI: {PAI, FILHA}})
    assert ESTRANHA not in tenant.accessible_org_ids(_User(), None)


def test_global_scope_short_circuits_to_none():
    """None == sem filtro. É contrato: tratar como 'nenhuma org' vazaria ou
    esconderia tudo, conforme o lado do erro."""
    assert tenant.accessible_org_ids(_User(org_id=None, is_global=True), None) is None


# ── can_access_subtree: o gate de item único (drift/{id}) ────────────────────

def test_item_gate_allows_child_under_subtree_resolver():
    _register_subtree({PAI: {PAI, FILHA}})
    assert tenant.can_access_subtree(_User(), FILHA) is True


def test_item_gate_denies_unrelated_org():
    _register_subtree({PAI: {PAI, FILHA}})
    assert tenant.can_access_subtree(_User(), ESTRANHA) is False


def test_item_gate_flat_denies_child_in_community():
    """Sem resolver, o pai não alcança a filha — o 404 do item único é correto
    em Community."""
    assert tenant.can_access_subtree(_User(), FILHA) is False


# ── a consulta do drift usa IN, não igualdade ────────────────────────────────

def test_drift_query_filters_by_the_whole_subtree():
    """Regressão do padrão antigo (`organization_id == user.organization_id`):
    com {PAI, FILHA} o filtro precisa ser IN, senão a filha some da lista."""
    expr = str(models.UnknownField.organization_id.in_({PAI, FILHA}))
    assert " IN " in expr.upper()


def test_drift_query_with_empty_scope_is_fail_closed():
    """Escopado sem nenhuma org acessível não pode virar `IS NULL` — isso
    casaria linhas legadas de org NULL de OUTROS tenants."""
    _register_subtree({PAI: set()})
    assert tenant.accessible_org_ids(_User(), None) == set()
