"""Guard: TODO serviço que fala Celery aponta para o MESMO broker.

Regressão real que motivou este guard (PROD v2.5.1): o commit que isolou o
control-plane num Redis dedicado (``redis-control``) adicionou
``CELERY_BROKER_URL`` colando a mesma linha em 8 serviços — e esqueceu o serviço
``centralops`` (a API). A API caiu no fallback de ``_broker_url()``, que deriva a
URL do ``REDIS_URL`` trocando só o número do DB, e passou a publicar em
``redis:6379/1`` enquanto os workers consumiam ``redis-control:6379/1``.

O modo de falha é o pior possível — silencioso e enganoso:

- a API aceita o request e grava o job; ``apply_async`` não levanta nada;
- a task some: nenhum worker a vê, o job fica ``pending`` para sempre;
- a coleta periódica continua normal (quem a enfileira é o *beat*, que está do
  lado dos workers), então a stack parece saudável;
- o diagnóstico acusa "workers offline" com 7 workers vivos, porque o broadcast
  de inspeção também sai pelo broker errado.

Nada disso aparece em teste de unidade: é config de deploy. Este guard existe
porque a única barreira era um humano lembrar de editar 9 blocos em vez de 8.

Cobre também ``CELERY_RESULT_BACKEND``: divergir nele não perde task, mas quebra
silenciosamente qualquer leitura de ``AsyncResult`` feita pela API.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List

import pytest

yaml = pytest.importorskip("yaml")

_COMPOSE = Path(__file__).resolve().parents[4] / "compose" / "docker-compose.yml"

# Serviços que rodam a imagem da APLICAÇÃO — os que importam ``celery_app`` e,
# portanto, publicam e/ou consomem tasks. Derivado da imagem em vez de uma lista
# fixa: um worker novo entra na checagem sozinho, sem ninguém lembrar de vir aqui.
_APP_IMAGE_MARKER = "${IMAGE_NAME:-centralops}:"
# O SPA compartilha o prefixo da imagem mas não fala Celery.
_NOT_CELERY = {"frontend"}

_REQUIRED_VARS = ("CELERY_BROKER_URL", "CELERY_RESULT_BACKEND")


def _load_services() -> Dict[str, dict]:
    if not _COMPOSE.is_file():
        pytest.skip(f"compose não encontrado em {_COMPOSE} (rodando fora do repo)")
    data = yaml.safe_load(_COMPOSE.read_text())
    return {
        name: svc
        for name, svc in (data.get("services") or {}).items()
        if isinstance(svc, dict)
    }


def _env_map(svc: dict) -> Dict[str, str]:
    """Normaliza ``environment`` (lista ``K=V`` ou mapa) para dict."""
    env = svc.get("environment") or []
    if isinstance(env, dict):
        return {k: "" if v is None else str(v) for k, v in env.items()}
    out: Dict[str, str] = {}
    for item in env:
        key, _, value = str(item).partition("=")
        out[key.strip()] = value
    return out


def _celery_services() -> Dict[str, Dict[str, str]]:
    return {
        name: _env_map(svc)
        for name, svc in _load_services().items()
        if _APP_IMAGE_MARKER in str(svc.get("image", "")) and name not in _NOT_CELERY
    }


@pytest.mark.source_only
def test_every_celery_service_declares_the_broker() -> None:
    """Nenhum serviço da app pode depender do fallback derivado do REDIS_URL."""
    services = _celery_services()
    assert services, "nenhum serviço da imagem da app encontrado — marcador mudou?"

    missing: List[str] = [
        f"{name} (falta {var})"
        for name, env in sorted(services.items())
        for var in _REQUIRED_VARS
        if var not in env
    ]
    assert not missing, (
        "serviço(s) da app sem broker Celery explícito: "
        + "; ".join(missing)
        + ". Sem a var, _broker_url() DERIVA a URL do REDIS_URL e o serviço fala "
        "com o Redis de cache em vez do redis-control — tasks publicadas somem "
        "sem erro e jobs ficam 'pending' para sempre."
    )


@pytest.mark.source_only
@pytest.mark.parametrize("var", _REQUIRED_VARS)
def test_all_celery_services_share_one_broker(var: str) -> None:
    """A URL precisa ser IDÊNTICA — publicar e consumir em brokers diferentes
    não é degradação, é perda total e silenciosa do trabalho enfileirado."""
    services = _celery_services()
    values = {name: env[var] for name, env in services.items() if var in env}
    distinct = set(values.values())

    assert len(distinct) <= 1, (
        f"{var} diverge entre os serviços — quem publica e quem consome precisam "
        f"do MESMO broker:\n"
        + "\n".join(f"  {name}: {values[name]}" for name in sorted(values))
    )


@pytest.mark.source_only
@pytest.mark.parametrize(
    "example",
    [".env.example", "compose/.env.example"],
)
def test_env_examples_never_document_the_cache_redis_as_broker(example: str) -> None:
    """Os arquivos de exemplo não podem ensinar o broker ERRADO.

    O ``.env.example`` da raiz documentou ``CELERY_BROKER_URL=...@redis:6379/1``
    por toda a vida do control-plane isolado — o valor de antes do
    ``redis-control`` existir, e exatamente o que o fallback deriva. Copiar dali
    reproduzia o bug com a bênção da documentação oficial.
    """
    path = Path(__file__).resolve().parents[4] / example
    if not path.is_file():
        pytest.skip(f"{example} não encontrado (rodando fora do repo)")

    offenders = [
        line.strip()
        for line in path.read_text().splitlines()
        if re.match(r"^#?\s*CELERY_(BROKER_URL|RESULT_BACKEND)\s*=", line.strip())
        and "redis-control" not in line
    ]
    assert not offenders, (
        f"{example} documenta o broker Celery apontando para o Redis de CACHE "
        f"em vez do redis-control:\n" + "\n".join(f"  {o}" for o in offenders)
    )


@pytest.mark.source_only
def test_api_shares_the_workers_broker() -> None:
    """Guard explícito da regressão: a API publica, o worker consome.

    Redundante com o teste acima por escolha — se alguém relaxar o critério de
    descoberta por imagem, este par continua ancorado nos dois lados reais do
    contrato e a regressão original volta a quebrar o CI.
    """
    services = _celery_services()
    for side in ("centralops", "collector-worker-bulk"):
        assert side in services, f"serviço '{side}' sumiu do compose"

    api, worker = services["centralops"], services["collector-worker-bulk"]
    for var in _REQUIRED_VARS:
        assert api.get(var) == worker.get(var) and api.get(var), (
            f"{var}: API={api.get(var)!r} vs worker={worker.get(var)!r}. "
            "A API publica backfill, 'coletar agora', reprocess de quarentena, "
            "drain de DLQ e data deletion — com broker divergente, tudo isso "
            "some em silêncio."
        )
