"""O ``publish`` não pode promover imagem que um gate reprovou.

Este arquivo existe por causa de um buraco encontrado em auditoria: o job
``source-tree-guards`` **não estava** no ``needs`` do ``publish``. Como os testes
marcados ``source_only`` não rodam dentro da imagem (lá o ``.py`` não existe),
aquele job é o ÚNICO lugar que os executa — e, fora do ``needs``, eles
reprovavam o check do PR enquanto a imagem subia para ``latest`` assim mesmo.

O que estava desprotegido não era detalhe. Entre esses guards está o que reprova
``import redis|httpx|sqlalchemy`` em ``inflight/matcher.py`` — a restrição R1 do
ADR-0015, que é a fronteira objetiva entre este produto e um SIEM. Um import
acrescentado ali custa um round-trip por evento no gargalo do pipeline, e era
publicável.

Um gate que reprova e não bloqueia é pior que gate nenhum: ele produz o registro
de que alguém verificou.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML ausente")

_CI = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"

#: Jobs cujo veredito TEM de bloquear a promoção da imagem. Lista fechada e
#: explícita: crescer aqui é decisão consciente, e encolher reprova.
_GATES_QUE_BLOQUEIAM = frozenset({"build", "pg-migrations", "e2e", "source-tree-guards"})


def _ci() -> dict:
    if not _CI.is_file():
        pytest.skip(
            f"{_CI} ausente — rodando fora da árvore do repositório (imagem "
            "compilada não recebe .github/)."
        )
    return yaml.safe_load(_CI.read_text(encoding="utf-8"))


def test_publish_depende_de_todos_os_gates() -> None:
    jobs = _ci()["jobs"]

    # Anti-vacuidade: sem isto, um ci.yml que perdesse o job `publish` (ou fosse
    # renomeado) faria o assert abaixo passar sobre um conjunto vazio.
    assert "publish" in jobs, "job `publish` sumiu do ci.yml"
    needs = set(jobs["publish"].get("needs") or [])
    assert needs, "`publish` ficou sem `needs` — promove imagem sem esperar nada"

    faltando = _GATES_QUE_BLOQUEIAM - needs
    assert not faltando, (
        f"o publish promove a imagem sem esperar {sorted(faltando)}. Um gate fora "
        f"do `needs` reprova o check do PR e NÃO impede o push para `latest` — "
        f"foi exatamente assim que `source-tree-guards` ficou decorativo."
    )


def test_os_gates_declarados_existem_de_fato() -> None:
    """O PAR POSITIVO. `needs` apontando para job inexistente não é erro de
    sintaxe: o GitHub aceita o YAML e o workflow falha no *startup*, o que neste
    repositório significa **zero jobs e zero log** — o PR parece verde.

    Sem este caso, alguém poderia satisfazer o teste acima escrevendo um nome
    errado no `needs`."""
    jobs = _ci()["jobs"]
    declarados = set(jobs["publish"].get("needs") or [])

    orfaos = declarados - set(jobs)
    assert not orfaos, (
        f"`publish.needs` aponta para job(s) inexistente(s): {sorted(orfaos)}. "
        "O workflow falharia no startup — sem job, sem log, e o PR parecendo verde."
    )


def test_nenhum_gate_depende_do_publish() -> None:
    """Ciclo no grafo tem o mesmo sintoma do órfão: startup failure silencioso."""
    jobs = _ci()["jobs"]
    for nome in _GATES_QUE_BLOQUEIAM:
        if nome not in jobs:
            continue
        needs = set(jobs[nome].get("needs") or [])
        assert "publish" not in needs, (
            f"`{nome}` depende de `publish`, que depende dele — ciclo no grafo de "
            "jobs derruba o workflow inteiro no startup."
        )
