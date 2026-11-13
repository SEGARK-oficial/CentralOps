"""Guard: todo serviço da imagem da app na base tem override na overlay EE.

Regressão real (snoc-lab3, 2026-11-12): a ADR-0018 criou o
``collector-raw-processor`` em ``compose/docker-compose.yml`` e ninguém o
acrescentou em ``compose/docker-compose.ee.yml``. Numa stack EE o serviço
herdava a imagem COMMUNITY da base (``${IMAGE_NAME}:${IMAGE_TAG}``) e sem a
licença. O resultado foi o pior modo de falha: a coleta seguia saudável,
gravando no tópico raw, e rotas e destinos ficavam em 0/min sem erro algum.

A overlay só troca o que difere na EE; por isso a barreira era um humano
lembrar de editar dois arquivos. Este guard deriva a lista da base: um serviço
novo da imagem da app entra na checagem sozinho.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict

import pytest

yaml = pytest.importorskip("yaml")

_COMPOSE_DIR = Path(__file__).resolve().parents[2] / "compose"
_BASE = _COMPOSE_DIR / "docker-compose.yml"
_EE = _COMPOSE_DIR / "docker-compose.ee.yml"

# Backend e SPA da app compartilham este prefixo na base.
_APP_IMAGE_PREFIX = "${IMAGE_NAME:-centralops}"
_EE_BACKEND_IMAGE = "${CENTRALOPS_EE_IMAGE:-centralops-ee:latest}"


class _OverlayLoader(yaml.SafeLoader):
    """SafeLoader que aceita o ``!reset`` do compose (>= 2.24) usado na overlay."""


_OverlayLoader.add_constructor("!reset", lambda loader, node: None)


def _services(path: Path) -> Dict[str, dict]:
    if not path.is_file():
        pytest.skip(f"compose não encontrado em {path} (rodando fora do repo)")
    data = yaml.load(path.read_text(encoding="utf-8"), Loader=_OverlayLoader)
    return {
        name: svc
        for name, svc in (data.get("services") or {}).items()
        if isinstance(svc, dict)
    }


def _app_services() -> Dict[str, str]:
    return {
        name: str(svc.get("image", ""))
        for name, svc in _services(_BASE).items()
        if str(svc.get("image", "")).startswith(_APP_IMAGE_PREFIX)
    }


@pytest.mark.source_only
def test_every_app_service_is_overridden_by_the_ee_overlay() -> None:
    base = _app_services()
    assert "collector-raw-processor" in base, "marcador de imagem mudou? base sem o processor"
    overlay = _services(_EE)

    missing = sorted(name for name in base if name not in overlay)
    assert not missing, (
        "serviço(s) da imagem da app sem override em compose/docker-compose.ee.yml: "
        + ", ".join(missing)
        + ". Numa stack EE eles sobem com a imagem COMMUNITY e sem licença. "
        "Acrescente o bloco (image EE + *ee-license-env + *ee-keyring-vol)."
    )


@pytest.mark.source_only
def test_ee_overlay_backend_services_use_the_ee_image_and_license() -> None:
    overlay = _services(_EE)
    wrong = []
    for name, image in _app_services().items():
        svc = overlay.get(name) or {}
        if name == "frontend":
            continue  # SPA tem imagem própria (CENTRALOPS_WEB_EE_IMAGE), sem licença
        if svc.get("image") != _EE_BACKEND_IMAGE:
            wrong.append(f"{name} (image={svc.get('image')!r})")
        env = svc.get("environment") or []
        if not any(str(e).startswith("CENTRALOPS_LICENSE_TOKEN=") for e in env):
            wrong.append(f"{name} (sem CENTRALOPS_LICENSE_TOKEN)")
    assert not wrong, "override EE incompleto: " + "; ".join(wrong)
