"""Contrato do TETO POR CICLO: quem para no teto tem de DIZER que parou.

Todo coletor de polling tem um teto de trabalho por ciclo (``_MAX_PAGES_PER_CYCLE``
e primos). Ele existe para não estourar o ``task_soft_time_limit`` — sem ele um
backlog grande é drenado num único run, o soft-timeout reverte o cursor e a
coleta entra em poison-loop. Mas o teto tem um efeito colateral: para NÃO perder
dado, o coletor deixa o watermark PARADO no ponto já consumido e guarda a posição
de retomada.

Watermark parado é ambíguo. Ele significa "não há eventos novos" num stream
quieto e "não estamos dando conta" num stream em backlog — e os dois casos são
byte a byte idênticos na tabela. ``CollectorContext.hit_cycle_cap`` (via
``mark_cycle_capped``) é o que desfaz a ambiguidade, e é por isso que a Saúde do
Pipeline exige os DOIS sinais para escalar para ``degraded``.

Consequência de esquecer a chamada: ``last_run_capped`` fica ``false`` para
sempre naquele vendor, o backlog nunca é detectado, o status nunca escala. Nada
quebra, nada aparece vermelho — o coletor simplesmente atrasa em silêncio, que é
literalmente o incidente de produção (jul/2026) que originou esta branch: um
coletor 15 horas atrás reportando ``lag_seconds: 0`` e ``healthy``.

Por isso os guards aqui são ESTRUTURAIS, sobre o fonte de cada coletor
registrado: um vendor novo que ganhe teto e esqueça de sinalizar quebra o CI em
vez de estrear cego. Espelha o guard de filtros de
``test_collection_filter_contract.py``.

A tradução do cursor em si (``watermark_at``) e a persistência estão em
``test_watermark_lag.py``; a agregação e o escalonamento de status, em
``test_pipeline_health_router.py``.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import inspect
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import pytest

from backend.app.collectors import registry as registry_module
from backend.app.collectors.base import BaseCollector

_REGISTRATIONS = list(registry_module.all_registrations())
_IDS = [f"{r.platform}/{r.stream}" for r in _REGISTRATIONS]

# O gate do teto: TODO coletor com teto por ciclo consulta esta flag antes de
# encerrar o run (é ela que o backfill usa para drenar sem capar). Ancorar em
# ``self.ctx.`` e não no nome solto evita casar com as menções em comentário.
_CAP_GATE = "self.ctx.bounded_per_cycle"
#: Rede de segurança para um teto futuro que não passe pelo gate acima — o nome
#: da constante é a convenção seguida por todos os vendors de hoje.
_CAP_CONST = re.compile(r"_MAX_\w*_PER_CYCLE\b")
_SIGNAL = "self.mark_cycle_capped()"
#: Formas de encerrar o bloco do teto. ``raise``/``continue`` entram porque, sem
#: eles, um bloco mudo que saia por ``raise`` nunca "fecha" e o guard engole as
#: 40 linhas seguintes — inclusive o ``mark_cycle_capped()`` de um bloco POSTERIOR,
#: dando o sinal como presente onde ele não está.
_EXIT = re.compile(r"^\s*(return|break|continue|raise)\b")
#: Quantas linhas depois do gate ainda contam como "o bloco do teto". Os blocos
#: reais têm ~15 linhas (o maior é o do CloudTrail); a folga é generosa de
#: propósito, para o guard falhar por sinal AUSENTE e não por formatação.
_BLOCK_MAX_LINES = 40


def _module_source(reg) -> str:
    """Fonte do módulo do coletor, ou "" quando ele não existe.

    Na imagem de produção o ``cython-build.sh`` compila ``app/collectors`` para
    ``.so`` e APAGA os ``.py``: ``inspect.getsource`` levanta ``OSError`` sobre um
    módulo de extensão. Como este helper roda em tempo de IMPORT (alimenta o
    ``parametrize``), a exceção quebraria a COLETA da suíte inteira, e não só
    estes testes — que já são ``source_only`` e são pulados lá.
    """
    module = inspect.getmodule(reg.collector_cls)
    if module is None:
        return ""
    try:
        return inspect.getsource(module)
    except (OSError, TypeError):
        return ""


def _has_cycle_cap(src: str) -> bool:
    return _CAP_GATE in src or bool(_CAP_CONST.search(src))


def _cap_blocks(src: str) -> List[Tuple[int, str]]:
    """Cada gate de teto e o trecho até a saída (``return``/``break``) que ele governa.

    Olhar bloco a bloco, e não o módulo inteiro, é o que pega o caso perigoso: um
    coletor com DOIS pontos de teto que sinaliza em um só. O módulo conteria a
    chamada e um guard ingênuo passaria, enquanto metade das saídas continuaria
    muda.
    """
    lines = src.splitlines()
    blocks: List[Tuple[int, str]] = []
    for idx, line in enumerate(lines):
        if _CAP_GATE not in line:
            continue
        chunk = [line]
        for nxt in lines[idx + 1 : idx + 1 + _BLOCK_MAX_LINES]:
            chunk.append(nxt)
            if _EXIT.match(nxt):
                break
        blocks.append((idx + 1, "\n".join(chunk)))
    return blocks


_CAPPED = [(reg, _module_source(reg)) for reg in _REGISTRATIONS]
_CAPPED = [(reg, src) for reg, src in _CAPPED if _has_cycle_cap(src)]
_CAPPED_IDS = [f"{reg.platform}/{reg.stream}" for reg, _ in _CAPPED]


# ── Âncora: o guard abaixo precisa ter o que verificar ───────────────────


@pytest.mark.source_only  # lê o .py; na imagem Cython o fonte não existe
def test_the_fleet_still_has_capped_collectors() -> None:
    """Sem esta âncora, apagar o teto de todo mundo deixaria o guard verde e vazio.

    Hoje só os dois streams de PUSH (``push_ingest``) não têm teto — eles drenam
    um buffer local, não paginam fornecedor.
    """
    assert len(_CAPPED) >= 12, (
        f"só {len(_CAPPED)} coletores com teto por ciclo foram descobertos "
        f"({_CAPPED_IDS}) — o guard de sinalização ficou quase vazio"
    )


# ── GUARD 1: coletor com teto sinaliza o teto ────────────────────────────


@pytest.mark.source_only  # lê o .py; na imagem Cython o fonte não existe
@pytest.mark.parametrize("reg,src", _CAPPED, ids=_CAPPED_IDS)
def test_every_capped_collector_signals_the_cap(reg, src: str) -> None:
    """Teto sem sinal = backlog invisível naquele vendor, para sempre.

    O coletor continua correto (não perde evento, retoma do cursor), a coleta
    continua "verde" e o atraso cresce sem nada na tela. É o modo de falha mais
    caro deste subsistema justamente porque não parece falha.
    """
    assert _SIGNAL in src, (
        f"{reg.platform}/{reg.stream} tem teto por ciclo mas "
        f"{reg.collector_cls.__name__} nunca chama mark_cycle_capped() — "
        "last_run_capped fica false para sempre e a Saúde do Pipeline nunca "
        "detecta backlog neste vendor"
    )


# ── GUARD 2: TODA saída pelo teto sinaliza, não só a primeira ────────────


@pytest.mark.source_only  # lê o .py; na imagem Cython o fonte não existe
@pytest.mark.parametrize("reg,src", _CAPPED, ids=_CAPPED_IDS)
def test_every_cap_exit_signals_before_leaving(reg, src: str) -> None:
    """Cada ``return``/``break`` de teto tem de passar por ``mark_cycle_capped``.

    Um coletor pode ter mais de um ponto de teto (o CloudTrail, por exemplo, capa
    por objeto dentro de dois laços aninhados). Sinalizar em um e esquecer o
    outro produz um sinal INTERMITENTE — pior que a ausência dele, porque o
    operador vê o indicador acender e apagar e conclui que é ruído.
    """
    for line_no, block in _cap_blocks(src):
        assert _SIGNAL in block, (
            f"{reg.platform}/{reg.stream}: o teto em "
            f"{inspect.getsourcefile(reg.collector_cls)}:{line_no} encerra o run "
            "sem chamar mark_cycle_capped() — este caminho de backlog fica mudo"
        )


@pytest.mark.source_only  # lê o .py; na imagem Cython o fonte não existe
@pytest.mark.parametrize("reg,src", _CAPPED, ids=_CAPPED_IDS)
def test_the_signal_only_fires_from_a_cap_exit(reg, src: str) -> None:
    """``mark_cycle_capped`` fora do teto afirmaria backlog onde não há.

    Chamá-lo no caminho normal marcaria TODO ciclo como "sobrou trabalho"; junto
    com um watermark legitimamente parado (stream quieto), isso vira ``degraded``
    permanente — e um indicador que fica amarelo sozinho é um indicador que o
    operador desliga.
    """
    dentro_de_teto = sum(block.count(_SIGNAL) for _, block in _cap_blocks(src))
    assert src.count(_SIGNAL) == dentro_de_teto, (
        f"{reg.platform}/{reg.stream} chama mark_cycle_capped() fora de um bloco "
        "de teto — o ciclo passaria a alegar backlog em execução normal"
    )


@pytest.mark.source_only  # lê o .py; na imagem Cython o fonte não existe
@pytest.mark.parametrize(
    "reg,src",
    [(reg, _module_source(reg)) for reg in _REGISTRATIONS if not _has_cycle_cap(_module_source(reg))],
    ids=[f"{r.platform}/{r.stream}" for r in _REGISTRATIONS if not _has_cycle_cap(_module_source(r))],
)
def test_collector_without_a_cap_never_claims_backlog(reg, src: str) -> None:
    """Coletor sem teto (os de PUSH) não tem como ter sobrado trabalho de página."""
    assert _SIGNAL not in src, (
        f"{reg.platform}/{reg.stream} sinaliza teto sem ter teto por ciclo"
    )


# ── GUARD 3: cursor temporal reporta atraso; os demais devolvem None ─────

#: Para cada coletor que sobrescreve ``watermark_at``, o cursor que ele mesmo
#: grava ao BATER O TETO (o caso que importa: é quando o atraso é real) e o
#: instante que se espera extrair dele.
#:
#: A tabela é explícita e o teste abaixo exige que ela cubra exatamente o
#: conjunto de coletores que sobrescrevem. Motivo: o defeito silencioso deste
#: subsistema é a CHAVE ERRADA — ``watermark_at`` lendo ``"created_at"`` num
#: cursor que grava ``"created_after"`` devolve ``None`` para sempre, o campo
#: some da UI e nada acusa. Um teste genérico "não levanta exceção" (que existe
#: em test_watermark_lag.py) passaria feliz.
_ESPERADO = datetime(2026, 7, 24, 10, 0, 0)
_EPOCH_MS = int(_ESPERADO.replace(tzinfo=timezone.utc).timestamp() * 1000)

_CURSORES_DE_TETO: Dict[Tuple[str, str], Dict[str, Any]] = {
    # Wazuh grava o offset SEM dois-pontos — é o formato do Indexer.
    ("wazuh", "detections"): {"from_ts": "2026-07-24T10:00:00.000+0000"},
    ("sophos", "alerts"): {"from_ts": "2026-07-24T10:00:00Z", "pageFromKey": "pk-42"},
    ("sophos", "cases"): {"created_after": "2026-07-24T10:00:00Z", "page": 7},
    ("sophos", "detections"): {
        "run_id": "run-1",
        "from_ts": "2026-07-24T10:00:00Z",
        "page": 3,
    },
    ("crowdstrike", "detections"): {
        "created_after": "2026-07-24T10:00:00.000Z",
        "after": "tok-abc",
    },
    ("microsoft_defender", "incidents"): {
        "lastUpdateDateTime": "2026-07-24T10:00:00Z",
        "@odata.nextLink": "https://graph.microsoft.com/next",
    },
    ("microsoft_defender", "alerts"): {
        "lastUpdateDateTime": "2026-07-24T10:00:00Z",
        "@odata.nextLink": "https://graph.microsoft.com/next",
    },
    ("entra_id", "signins"): {
        "createdDateTime": "2026-07-24T10:00:00Z",
        "@odata.nextLink": "https://graph.microsoft.com/next",
    },
    # Chave DIFERENTE da subclasse acima — é o que ``_CURSOR_FIELD`` resolve.
    ("entra_id", "audit"): {
        "activityDateTime": "2026-07-24T10:00:00Z",
        "@odata.nextLink": "https://graph.microsoft.com/next",
    },
    ("veeam", "sessions"): {"created_after": "2026-07-24T10:00:00Z", "skip": 400},
    # ``datetime.isoformat()`` de um aware UTC → offset com dois-pontos.
    ("aws_cloudtrail", "events"): {
        "last_modified": "2026-07-24T10:00:00+00:00",
        "prefix": "AWSLogs/1/CloudTrail/us-east-1/2026/07/24/",
        "start_after": "chave.json.gz",
    },
    # Epoch em MILISSEGUNDOS — o único da frota que não é string.
    ("aws_cloudwatch", "events"): {
        "start_time_ms": _EPOCH_MS,
        "end_time_ms": _EPOCH_MS + 3_600_000,
        "next_token": "tok-xyz",
    },
}


def _overrides_watermark(reg) -> bool:
    return (
        reg.collector_cls.watermark_at.__func__
        is not BaseCollector.watermark_at.__func__
    )


def test_the_watermark_table_covers_exactly_the_collectors_that_report_one() -> None:
    """A tabela abaixo é o inventário: implementar sem cobrir aqui não vale.

    Sem esta amarração, um ``watermark_at`` novo entraria sem ninguém verificar
    que ele lê a chave certa — que é o único jeito de o campo falhar em silêncio.
    """
    implementam = {(r.platform, r.stream) for r in _REGISTRATIONS if _overrides_watermark(r)}
    assert implementam == set(_CURSORES_DE_TETO), (
        "divergência entre quem implementa watermark_at e a tabela de cursores: "
        f"sem cobertura={sorted(implementam - set(_CURSORES_DE_TETO))}, "
        f"na tabela sem implementar={sorted(set(_CURSORES_DE_TETO) - implementam)}"
    )


@pytest.mark.parametrize(
    "chave", sorted(_CURSORES_DE_TETO), ids=[f"{p}/{s}" for p, s in sorted(_CURSORES_DE_TETO)]
)
def test_cap_cursor_yields_the_real_instant(chave: Tuple[str, str]) -> None:
    """Chave errada em ``watermark_at`` = ``None`` eterno = indicador ausente.

    O cursor usado é o que o próprio coletor grava ao bater o teto — o momento em
    que o atraso é real e o indicador precisa funcionar.
    """
    reg = registry_module.get(*chave)
    got = reg.collector_cls.watermark_at(_CURSORES_DE_TETO[chave])
    assert got == _ESPERADO, (
        f"{chave[0]}/{chave[1]}.watermark_at devolveu {got!r} para o cursor que o "
        f"próprio coletor grava no teto — chave do cursor errada? O campo sumiria "
        "da Saúde do Pipeline sem nenhum erro"
    )
    assert got.tzinfo is None, "aware quebraria a subtração com utcnow() no ciclo"


@pytest.mark.parametrize(
    "reg",
    [r for r in _REGISTRATIONS if not _overrides_watermark(r)],
    ids=[f"{r.platform}/{r.stream}" for r in _REGISTRATIONS if not _overrides_watermark(r)],
)
def test_collector_without_a_temporal_cursor_reports_none(reg) -> None:
    """``None`` é resposta legítima — a UI omite o indicador em vez de inventar.

    Okta pagina por ``next_url`` opaco e NinjaOne por keyset de id (o
    ``activity_time_after`` é piso fixo, não posição consumida): não há instante
    para traduzir. Fabricar um número aqui seria pior que não ter o campo, porque
    um "0" afirmaria "em dia". Nestes streams o ``last_run_capped`` sozinho é o
    que sobra — e é por isso que o Guard 1 acima vale para eles também.
    """
    assert reg.collector_cls.watermark_at({"qualquer": "coisa"}) is None


# ── O par de sinais, exercitado num coletor de verdade ───────────────────


@pytest.mark.source_only  # usa o mock de aiohttp que vive junto do fonte dos coletores
async def test_empty_cycle_does_not_claim_backlog() -> None:
    """Ciclo sem eventos NÃO liga ``hit_cycle_cap`` — o falso positivo mais provável.

    Este é o outro lado de ``test_determine_status_backlog_requires_both_signals``
    (em ``test_pipeline_health_router.py``): lá se garante que o status não escala
    com um sinal só; aqui se garante que o coletor não produz o sinal à toa. Se
    produzisse, todo stream quieto — que também tem watermark parado — cairia em
    ``degraded`` e o indicador morreria de descrédito na primeira semana.
    """
    ctx, collect = await _run_veeam(paginas=[[]], teto=50)
    assert collect == []
    assert ctx.hit_cycle_cap is False, (
        "janela vazia marcada como backlog — todo stream quieto viraria degraded"
    )


@pytest.mark.source_only  # usa o mock de aiohttp que vive junto do fonte dos coletores
async def test_cycle_stopped_by_the_cap_claims_backlog() -> None:
    """O mesmo coletor, agora parando no teto, TEM de sinalizar.

    O par com o teste acima é o que prova que a flag discrimina — uma flag que
    nunca sobe e uma que sempre sobe passam, cada uma, em metade dos testes.
    """
    cheia = [_sessao_veeam(f"s-{i}") for i in range(2)]
    ctx, collect = await _run_veeam(paginas=[cheia, cheia, cheia], teto=2, page_size=2)
    assert len(collect) == 4, "parou no teto, como esperado"
    assert ctx.hit_cycle_cap is True, (
        "parou no teto sem sinalizar — o backlog restante fica invisível"
    )


def _sessao_veeam(sid: str) -> Dict[str, Any]:
    return {
        "id": sid,
        "name": "Daily Backup",
        "sessionType": "BackupJob",
        "state": "Stopped",
        "creationTime": "2026-07-24T10:00:00.000Z",
        "result": {"result": "Success", "message": "", "isCanceled": False},
    }


async def _run_veeam(
    paginas: List[List[Dict[str, Any]]], teto: int, page_size: int = 200
) -> Tuple[Any, List[Dict[str, Any]]]:
    """Roda o coletor do Veeam contra páginas fixas. Import tardio: o mock de
    aiohttp mora em ``app/collectors/tests`` e não existe na imagem compilada."""
    import re as _re
    from unittest.mock import MagicMock, patch

    import aiohttp

    from backend.app.collectors.base import CollectorContext
    from backend.app.collectors.tests._aiohttp_mock import aioresponses
    from backend.app.collectors.vendors import veeam as vm

    conn = {
        "base_url": "https://vbr.local:9419",
        "username": "svc",
        "password": "s3cr3t",
        "api_version": "1.2-rev0",
        "verify_ssl": False,
    }

    class _NoopLimiter:
        def slot(self, domain):
            class _Ctx:
                async def __aenter__(self):
                    return None

                async def __aexit__(self, *a):
                    return False

            return _Ctx()

        async def acquire(self, tenant_id, vendor):
            return None

        async def backoff(self, vendor, retry_after):
            return None

    limiter = _NoopLimiter()
    with aioresponses() as m:
        m.post(
            _re.compile(r"^https://vbr\.local:9419/api/oauth2/token"),
            payload={"access_token": "tok", "expires_in": 900},
        )
        for idx, page in enumerate(paginas):
            m.get(
                _re.compile(r"^https://vbr\.local:9419/api/v1/sessions"),
                payload={
                    "data": page,
                    # ``total`` alto de propósito: o freio tem de ser o teto, não
                    # o fim do backlog.
                    "pagination": {
                        "total": 10_000,
                        "count": len(page),
                        "skip": idx * page_size,
                        "limit": page_size,
                    },
                },
            )
        async with aiohttp.ClientSession() as session:
            ctx = CollectorContext(
                integration_id=77,
                organization_id=3,
                platform="veeam",
                headers={},
                session=session,
                cursor={"created_after": "2026-07-24T10:00:00Z", "skip": 0},
                domain_limiter=limiter,
                rate_limiter=limiter,
                redis=MagicMock(),
            )
            with patch.object(vm, "_MAX_PAGES_PER_CYCLE", teto), patch.object(
                vm, "_PAGE_SIZE", page_size
            ), patch.object(
                vm.VeeamSessionsCollector, "_load_conn", return_value=dict(conn)
            ):
                got = [ev async for ev in vm.VeeamSessionsCollector(ctx).collect()]
    return ctx, got


# ── Contrato do helper compartilhado de parse ────────────────────────────


@pytest.mark.parametrize(
    "texto",
    [
        "2026-07-24T10:00:00Z",
        "2026-07-24T10:00:00+00:00",
        "2026-07-24T10:00:00.000+0000",
        "2026-07-24T07:00:00-0300",
        "2026-07-24T10:00:00",  # sem offset: lido como UTC
    ],
    ids=["z", "offset-com-dois-pontos", "offset-sem-dois-pontos", "fuso-local", "naive"],
)
def test_shared_iso_helper_normalizes_every_format_the_fleet_emits(texto: str) -> None:
    """Um helper só para 10 coletores — cada parse próprio era uma chance de divergir.

    O caso ``naive`` é o que mais importa: ``astimezone`` em datetime sem tzinfo
    assume o fuso do WORKER, então um pod em America/Sao_Paulo reportaria 3h de
    atraso fantasma no mesmo cursor que um pod em UTC reporta zero.
    """
    from backend.app.collectors.vendors.veeam import VeeamSessionsCollector

    got = VeeamSessionsCollector.watermark_from_iso({"created_after": texto}, "created_after")
    assert got == _ESPERADO


@pytest.mark.parametrize(
    "valor",
    [None, "", "   ", 1753351800, "ontem", "2026-13-45T99:99:99Z", True],
    ids=["none", "vazio", "espacos", "epoch-em-campo-iso", "texto", "data-invalida", "bool"],
)
def test_shared_iso_helper_never_raises(valor: Any) -> None:
    """Roda no fim de TODO ciclo: uma exceção aqui pararia a ingestão."""
    from backend.app.collectors.vendors.veeam import VeeamSessionsCollector

    assert VeeamSessionsCollector.watermark_from_iso({"created_after": valor}, "created_after") is None


@pytest.mark.parametrize(
    "valor,esperado",
    [
        (_EPOCH_MS, _ESPERADO),
        (float(_EPOCH_MS), _ESPERADO),
        (0, None),
        (-1, None),
        (None, None),
        (True, None),
        ("2026-07-24T10:00:00Z", None),
        (10**18, None),
    ],
    ids=["ms", "float", "zero", "negativo", "ausente", "bool", "iso-em-campo-epoch", "absurdo"],
)
def test_shared_epoch_ms_helper(valor: Any, esperado: Optional[datetime]) -> None:
    """Epoch tem helper próprio porque a unidade não é adivinhável.

    ``1753351800`` é 2026 em SEGUNDOS e 1970 em milissegundos. Deixar o parser
    escolher erraria por 1000x — na tela, a diferença entre "em dia" e "55 anos
    atrasado". Quem tem cursor em epoch declara a unidade na chamada.
    """
    from backend.app.collectors.vendors.aws_cloudwatch import AWSCloudWatchCollector

    assert (
        AWSCloudWatchCollector.watermark_from_epoch_ms({"start_time_ms": valor}, "start_time_ms")
        == esperado
    )
