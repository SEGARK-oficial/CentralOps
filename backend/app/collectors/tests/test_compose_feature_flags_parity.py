"""Guard: as flags de feature chegam ao processo, e valem o MESMO em todos eles.

Duas regressões distintas, e este arquivo fecha as duas.

**1. A flag não chegava a lugar nenhum.** Nenhuma das flags de ADR estava
declarada em serviço NENHUM do compose. E como o compose não tem ``env_file:``, o
``compose/.env`` só serve para interpolar ``${VAR}`` DENTRO do compose — não vira
variável de ambiente do processo. Escrever ``OCSF_VALIDATION_ENABLED=true`` no
``.env`` era **no-op mudo**: a stack subia, a flag continuava no default, e nada
avisava. Não existia caminho suportado para ligar a validação OCSF, nem para
DESLIGAR a emissão da Detection ao SIEM — e um kill switch que não chega é pior
que nenhum.

**2. Declarar em ALGUNS é pior que em nenhum.** ``GET /correlation-rules/limits``
devolve ``inflight_cap_as_seen_by_api``, e o nome do campo já admite o problema:
quem AVALIA as regras é o worker, quem RESPONDE a rota é a API, e as duas leem a
mesma variável em processos diferentes. Declarar só nos workers — o gesto natural,
"é config de coletor" — faz a API responder 50 com toda a confiança enquanto o
coletor corta em outro número. ``truncated_count`` vira ficção: o operador lê
"nenhuma regra truncada" e a regra dele continua sem disparar.

É a forma EXATA do incidente do broker partido (ver o guard irmão
``test_compose_celery_broker_parity.py``): a variável foi colada em 8 serviços e
esquecida no nono, e o modo de falha foi silêncio dos dois lados.

O critério é TUDO OU NADA, igual ao guard do EE para o teto de avaliação:
declarar em todos os serviços da app é correto (explícito), não declarar em
nenhum seria correto (default compartilhado) — mas aqui exigimos o primeiro,
porque o segundo é o estado que tornava a feature inalcançável.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict

import pytest

yaml = pytest.importorskip("yaml")

_COMPOSE = Path(__file__).resolve().parents[4] / "compose" / "docker-compose.yml"
_ENV_EXAMPLE = Path(__file__).resolve().parents[4] / "compose" / ".env.example"

#: Serviços que rodam a imagem da APLICAÇÃO — derivado da IMAGEM e não de uma
#: lista fixa, para um worker novo entrar na checagem sozinho. Mesmo marcador dos
#: guards irmãos.
_APP_IMAGE_MARKER = "${IMAGE_NAME:-centralops}:"
#: O SPA compartilha o prefixo da imagem e não roda Python.
_NAO_E_APP = {"frontend"}

#: As flags que decidem se cada feature de ADR roda. Cada uma tem de estar em
#: TODOS os serviços da app, com o MESMO valor.
_FLAGS = (
    "OCSF_VALIDATION_ENABLED",
    "OCSF_DEFAULT_ENFORCEMENT",
    "INFLIGHT_MAX_RULES_PER_CYCLE",
    "INFLIGHT_MAX_WHERE_CLAUSES",
    "INFLIGHT_EMIT_OCSF_EVENT",
    "CORRELATION_MAX_RULES_PER_ORG",
    "ENRICHMENT_ENABLED",
)


def _servicos_da_app() -> Dict[str, Dict[str, str]]:
    if not _COMPOSE.is_file():
        pytest.skip(f"compose não encontrado em {_COMPOSE} (rodando fora do repo)")
    data = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))
    saida: Dict[str, Dict[str, str]] = {}
    for nome, cfg in (data.get("services") or {}).items():
        if nome in _NAO_E_APP:
            continue
        if _APP_IMAGE_MARKER not in str((cfg or {}).get("image", "") or ""):
            continue
        env = (cfg or {}).get("environment") or []
        if isinstance(env, dict):
            saida[nome] = {str(k): str(v) for k, v in env.items()}
        else:
            pares = (str(e).split("=", 1) for e in env)
            saida[nome] = {p[0]: (p[1] if len(p) > 1 else "") for p in pares}
    return saida


def test_o_conjunto_de_servicos_da_app_nao_esta_vazio() -> None:
    """ANTI-VACUIDADE, e não formalidade: se o marcador de imagem mudar, todo
    teste abaixo passaria varrendo um dicionário vazio — exatamente o silêncio
    que este arquivo existe para impedir."""
    svcs = _servicos_da_app()
    assert len(svcs) >= 5, f"só {len(svcs)} serviços da app encontrados: {sorted(svcs)}"
    assert "centralops" in svcs, "a API sumiu do conjunto — o marcador de imagem mudou?"


@pytest.mark.parametrize("flag", _FLAGS)
def test_toda_flag_esta_declarada_em_todos_os_servicos_da_app(flag: str) -> None:
    svcs = _servicos_da_app()
    faltando = sorted(n for n, env in svcs.items() if flag not in env)
    assert not faltando, (
        f"{flag} não está declarada em {faltando}. Sem `env_file:` no compose, "
        "o que não está no `environment:` NÃO chega ao processo — pôr no `.env` "
        "é no-op mudo. E declarar em alguns serviços é pior que em nenhum: API e "
        "workers passam a ler valores diferentes, em silêncio."
    )


@pytest.mark.parametrize("flag", _FLAGS)
def test_toda_flag_tem_o_MESMO_valor_em_todos_os_servicos(flag: str) -> None:
    """PAR do teste acima. Aquele só exige presença — este exige acordo. Declarar
    a mesma var com valores diferentes por serviço reproduz a divergência que a
    presença sozinha não impede."""
    svcs = _servicos_da_app()
    valores = {n: env[flag] for n, env in svcs.items() if flag in env}
    distintos = set(valores.values())
    assert len(distintos) <= 1, (
        f"{flag} tem valores divergentes entre serviços: {valores}"
    )


@pytest.mark.parametrize("flag", _FLAGS)
def test_toda_flag_e_DESCOBRIVEL_no_env_example(flag: str) -> None:
    """Declarada no compose e ausente do `.env.example` é uma capacidade que só
    quem lê o YAML descobre. O `.env.example` é o único lugar onde o operador
    procura."""
    if not _ENV_EXAMPLE.is_file():
        pytest.skip(f"{_ENV_EXAMPLE} ausente")
    texto = _ENV_EXAMPLE.read_text(encoding="utf-8")
    assert flag in texto, (
        f"{flag} está no compose e não aparece em compose/.env.example — o "
        "operador não tem como saber que ela existe."
    )


@pytest.mark.parametrize("flag", _FLAGS)
def test_o_default_do_compose_REPETE_o_default_do_codigo(flag: str) -> None:
    """O compose não pode mudar comportamento por si.

    Cada entrada é ``${VAR:-default}``, e esse default tem de ser o mesmo de
    ``config.py``. Se divergirem, subir a stack sem `.env` produz um
    comportamento diferente de rodar o mesmo código fora do compose — e a
    diferença não aparece em teste de unidade nenhum, porque é config de deploy.
    """
    from backend.app.core.config import settings

    svcs = _servicos_da_app()
    bruto = svcs["centralops"][flag]
    assert bruto.startswith("${") and ":-" in bruto, (
        f"{flag} não usa a forma ${{VAR:-default}}: {bruto!r}. Sem default, um "
        ".env ausente injeta string vazia em vez de cair no default do código."
    )
    do_compose = bruto.split(":-", 1)[1].rstrip("}")
    do_codigo = getattr(settings, flag)

    esperado = (
        "true" if do_codigo is True else "false" if do_codigo is False else str(do_codigo)
    )
    assert do_compose == esperado, (
        f"{flag}: compose default {do_compose!r} ≠ default do código {esperado!r}. "
        "Subir a stack sem .env passaria a mudar comportamento."
    )
