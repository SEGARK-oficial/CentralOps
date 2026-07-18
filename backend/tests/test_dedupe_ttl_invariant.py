"""Invariante do TTL de dedupe: nunca menor que a janela real de reentrega.

Regressão-alvo: o TTL de `dedupe:{integration_id}:{message_id}` (SET NX EX)
existe para cobrir REENTREGA (retry do Celery, redelivery do broker após
crash do worker, replay manual de um run falho — ver
`backend/app/collectors/pipeline.py:784-798`), não dedupe de longo prazo.
Baixar o TTL sem olhar para essas janelas reabriria a MESMA classe de bug que
o TTL de 7 dias tinha na outra ponta (silenciosamente generoso demais para a
memória do Redis) — só que na direção oposta: curto demais faria uma claim
ainda "em voo" (retry automático em progresso) expirar e ser re-reclamada por
um segundo worker, ou pior, uma claim liberada por `release()` reaparecer como
"nova" antes do sistema realmente ter desistido de reentregar.

Este teste ancora o número escolhido (`DEFAULT_TTL_DAYS` em
`state/dedupe.py` e `Settings.DEDUPE_TTL_DAYS` em `core/config.py`) nos
valores REAIS configurados no Celery deste repo — se alguém subir
`visibility_timeout` (ou os time limits) sem revisitar o TTL do dedupe, ou
baixar o TTL sem revisitar esses valores, o teste quebra.
"""

from __future__ import annotations

import pytest

from backend.app.collectors.celery_app import celery_app
from backend.app.collectors.dispatch_runtime import DISPATCH_RESULT_TIMEOUT
from backend.app.collectors.state.dedupe import DEFAULT_TTL_DAYS
from backend.app.core.config import settings

# Margem mínima exigida entre o TTL do dedupe e o pior caso de redelivery
# AUTOMÁTICO (acks_late + task_reject_on_worker_lost + visibility_timeout).
# 4x é conservador: cobre jitter dos retries do Celery + o tempo de
# processamento do próprio ciclo de coleta sem exigir precisão milimétrica.
_MIN_MARGIN_OVER_VISIBILITY_TIMEOUT = 4


def _visibility_timeout_s() -> int:
    opts = celery_app.conf.broker_transport_options or {}
    timeout = opts.get("visibility_timeout")
    assert timeout is not None, (
        "broker_transport_options.visibility_timeout não está configurado — "
        "sem ele não há teto documentado para redelivery de task acks_late, "
        "e a invariante do TTL de dedupe não tem contra o que ser medida."
    )
    return int(timeout)


def test_celery_redelivery_ordering_invariant_holds() -> None:
    """Ancora a cadeia documentada em celery_app.py:248-253:

        DISPATCH_RESULT_TIMEOUT < task_soft_time_limit < task_time_limit
            < visibility_timeout

    Se isso deixar de valer, o raciocínio usado para calibrar o TTL do
    dedupe (pior caso de redelivery ~= visibility_timeout) fica inválido.
    """
    soft = celery_app.conf.task_soft_time_limit
    hard = celery_app.conf.task_time_limit
    visibility = _visibility_timeout_s()

    assert DISPATCH_RESULT_TIMEOUT < soft, (
        f"DISPATCH_RESULT_TIMEOUT={DISPATCH_RESULT_TIMEOUT}s deveria ser < "
        f"task_soft_time_limit={soft}s"
    )
    assert soft < hard, f"task_soft_time_limit={soft}s deveria ser < task_time_limit={hard}s"
    assert hard < visibility, (
        f"task_time_limit={hard}s deveria ser < visibility_timeout={visibility}s — "
        "senão o broker pode redeliver ANTES do hard time-limit matar a task presa, "
        "e um worker saudável recebe uma 2ª cópia da MESMA task ainda em execução."
    )


def test_dedupe_default_ttl_covers_worst_case_redelivery_window() -> None:
    """`DEFAULT_TTL_DAYS` (state/dedupe.py) precisa cobrir, com folga, o pior
    caso de redelivery automático (visibility_timeout) — senão uma claim
    ÓRFÃ (worker morto antes do `except` de pipeline.py rodar `release()`)
    expira e é re-reclamada como "evento novo" ANTES do broker sequer ter
    desistido de redeliverar a task original, criando uma corrida onde dois
    workers processam o mesmo evento como se fossem independentes.
    """
    ttl_s = DEFAULT_TTL_DAYS * 86400
    visibility = _visibility_timeout_s()

    assert ttl_s >= visibility * _MIN_MARGIN_OVER_VISIBILITY_TIMEOUT, (
        f"DEFAULT_TTL_DAYS={DEFAULT_TTL_DAYS}d ({ttl_s}s) não cobre "
        f"{_MIN_MARGIN_OVER_VISIBILITY_TIMEOUT}x o pior caso de redelivery "
        f"automático (visibility_timeout={visibility}s) — TTL curto demais "
        "para a janela real de reentrega deste pipeline."
    )


def test_settings_dedupe_ttl_days_covers_worst_case_redelivery_window() -> None:
    """Mesma invariante para o default exposto via env/settings
    (`core.config.Settings.DEDUPE_TTL_DAYS`) — é o valor que
    `config_loader._snapshot_from_env` usa quando DB e cache Redis falham.
    """
    ttl_s = settings.DEDUPE_TTL_DAYS * 86400
    visibility = _visibility_timeout_s()

    assert ttl_s >= visibility * _MIN_MARGIN_OVER_VISIBILITY_TIMEOUT, (
        f"Settings.DEDUPE_TTL_DAYS={settings.DEDUPE_TTL_DAYS}d ({ttl_s}s) não cobre "
        f"{_MIN_MARGIN_OVER_VISIBILITY_TIMEOUT}x o visibility_timeout={visibility}s."
    )


@pytest.mark.parametrize("ttl_days", [0, -1])
def test_ttl_below_one_day_is_rejected_by_the_invariant_math(ttl_days: int) -> None:
    """Sanity do próprio teste: um TTL claramente curto demais (<1h) DEVE
    reprovar a checagem acima — prova que o invariante não é vácuo (não
    passaria com qualquer número)."""
    ttl_s = ttl_days * 86400
    visibility = _visibility_timeout_s()
    assert ttl_s < visibility * _MIN_MARGIN_OVER_VISIBILITY_TIMEOUT


def test_default_ttl_is_dramatically_shorter_than_the_old_seven_days() -> None:
    """Documenta a intenção da mudança: o TTL não deve voltar a ser da ordem
    de dias-múltiplos sem justificativa nova — reduz o raio de silêncio de
    uma claim órfã (crash antes do `release()`) e o footprint de memória do
    keyspace ``dedupe:*`` sob o ``volatile-lru`` 512mb do compose."""
    assert DEFAULT_TTL_DAYS <= 1, (
        f"DEFAULT_TTL_DAYS={DEFAULT_TTL_DAYS}d — se isto subiu de volta para "
        "vários dias, revise a justificativa em state/dedupe.py (o TTL é "
        "idempotência de reentrega, não dedupe de longo prazo)."
    )


# ── Coerência entre as CINCO declarações do default (ADR-0015) ───────────────
#
# O valor do TTL default é declarado em cinco lugares, por razões estruturais
# (import circular impede uma constante única alcançável por todos):
#   1. ``collectors.config_loader.DEFAULT_DEDUPE_TTL_DAYS``  — fonte canônica
#   2. ``collectors.state.dedupe.DEFAULT_TTL_DAYS``          — usado pelo claim()
#   3. ``core.config.Settings.DEDUPE_TTL_DAYS``              — fallback de env
#   4. ``db.models.CollectorConfig.dedupe_ttl_days``         — default da coluna
#   5. ``api.schemas`` (create)                              — default da API
#
# Antes desta ADR os cinco divergiam: baixar (3) não mudava NADA em produção,
# porque ``_snapshot_from_env`` e ``_snapshot_from_row`` tinham fallbacks ``or 7``
# que reintroduziam o valor antigo. A invariante "existe um default e ele vale em
# todo lugar" era só um comentário implícito — o mesmo padrão que já mordeu este
# produto três vezes (lock do RedBeat, coletor sem teto por ciclo, e o próprio
# dedupe com 7 dias contra memória finita).
#
# Este teste é o guard executável exigido pela regra R8 da ADR-0015.

def _declared_defaults() -> dict[str, int]:
    from backend.app.api import schemas
    from backend.app.collectors.config_loader import DEFAULT_DEDUPE_TTL_DAYS
    from backend.app.collectors.state.dedupe import DEFAULT_TTL_DAYS
    from backend.app.core.config import settings
    from backend.app.db import models

    return {
        "config_loader.DEFAULT_DEDUPE_TTL_DAYS": DEFAULT_DEDUPE_TTL_DAYS,
        "state.dedupe.DEFAULT_TTL_DAYS": DEFAULT_TTL_DAYS,
        "Settings.DEDUPE_TTL_DAYS": int(settings.DEDUPE_TTL_DAYS),
        "models.CollectorConfig.dedupe_ttl_days": (
            models.CollectorConfig.__table__.c.dedupe_ttl_days.default.arg
        ),
        "schemas.CollectorConfigBase.dedupe_ttl_days": (
            schemas.CollectorConfigBase.model_fields["dedupe_ttl_days"].default
        ),
    }


def test_all_declared_defaults_agree() -> None:
    """As cinco declarações do default DEVEM concordar.

    Falhar aqui significa que alguém mudou o TTL num lugar só — e o efeito em
    produção seria silencioso e dependente de qual caminho de config foi usado
    (env vs DB vs API), que é o pior modo de falha possível para um guard de
    idempotência.
    """
    declared = _declared_defaults()
    distinct = set(declared.values())
    assert len(distinct) == 1, (
        "defaults de dedupe_ttl_days DIVERGEM entre si:\n"
        + "\n".join(f"  {k} = {v}" for k, v in sorted(declared.items()))
        + "\n\nAlinhe todos com collectors.config_loader.DEFAULT_DEDUPE_TTL_DAYS."
    )


def test_the_or_fallbacks_do_not_reintroduce_a_stale_literal() -> None:
    """``_snapshot_from_env``/``_snapshot_from_row`` não podem ter ``or 7`` literal.

    Este é o defeito EXATO que tornava a mudança de default inócua. Um guard de
    valor (o teste acima) não o pegaria: com env setado, o ``or`` nunca dispara e
    tudo parece coerente — até o dia em que o env falta e o valor antigo volta.
    """
    import inspect

    from backend.app.collectors import config_loader

    src = inspect.getsource(config_loader)
    offenders = [
        line.strip()
        for line in src.splitlines()
        if "dedupe_ttl_days=" in line and " or " in line
        and "DEFAULT_DEDUPE_TTL_DAYS" not in line
    ]
    assert not offenders, (
        "fallback de dedupe_ttl_days com literal em vez da constante canônica:\n"
        + "\n".join(f"  {o}" for o in offenders)
    )
