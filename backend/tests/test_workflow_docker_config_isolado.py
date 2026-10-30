"""Todo job que faz login num registry usa um ``DOCKER_CONFIG`` só dele.

Os runners self-hosted do pool são várias instâncias (``/opt/actions-runner-N``)
na MESMA máquina e com o MESMO usuário. Sem isolamento, todos os jobs dividem
``~/.docker/config.json`` — e o post-step do ``docker/login-action`` de um job
faz ``docker logout ghcr.io`` e apaga a credencial de outro que ainda roda.

O sintoma engana. O buildx lê o arquivo quando começa e empurra a imagem
normalmente; o ``cosign sign``, que lê na hora de assinar, leva
``UNAUTHORIZED``. A v2.16.0 saiu assim: imagem ``final`` publicada com as tags
da versão e SEM assinatura, porque o job do ``frontend`` (mesma matrix, outra
instância na mesma máquina) terminou durante o build de 40 min. As releases
anteriores passaram por sorte — os dois jobs não se sobrepuseram no tempo.

O conserto é um passo que aponta ``DOCKER_CONFIG`` para ``$RUNNER_TEMP`` (de
cada instância, esvaziado pelo runner a cada job) ANTES do buildx e do login,
que gravam ali o builder e a credencial.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML ausente")

_WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"

#: Passos que gravam no DOCKER_CONFIG (credencial ou builder) e por isso têm de
#: vir DEPOIS do isolamento.
_GRAVAM_NO_DOCKER_CONFIG = ("docker/login-action@", "docker/setup-buildx-action@")

#: Jobs que hoje fazem login e TÊM de aparecer na varredura. Sem isto, um glob
#: que deixasse de achar os arquivos (ou um `uses` reescrito) aprovaria por
#: vacuidade. Crescer aqui é decisão consciente; encolher reprova.
_JOBS_CONHECIDOS = frozenset(
    {
        ("build-and-publish.yml", "build-and-push"),
        ("ci.yml", "build"),
        ("ci.yml", "e2e-setup"),
        ("ci.yml", "e2e"),
        ("ci.yml", "publish"),
        ("release-please.yml", "build-release-image"),
    }
)


def _isola(step: dict) -> bool:
    run = step.get("run") or ""
    # $RUNNER_TEMP e não /tmp ou ~/: um caminho fixo seria o MESMO arquivo
    # compartilhado com outro nome.
    return "DOCKER_CONFIG=$RUNNER_TEMP/" in run and "GITHUB_ENV" in run


def _grava(step: dict) -> bool:
    return str(step.get("uses") or "").startswith(_GRAVAM_NO_DOCKER_CONFIG)


def _faz_login(steps: list[dict]) -> bool:
    return any(str(s.get("uses") or "").startswith("docker/login-action@") for s in steps)


def _violacao(steps: list[dict]) -> str | None:
    """Motivo pelo qual o job vaza a credencial, ou ``None`` se está isolado."""
    primeiro_que_grava = next(i for i, s in enumerate(steps) if _grava(s))
    isolamentos = [i for i, s in enumerate(steps) if _isola(s)]
    if not isolamentos:
        return "faz login sem isolar o DOCKER_CONFIG"
    if isolamentos[0] > primeiro_que_grava:
        nome = steps[primeiro_que_grava].get("name") or steps[primeiro_que_grava]["uses"]
        return f"isola o DOCKER_CONFIG DEPOIS de {nome!r}, que já gravou no compartilhado"
    return None


def _jobs_com_login() -> dict[tuple[str, str], list[dict]]:
    if not _WORKFLOWS.is_dir():
        pytest.skip(
            f"{_WORKFLOWS} ausente — rodando fora da árvore do repositório "
            "(imagem compilada não recebe .github/)."
        )
    achados: dict[tuple[str, str], list[dict]] = {}
    for caminho in sorted(_WORKFLOWS.glob("*.yml")):
        doc = yaml.safe_load(caminho.read_text(encoding="utf-8"))
        for nome, job in (doc.get("jobs") or {}).items():
            steps = job.get("steps") or []
            if _faz_login(steps):
                achados[(caminho.name, nome)] = steps
    return achados


def test_todo_job_com_login_isola_o_docker_config() -> None:
    jobs = _jobs_com_login()

    faltando = _JOBS_CONHECIDOS - set(jobs)
    assert not faltando, (
        f"a varredura não achou {sorted(faltando)} — o job mudou de nome, parou de "
        "fazer login ou o glob quebrou. Atualize _JOBS_CONHECIDOS conscientemente."
    )

    violacoes = {k: v for k, s in jobs.items() if (v := _violacao(s))}
    assert not violacoes, (
        "jobs que dividem ~/.docker/config.json com os outros runners da mesma "
        f"máquina: {violacoes}. Um logout de outro job apaga a credencial no meio "
        "do caminho e o cosign assina com UNAUTHORIZED (foi o que deixou a v2.16.0 "
        "sem assinatura). Acrescente o passo 'Isolar DOCKER_CONFIG deste job' "
        "antes do buildx e do login."
    )


_ISOLA = {"name": "Isolar", "run": 'echo "DOCKER_CONFIG=$RUNNER_TEMP/docker-config" >> "$GITHUB_ENV"'}
_BUILDX = {"uses": "docker/setup-buildx-action@abc"}
_LOGIN = {"name": "Log in", "uses": "docker/login-action@abc"}


@pytest.mark.parametrize(
    ("steps", "esperado"),
    [
        pytest.param([_ISOLA, _BUILDX, _LOGIN], None, id="isolado-antes-de-tudo"),
        pytest.param([_BUILDX, _LOGIN], "sem isolar", id="sem-isolamento"),
        pytest.param([_BUILDX, _ISOLA, _LOGIN], "DEPOIS de", id="isolado-depois-do-buildx"),
        pytest.param(
            [{"run": 'echo "DOCKER_CONFIG=/tmp/docker" >> "$GITHUB_ENV"'}, _LOGIN],
            "sem isolar",
            id="caminho-fixo-compartilhado",
        ),
    ],
)
def test_o_detector_reprova_o_que_deve(steps: list[dict], esperado: str | None) -> None:
    """O PAR NEGATIVO: sem ele, um `_violacao` que sempre devolvesse ``None``
    deixaria o teste de cima verde para sempre."""
    motivo = _violacao(steps)
    if esperado is None:
        assert motivo is None
    else:
        assert motivo is not None and esperado in motivo
