"""Escritas em FK de ``app_users`` por SERVICE ACCOUNTS não podem violar constraint.

Regressão do incidente jul/2026: SA autentica como shim de ``AppUser`` com id
sintético NEGATIVO (``-<sa.id>``, ver ``auth._build_sa_appuser_shim``) que não
existe na tabela. ``create_version`` gravava ``author_user_id=-1`` →
``ForeignKeyViolation`` → 500 no commit de mapping via MCP; o middleware de
audit perdia a linha de ``audit_logs`` pelo mesmo motivo (só WARNING no log).
O fix canônico é ``auth.persistable_user_id``: usuário real → id; shim/SA →
``None`` (atribuição preservada em ``username='sa:<name>'``).
"""

from __future__ import annotations

import pytest

from backend.app.core import auth as app_auth
from backend.app.db import models


class _Shim:
    def __init__(self, uid):
        self.id = uid


@pytest.mark.parametrize(
    "value,expected",
    [
        (7, 7),                    # usuário real (int)
        (-1, None),                # shim de SA id=1
        (-12, None),               # shim de SA id=12
        (0, None),                 # id impossível
        (None, None),              # anônimo
        (True, None),              # bool é subclasse de int — nunca é id válido
        ("7", None),               # tipo errado nunca vai pra FK
    ],
)
def test_persistable_user_id_raw_values(value, expected):
    assert app_auth.persistable_user_id(value) == expected


def test_persistable_user_id_accepts_appuser_like_objects():
    assert app_auth.persistable_user_id(_Shim(42)) == 42
    assert app_auth.persistable_user_id(_Shim(-3)) is None


def test_sa_shim_id_is_negative_and_sanitized():
    """O contrato do shim (id = -sa.id) continua o que o fix pressupõe."""
    sa = models.ServiceAccount(
        id=5, name="mcp-bot", role="admin", organization_id=None, is_active=True
    )
    shim = app_auth._build_sa_appuser_shim(sa)
    assert shim.id == -5
    assert shim.username == "sa:mcp-bot"
    assert app_auth.persistable_user_id(shim) is None
