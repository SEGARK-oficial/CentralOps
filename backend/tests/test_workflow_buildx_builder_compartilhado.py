"""Builder buildx com nome FIXO não pode ser removido no fim do job.

O ``ci.yml`` usa um builder nomeado (``centralops-ci``) com ``keep-state`` para
o estado do BuildKit sobreviver entre jobs no runner self-hosted. O container
dele (``buildx_buildkit_centralops-ci0``) é UM SÓ no Docker da máquina: o
``build`` da API, o ``build-frontend`` e os jobs de qualquer outro run
concorrente usam o mesmo. O ``DOCKER_CONFIG`` isolado por job (ver
``test_workflow_docker_config_isolado.py``) esconde a INSTÂNCIA, mas não o
container.

O ``docker/setup-buildx-action`` tem ``cleanup: true`` por padrão: no post-step
ele roda ``docker buildx rm <nome> --keep-state``, e isso derruba o build que
outro job ainda está fazendo no mesmo container. O sintoma foi a main vermelha
desde o #125: o "Build once" da API morria com ``graceful_stop`` ("container is
marked for removal") segundos depois que o ``build-frontend`` terminava. Na PR
tinha passado por sorte de horário.

Regra: todo ``setup-buildx-action`` com ``name`` fixo desliga o ``cleanup`` no
self-hosted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML ausente")

_WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"

#: Jobs que hoje usam builder nomeado e TÊM de aparecer na varredura — sem isto,
#: um ``uses`` reescrito ou um glob quebrado aprovaria por vacuidade.
_JOBS_CONHECIDOS = frozenset({("ci.yml", "build"), ("ci.yml", "build-frontend")})


def _nomeado(with_: dict) -> bool:
    nome = str(with_.get("name") or "").strip()
    return bool(nome) and nome != "''"


def _cleanup_desligado_no_self_hosted(with_: dict) -> bool:
    valor = with_.get("cleanup")
    if valor is False or str(valor).strip().lower() == "false":
        return True
    texto = " ".join(str(valor or "").split())
    return "runner.environment != 'self-hosted'" in texto


def _violacao(with_: dict) -> str | None:
    if not _nomeado(with_):
        return None
    if _cleanup_desligado_no_self_hosted(with_):
        return None
    return "builder nomeado com cleanup ligado (o buildx rm do post-step derruba o build de outro job)"


def _builders_nomeados() -> dict[tuple[str, str], list[dict]]:
    if not _WORKFLOWS.is_dir():
        pytest.skip(
            f"{_WORKFLOWS} ausente — rodando fora da árvore do repositório "
            "(imagem compilada não recebe .github/)."
        )
    achados: dict[tuple[str, str], list[dict]] = {}
    for caminho in sorted(_WORKFLOWS.glob("*.yml")):
        doc = yaml.safe_load(caminho.read_text(encoding="utf-8"))
        for nome, job in (doc.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                if not str(step.get("uses") or "").startswith("docker/setup-buildx-action@"):
                    continue
                with_ = step.get("with") or {}
                if _nomeado(with_):
                    achados.setdefault((caminho.name, nome), []).append(with_)
    return achados


def test_builder_nomeado_nao_e_removido_no_fim_do_job() -> None:
    achados = _builders_nomeados()

    faltando = _JOBS_CONHECIDOS - set(achados)
    assert not faltando, (
        f"a varredura não achou builder nomeado em {sorted(faltando)} — o job mudou "
        "de nome, deixou de nomear o builder ou o glob quebrou. Atualize "
        "_JOBS_CONHECIDOS conscientemente."
    )

    violacoes = {k: v for k, ws in achados.items() for w in ws if (v := _violacao(w))}
    assert not violacoes, (
        f"{violacoes}: acrescente `cleanup: ${{{{ runner.environment != 'self-hosted' }}}}` "
        "ao setup-buildx-action. O container do builder nomeado é dividido por todos "
        "os jobs da máquina."
    )


_NOME = "${{ runner.environment == 'self-hosted' && 'centralops-ci' || '' }}"


@pytest.mark.parametrize(
    ("with_", "reprova"),
    [
        pytest.param({"name": _NOME, "cleanup": "${{ runner.environment != 'self-hosted' }}"}, False, id="corrigido"),
        pytest.param({"name": "centralops-ci", "cleanup": False}, False, id="cleanup-false-literal"),
        pytest.param({"name": _NOME, "keep-state": True}, True, id="sem-cleanup-padrao-true"),
        pytest.param({"name": "centralops-ci", "cleanup": True}, True, id="cleanup-true-explicito"),
        pytest.param({}, False, id="builder-efemero-sem-nome"),
    ],
)
def test_o_detector_reprova_o_que_deve(with_: dict, reprova: bool) -> None:
    """O PAR NEGATIVO: um `_violacao` que sempre devolvesse ``None`` deixaria o
    teste de cima verde para sempre."""
    assert (_violacao(with_) is not None) is reprova
