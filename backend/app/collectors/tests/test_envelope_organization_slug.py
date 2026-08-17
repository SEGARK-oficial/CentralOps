"""O slug precisa CHEGAR ao envelope, senão a derivação é decorativa.

``row_fields_from`` resolve o rótulo lendo ``_centralops.organization_slug``. A
derivação tem cobertura própria em ``test_row_fields_from``; o que se testa aqui
é o outro lado, que é onde a falha é invisível: se um caminho de produção monta
o ``EnvelopeContext`` sem o slug, aquele caminho passa a entregar ``unresolved``
enquanto os outros entregam o rótulo certo. Mesma tabela, mesmo cliente, duas
facetas — e nada falha.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from backend.app.collectors.normalize.envelope import EnvelopeContext, build_envelope


def test_o_envelope_carrega_o_slug():
    env = build_envelope(
        {"a": 1},
        {"class_uid": 2004},
        EnvelopeContext(
            vendor="sophos",
            integration_id=7,
            customer_id=3,
            stream="alerts",
            event_type="sophos.alert",
            mapping_version_id=None,
            organization_slug="acme-corp",
        ),
    )
    assert env["_centralops"]["organization_slug"] == "acme-corp"


def test_o_slug_e_um_campo_PROPRIO_e_nao_o_nome():
    """São duas coisas distintas de propósito: ``customer_name`` é editável na
    tela e um rename mudaria o rótulo de todo evento novo. Se alguém colapsar os
    dois campos, isto avisa."""
    env = build_envelope(
        {},
        {},
        EnvelopeContext(
            vendor="v",
            integration_id=1,
            customer_id=1,
            stream="s",
            event_type="e",
            mapping_version_id=None,
            customer_name="Acme Corp",
            organization_slug="acme-corp",
        ),
    )
    meta = env["_centralops"]
    assert meta["customer_name"] == "Acme Corp"
    assert meta["organization_slug"] == "acme-corp"


def test_ausente_vira_None_e_nao_explode():
    """Fluxo antigo/de teste que não passa o slug precisa continuar montando o
    envelope — quem decide o fallback é o destino, não o envelope."""
    env = build_envelope(
        {},
        {},
        EnvelopeContext(
            vendor="v",
            integration_id=1,
            customer_id=1,
            stream="s",
            event_type="e",
            mapping_version_id=None,
        ),
    )
    assert env["_centralops"]["organization_slug"] is None


# ── Nenhum caminho de produção pode esquecer o slug ──────────────────────────

#: Todo módulo que monta envelope para EVENTO REAL. Um caminho novo que não
#: entre nesta lista escapa do guard — por isso a lista é curta e o teste
#: falha com o nome do arquivo, não com um contador.
_CAMINHOS_DE_PRODUCAO = (
    "backend/app/collectors/pipeline.py",
    "backend/app/collectors/backfill_tasks.py",
    "backend/app/collectors/scheduler_tasks.py",
    "backend/app/collectors/normalize/reprocess.py",
    "backend/app/collectors/inflight/preview.py",
)


def _raiz() -> pathlib.Path:
    # 5 níveis: tests → collectors → app → backend → raiz do repo.
    return pathlib.Path(__file__).resolve().parents[4]


@pytest.mark.source_only  # lê a árvore de fonte; na imagem Cython ela não existe
@pytest.mark.parametrize("caminho", _CAMINHOS_DE_PRODUCAO)
def test_todo_call_site_de_producao_passa_o_slug(caminho):
    """AST e não substring: o que importa é o ARGUMENTO da chamada.

    Um grep por "organization_slug" no arquivo passaria com a variável
    resolvida e nunca passada adiante — que é exatamente o erro plausível aqui,
    porque resolver o slug e esquecer de encaminhá-lo não muda nada visível.
    """
    arquivo = _raiz() / caminho
    arvore = ast.parse(arquivo.read_text())
    chamadas = [
        no
        for no in ast.walk(arvore)
        if isinstance(no, ast.Call)
        and isinstance(no.func, ast.Name)
        and no.func.id == "EnvelopeContext"
    ]
    assert chamadas, f"{caminho} não monta EnvelopeContext — tire-o da lista"
    sem_slug = [
        no.lineno
        for no in chamadas
        if "organization_slug" not in {kw.arg for kw in no.keywords}
    ]
    assert sem_slug == [], (
        f"{caminho}: EnvelopeContext sem organization_slug nas linhas {sem_slug}. "
        "Esse caminho entregaria 'unresolved' num destino que deriva o rótulo, "
        "enquanto os outros caminhos entregam o slug — divergência silenciosa "
        "na mesma tabela."
    )
