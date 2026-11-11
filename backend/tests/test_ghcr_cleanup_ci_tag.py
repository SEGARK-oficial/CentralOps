"""O cleanup do ``ci.yml`` nunca apaga uma imagem publicada.

Na main, o ``publish`` promove a imagem ``ci-<run_id>`` por ``imagetools``:
``latest`` e ``sha-<commit>`` (e a ``vX.Y.Z`` do release-please) ficam no MESMO
digest, e no GHCR um digest é UMA versão de pacote. O cleanup apagava "a versão
que tem ``ci-<run_id>``" e levava todas as tags junto: a v2.18.0 foi promovida às
14:28:22 e apagada às 14:28:26, e ``latest`` ficou 404.

O script ``scripts/ci/ghcr_cleanup_ci_tag.py`` só apaga quando TODAS as tags da
versão são ``ci-*``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "ci" / "ghcr_cleanup_ci_tag.py"
_CI = _ROOT / ".github" / "workflows" / "ci.yml"


def _load():
    if not _SCRIPT.is_file():
        pytest.skip(f"{_SCRIPT} ausente — fora da árvore do repositório")
    spec = importlib.util.spec_from_file_location("ghcr_cleanup_ci_tag", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _v(vid: int, *tags: str) -> dict:
    return {"id": vid, "metadata": {"container": {"tags": list(tags)}}}


def test_imagem_promovida_nao_e_apagada() -> None:
    """O caso da v2.18.0: a ci-<run> divide a versão com latest, sha e a release."""
    mod = _load()
    versions = [
        _v(1, "v2.17.1", "v2.17", "v2"),
        _v(2, "ci-43036190708", "latest", "sha-6ba1ecf", "v2.18.0", "v2.18", "v2"),
    ]
    vid, reason = mod.version_to_delete(versions, "ci-43036190708")
    assert vid is None
    assert "latest" in reason and "v2.18.0" in reason


def test_imagem_so_efemera_e_apagada() -> None:
    """O par negativo: em PR (sem publish) a ci-<run> é a única tag e TEM de sair,
    senão um `version_to_delete` que nunca apaga passaria no teste de cima."""
    mod = _load()
    versions = [_v(1, "latest", "sha-aaaaaaa"), _v(7, "ci-123")]
    assert mod.version_to_delete(versions, "ci-123")[0] == 7


def test_varias_tags_efemeras_ainda_contam_como_efemera() -> None:
    mod = _load()
    assert mod.version_to_delete([_v(9, "ci-1", "ci-2")], "ci-2")[0] == 9


def test_tag_ausente_nao_apaga_nada() -> None:
    mod = _load()
    vid, reason = mod.version_to_delete([_v(1, "latest"), _v(2, "ci-9")], "ci-123")
    assert vid is None and "nada a apagar" in reason


def test_recusa_tag_que_nao_e_efemera(capsys) -> None:
    """Chamado com ``latest`` por engano, o script não chega a listar nada."""
    mod = _load()
    assert mod.main(["owner", "latest", "centralops"]) == 2
    assert "recusado" in capsys.readouterr().err


def test_ci_yml_limpa_pelo_script_e_nao_por_delete_cru() -> None:
    if not _CI.is_file():
        pytest.skip(f"{_CI} ausente — fora da árvore do repositório")
    yaml = pytest.importorskip("yaml", reason="PyYAML ausente")
    doc = yaml.safe_load(_CI.read_text(encoding="utf-8"))
    steps = doc["jobs"]["cleanup"]["steps"]
    runs = "\n".join(str(s.get("run") or "") for s in steps)
    assert "scripts/ci/ghcr_cleanup_ci_tag.py" in runs, "o cleanup deixou de usar o script com a regra"
    assert any("sparse-checkout" in (s.get("with") or {}) for s in steps), "o script precisa do checkout"
    texto = _CI.read_text(encoding="utf-8")
    assert "method DELETE" not in texto and "-X DELETE" not in texto, (
        "DELETE de versão de pacote direto no ci.yml: apaga também latest/sha/vX.Y.Z "
        "que dividem o digest. Use scripts/ci/ghcr_cleanup_ci_tag.py."
    )


def test_main_le_todas_as_paginas_e_so_apaga_a_efemera(monkeypatch, capsys) -> None:
    """``gh api --paginate`` devolve um array JSON por página, colados. A versão
    efêmera na 2ª página precisa ser achada; a promovida, na 1ª, nunca apagada."""
    mod = _load()
    calls: list = []
    paginas = {
        "centralops": '[{"id": 1, "metadata": {"container": {"tags": ["ci-5", "latest"]}}}]\n'
        '[{"id": 2, "metadata": {"container": {"tags": ["v1"]}}}]',
        "centralops-frontend": '[{"id": 3, "metadata": {"container": {"tags": ["x"]}}}]'
        '[{"id": 4, "metadata": {"container": {"tags": ["ci-5"]}}}]',
    }

    def _fake_gh(*args):
        calls.append(args)
        if "--method" in args:
            return ""
        pkg = args[-1].split("/container/")[1].split("/")[0]
        return paginas[pkg]

    monkeypatch.setattr(mod, "_gh", _fake_gh)
    assert mod.main(["SEGARK-oficial", "ci-5", "centralops", "centralops-frontend"]) == 0
    deletes = [a[-1] for a in calls if "--method" in a]
    assert deletes == ["/users/SEGARK-oficial/packages/container/centralops-frontend/versions/4"]
    out = capsys.readouterr().out
    assert "centralops: mantida" in out and "centralops-frontend: apagada" in out
