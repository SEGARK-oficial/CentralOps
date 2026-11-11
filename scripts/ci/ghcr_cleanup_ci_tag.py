#!/usr/bin/env python3
"""Apaga a imagem efêmera ``ci-<run_id>`` do GHCR, mas nunca uma imagem publicada.

O ``ci.yml`` compila uma vez e publica a imagem ``ci-<run_id>``. Na main, o job
``publish`` *promove* essa mesma imagem: o ``imagetools create`` aplica
``latest`` e ``sha-<commit>`` ao MESMO digest, e o release-please ainda aplica
``vX.Y.Z``. No GHCR, um digest é UMA versão de pacote, com todas essas tags.
Apagar "a versão que tem ``ci-<run_id>``" apagava junto ``latest``,
``sha-<commit>`` e a release inteira: foi assim que a v2.18.0 saiu sem imagem.

A regra: só apaga a versão cujas tags são TODAS ``ci-*``. Se houver qualquer
outra tag, a ``ci-<run_id>`` fica (o GHCR não remove uma tag sozinha) e a
retenção semanal também não a toca, porque exige que todas as tags casem com
``ci-*``.

Uso: ``ghcr_cleanup_ci_tag.py <owner> <tag> <pacote> [<pacote> ...]``.
Depende só do ``gh`` autenticado (``GH_TOKEN``) e da biblioteca padrão.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any, Iterable, Optional, Tuple

EPHEMERAL_PREFIX = "ci-"


def version_to_delete(versions: Iterable[dict], tag: str) -> Tuple[Optional[int], str]:
    """Escolhe a versão a apagar. Devolve ``(id, motivo)``; ``id`` é ``None``
    quando nada pode ser apagado, com o motivo para o log."""
    for version in versions:
        tags = list(((version.get("metadata") or {}).get("container") or {}).get("tags") or [])
        if tag not in tags:
            continue
        published = sorted(t for t in tags if not t.startswith(EPHEMERAL_PREFIX))
        if published:
            return None, f"mantida: {tag} divide a versão {version.get('id')} com {', '.join(published)}"
        return version.get("id"), f"apagada: versão {version.get('id')} só tinha {', '.join(sorted(tags))}"
    return None, f"nada a apagar: nenhuma versão com {tag}"


def _gh(*args: str) -> str:
    return subprocess.run(["gh", "api", *args], check=True, capture_output=True, text=True).stdout


def _list_versions(owner: str, package: str) -> list[dict[str, Any]]:
    out = _gh("--paginate", f"/users/{owner}/packages/container/{package}/versions?per_page=100")
    # ``--paginate`` concatena um array JSON por página.
    versions: list[dict[str, Any]] = []
    decoder = json.JSONDecoder()
    pos, text = 0, out.strip()
    while pos < len(text):
        page, pos = decoder.raw_decode(text, pos)
        versions.extend(page)
        while pos < len(text) and text[pos].isspace():
            pos += 1
    return versions


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    owner, tag, packages = argv[0], argv[1], argv[2:]
    if not tag.startswith(EPHEMERAL_PREFIX):
        print(f"recusado: {tag!r} não é uma tag efêmera {EPHEMERAL_PREFIX}*", file=sys.stderr)
        return 2
    for package in packages:
        try:
            vid, reason = version_to_delete(_list_versions(owner, package), tag)
            if vid is not None:
                _gh("--method", "DELETE", f"/users/{owner}/packages/container/{package}/versions/{vid}")
        except subprocess.CalledProcessError as exc:  # best-effort, como antes
            reason = f"falhou: {(exc.stderr or '').strip()[:200]}"
        except Exception as exc:  # noqa: BLE001 — limpeza nunca derruba o run
            reason = f"falhou: {type(exc).__name__}: {exc}"[:200]
        print(f"{package}: {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
