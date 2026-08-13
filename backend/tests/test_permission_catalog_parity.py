"""Paridade entre o RBAC do backend e como o console o explica ao operador.

O backend é a fonte da verdade de QUAIS permissões existem (``Permission``
StrEnum em ``core/auth.py``, exposto por ``GET /auth/permissions``). O frontend
acrescenta duas coisas que o backend não tem: a CATEGORIA de cada permissão e
uma DESCRIÇÃO legível.

Essas duas camadas divergiram na prática. ``query.run``, ``query.save`` e
``correlation.preview`` foram adicionadas ao enum e o seletor de scopes do
token seguiu jogando as três num balde "Outros", sem descrição: quem emitia um
token via três strings cruas e tinha que adivinhar o que marcavam.

**Por que este teste está no pytest e não no vitest.** Ele precisa ler o fonte
Python E os arquivos do frontend ao mesmo tempo. O Vite recusa importar de fora
da raiz do projeto (``fs.allow``), e afrouxar isso por causa de um teste seria
trocar uma trava de segurança do bundler por conveniência. Aqui os dois lados
são só arquivos.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from backend.app.core import auth as app_auth

_REPO = Path(__file__).resolve().parents[2]
_CATALOG_TS = _REPO / "frontend" / "src" / "lib" / "permissions.ts"
_ADMIN_PT = _REPO / "frontend" / "src" / "i18n" / "locales" / "pt" / "admin.json"
_LOCALES = ("pt", "en", "es")


def _backend_permissions() -> set[str]:
    """Toda permissão declarada, direto do enum — nunca uma cópia."""
    return {str(p) for p in app_auth.Permission}


def _catalog_permissions() -> set[str]:
    """Chaves de ``PERMISSION_CATALOG`` em ``frontend/src/lib/permissions.ts``."""
    src = _CATALOG_TS.read_text(encoding="utf-8")
    start = src.index("export const PERMISSION_CATALOG")
    end = src.index("}", start)
    return set(re.findall(r'"([^"]+)":\s*"[a-zA-Z]+"', src[start:end]))


def _descriptions(locale: str) -> dict[str, str]:
    path = _REPO / "frontend" / "src" / "i18n" / "locales" / locale / "admin.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("permissions", {}).get("descriptions", {})


def test_catalog_covers_every_backend_permission() -> None:
    """Permissão sem categoria cai num balde genérico e aparece crua na tela."""
    missing = sorted(_backend_permissions() - _catalog_permissions())
    assert not missing, (
        f"Permissões sem categoria em {_CATALOG_TS.relative_to(_REPO)}: {missing}. "
        "Sem categoria elas caem no balde genérico do seletor de scopes e da "
        "matriz de /admin/users."
    )


def test_catalog_does_not_invent_permissions() -> None:
    """Divergência ao contrário: linha fantasma numa matriz que se diz fiel."""
    extra = sorted(_catalog_permissions() - _backend_permissions())
    assert not extra, (
        f"Permissões que não existem no backend, listadas em "
        f"{_CATALOG_TS.relative_to(_REPO)}: {extra}."
    )


@pytest.mark.parametrize("locale", _LOCALES)
def test_every_permission_has_a_human_description(locale: str) -> None:
    """A matriz responde "o que essa pessoa vai poder fazer?", não lista slugs."""
    missing = sorted(_backend_permissions() - set(_descriptions(locale)))
    assert not missing, (
        f"Permissões sem descrição em locales/{locale}/admin.json: {missing}."
    )


@pytest.mark.parametrize("locale", _LOCALES)
def test_descriptions_do_not_outlive_the_permission(locale: str) -> None:
    extra = sorted(set(_descriptions(locale)) - _backend_permissions())
    assert not extra, (
        f"Descrições órfãs em locales/{locale}/admin.json (permissão não existe "
        f"mais no backend): {extra}."
    )


def test_matrix_endpoint_serves_the_same_permissions() -> None:
    """O que a UI recebe é o que este teste conferiu.

    ``GET /auth/permissions`` devolve ``ROLE_PERMISSIONS`` derivado do enum. Se
    um dia ele passasse a filtrar ou renomear, o catálogo estaria certo e a tela
    errada — e nenhum dos testes acima perceberia.
    """
    served = {perm for perms in app_auth.ROLE_PERMISSIONS.values() for perm in perms}
    # admin tem todas por construção (`{p for p in Permission}`), então a união
    # das roles é exatamente o enum.
    assert {str(p) for p in served} == _backend_permissions()
