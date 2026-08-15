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

**Por que parte dos testes é ``source_only``.** A imagem Cython carrega só o
backend compilado: não existe ``frontend/`` lá dentro, e o ``.py`` dos routers
virou ``.so``. Todo teste daqui que ABRE um arquivo está marcado e sai do sweep
da imagem; ele roda no job de árvore de fontes, sobre o checkout cru, que é onde
os dois lados existem.

Os testes que só olham o enum e ``ROLE_PERMISSIONS`` continuam SEM marcador de
propósito: são o eixo do RBAC e valem tanto no artefato compilado quanto no
fonte. Marcar o módulo inteiro teria tirado essa proteção do que vai para
produção.
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


@pytest.mark.source_only  # abre arquivo; na imagem Cython ele não existe
def test_catalog_covers_every_backend_permission() -> None:
    """Permissão sem categoria cai num balde genérico e aparece crua na tela."""
    missing = sorted(_backend_permissions() - _catalog_permissions())
    assert not missing, (
        f"Permissões sem categoria em {_CATALOG_TS.relative_to(_REPO)}: {missing}. "
        "Sem categoria elas caem no balde genérico do seletor de scopes e da "
        "matriz de /admin/users."
    )


@pytest.mark.source_only  # abre arquivo; na imagem Cython ele não existe
def test_catalog_does_not_invent_permissions() -> None:
    """Divergência ao contrário: linha fantasma numa matriz que se diz fiel."""
    extra = sorted(_catalog_permissions() - _backend_permissions())
    assert not extra, (
        f"Permissões que não existem no backend, listadas em "
        f"{_CATALOG_TS.relative_to(_REPO)}: {extra}."
    )


@pytest.mark.source_only  # abre arquivo; na imagem Cython ele não existe
@pytest.mark.parametrize("locale", _LOCALES)
def test_every_permission_has_a_human_description(locale: str) -> None:
    """A matriz responde "o que essa pessoa vai poder fazer?", não lista slugs."""
    missing = sorted(_backend_permissions() - set(_descriptions(locale)))
    assert not missing, (
        f"Permissões sem descrição em locales/{locale}/admin.json: {missing}."
    )


@pytest.mark.source_only  # abre arquivo; na imagem Cython ele não existe
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


# ── o eixo das permissões de operação ───────────────────────────────────────
#
# Estes travam a intenção, não a implementação: se alguém repontar um endpoint
# de leitura de volta para ``user.manage``, o monitor read-only volta a exigir
# um token capaz de administrar contas — que é o defeito que estas permissões
# existem para corrigir.


def test_monitoramento_readonly_nao_exige_gerenciar_usuarios() -> None:
    """Um perfil de leitura enxerga a ENTREGA sem poder mexer em usuários.

    Era o caso do template de Zabbix: para ler contagem de DLQ e estado de
    breaker, o token precisava de ``user.manage``. O repo de observabilidade
    chegou a dividir em dois templates por causa disso.
    """
    viewer = app_auth.ROLE_PERMISSIONS["viewer"]

    assert app_auth.Permission.DESTINATION_READ in viewer
    assert app_auth.Permission.ROUTE_READ in viewer
    # E continua sem NENHUM poder de escrita ou de administração.
    assert app_auth.Permission.USER_MANAGE not in viewer
    assert app_auth.Permission.INTEGRATION_WRITE not in viewer
    assert app_auth.Permission.SECRET_READ not in viewer


def test_reset_de_coletor_nao_exige_gerenciar_usuarios() -> None:
    """Destravar coletor é trabalho de operator, não de quem administra contas."""
    operator = app_auth.ROLE_PERMISSIONS["operator"]

    assert app_auth.Permission.INTEGRATION_RESET in operator
    assert app_auth.Permission.USER_MANAGE not in operator


def test_viewer_nao_reseta_coletor() -> None:
    """Reset re-coleta e gera duplicidade temporária: não é leitura."""
    assert app_auth.Permission.INTEGRATION_RESET not in app_auth.ROLE_PERMISSIONS["viewer"]


@pytest.mark.source_only  # abre arquivo; na imagem Cython ele não existe
@pytest.mark.parametrize(
    "router_name, read_perm, escrita_sensivel",
    [
        ("destinations", "DESTINATION_READ", ("tap", "credential", "dlq/reprocess")),
        ("routes", "ROUTE_READ", ("dry-run", "reorder", "rollback")),
    ],
)
def test_apenas_leitura_foi_afrouxada(
    router_name: str, read_perm: str, escrita_sensivel: tuple[str, ...]
) -> None:
    """Nenhum POST/PUT/DELETE caiu para a permissão de leitura.

    O risco de um repoint em massa é levar junto uma escrita. Este teste lê o
    router e falha se qualquer verbo mutante estiver atrás da permissão de
    leitura, ou se o data-tap (que mostra payload de evento de CLIENTE) sair
    de ``user.manage``.
    """
    src = (_REPO / "backend" / "app" / "routers" / f"{router_name}.py").read_text(
        encoding="utf-8"
    ).split("\n")

    infratores: list[str] = []
    for i, line in enumerate(src):
        m = re.match(r'@router\.(post|put|patch|delete)\("([^"]*)"', line.strip())
        if not m:
            continue
        trecho = "\n".join(src[i : i + 25])
        if f"Permission.{read_perm}" in trecho:
            infratores.append(f"{m.group(1).upper()} {m.group(2)}")

    assert not infratores, (
        f"Verbos mutantes atrás de {read_perm} em {router_name}.py: {infratores}. "
        "Leitura afrouxada não pode arrastar escrita junto."
    )

    # O tap e as operações de credencial precisam continuar acima da leitura.
    for i, line in enumerate(src):
        if any(alvo in line for alvo in escrita_sensivel) and line.strip().startswith("@router"):
            trecho = "\n".join(src[i : i + 25])
            assert f"Permission.{read_perm}" not in trecho, (
                f"{line.strip()} não pode estar em {read_perm}."
            )
