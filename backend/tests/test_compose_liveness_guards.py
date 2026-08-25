"""Container ``Up`` não é worker consumindo, e ``PING`` não é Redis gravando.

Duas cegueiras que, juntas, transformaram ~15 min de disco cheio em ~6h de coleta
parada (ago/2026):

1. Os serviços ``collector-*`` não tinham healthcheck. O consumer Celery morreu
   com ``Unrecoverable error`` ao não conseguir gravar o ACK, o processo ficou de
   pé, e ``docker compose ps`` mostrou ``Up`` o tempo todo. O ``restart:
   unless-stopped`` só age se o processo SAIR — e ele não saiu.
2. O healthcheck dos dois Redis era ``redis-cli ping``. ``PING`` é comando de
   LEITURA e responde ``PONG`` normalmente sob MISCONF, o estado em que o Redis
   recusa toda ESCRITA por não conseguir persistir. As duas instâncias ficaram
   ``(healthy)`` por horas recusando 100% das escritas.

Guarda estrutural: lê o compose como texto (sem dependência de YAML, igual a
``test_compose_celery_queues_consumed``).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO_ROOT / "compose" / "docker-compose.yml"

_SERVICE_RE = re.compile(r"^  ([a-z0-9][a-z0-9._-]*):\s*$")

#: Serviço → nome do nó Celery (o ``-n <nome>@%h`` do ``command``). O healthcheck
#: precisa perguntar pelo nó CERTO: ``inspect ping`` sem ``-d`` responde por
#: qualquer worker do broker, e um worker morto passaria pelo vizinho vivo — que
#: é exatamente o modo de falha que este guard existe para pegar.
_CELERY_WORKERS = {
    "collector-worker-priority": "priority",
    "collector-worker-bulk": "bulk",
    "collector-worker-maintenance": "maintenance",
    "collector-worker-query": "query",
    "collector-dispatcher": "dispatch",
}

_REDIS_SERVICES = ("redis", "redis-control")


def _service_blocks() -> dict[str, list[str]]:
    """``{nome_do_serviço: [linhas do bloco]}``."""
    blocks: dict[str, list[str]] = {}
    current: str | None = None
    for line in COMPOSE_PATH.read_text(encoding="utf-8").splitlines():
        m = _SERVICE_RE.match(line)
        if m:
            current = m.group(1)
            blocks[current] = []
            continue
        if current is not None:
            blocks[current].append(line)
    return blocks


def _healthcheck_test(block: list[str]) -> str | None:
    for line in block:
        stripped = line.strip()
        if stripped.startswith("test:"):
            return stripped
    return None


def test_parser_enxerga_os_servicos_esperados() -> None:
    """Controle POSITIVO — sem ele os testes abaixo passam por vacuidade.

    Um regex quebrado devolveria zero blocos e todo ``for`` viraria no-op.
    """
    blocks = _service_blocks()
    esperados = set(_CELERY_WORKERS) | set(_REDIS_SERVICES) | {"collector-beat"}
    faltando = esperados - set(blocks)
    assert not faltando, f"parser não achou os serviços: {sorted(faltando)}"


def test_todo_worker_celery_tem_healthcheck_de_consumer() -> None:
    for service, node in _CELERY_WORKERS.items():
        block = _service_blocks()[service]
        test = _healthcheck_test(block)
        assert test is not None, (
            f"{service} está sem healthcheck. Sem ele, um consumer morto dentro "
            f"de um processo vivo aparece como 'Up' e a fila para em silêncio."
        )
        assert "inspect ping" in test, (
            f"{service}: o healthcheck tem de exercitar o CONSUMER (inspect ping), "
            f"não só o processo. Achei: {test}"
        )
        assert f"-d {node}@" in test, (
            f"{service}: o ping tem de nomear o próprio nó ({node}@...), senão um "
            f"worker vizinho vivo responde por ele. Achei: {test}"
        )


def test_healthcheck_do_redis_prova_escrita_e_nao_so_leitura() -> None:
    for service in _REDIS_SERVICES:
        test = _healthcheck_test(_service_blocks()[service])
        assert test is not None, f"{service} está sem healthcheck"
        assert " SET " in test, (
            f"{service}: o healthcheck precisa PROVAR ESCRITA. PING responde PONG "
            f"com o Redis em MISCONF recusando todo write. Achei: {test}"
        )
        assert not re.search(r'"ping"|\bping\b\s*\]', test), (
            f"{service}: voltou a usar PING como prova de saúde. Achei: {test}"
        )


def test_beat_mantem_o_healthcheck_de_heartbeat() -> None:
    """O beat não responde a ``inspect ping`` — a prova dele é o heartbeat.

    Guarda contra alguém "uniformizar" os healthchecks e trocar o do beat por um
    ping que nunca responderia, deixando-o permanentemente unhealthy.
    """
    test = _healthcheck_test(_service_blocks()["collector-beat"])
    assert test is not None and "beat-heartbeat" in test, (
        f"o beat perdeu o healthcheck de heartbeat. Achei: {test}"
    )
