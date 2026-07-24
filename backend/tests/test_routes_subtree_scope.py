"""Escopo SUBTREE-AWARE na visibilidade de rotas.

INCIDENTE: um admin escopado na org PAI (MSP) não via as rotas nem o /flow das
orgs FILHAS — só da própria. Resolveram promovendo o usuário a admin GLOBAL, o
que lhe deu leitura E escrita sobre TODOS os clientes da plataforma (escalação de
privilégio como workaround).

Diagnóstico: em Community o resolver de escopo é FLAT por contrato de edição — o
pai realmente não vê a filha, e isso é limitação, não bug. MAS o router de rotas
usava IGUALDADE EXATA de organization_id, então a tela era estruturalmente
incapaz de mostrar rotas da subárvore MESMO com o resolver Enterprise registrado
— divergindo do /flow, que já lista as FONTES da subárvore via
accessible_org_ids. Estes testes travam o novo contrato pelos dois lados.
"""
from __future__ import annotations

from backend.app.db import repository


class _Q:
    """Query fake que só registra os filtros aplicados."""

    def __init__(self):
        self.filters = []

    def filter(self, expr):
        self.filters.append(str(expr))
        return self


def _scope(org_id=None, global_scope=False, org_ids=None):
    repo = repository.RouteRepository.__new__(repository.RouteRepository)
    return repo._scope(_Q(), org_id, global_scope, org_ids).filters


def test_global_scope_applies_no_filter():
    assert _scope(global_scope=True) == []


def test_subtree_uses_in_not_equality():
    """O coração da correção: com a subárvore {pai, filha1, filha2} o filtro tem
    que ser IN (...), não '= pai'."""
    (expr,) = _scope(org_id=1, org_ids={1, 2, 3})
    assert " IN " in expr.upper()


def test_global_routes_stay_visible_in_subtree_mode():
    """Rota GLOBAL (org NULL) vale para todo tenant — não pode sumir quando o
    filtro passa a ser por conjunto."""
    (expr,) = _scope(org_id=1, org_ids={1, 2})
    assert "IS NULL" in expr.upper()


def test_empty_subtree_sees_only_global_routes():
    """Escopado sem nenhuma org acessível: só as rotas compartilhadas."""
    (expr,) = _scope(org_id=None, org_ids=set())
    assert "IS NULL" in expr.upper() and " IN " not in expr.upper()


def test_flat_community_behaviour_is_unchanged():
    """Sem org_ids (call-site sem sessão) o comportamento anterior é preservado:
    igualdade exata + globais."""
    (expr,) = _scope(org_id=7)
    assert "IS NULL" in expr.upper()
    assert " IN " not in expr.upper()


def test_single_org_subtree_matches_flat_semantics():
    """Community: o resolver FLAT devolve {própria org} — mesmo resultado
    efetivo do caminho antigo, agora expresso como IN de um elemento."""
    (expr,) = _scope(org_id=7, org_ids={7})
    assert " IN " in expr.upper() and "IS NULL" in expr.upper()
