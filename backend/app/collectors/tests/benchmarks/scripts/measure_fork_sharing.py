#!/usr/bin/env python3
"""Mede se uma tabela de enriquecimento é COMPARTILHADA entre forks (ADR-LOCAL-0002, Fase 0.5).

**A pergunta que este script existe para responder.** O ADR orça o teto de memória de
enriquecimento (``ENRICH_MAX_TABLE_BYTES``) e a conta muda por um fator igual à
concorrência do worker conforme a resposta:

* se a tabela carregada no processo PAI (``worker_init``) for compartilhada por
  copy-on-write com os forks, o custo é **1×** e um MMDB de 54 MB cabe folgado;
* se cada fork acabar com a própria cópia, o custo é **N×** — e no ``collect.bulk``,
  que roda ``--concurrency=8`` sob ``memory: 2g``, 8 × 54 MB = 432 MB somados a
  ~200 MB/fork de baseline **estouram o limite do cgroup**. Sem sinal nenhum: o HPA
  escala só por CPU, então o sintoma é OOMKill silencioso.

O ADR registrava isso como **[NÃO VERIFICADO]**. Este script verifica.

**Por que a resposta não é óbvia.** COW é propriedade de PÁGINA, não de objeto. Em
CPython todo objeto carrega o refcount no cabeçalho, então **ler** um dict escreve na
página (incref/decref) e dispara a cópia. Um ``mmap`` de arquivo não tem refcount por
página: as páginas ficam limpas, respaldadas pelo arquivo, e uma cópia física serve
todos os processos.

**Por que NÃO basta olhar RSS.** RSS conta página compartilhada em CADA processo que a
toca. Um mmap de 48 MB tocado por 4 forks aparece como +48 MB em cada um — 192 MB de
RSS somado para 48 MB de memória física real. Usar RSS aqui daria a resposta errada
com aparência de medição. Por isso o script mede também a memória do SISTEMA com todos
os filhos simultaneamente vivos, que é o número que decide se o cgroup mata o container.

Uso::

    python app/collectors/tests/benchmarks/scripts/measure_fork_sharing.py --mb 48 --forks 4
"""

from __future__ import annotations

import argparse
import mmap
import os
import re
import resource
import subprocess
import sys
import tempfile
from typing import Callable, Optional, Tuple

_PAGE = 4096


def rss_bytes() -> int:
    """RSS do processo corrente, em bytes.

    ``ru_maxrss`` é PICO, e a unidade muda por plataforma: bytes no macOS,
    kilobytes no Linux. Errar isso daria um fator de 1024 no relatório — que é
    exatamente a ordem de grandeza da pergunta sendo respondida.
    """
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw if sys.platform == "darwin" else raw * 1024


def system_used_bytes() -> Optional[int]:
    """Memória do sistema efetivamente EM USO, ou ``None`` se não der para medir.

    É a métrica que decide o OOM: páginas compartilhadas contam UMA vez.
    """
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=10).stdout
            m = re.search(r"page size of (\d+) bytes", out)
            page = int(m.group(1)) if m else _PAGE
            def pages(label: str) -> int:
                mm = re.search(rf"{label}:\s+(\d+)", out)
                return int(mm.group(1)) if mm else 0
            # active + wired + compressed = o que não é reclamável de graça.
            return (pages("Pages active") + pages("Pages wired down")
                    + pages("Pages occupied by compressor")) * page
        with open("/proc/meminfo", encoding="utf-8") as fh:
            info = {}
            for line in fh:
                k, _, v = line.partition(":")
                info[k.strip()] = int(v.strip().split()[0]) * 1024
        return info["MemTotal"] - info["MemAvailable"]
    except Exception:  # noqa: BLE001 — medição é best-effort
        return None


def _human(n: float) -> str:
    return f"{n / (1024 * 1024):7.1f} MiB"


def _build_dict_table(target_bytes: int) -> dict:
    """Tabela no formato REAL do enricher: ``{chave: {campo: valor}}``."""
    rows: dict = {}
    i = 0
    # ~200 B/linha é a estimativa que o ADR usa para inventário de ativos.
    while len(rows) * 200 < target_bytes:
        rows[f"10.{(i >> 16) & 255}.{(i >> 8) & 255}.{i & 255}-{i}"] = {
            "score": i % 100,
            "labels": ["tor-exit-node", "scanner"],
            "site": f"site-{i % 64}",
        }
        i += 1
    return rows


def _build_mmap(target_bytes: int):
    """Buffer mmapado de arquivo — o que um MMDB seria."""
    fd, path = tempfile.mkstemp(suffix=".bin")
    with os.fdopen(fd, "wb") as fh:
        fh.write(b"\xa5" * target_bytes)
    fh2 = open(path, "rb")
    buf = mmap.mmap(fh2.fileno(), 0, access=mmap.ACCESS_READ)
    return buf, fh2, path


def run_scenario(label: str, target_bytes: int, forks: int) -> Tuple[float, Optional[float]]:
    """Devolve (delta RSS médio por fork, delta de memória do SISTEMA)."""
    print(f"\n  {label} — alvo {_human(target_bytes)}, {forks} fork(s)")

    keep = None
    if label.startswith("dict"):
        table = _build_dict_table(target_bytes)
        print(f"    PAI carregou {len(table)} entradas, RSS={_human(rss_bytes())}")

        def scan() -> int:
            # Varre TUDO: pior caso e também o realista ao longo de um ciclo, porque
            # as chaves quentes de um lote grande espalham pelo dict inteiro.
            return sum(len(v["site"]) for v in table.values())
    else:
        buf, fh, path = _build_mmap(target_bytes)
        keep = (buf, fh, path)
        print(f"    PAI mapeou o arquivo, RSS={_human(rss_bytes())}")

        def scan() -> int:
            total = 0
            for off in range(0, len(buf), _PAGE):  # uma leitura por página
                total += buf[off]
            return total

    sys_before = system_used_bytes()

    # Um pipe por filho: ele reporta o delta e SEGURA a estrutura até o pai medir a
    # memória do sistema com todos vivos ao mesmo tempo. Sem esse rendez-vous o
    # primeiro filho já teria saído quando o último terminasse.
    ready_r, ready_w = os.pipe()
    go_r, go_w = os.pipe()
    pids = []
    for _ in range(forks):
        # ``flush`` ANTES do fork: o buffer do stdout é herdado, e sem isso cada
        # filho reimprime tudo o que o pai ainda não esvaziou.
        sys.stdout.flush()
        pid = os.fork()
        if pid == 0:
            os.close(ready_r)
            os.close(go_w)
            before = rss_bytes()
            checksum = scan()
            after = rss_bytes()
            os.write(ready_w, f"{after - before}:{checksum % 997}\n".encode())
            os.read(go_r, 1)  # segura até o pai liberar
            os._exit(0)
        pids.append(pid)
    os.close(ready_w)
    os.close(go_r)

    deltas = []
    with os.fdopen(ready_r, "r") as rf:
        for _ in range(forks):
            line = rf.readline()
            if not line:
                break
            deltas.append(int(line.split(":")[0]))

    sys_with_children = system_used_bytes()
    os.write(go_w, b"x" * forks)
    os.close(go_w)
    for pid in pids:
        os.waitpid(pid, 0)

    if keep is not None:
        keep[0].close()
        keep[1].close()
        os.unlink(keep[2])

    avg = sum(deltas) / len(deltas) if deltas else 0.0
    sys_delta = (
        (sys_with_children - sys_before)
        if (sys_before is not None and sys_with_children is not None)
        else None
    )
    print(f"    RSS: cada fork cresceu {_human(avg)} ao varrer (média de {len(deltas)})")
    print(f"    RSS somado dos {forks} forks:            {_human(avg * forks)}")
    if sys_delta is not None:
        print(f"    MEMÓRIA DO SISTEMA com todos vivos:  {_human(sys_delta)}  <-- é esta que mata o container")
    return avg, sys_delta


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mb", type=int, default=48, help="tamanho alvo da tabela (MiB)")
    ap.add_argument("--forks", type=int, default=4)
    args = ap.parse_args()
    target = args.mb * 1024 * 1024

    print("=" * 82)
    print("ADR-LOCAL-0002 Fase 0.5 — a tabela é compartilhada entre forks?")
    print("=" * 82)
    print(f"  plataforma={sys.platform}  python={sys.version.split()[0]}")
    print(
        "\n  Como ler: RSS conta página compartilhada em CADA processo, então o RSS\n"
        "  somado SUPERESTIMA o mmap. A linha que decide é a memória do SISTEMA."
    )

    d_rss, d_sys = run_scenario("dict Python (tabela do enricher)", target, args.forks)
    m_rss, m_sys = run_scenario("mmap de arquivo (o que um MMDB é)", target, args.forks)

    print("\n" + "=" * 82)
    print("  VEREDITO")
    print("=" * 82)
    if d_sys is not None and m_sys is not None:
        print(f"    dict : sistema +{_human(d_sys)} para {args.forks} forks × {_human(target)}")
        print(f"    mmap : sistema +{_human(m_sys)} para {args.forks} forks × {_human(target)}")
    print(
        "\n    dict  ⇒ custo N×concurrency (páginas viram privadas ao ler: refcount).\n"
        "    mmap  ⇒ custo ~1× (páginas limpas, respaldadas pelo arquivo).\n"
        "\n    Logo ENRICH_MAX_TABLE_BYTES é POR FORK para tabela em dict, e o teto\n"
        "    útil por container = teto × concurrency. Ver ADR §6.1.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
