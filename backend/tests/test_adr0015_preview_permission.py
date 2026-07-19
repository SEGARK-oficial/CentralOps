"""``CORRELATION_PREVIEW`` não pode vazar para papéis de leitura (ADR-0015, Fase 3).

O preview de regra devolve dados derivados de EVENTOS REAIS de cliente — valores
observados de campos do payload. Isso o torna categoricamente diferente das
outras rotas de correlação, que só leem CONFIGURAÇÃO.

Por isso a permissão é própria, e não reuso:

* ``MAPPING_READ`` está em VIEWER. Herdar dela faria um viewer passar a ler
  payload de cliente por um endpoint novo, sem que ninguém tivesse decidido isso.
* ``QUERY_RUN`` está em OPERATOR e hoje autoriza apenas LER a configuração das
  regras (``correlation_rules.py`` a usa nas rotas de listagem). Ampliá-la para
  liberar payload alargaria em silêncio o alcance de uma permissão já concedida
  a quem já a tem.

Este repositório já fechou 11 gaps de vazamento cross-org; a forma daquele
incidente foi exatamente esta — uma permissão existente ganhando alcance novo
sem revisão.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import pytest

from backend.app.core.auth import ROLE_PERMISSIONS, Permission, UserRole


@pytest.mark.parametrize("role", [UserRole.VIEWER, UserRole.OPERATOR])
def test_read_only_roles_never_get_correlation_preview(role):
    """O guard central deste arquivo.

    Se algum dia alguém "simplificar" concedendo a permissão a estes papéis, o
    CI reprova com o motivo escrito.
    """
    assert Permission.CORRELATION_PREVIEW not in ROLE_PERMISSIONS[role], (
        f"{role} recebeu CORRELATION_PREVIEW — o preview expõe valores de "
        "eventos reais de cliente e não pode ser concedido a papel de leitura"
    )


def test_engineer_can_preview():
    """Quem escreve a regra precisa poder testá-la; senão a permissão nova só
    cria atrito sem cobrir ninguém."""
    assert Permission.CORRELATION_PREVIEW in ROLE_PERMISSIONS[UserRole.ENGINEER]


def test_admin_can_preview():
    assert Permission.CORRELATION_PREVIEW in ROLE_PERMISSIONS[UserRole.ADMIN]


def test_preview_is_not_an_alias_of_an_existing_permission():
    """Guard contra a "simplificação" mais provável: reusar QUERY_RUN ou
    MAPPING_READ em vez de manter a permissão própria."""
    assert Permission.CORRELATION_PREVIEW.value == "correlation.preview"
    assert Permission.CORRELATION_PREVIEW not in (
        Permission.QUERY_RUN,
        Permission.QUERY_SAVE,
        Permission.MAPPING_READ,
    )


def test_viewer_still_has_only_read_configuration_permissions():
    """Sanidade da matriz: VIEWER não deve ter ganhado nada que toque payload."""
    viewer = ROLE_PERMISSIONS[UserRole.VIEWER]
    forbidden = {
        Permission.CORRELATION_PREVIEW,
        Permission.QUERY_RUN,
        Permission.QUERY_SAVE,
        Permission.MAPPING_WRITE,
        Permission.SECRET_READ,
    }
    leaked = forbidden & set(viewer)
    assert not leaked, f"VIEWER ganhou permissões de escrita/dados: {leaked}"


def test_permission_matrix_has_no_role_without_entry():
    """Uma role sem entrada na matriz cairia num ``KeyError`` em runtime ou,
    pior, num ``.get(role, set())`` que negaria tudo em silêncio."""
    for role in UserRole:
        assert role in ROLE_PERMISSIONS, f"{role} não está na matriz"
