"""Carga, compilação, acumulação e flush das regras em voo (ADR-0015, Fase 1).

Tudo que toca o mundo mora aqui, para que ``matcher`` possa permanecer puro. Nada
neste módulo roda por evento exceto ``InflightAccumulator.add``, que é aritmética
em memória sobre dicts.

Fluxo por ciclo de coleta:

1. ``load_inflight_rules_for_org`` — 1x, fora do laço, via ``asyncio.to_thread``.
   Abre e fecha a própria sessão (não há sessão de DB aberta no hot path).
2. ``InflightAccumulator.add`` — por evento, só quando há match. Em memória.
3. ``flush_inflight`` — 1x, no ``finally`` do ciclo. Escreve as Detections
   off-loop, EMITE cada Detection nova como evento OCSF 2004 pelo caminho normal
   de dispatch (atrás de ``INFLIGHT_EMIT_OCSF_EVENT``, OFF por default) e emite
   as métricas de fim de ciclo.

Por que a detecção SAI como evento, e não só como linha: enquanto ela só existe
numa tabela que a UI lê, a resposta para "onde chega o alerta? meu SOC vive no
Splunk/Sentinel" é "em lugar nenhum". A emissão reusa a máquina que o
``scheduled_query`` já tinha testada em produção (``_dispatch_scheduled_query_
alert`` → ``_enqueue_dispatch``), e paga R1 pelo mesmo motivo do flush: é 1x por
CICLO, em bulk, nunca por evento.

Por que acumular em vez de escrever por match: ``DetectionRepository.record``
faz SELECT + commit + refresh, ou seja ≥3 round-trips de Postgres por chamada.
Escrever uma Detection por evento casado dentro do ``async for`` de coleta
violaria R1 e reproduziria a forma do poison-loop de coletor já vivido em
produção — o laço de coleta awaitando I/O de escrita proporcional ao backlog.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from ...core.config import settings
from .matcher import CompiledClause, CompiledInflightRule, CompiledRuleSet
from ..normalize.envelope import ENVELOPE_ROOTS

logger = logging.getLogger(__name__)

#: Vocabulário aceito em ``where_json`` no modo ``inflight``. Superconjunto do
#: batch (``services/correlation_engine._OPS``) com os três de tuning.
INFLIGHT_ALLOWED_OPS = frozenset(
    {"eq", "ne", "contains", "gt", "lt", "gte", "lte", "in", "nin", "exists"}
)

#: Operadores cujo lado esquerdo é coagido a ``float`` na avaliação. Ver o
#: docstring de ``CompiledClause.numeric``: sem isso, severidade serializada
#: como string nunca casa e o sintoma é um contador zerado.
NUMERIC_OPS = frozenset({"gt", "lt", "gte", "lte"})

#: Operadores negativos, que casam por VACUIDADE quando o campo está ausente.
#: Todo path usado por um deles ganha uma cláusula ``exists`` auto-injetada.
NEGATIVE_OPS = frozenset({"ne", "nin"})

#: Enum FECHADO de razões de rejeição — vira label de métrica, logo nunca pode
#: conter valor vindo de evento ou nome de regra (esses vão no log).
#: Governa APENAS ``collector_inflight_rules_rejected_total``, a série da
#: CARGA (1x por ciclo). Não se mistura com ``ERROR_REASONS``, que é a outra
#: série e o outro momento (avaliação/flush).
#:
#: ``load_failed`` é a única razão que não é falha de COMPILAÇÃO de uma regra:
#: é o banco fora, com o ``except`` amplo da carga devolvendo ruleset VAZIO.
#: Mora aqui, e não numa série nova, porque é o mesmo momento e o mesmo eixo de
#: leitura ("por que a carga não entregou regra?") — e porque uma série nova
#: por causa disso precisaria de label de org, que é id global. O que a separa
#: de "a org não tem regra em voo" é o PAR: gauge ``rules_loaded`` em 0 (que a
#: carga passou a emitir também neste caminho) mais este contador subindo.
REJECT_REASONS = (
    "bad_json", "empty_where", "unknown_op", "over_cap", "truncated", "load_failed",
    # ``group_by_field`` cujo primeiro segmento não é chave de topo do envelope.
    # Rejeitar na COMPILAÇÃO e não na avaliação é a diferença entre um sinal e
    # um silêncio: rejeitada, a regra entra em ``uncompilable_count`` e ganha o
    # selo "Não avaliada" na tela; aceita, ela contava match a cada evento e
    # produzia Detection nenhuma, para sempre.
    "group_by_root",
)

#: Enum FECHADO de razões de ERRO de avaliação/flush — label de
#: ``collector_inflight_errors_total``. Mesma disciplina do enum acima: nunca
#: carrega valor de evento nem nome de regra.
#:
#: ``rule_id`` deliberadamente NÃO entra como label desta série: é id global,
#: multiplicaria a cardinalidade por ``reason`` e o lado OTLP não tem TTL para
#: envelhecer a série de uma regra apagada — a mesma recusa já escrita para
#: ``collector_capture_tap_disabled_total``. O breakdown por regra vai para o
#: ``observability_store`` (Redis, TTL 25h), que é de onde a UI lê.
#:
#: ``matcher`` é o único reason escrito de FORA deste módulo
#: (``pipeline.py``, no ``except`` que envolve ``evaluate_ruleset``) e o único
#: NÃO atribuível a uma regra: a exceção nasce antes de se saber qual regra
#: estava sendo avaliada, então não há ``rule_id`` honesto para carregar. Ele
#: pertence ao enum mesmo assim, porque chega a ``INFLIGHT_ERRORS.labels()`` em
#: produção — deixá-lo de fora tornaria a palavra "FECHADO" acima falsa, e um
#: invariante que é só comentário é exatamente a dívida que este arquivo existe
#: para não repetir.
#:
#: ``emit_failed`` é a falha da EMISSÃO do evento OCSF 2004 (a Detection foi
#: gravada, o evento não saiu). É ATRIBUÍVEL — o ticket de emissão carrega o
#: ``rule_id`` —, então ganha breakdown por regra e NÃO entra em
#: ``UNATTRIBUTED_ERROR_REASONS``: a pergunta que ele responde é "qual regra
#: parou de chegar no meu SIEM?", e um total sem regra não responde nada.
#:
#: ``flush_cap`` é o teto GLOBAL de Detections por flush
#: (``INFLIGHT_MAX_DETECTIONS_PER_FLUSH``). Vizinho de ``key_cap`` no espírito
#: e de ``flush_lost`` na consequência: a chave existia, casou, e NÃO virou
#: Detection. É ATRIBUÍVEL (o item pendente carrega a regra compilada), então
#: ganha breakdown por regra — a pergunta que ele responde é "qual regra comeu
#: o orçamento de escrita do ciclo?", e um total sem regra não responde nada.
#: ``mark_failed`` é a falha ao gravar a MARCA de detecção no envelope
#: (``_centralops.detection_matched``, escrita pelo ``pipeline.py`` quando o
#: evento casa). Vizinha de ``matcher`` na origem — call site FORA deste módulo,
#: forma plana, sem ``rule_id`` — e por isso entra em
#: ``UNATTRIBUTED_ERROR_REASONS``: a marca é UMA escrita para as N regras que
#: casaram, então atribuí-la a uma delas seria inventar culpado.
#:
#: Existe separada de ``matcher`` porque responde a outra pergunta. ``matcher``
#: diz "a avaliação quebrou, este evento não foi classificado"; ``mark_failed``
#: diz "classificou, a Detection foi gravada, mas o evento saiu SEM a marca" —
#: e o sintoma é uma rota condicionada à detecção que deixa de entregar. Somá-la
#: em ``matcher`` mandaria o operador depurar a regra em vez do envelope.
ERROR_REASONS = (
    "group_by_unresolved",
    "key_cap",
    "flush_cap",
    "flush_lost",
    "group_value_truncated",
    "matcher",
    "emit_failed",
    "mark_failed",
)

#: Razões que NÃO descem ao ``observability_store`` por regra, porque não há
#: ``rule_id`` honesto para carregar. Declarado aqui, e não escondido num teste,
#: para que a pergunta "por que esta razão não aparece no breakdown da UI?"
#: tenha resposta no mesmo lugar onde o enum vive. Uma razão nova escrita de
#: fora deste módulo entra AQUI ou ganha ``rule_id`` — nunca some em silêncio.
UNATTRIBUTED_ERROR_REASONS = ("matcher", "mark_failed")

#: Bytes do digest anexado ao token de group_by quando há corte (16 chars hex).
#: 64 bits é folgado para o universo real (chaves distintas por regra/ciclo é
#: teto de 2 dígitos), e o custo do sufixo entra no índice B-tree de
#: ``ix_detections_org_dedup`` — por isso curto, não sha256 inteiro.
GROUP_VALUE_DIGEST_BYTES = 8

#: Separador prefixo↔digest. Fora do alfabeto hex de propósito: o operador que
#: lê a dedup_key precisa enxergar onde o valor legível termina.
GROUP_VALUE_DIGEST_SEP = "~"

#: Granularidade dos contadores de disparo por regra (``observability_store``,
#: kind="rule") — HORÁRIA, não per-minute (default do store). Uma janela de
#: 24h em buckets de minuto seria 1440 campos no hash por regra; horária são
#: 24 — a mesma janela, hash 60x menor.
RULE_METRIC_BUCKET_SECONDS = 60 * 60

#: TTL dos contadores de disparo por regra: cobre a janela de leitura de 24h
#: (``RULE_METRIC_WINDOW_MINUTES``) com 1h de folga, para o bucket mais antigo
#: da janela nunca ter expirado no momento da leitura. O default do store
#: (3h) é insuficiente por construção para uma janela de 24h — é exatamente o
#: bug que estes dois valores existem para fechar.
RULE_METRIC_TTL_SECONDS = 25 * 60 * 60

#: Janela que a UI lê — "disparos nas últimas 24h" por regra.
RULE_METRIC_WINDOW_MINUTES = 24 * 60

# ── A detecção SAINDO como evento OCSF 2004 ──────────────────────────────
#
# Enum FECHADO de motivos para a Detection ter sido gravada e o evento NÃO ter
# saído — motivos DELIBERADOS, não falhas (falha é ``emit_failed``, acima).
# É label de ``collector_inflight_events_not_emitted_total``, logo mesma
# disciplina dos enums vizinhos: nunca carrega valor de evento nem nome de regra.
EMIT_SKIP_REASONS = ("suppressed", "loop_guard", "cycle_cap")

#: ``event_type`` do evento emitido. É a MARCA de auto-identificação usada pelo
#: guard de laço (ver ``_is_self_emitted``) — trocá-la sem trocar o guard
#: reabriria a cascata em silêncio, por isso as duas coisas vivem juntas aqui.
DETECTION_EVENT_TYPE = "centralops.inflight.detection"

#: ``stream`` e ``vendor`` do evento emitido. ``vendor`` é "centralops" (quem
#: PRODUZIU o achado) enquanto ``platform`` do envelope permanece a da
#: integração de origem (sobre QUEM é o achado) — mesma divisão que
#: ``_dispatch_scheduled_query_alert`` já usa, e é ela que faz a rota do cliente
#: para aquela plataforma também receber a detecção dela.
DETECTION_EVENT_STREAM = "inflight_detection"
DETECTION_EVENT_VENDOR = "centralops"

#: Teto de caracteres de TODO campo textual do evento emitido que veio do evento
#: do cliente (ou de texto que o operador escreveu). É o MESMO 120 de
#: ``preview._OBSERVED_MAXLEN``, e pela mesma razão declarada lá: o objetivo é
#: dar ao analista o que IDENTIFICA a entidade, não exportar o payload do
#: cliente para fora. Um teste amarra os dois números — se divergirem, a
#: disciplina de truncamento passa a depender de por onde o dado saiu.
DETECTION_EVENT_TEXT_MAXLEN = 120

#: Teto de bytes do evento emitido, serializado. NÃO é enforcement em runtime, e
#: isso é deliberado: o evento é limitado POR CONSTRUÇÃO (todo campo textual
#: passa por ``_evidence_text``; o resto são escalares e uma ``dedup_key`` já
#: capada por ``INFLIGHT_MAX_GROUP_VALUE_LEN``), então um ``json.dumps`` de
#: verificação por evento seria custo sem poder de decisão. O número existe para
#: ancorar a comparação com o limite REAL de destino — ``OS_MAXSTR`` = 65536 do
#: Wazuh, acima do qual o ``analysisd`` TRUNCA em silêncio — e um teste constrói
#: o pior caso possível e mede contra ele.
#:
#: MEDIDO, não estimado: um evento realista em ASCII ocupa ~1,5 KiB; com TODOS
#: os campos textuais no máximo em ASCII, ~3,9 KiB; e no pior caso patológico —
#: todo campo no máximo com caractere astral de 4 bytes, porque o truncamento é
#: em CARACTERES e o teto é em BYTES — 10,8 KiB. Daí os 12 KiB, que continuam a
#: um quinto do OS_MAXSTR.
DETECTION_EVENT_MAX_BYTES = 12 * 1024


def validate_where_json(raw: Optional[str]) -> tuple[list[dict], Optional[str]]:
    """``(cláusulas, None)`` ou ``([], razão)``. Público: o CRUD do EE deve
    reusar isto para rejeitar com 422 na escrita, em vez de deixar a regra
    entrar no banco e falhar silenciosamente na compilação."""
    try:
        parsed = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return [], "bad_json"
    if not isinstance(parsed, list):
        return [], "bad_json"
    clauses = [c for c in parsed if isinstance(c, dict)]
    if not clauses:
        # Uma regra em voo sem predicado casaria 100% dos eventos. No batch isso
        # é atenuado por min_count/janela; aqui não há nada atenuando.
        return [], "empty_where"
    return clauses, None


def compile_rule(row: Any) -> tuple[Optional[CompiledInflightRule], Optional[str]]:
    """``CorrelationRule`` (ORM) → regra compilada, ou ``(None, razão)``.

    Roda 1x por ciclo, fora do laço — pode ser generosa em validação.
    """
    clauses_raw, reason = validate_where_json(getattr(row, "where_json", None))
    if reason is not None:
        return None, reason

    cap = int(settings.INFLIGHT_MAX_WHERE_CLAUSES)
    if len(clauses_raw) > cap:
        return None, "over_cap"

    compiled: list[CompiledClause] = []
    negative_paths: set[tuple[str, ...]] = set()
    paths_with_exists: set[tuple[str, ...]] = set()

    for c in clauses_raw:
        op = str(c.get("op") or "eq")
        if op not in INFLIGHT_ALLOWED_OPS:
            return None, "unknown_op"
        field = c.get("field")
        if not field or not isinstance(field, str):
            return None, "bad_json"
        path = tuple(field.split("."))
        value = c.get("value")
        numeric = False

        if op in ("in", "nin"):
            # String CSV é REJEITADA de propósito: aceitá-la faria
            # ``"a,b"`` virar uma lista de 3 caracteres em silêncio.
            if not isinstance(value, list):
                return None, "bad_json"
            try:
                value = frozenset(value)
            except TypeError:
                value = tuple(value)  # elementos não-hashable
        elif op == "exists":
            value = bool(value)
            paths_with_exists.add(path)
        elif op in NUMERIC_OPS:
            coerced = _as_float(value)
            if coerced is None:
                # Operador numérico com alvo não-numérico nunca casaria nada.
                return None, "bad_json"
            value, numeric = coerced, True

        if op in NEGATIVE_OPS:
            negative_paths.add(path)

        compiled.append(CompiledClause(path=path, op=op, value=value, numeric=numeric))

    # Fail-open de allowlist, fechado por construção: ``nin``/``ne`` casam por
    # vacuidade em campo ausente, então um evento cujo ``raw.user`` sumiu (path
    # atravessa lista, ou o raw foi trimado) passaria pelo filtro que deveria
    # excluí-lo — disparando exatamente sobre o que o operador quis calar.
    # Exigir que o campo EXISTA torna o operador fail-closed sem mudar sua
    # semântica nem obrigar o operador a conhecer o idioma.
    for path in sorted(negative_paths - paths_with_exists):
        compiled.append(CompiledClause(path=path, op="exists", value=True))
        logger.debug(
            "inflight: regra %s — cláusula exists auto-injetada para %s "
            "(fecha o fail-open de allowlist em campo ausente)",
            getattr(row, "id", "?"), ".".join(path),
        )

    group_by = getattr(row, "group_by_field", None)
    if group_by:
        # O primeiro segmento decide TUDO: ``_resolve`` parte da raiz do
        # envelope, que tem exatamente as chaves de ``ENVELOPE_ROOTS``. Um path
        # que comece em qualquer outra coisa resolve ``None`` em 100% dos
        # eventos — não é "raro", é impossível de acertar.
        #
        # Isto vale SÓ para o modo em voo. Em lote o motor roda sobre resultados
        # de busca federada (``correlation_engine.extract_path``), onde um
        # ``source.ip`` pode ser perfeitamente válido — e este ``compile_rule``
        # nunca vê regra em lote.
        #
        # Custa uma comparação de string por regra por CICLO, não por evento
        # (R1 do ADR-0015: zero I/O novo no caminho do evento).
        raiz = str(group_by).split(".", 1)[0]
        if raiz not in ENVELOPE_ROOTS:
            return None, "group_by_root"
    return (
        CompiledInflightRule(
            rule_id=int(row.id),
            name=str(row.name),
            severity_id=int(getattr(row, "severity_id", 4) or 4),
            # Checagem explícita de None, NUNCA ``or``: ``0`` é um valor
            # LEGÍTIMO (supressão desligada) e ``or 3600`` o engoliria em
            # silêncio, dando ao operador uma janela de 1h que ele não pediu.
            # É a mesma classe do bug ``or 7`` do TTL de dedupe corrigido nesta
            # mesma branch — e eu o reintroduzi aqui.
            suppression_window_seconds=int(
                _sup if (_sup := getattr(row, "suppression_window_seconds", None)) is not None
                else 3600
            ),
            group_by_path=tuple(str(group_by).split(".")) if group_by else None,
            clauses=tuple(compiled),
        ),
        None,
    )


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


#: Quantas regras cortadas o aviso de truncamento NOMEIA. Teto de LOG, não de
#: política: nada é decidido por ele — a decisão de quem roda é a cláusula de
#: ordenação do repositório. Existe porque a população cortada pode chegar a
#: ``CORRELATION_MAX_RULES_PER_ORG - INFLIGHT_MAX_RULES_PER_CYCLE`` (150 nos
#: defaults) e uma linha de log com 150 ids não é lida por ninguém; 10 é o que
#: cabe num alerta e já responde "cadê a regra que acabei de escrever?".
#: O total exato continua no próprio texto e na métrica ``truncated`` — o corte
#: aqui nunca esconde QUANTAS, só QUAIS além das 10 primeiras.
CUT_RULES_NAMED_IN_LOG = 10


def _describe_cut_rules(repo: Any, organization_id: int, cap: int) -> str:
    """Trecho de log nomeando as regras que ficaram FORA do teto.

    Lê pelo ``list_inflight_cut_for_org``, que aplica a MESMA ordenação da
    consulta que escolheu as sobreviventes — derivar a lista aqui ("são as de
    maior id") reintroduziria a divergência que ``eval_priority`` acabou de
    fechar, e o aviso passaria a acusar regras que estão rodando.

    À prova de falha por construção: roda DENTRO do ``try`` que protege a carga,
    cujo ``except`` devolve ruleset VAZIO e desliga a avaliação do ciclo
    inteiro. Um diagnóstico que derrubasse a detecção seria uma troca terrível,
    então qualquer erro daqui vira texto degradado — o total de cortadas e a
    métrica ``truncated`` já saíram no chamador e não dependem desta função.

    Ids e nomes vão para o LOG, nunca para label de métrica: ``id`` é global e
    ``name`` é texto do cliente — os dois explodiriam a cardinalidade da série,
    recusa já escrita em ``ERROR_REASONS``.
    """
    try:
        cut = repo.list_inflight_cut_for_org(
            organization_id,
            limit=cap,
            max_rows=int(settings.CORRELATION_MAX_RULES_PER_ORG),
        )
    except Exception:  # noqa: BLE001 — diagnóstico nunca derruba a carga
        logger.debug(
            "inflight: falha listando as regras cortadas (org %s)",
            organization_id, exc_info=True,
        )
        return "não foi possível listar quais ficaram de fora"
    if not cut:
        # Chegar aqui com o chamador tendo visto ``total > cap`` significa que
        # as duas consultas discordam (linhas mudaram entre elas, ou a ordem
        # deixou de ser complementar). Dizer "nenhuma" seria contradizer o
        # número que a mesma linha de log acabou de imprimir.
        return "a lista das cortadas veio vazia, o que contradiz o total acima"
    nomeadas = cut[:CUT_RULES_NAMED_IN_LOG]
    trecho = ", ".join(
        f"{getattr(r, 'id', '?')} ({getattr(r, 'name', '?')})" for r in nomeadas
    )
    restantes = len(cut) - len(nomeadas)
    if restantes > 0:
        trecho += f" e mais {restantes}"
    return f"fora do teto: {trecho}"


def load_inflight_rules_for_org(
    organization_id: Optional[int],
) -> CompiledRuleSet:
    """Regras habilitadas em modo ``inflight`` da org, compiladas. SÍNCRONA.

    Chamada via ``asyncio.to_thread`` 1x por ciclo. Fail-safe para ``()`` em
    qualquer erro: um problema de DB não pode impedir a COLETA, que é o produto.
    ``organization_id is None`` ⇒ ``()`` (fail-CLOSED — nunca avaliar regra sem
    saber de quem é o evento).
    """
    if organization_id is None:
        return CompiledRuleSet(rules=(), share_paths=False)

    from ...db import database, repository
    from ..metrics import INFLIGHT_RULES_LOADED, INFLIGHT_RULES_REJECTED

    cap = int(settings.INFLIGHT_MAX_RULES_PER_CYCLE)
    try:
        with database.SessionLocal() as db:
            repo = repository.CorrelationRuleRepository(db)
            if cap <= 0:
                # Kill-switch de ambiente. COUNT antes de sair, para que o
                # diagnóstico não vire "não há regras" quando na verdade o
                # operador desligou a feature.
                total = repo.count_enabled_for_org(organization_id)
                if total:
                    logger.warning(
                        "inflight: %d regras habilitadas na org %s, mas "
                        "INFLIGHT_MAX_RULES_PER_CYCLE=0 desliga a avaliação",
                        total, organization_id,
                    )
                # O gauge SAI aqui também. Kill-switch ligado é detector
                # parado, e um gauge congelado no último valor conhecido diria
                # "12 regras carregadas" com nada avaliando — a mesma mentira
                # do caminho de banco fora, logo abaixo. Sem contador de falha:
                # desligar é ato deliberado do operador, não degradação.
                INFLIGHT_RULES_LOADED.labels(org_id=str(organization_id)).set(0)
                return CompiledRuleSet(rules=(), share_paths=False)
            rows = repo.list_inflight_for_org(organization_id, limit=cap)
            if len(rows) >= cap:
                # TRUNCAMENTO — o pior modo de falha desta feature enquanto era
                # SILENCIOSO, e ainda o mais caro depois de audível.
                #
                # ``CORRELATION_MAX_RULES_PER_ORG`` (200) governa a CRIAÇÃO;
                # ``INFLIGHT_MAX_RULES_PER_CYCLE`` (50) governa a AVALIAÇÃO. Um
                # cliente pode criar 200 regras em voo e apenas 50 rodam.
                #
                # QUAIS 50 é decisão do operador desde ``eval_priority``: a
                # ordem é ``eval_priority DESC, id ASC``, então quem não mexeu
                # em nada continua com o corte por ``id ASC`` de antes (todas
                # empatadas em 0) e quem precisa fixar a regra recém-escrita
                # sobe a prioridade dela. Por isso o aviso NÃO diz mais "são as
                # mais recentes" — diria mentira na primeira org que usasse o
                # campo. Ele NOMEIA as cortadas, lidas com a MESMA cláusula de
                # ordenação que escolheu as sobreviventes.
                total = repo.count_inflight_for_org(organization_id)
                if total > cap:
                    INFLIGHT_RULES_REJECTED.labels(reason="truncated").inc(total - cap)
                    logger.warning(
                        "inflight: org %s tem %d regras em voo e o teto por "
                        "ciclo é %d (INFLIGHT_MAX_RULES_PER_CYCLE) — %d regra(s) "
                        "NÃO estão sendo avaliadas. A ordem de sobrevivência é "
                        "eval_priority DESC, id ASC; %s. Suba eval_priority das "
                        "regras que precisam rodar, desabilite as demais ou "
                        "eleve o teto.",
                        organization_id, total, cap, total - cap,
                        _describe_cut_rules(repo, organization_id, cap),
                    )
            if not rows:
                # Diagnóstico do caso mais comum de suporte: o operador criou a
                # regra e ela ficou em modo batch.
                total = repo.count_enabled_for_org(organization_id)
                if total:
                    logger.info(
                        "inflight: org %s tem %d regra(s) habilitada(s), 0 em "
                        "modo inflight (eval_mode='batch')",
                        organization_id, total,
                    )
                INFLIGHT_RULES_LOADED.labels(org_id=str(organization_id)).set(0)
                return CompiledRuleSet(rules=(), share_paths=False)
    except Exception:  # noqa: BLE001 — coleta nunca cai por causa do detector
        # DETECTOR MORTO ≠ ORG SEM REGRA — e até aqui o produto não sabia
        # dizer a diferença. O ruleset vazio devolvido logo abaixo desliga a
        # avaliação do ciclo INTEIRO; sem as duas linhas abaixo o gauge
        # continuaria marcando o último valor conhecido (o OTLP não expira
        # série, ninguém reescreve o ponto) e o painel mostraria "12 regras
        # carregadas" com o detector parado. É exatamente a forma do watermark
        # que reportava ``healthy`` com 15h de atraso (incidente jul/2026):
        # a degradação não mente, ela simplesmente não fala.
        #
        # O zero SOZINHO também não resolveria — ele é indistinguível de "esta
        # org não tem regra em voo", que é o caso mais comum e é saudável. O
        # que separa os dois é o PAR: gauge em 0 E
        # ``rules_rejected{reason="load_failed"}`` subindo.
        #
        # Guardado no próprio try porque esta é a rede de segurança da coleta:
        # a métrica do fracasso não pode virar o fracasso.
        try:
            INFLIGHT_RULES_LOADED.labels(org_id=str(organization_id)).set(0)
            INFLIGHT_RULES_REJECTED.labels(reason="load_failed").inc()
        except Exception:  # noqa: BLE001
            logger.debug(
                "inflight: falha emitindo o sinal de detector morto", exc_info=True
            )
        logger.exception("inflight: falha carregando regras (org %s)", organization_id)
        return CompiledRuleSet(rules=(), share_paths=False)

    compiled: list[CompiledInflightRule] = []
    for row in rows:
        rule, reason = compile_rule(row)
        if rule is None:
            INFLIGHT_RULES_REJECTED.labels(reason=reason).inc()
            logger.warning(
                "inflight: regra %s (%s) rejeitada na compilação: %s",
                getattr(row, "id", "?"), getattr(row, "name", "?"), reason,
            )
            continue
        compiled.append(rule)

    INFLIGHT_RULES_LOADED.labels(org_id=str(organization_id)).set(len(compiled))

    # Decide UMA VEZ se vale cachear resolução de path no laço por evento. Ver
    # ``CompiledRuleSet.share_paths``: o cache incondicional PIORA 1,21x quando
    # todo path é único, então a decisão tem de sair do caminho quente.
    seen: set[tuple[str, ...]] = set()
    share = False
    for rule in compiled:
        for clause in rule.clauses:
            if clause.path in seen:
                share = True
                break
            seen.add(clause.path)
        if share:
            break

    return CompiledRuleSet(rules=tuple(compiled), share_paths=share)


def _group_value_digest(value: str) -> str:
    """Digest curto e determinístico do valor COMPLETO de group_by.

    Sem sal e sem estado de propósito: a MESMA entidade tem de produzir a mesma
    ``dedup_key`` em qualquer worker e em qualquer ciclo, ou a supressão deixa
    de suprimir e o operador recebe o mesmo alerta a cada ciclo. ``blake2s`` e
    não sha256 porque o requisito aqui é injetividade prática, não resistência
    a adversário — e é mais barato no ramo em que roda.

    ``surrogatepass``: o valor vem do evento e pode carregar surrogate solto
    vindo de JSON malformado; ``encode`` estrito levantaria dentro de ``add``,
    que roda no laço de coleta.
    """
    return hashlib.blake2s(
        value.encode("utf-8", "surrogatepass"), digest_size=GROUP_VALUE_DIGEST_BYTES
    ).hexdigest()


def _evidence_text(value: Any) -> Optional[str]:
    """Texto que vai SAIR do produto, truncado. ``None`` continua ``None``.

    Devolver ``None`` em vez de ``""`` (que é o que ``preview._truncate`` faz,
    porque lá o destino é uma tabela na tela) é o que mantém honesto o evento
    entregue ao SIEM: ausente e vazio são estados diferentes, e um campo que o
    evento de origem não tinha não pode chegar ao analista como string vazia —
    ele leria "existe e está em branco".
    """
    if value is None:
        return None
    text = str(value)
    if len(text) <= DETECTION_EVENT_TEXT_MAXLEN:
        return text
    return text[:DETECTION_EVENT_TEXT_MAXLEN] + "…"


def _is_self_emitted(envelope: Mapping[str, Any]) -> bool:
    """O evento que casou JÁ ERA um evento de detecção emitido por este produto?

    GUARD DE LAÇO — a conclusão da investigação, escrita aqui porque é onde ela
    é acionada:

    Pelo desenho atual a cascata NÃO é alcançável por dentro. ``_enqueue_dispatch``
    é SAÍDA e só saída: ele chama ``_enqueue_routed`` → ``route_batch`` →
    ``dispatch_to_destination`` (ou o tópico Kafka que o dispatcher consome), e
    nenhum desses caminhos volta a ``run_collection_once``, que é o ÚNICO lugar
    onde o matcher em voo roda. Emitir daqui não realimenta a ingestão.

    O que é alcançável é a reentrada pela PORTA DA FRENTE, e ela não é
    hipotética neste repo: ``POST /api/ingest/{stream}`` empurra para o buffer
    Redis que o ``PushBufferCollector`` drena DENTRO do ``run_collection_once``
    normal — "reaproveitando 100% do pipeline existente", nas palavras do
    próprio módulo. Um destino apontado para o ``/api/ingest`` da própria
    instalação (ou para um manager Wazuh que também é FONTE coletada — o laço
    que ``_load_wazuh_loop_destination_ids`` já existe para cortar) devolve o
    evento emitido à ingestão, onde ele é indistinguível de um evento de vendor.
    A partir daí uma regra larga o casa, gera Detection, que emite outro evento.

    Por isso a marca é carregada no próprio evento e checada em DOIS níveis:
    ``_centralops.event_type`` (reentrada que preserve o envelope) e
    ``raw._centralops.event_type`` (reentrada em que o envelope inteiro vira o
    ``raw`` do evento novo, que é o formato do push-ingest). O guard corta a
    cascata na profundidade 1: a Detection continua sendo GRAVADA — o detector é
    observador, nunca porteiro (R3) —, só o evento não sai de novo.

    O que este guard NÃO cobre, dito por escrito para ninguém o superestimar: um
    destino que entregue SÓ o bloco ``normalized`` (Chronicle, Security Lake,
    webhook em modo OCSF) e cujo consumidor reingira aquilo por outra rota perde
    o ``_centralops`` e leva junto a marca. Sobra a marca dentro de
    ``normalized.unmapped.centralops_detection``, que é o que a checagem de
    ``raw`` alcança quando o payload volta como raw; fora disso, o corte tem de
    ser feito na rota (não emitir para um destino que reingere).
    """
    if not isinstance(envelope, Mapping):
        return False
    meta = envelope.get("_centralops")
    if isinstance(meta, Mapping) and meta.get("event_type") == DETECTION_EVENT_TYPE:
        return True
    raw = envelope.get("raw")
    if isinstance(raw, Mapping):
        inner = raw.get("_centralops")
        if isinstance(inner, Mapping) and inner.get("event_type") == DETECTION_EVENT_TYPE:
            return True
        unmapped = raw.get("unmapped")
        if isinstance(unmapped, Mapping) and unmapped.get("centralops_detection"):
            return True
    return False


def _event_source_pointer(
    envelope: Mapping[str, Any],
    group_path: Optional[tuple[str, ...]],
    group_value: Optional[str],
) -> dict[str, Any]:
    """PONTEIRO para o evento que casou — deliberadamente NÃO a evidência dele.

    A DECISÃO, e o que ela custa, porque um alerta que chega vazio reprova no
    primeiro clique do analista:

    O que NÃO vai junto é o envelope do evento (``raw`` + ``normalized``), e por
    três razões independentes, cada uma suficiente sozinha:

    1. PII. O envelope carrega o payload do cliente inteiro. O evento de
       detecção é roteado por conta própria, e uma rota que case ``class_uid
       2004`` pode entregá-lo a um destino para onde o evento de origem NUNCA
       iria — anexar o payload transformaria a emissão de alerta num canal de
       exfiltração criado por configuração de rota, sem que ninguém tivesse
       decidido isso. O truncamento aqui segue a MESMA disciplina do preview
       (``_OBSERVED_MAXLEN``), que é o precedente do repo para dado de cliente
       que sai do produto.
    2. TAMANHO. O ``raw`` não tem teto no CentralOps; o teto real é do destino
       (``OS_MAXSTR`` = 65536, acima do qual o ``analysisd`` do Wazuh TRUNCA em
       silêncio). Um evento de detecção que estoura vira alerta cortado ao meio,
       que é pior que alerta sem evidência: parece completo.
    3. CUSTO. Duplicar o evento dobra os bytes que o cliente paga, na exata
       feature que este produto vende como redução de custo.

    O QUE FALTA, dito por escrito: o analista recebe o suficiente para PIVOTAR
    (``event_id``, integração, plataforma, stream, instante e a entidade do
    ``group_by``) e NÃO recebe o evento. Se ele já não tiver o evento de origem
    no SIEM — porque a rota daquele stream não entrega lá, ou porque uma redução
    apagou o campo —, o ponteiro aponta para o nada. Fechar isso exige um campo
    de evidência com política própria (quais campos, por regra, com allowlist),
    o que é a fase seguinte; entregar o envelope inteiro agora não seria a fase
    seguinte, seria as três razões acima de uma vez.

    Custo em CPU: ~10 ``dict.get`` UMA VEZ POR CHAVE NOVA do ciclo (nunca por
    evento — o call site já retornou antes quando a chave repete), logo limitado
    por ``INFLIGHT_MAX_DEDUP_KEYS_PER_RULE_PER_CYCLE``, não pela taxa de
    eventos. R1 intacto. Incondicional (não olha a flag de emissão) de
    propósito: um ponteiro construído só quando a flag está ligada faria um
    ``flush`` que começa com a flag desligada e termina com ela ligada produzir
    tickets pela metade.
    """
    meta = envelope.get("_centralops") if isinstance(envelope, Mapping) else None
    meta = meta if isinstance(meta, Mapping) else {}
    norm = envelope.get("normalized") if isinstance(envelope, Mapping) else None
    norm = norm if isinstance(norm, Mapping) else {}
    event_time = norm.get("time")
    class_uid = norm.get("class_uid")
    return {
        "event_id": _evidence_text(meta.get("event_id")),
        "vendor": _evidence_text(meta.get("vendor")),
        "platform": _evidence_text(meta.get("platform")),
        "stream": _evidence_text(meta.get("stream")),
        "event_type": _evidence_text(meta.get("event_type")),
        # Sem ``bool`` no isinstance: ``True`` é ``int`` em Python e viraria
        # ``time: 1`` — epoch 1ms de 1970, um instante plausível o bastante para
        # ninguém desconfiar dele na tela do analista.
        "event_time": (
            event_time
            if isinstance(event_time, (int, float)) and not isinstance(event_time, bool)
            else None
        ),
        "event_class_uid": (
            class_uid if isinstance(class_uid, int) and not isinstance(class_uid, bool)
            else None
        ),
        # Identidade do tenant COPIADA do evento de origem em vez de relida do
        # banco: é o mesmo tenant por construção (a regra é carregada por org) e
        # uma consulta aqui seria I/O novo num caminho que existe para não ter.
        "customer_name": _evidence_text(meta.get("customer_name")),
        "organization_slug": _evidence_text(meta.get("organization_slug")),
        "data_geography": _evidence_text(meta.get("data_geography")),
        # ``_evidence_text`` também aqui, e não é simetria decorativa: este é o
        # ÚNICO campo textual do evento cuja origem não é o evento, e sim a
        # CONFIGURAÇÃO — ``CorrelationRule.group_by_field`` é ``Column(String)``
        # sem teto, e ``compile_rule`` não valida comprimento. Um dot-path
        # absurdo escrito por engano na regra aparece três vezes no evento
        # (aqui, na ``message`` e em ``finding_info.desc``) e sozinho estoura
        # ``DETECTION_EVENT_MAX_BYTES`` — medido: 4.000 chars levam o evento a
        # ~13 KiB contra um teto de 12 KiB.
        #
        # Não é exfiltração (é texto do operador, não do evento), mas falsifica
        # a invariante que este módulo declara — "o tamanho é limitado POR
        # CONSTRUÇÃO" — e teto que depende de ninguém digitar demais não é teto.
        "group_field": _evidence_text(".".join(group_path)) if group_path else None,
        "group_value": _evidence_text(group_value),
        "self_emitted": _is_self_emitted(envelope),
    }


class InflightAccumulator:
    """Matches do ciclo, em memória. Nada aqui toca I/O."""

    __slots__ = ("pending", "matches", "errors", "overflow", "_keys_per_rule", "_logged_once")

    def __init__(self) -> None:
        #: dedup_key → payload da Detection a criar
        self.pending: dict[str, dict[str, Any]] = {}
        #: rule_id → nº de eventos casados (pode ser >> len(pending))
        self.matches: dict[int, int] = {}
        #: razão → rule_id → contagem. ANINHADO: o total por razão continua
        #: sendo o que vai para o OTel (cardinalidade intacta), e o breakdown
        #: por regra — que responde "QUAL regra parou de alertar?" — vai para o
        #: observability_store. Ver ``count_error``.
        self.errors: dict[str, dict[int, int]] = {}
        #: rule_id → matches perdidos por teto de chaves
        self.overflow: dict[int, int] = {}
        #: rule_id → nº de chaves distintas já criadas. Contador dedicado, e não
        #: uma varredura de ``pending`` por match: varrer seria O(nº de chaves)
        #: POR EVENTO CASADO, um custo que cresce ao longo do ciclo dentro do
        #: laço de coleta — exatamente o que R1 existe para impedir.
        self._keys_per_rule: dict[int, int] = {}
        #: (razão, rule_id) já avisado neste ciclo. Chaveado pela DUPLA e não só
        #: pelo rule_id: uma regra pode estourar o teto de chaves E truncar o
        #: valor de group_by no mesmo ciclo, e calar o segundo aviso porque o
        #: primeiro saiu esconderia justamente a causa que o operador procura.
        #: ``rule_id=None`` é a razão NÃO atribuível a uma regra (falha do
        #: observability_store, que é um só para todas) — ali a dupla degenera
        #: em "1 aviso por razão por ciclo", que é exatamente o rate certo.
        self._logged_once: set[tuple[str, Optional[int]]] = set()

    def count_error(self, reason: str, rule_id: int, amount: int = 1) -> None:
        """Contabiliza ``amount`` erros de ``reason`` ATRIBUÍDOS a uma regra.

        ``reason`` deve pertencer a ``ERROR_REASONS`` — é label de métrica. O
        ``rule_id`` NÃO vira label (ver o comentário do enum); ele só desce até
        o ``observability_store`` no flush.
        """
        by_rule = self.errors.setdefault(reason, {})
        by_rule[rule_id] = by_rule.get(rule_id, 0) + amount

    def _warn_once(
        self, reason: str, rule_id: Optional[int], message: str, *args: Any
    ) -> None:
        """Rate-limit de log por (razão, regra) por CICLO — sem isso um evento
        ruim repetido troca degradação de detecção por amplificação de escrita
        no log, que é o dano maior.

        ``rule_id=None`` para a razão que NÃO é atribuível a uma regra: N regras
        falhando no mesmo ciclo por causa do mesmo Redis fora são N sintomas de
        UMA causa, e um aviso por regra ali seria a amplificação que esta função
        existe para impedir."""
        token = (reason, rule_id)
        if token in self._logged_once:
            return
        self._logged_once.add(token)
        logger.warning(message, *args)

    def add(
        self,
        rule: CompiledInflightRule,
        envelope: Mapping[str, Any],
        organization_id: int,
        integration_id: Optional[int] = None,
    ) -> None:
        from .matcher import _resolve

        self.matches[rule.rule_id] = self.matches.get(rule.rule_id, 0) + 1

        group_value: Optional[str] = None
        if rule.group_by_path is None:
            token = "*"
        else:
            raw = _resolve(envelope, rule.group_by_path)
            if raw is None:
                # Agrupar os não-resolvidos numa Detection genérica esconderia
                # "regra apontando para campo errado" dentro de um alerta que
                # parece legítimo. Vira erro contado, não alerta.
                self.count_error("group_by_unresolved", rule.rule_id)
                return
            value = str(raw)
            group_value = value
            cap = int(settings.INFLIGHT_MAX_GROUP_VALUE_LEN)
            token = value[:cap]
            if len(value) > cap:
                # FUSÃO SILENCIOSA DE ENTIDADES. Este token vai inteiro para a
                # dedup_key, que é PERSISTIDA em ``Detection.dedup_key`` e
                # governa a supressão: dois valores de group_by distintos que
                # compartilhassem o prefixo cortado viravam UMA única Detection,
                # sem erro nenhum — a segunda entidade some dentro do alerta da
                # primeira.
                #
                # Não é hipótese, foi MEDIDO em dado real de produção: com
                # group_by em ``rawData.cmdline``, 83,2% dos valores passam do
                # teto e 2 colisões reais foram observadas; em
                # ``rawData.parent_cmdline``, 6,3% passam e 9 colisões reais.
                #
                # O digest do valor COMPLETO restaura a injetividade; o prefixo
                # segue legível para quem investiga. Só neste ramo raro — o
                # caminho comum (valor abaixo do teto) continua sendo uma fatia
                # de string e produz a chave byte-idêntica à de antes. O digest
                # NUNCA vira label de métrica.
                token = f"{token}{GROUP_VALUE_DIGEST_SEP}{_group_value_digest(value)}"
                self.count_error("group_value_truncated", rule.rule_id)
                self._warn_once(
                    "group_value_truncated", rule.rule_id,
                    "inflight: regra %s (%s) — valor de group_by com %d chars "
                    "excede o teto de %d; a dedup_key passa a levar sufixo de "
                    "digest para não fundir entidades distintas numa Detection. "
                    "Sai só o COMPRIMENTO: o valor vem do evento e não entra em "
                    "log. group_by de alta cardinalidade textual (cmdline) é o "
                    "caso típico.",
                    rule.rule_id, rule.name, len(value), cap,
                )

        key = f"inflight:{organization_id}:{rule.rule_id}:{token}"
        if key in self.pending:
            return

        # O teto é sobre CHAVES DISTINTAS, não sobre matches: a variável
        # perigosa é a cardinalidade do group_by, não a taxa de acerto. Uma
        # regra que casa 100% dos eventos com group_by=None gera UMA chave.
        key_cap = int(settings.INFLIGHT_MAX_DEDUP_KEYS_PER_RULE_PER_CYCLE)
        if self._keys_per_rule.get(rule.rule_id, 0) >= key_cap:
            self.overflow[rule.rule_id] = self.overflow.get(rule.rule_id, 0) + 1
            self.count_error("key_cap", rule.rule_id)
            self._warn_once(
                "key_cap", rule.rule_id,
                "inflight: regra %s (%s) atingiu o teto de %d chaves de dedup "
                "no ciclo — matches seguem contados, nenhuma Detection nova é "
                "criada, nenhum evento é descartado. Teto atingido costuma "
                "indicar group_by_field de alta cardinalidade.",
                rule.rule_id, rule.name, key_cap,
            )
            return

        self._keys_per_rule[rule.rule_id] = self._keys_per_rule.get(rule.rule_id, 0) + 1
        self.pending[key] = {
            "rule": rule,
            "integration_id": integration_id,
            # Capturado AQUI e não no flush porque aqui é o único ponto em que o
            # envelope do evento que casou ainda está à mão — o flush roda em
            # thread, depois do laço de coleta, e só enxerga ``pending``. Ver
            # ``_event_source_pointer`` para o que entra, o que NÃO entra e por
            # quê; e note que isto roda 1x por chave nova, nunca por evento.
            "source": _event_source_pointer(envelope, rule.group_by_path, group_value),
        }


class InflightFlushInterrupted(Exception):
    """Falha no MEIO da escrita das Detections, carregando o que já foi gravado.

    ``DetectionRepository.record`` faz ``commit`` POR CHAVE: quando a escrita da
    n-ésima Detection estoura, as n-1 anteriores já estão DURÁVEIS no banco e
    não são perda. Contá-las mesmo assim inflava ``flush_lost``, que é
    justamente a série que responde "quanto de detecção o cliente deixou de
    receber" — um número inflado ali manda investigar prejuízo que não houve, e
    corrói a confiança no único contador que mede o dano real.

    O prejuízo viaja NESTA exceção porque o ``written`` que ``_flush_sync``
    devolve não sobrevive ao ``raise``: ``asyncio.to_thread`` propaga o objeto
    de exceção e descarta o valor de retorno. A alternativa — um out-param —
    mudaria a assinatura de ``_flush_sync``, que é ponto de monkeypatch em
    testes deste repo e do EE; um duplo com a assinatura antiga passaria a
    estourar ``TypeError`` dentro do ``finally`` do ciclo de coleta.
    """

    def __init__(
        self,
        written_keys: tuple[str, ...],
        emits: "tuple[DetectionEmit, ...]" = (),
    ) -> None:
        super().__init__(
            f"flush interrompido após {len(written_keys)} Detection(s) gravada(s)"
        )
        #: ``dedup_key`` das Detections que JÁ foram commitadas antes da falha.
        self.written_keys = written_keys
        #: Tickets de emissão das Detections NOVAS entre as já commitadas. Viaja
        #: aqui pelo MESMO motivo de ``written_keys``: o ``return`` não sobrevive
        #: ao ``raise`` e ``asyncio.to_thread`` descarta o valor de retorno.
        #: Sem isto, uma falha na n-ésima escrita calaria o evento das n-1
        #: Detections que estão DURÁVEIS no banco — perda de alerta silenciosa
        #: no exato ciclo em que algo já deu errado. Default ``()`` para que
        #: qualquer construção existente (inclusive em teste) siga válida.
        self.emits = emits


@dataclass(frozen=True)
class DetectionEmit:
    """Ticket de emissão de UMA Detection recém-CRIADA.

    Existe porque a emissão do evento OCSF acontece fora da thread que escreveu
    a Detection, e tudo que ela precisa saber (identidade da regra, chave, e o
    ponteiro capturado lá atrás no ``add``) morreria com o ``pending`` se não
    fosse carregado explicitamente.

    ``frozen``: o ticket atravessa uma fronteira de thread. Um dict mutável ali
    convidaria alguém a "só completar um campo" no lado de cá, e o campo
    completado não teria nenhuma relação com a linha que foi gravada.
    """

    dedup_key: str
    detection_id: Optional[int]
    rule_id: int
    rule_name: str
    severity_id: int
    integration_id: Optional[int]
    source: Mapping[str, Any]


def _apply_flush_cap(acc: InflightAccumulator) -> int:
    """Corta ``acc.pending`` no teto GLOBAL de Detections por flush. Devolve o
    nº de chaves descartadas.

    POR QUE EXISTE. ``INFLIGHT_MAX_RULES_PER_CYCLE`` e
    ``INFLIGHT_MAX_DEDUP_KEYS_PER_RULE_PER_CYCLE`` limitam POR REGRA e não somam
    a nada: 50 × 50 = 2500 chaves num único flush, e ``record`` COMMITA POR
    CHAVE — em Postgres 5 round-trips por chave (advisory lock, SELECT da
    janela, INSERT/UPDATE, COMMIT, refresh), até 12.500 em SÉRIE dentro do
    ``finally`` do ciclo de coleta. Não é o hot path por evento, e por isso
    passou despercebido; é trabalho serial que não coleta nada, e que numa org
    com group_by de alta cardinalidade cresce sozinho.

    DEGRADAÇÃO DECLARADA — o excedente NÃO vira Detection, e isso é perda de
    detecção, não de performance. Ela é aceita, e não escondida: cada chave
    descartada é contada em ``flush_cap`` ATRIBUÍDA à regra (logo desce ao
    ``observability_store`` como ``err_flush_cap`` e aparece na UI), sai um
    WARNING 1x por ciclo, e ``acc.matches`` fica INTACTO — a regra não parece
    morta, ela parece o que é: casando mais do que cabe gravar.

    Por que não gravar tudo: o excedente não tem para onde ser adiado. O
    acumulador morre com o ciclo, os eventos do cliente já foram despachados
    muito antes disto rodar, e as claims de dedupe fazem o retry descartá-los —
    um carry-over exigiria estado durável, isto é, exatamente as escritas que
    este teto limita. Sem teto, uma org troca a COLETA de todos os tenants
    daquele worker por detecções que ela mesma não vai ler.
    """
    cap = max(0, int(settings.INFLIGHT_MAX_DETECTIONS_PER_FLUSH))
    total = len(acc.pending)
    if total <= cap:
        # Caminho de todo mundo: um ``len()`` por ciclo. Nenhuma cópia, nenhuma
        # varredura, nenhum objeto novo — o custo desta feature na instalação
        # que nunca chega perto do teto é esta comparação.
        return 0

    # ROUND-ROBIN entre as regras, e não "as ``cap`` primeiras de ``pending``".
    # ``pending`` é ordenado por INSERÇÃO, então cortar a cauda entregaria o
    # orçamento inteiro à regra que casou primeiro: uma regra ruidosa recém
    # publicada calaria TODAS as outras — inclusive as de volume baixo e
    # severidade alta, que são justamente as que o operador não pode perder. É
    # a mesma armadilha já escrita para o truncamento de REGRAS na carga ("as
    # descartadas são sempre as mais recentes"), com o eixo trocado.
    #
    # Roda 1x por ciclo, SÓ quando o teto morde, e é O(total) com ``total``
    # limitado pelo teto estrutural (2500).
    por_regra: dict[int, list[str]] = {}
    for chave, item in acc.pending.items():
        por_regra.setdefault(int(item["rule"].rule_id), []).append(chave)

    mantidas: set[str] = set()
    rodada = 0
    while len(mantidas) < cap:
        avancou = False
        for chaves in por_regra.values():
            if rodada >= len(chaves):
                continue
            avancou = True
            mantidas.add(chaves[rodada])
            if len(mantidas) >= cap:
                break
        if not avancou:  # pragma: no cover — total > cap garante rodadas
            break
        rodada += 1

    descartadas: dict[int, int] = {}
    for chave in list(acc.pending):
        if chave in mantidas:
            continue
        descartadas[int(acc.pending[chave]["rule"].rule_id)] = (
            descartadas.get(int(acc.pending[chave]["rule"].rule_id), 0) + 1
        )
        del acc.pending[chave]

    for rule_id, quantas in descartadas.items():
        acc.count_error("flush_cap", rule_id, quantas)

    perdidas = total - len(acc.pending)
    # ``rule_id=None``: N regras cortadas no mesmo ciclo são N sintomas de UMA
    # causa (o orçamento do flush), e um aviso por regra seria a amplificação de
    # log que ``_warn_once`` existe para impedir. Só INTEIROS nos args — nome de
    # regra e valor de group_by vêm do evento e não entram neste log.
    acc._warn_once(
        "flush_cap", None,
        "inflight: teto global de %d Detection(s) por flush atingido — %d de %d "
        "chave(s) pendentes NÃO viraram Detection neste ciclo, em %d regra(s). "
        "Os matches seguem contados e nenhum evento do cliente foi tocado: o "
        "que se perdeu foi DETECÇÃO. O orçamento é dividido em round-robin "
        "entre as regras, então nenhuma regra sozinha cala as outras. Veja "
        "err_flush_cap por regra para achar a de alta cardinalidade, ou eleve "
        "INFLIGHT_MAX_DETECTIONS_PER_FLUSH sabendo que cada chave custa 5 "
        "round-trips de Postgres em série no fim do ciclo de coleta.",
        cap, perdidas, total, len(descartadas),
    )
    return perdidas


def _flush_sync(
    pending: dict[str, dict[str, Any]], organization_id: int
) -> tuple[DetectionEmit, ...]:
    """Escreve as Detections. SÍNCRONA, roda em thread.

    Devolve UM ticket por Detection NOVA (ver ``DetectionEmit``) — antes devolvia
    a CONTAGEM de gravadas, que nenhum call site lia. As duas grandezas não são
    a mesma: um match que cai dentro da janela de supressão é gravado (bumpa
    ``count``/``last_seen``) e NÃO gera ticket.

    Em falha levanta ``InflightFlushInterrupted`` com as chaves já commitadas e
    os tickets já produzidos, encadeada (``from``) na exceção original — o
    ``logger.exception`` do call site continua imprimindo a causa verdadeira, e a
    contagem de perda deixa de ser um chute sobre ``pending`` inteiro.
    """
    from ...db import database, repository

    written: list[str] = []
    emits: list[DetectionEmit] = []
    try:
        with database.SessionLocal() as db:
            repo = repository.DetectionRepository(db)
            for dedup_key, item in pending.items():
                rule: CompiledInflightRule = item["rule"]
                det = repo.record(
                    organization_id=organization_id,
                    source="inflight",
                    dedup_key=dedup_key,
                    severity_id=rule.severity_id,
                    rule_id=str(rule.rule_id),
                    rule_name=rule.name,
                    integration_id=item.get("integration_id"),
                    suppression_window_seconds=rule.suppression_window_seconds,
                )
                # DEPOIS do ``record``, nunca antes: ``record`` commita, então a
                # chave só entra aqui quando a linha está durável. Registrar
                # antes traria de volta a mentira, com o sinal invertido —
                # perda real contada como gravação.
                written.append(dedup_key)
                # ``count == 1`` é o ÚNICO sinal de que ESTA chamada CRIOU a
                # linha: dentro da janela de supressão, ``record`` devolve a
                # Detection que já existia com ``count`` incrementado. Emitir
                # também no bump trocaria a janela que o operador configurou
                # (1h por default) por um evento a cada ciclo de coleta (2 min)
                # — ~30x o volume de alerta que ele pediu, no SIEM dele. A
                # supressão é o contrato anti-spam da feature; a emissão não
                # pode ser a porta dos fundos que a anula.
                #
                # O OCSF 2004 tem ``activity_id`` 2 (Update) e 3 (Close), então
                # emitir o ciclo de vida completo seria legítimo em tese. Não é
                # o que se entrega agora, e por escrito: ``Update`` a cada ciclo
                # é exatamente o volume que o parágrafo acima recusa, e ``Close``
                # não tem gatilho — nada no produto fecha uma Detection hoje.
                if getattr(det, "count", None) == 1:
                    emits.append(
                        DetectionEmit(
                            dedup_key=dedup_key,
                            detection_id=getattr(det, "id", None),
                            rule_id=rule.rule_id,
                            rule_name=rule.name,
                            severity_id=rule.severity_id,
                            integration_id=item.get("integration_id"),
                            source=item.get("source") or {},
                        )
                    )
    except Exception as exc:  # noqa: BLE001 — reembalado, não engolido
        raise InflightFlushInterrupted(tuple(written), tuple(emits)) from exc
    return tuple(emits)


def _record_rule_metric(metric: str, rule_id: int, count: int) -> bool:
    """Escreve UM contador de regra no observability_store (kind="rule").
    Devolve se a gravação foi CONFIRMADA — é esse veredito que
    ``_mirror_rule_metric`` transforma em métrica de perda.

    Best-effort e nunca deixa vazar: ``observability_store.record_counter`` já
    engole exceções internamente (é o contrato do módulo), mas esta função
    ainda envolve a chamada — inclusive o próprio ``import`` — em try/except,
    porque roda dentro do ``finally`` de ``flush_inflight`` e uma falha aqui
    JAMAIS pode mascarar a exceção original do ciclo de coleta nem derrubá-lo.

    ``is not False`` e não ``is True``: ``record_counter`` só passou a devolver
    veredito agora, e ela é ponto de monkeypatch em dezenas de testes deste repo
    e do EE, cujos duplos devolvem ``None``. Ler ``None`` como falha inverteria
    o sinal e faria a série de perda contar prejuízo que não houve — o mesmo
    erro de que ``flush_lost`` acabou de ser curado, com o sinal trocado. Sem
    veredito ⇒ nada a reportar.
    """
    try:
        from .. import observability_store as obs

        gravou = obs.record_counter(
            "rule",
            str(rule_id),
            metric,
            float(count),
            ttl_seconds=RULE_METRIC_TTL_SECONDS,
            bucket_seconds=RULE_METRIC_BUCKET_SECONDS,
        )
    except Exception:  # noqa: BLE001 — best-effort, nunca derruba o flush
        # O traceback fica em DEBUG porque sai POR CHAMADA (nº de regras ×
        # famílias de contador): promovê-lo a WARNING seria exatamente a
        # amplificação de escrita no log que o rate-limit existe para evitar. A
        # linha que o OPERADOR precisa ver sobe a WARNING uma vez por ciclo, em
        # ``_mirror_rule_metric``.
        logger.debug(
            "inflight: falha gravando '%s' no observability_store (rule %s)",
            metric, rule_id, exc_info=True,
        )
        return False
    return gravou is not False


def _mirror_rule_metric(
    acc: InflightAccumulator, metric: str, rule_id: int, count: int
) -> None:
    """Espelha um contador de regra e CONTA a escrita que se perdeu.

    DEGRADAÇÃO DECLARADA — o que o operador vê quando o Redis cai NA ESCRITA: a
    chave ``obs:rule:{rule_id}:{metric}`` nunca é criada. Depois, na leitura, o
    endpoint ENCONTRA a ausência (não um erro): ``read_window_total_strict``
    varre um hash vazio e soma ``0.0`` sem levantar nada, e a UI escreve
    "0 disparos" para uma regra que disparou. É a mentira que o ``strict`` foi
    escrito para matar, um passo antes de onde ele age.

    Por que o ``strict`` da leitura NÃO alcança, e não é questão de melhorá-lo:
    ele distingue "não sei" de "zero" quando é a LEITURA que falha. Aqui a
    leitura funciona perfeitamente — o que falta é o DADO. Ausência de chave e
    "a regra não disparou" são literalmente o mesmo estado no Redis, então
    nenhuma leitura, por mais estrita, consegue separá-los. A perda só é
    conhecível no lado que a produz, e é por isso que ela é contada aqui.

    Deliberadamente NÃO se tenta inferir buraco na série do lado da leitura: um
    heurístico ali transformaria toda regra genuinamente muda em suspeita de
    falha, e trocar um falso "0" por um falso "não sei" não é progresso. O
    objetivo é tornar a perda VISÍVEL (``increase(collector_inflight_rule_
    metric_write_failures_total) > 0`` ⇒ "o contador de disparos está
    sub-reportando agora"), não adivinhá-la.

    O contador do OTel (``INFLIGHT_MATCHES``) NÃO cobre esse buraco: ele é
    no-op quando ``OTEL_ENABLED=False``, que é o default da instalação padrão —
    justamente a razão de este espelhamento existir.
    """
    if _record_rule_metric(metric, rule_id, count):
        return
    try:
        from ..metrics import INFLIGHT_METRIC_WRITE_FAILURES

        INFLIGHT_METRIC_WRITE_FAILURES.labels(metric=metric).inc()
        # ``rule_id=None``: o aviso é 1x por CICLO e não por regra. Quem falhou
        # foi o STORE, que é um só — N regras falhando são N sintomas de UMA
        # causa. Mesma razão pela qual ``rule_id`` não é label da série.
        acc._warn_once(
            "obs_store_write_failed", None,
            "inflight: o observability_store recusou a escrita dos contadores "
            "por regra neste ciclo (1ª família a falhar: '%s') — os disparos "
            "deste ciclo NÃO entram na série que a UI lê e vão aparecer como "
            "'0 disparos' para regras que dispararam. A leitura strict não "
            "alcança: ausência de chave é indistinguível de regra muda. "
            "Ver collector_inflight_rule_metric_write_failures_total.",
            metric,
        )
    except Exception:  # noqa: BLE001
        # Best-effort dentro de best-effort, como na atribuição de
        # ``flush_lost``: quem estoura aqui é o CONTADOR do prejuízo, e ele não
        # pode agravá-lo levantando dentro do ``finally`` do ciclo de coleta.
        logger.debug(
            "inflight: falha contabilizando a perda de escrita de '%s'",
            metric, exc_info=True,
        )


def _coerce_emits(value: Any) -> "tuple[tuple[DetectionEmit, ...], bool]":
    """``(tickets, conhecidos)`` a partir do que ``_flush_sync`` devolveu.

    ``conhecidos=False`` quando a resposta NÃO tem a forma de tickets — que é o
    que um duplo antigo de ``_flush_sync`` devolve, porque até esta mudança a
    função devolvia um ``int`` e ela é ponto de monkeypatch em dezenas de testes
    deste repo e do EE.

    A distinção entre ``()`` e "não sei" é o ponto inteiro desta função, e é a
    mesma lição do ``is not False`` de ``_record_rule_metric``: ler um retorno
    legado como "nenhuma Detection nova" faria a contagem de ``suppressed``
    reportar TODO o ``pending`` como suprimido — prejuízo inventado na série que
    existe justamente para explicar alerta que não chegou.
    """
    if isinstance(value, tuple) and all(isinstance(v, DetectionEmit) for v in value):
        return value, True
    return (), False


def _build_detection_event(
    emit: DetectionEmit, organization_id: int, now_ms: int
) -> dict[str, Any]:
    """Ticket → envelope com ``normalized`` OCSF 1.8 **Detection Finding (2004)**.

    Espelha ``_dispatch_scheduled_query_alert`` (scheduler_tasks.py) porque o
    contrato é o mesmo, testado e em produção no caminho ao lado: identidade
    completa da classe (``class_uid``/``category_uid``/``activity_id``/
    ``type_uid``), ``time`` em MILISSEGUNDOS e o tenant no envelope.

    ``activity_id=1`` (Create) e não 6/"Start" como na 1006: a 2004 tem
    semântica de CICLO DE VIDA do achado (Create/Update/Close), e o que acabou
    de acontecer é a criação de um achado.

    O contexto específico do produto vai sob ``normalized.unmapped`` — é onde o
    OCSF manda pôr o que não é campo da classe, em vez de inventar campo de
    primeiro nível que nenhum consumidor sabe ler.

    Por que ``unmapped`` e NÃO ``raw``, apesar de a 2004 ter ``evidences[]``:
      * ``raw`` some. Destino com ``payload="ocsf"`` (Chronicle, Security Lake,
        webhook em modo OCSF) entrega SÓ o bloco ``normalized``; ``drop_raw`` e
        ``raw_reduction`` apagam o resto. Dado de triagem que vive no ``raw`` é
        dado que o analista não recebe — a armadilha já paga neste repo.
      * ``evidences[]`` é um array de Evidence Artifacts (process, file, url,
        …), não um saco de chaves. O que existe aqui é um PONTEIRO, não um
        artefato; enfiá-lo em ``evidences`` produziria objeto estruturalmente
        inválido que o consumidor TENTA parsear — pior que campo ausente.
    """
    from ..normalize import OCSF_VERSION
    from ..normalize.envelope import EnvelopeContext, build_envelope
    from ..normalize.ocsf.classes import (
        ACTIVITY_ID_DETECTION_FINDING,
        CATEGORY_UID_FINDINGS,
        CLASS_UID_DETECTION_FINDING,
        SEVERITY_ID,
        STATUS_ID,
        is_valid_severity_id,
    )

    src = emit.source or {}
    activity_id = ACTIVITY_ID_DETECTION_FINDING["create"]
    # ``severity_id`` vem de coluna do banco e NÃO é validado na escrita. Um
    # valor fora do enum universal derruba o evento no GATE-3 do validador
    # estrutural — o alerta sumiria por causa de um número que o operador digitou
    # numa tela. Cai para "high", que é o mesmo default da coluna.
    severity_id = (
        emit.severity_id
        if is_valid_severity_id(emit.severity_id)
        else SEVERITY_ID["high"]
    )

    ctx = EnvelopeContext(
        vendor=DETECTION_EVENT_VENDOR,
        # Integração de ORIGEM, não uma sintética: é o que faz a rota que o
        # cliente já criou para aquela integração também receber a detecção
        # dela. ``None`` só acontece em fluxo sem integração (teste/legado).
        integration_id=emit.integration_id,
        # ``customer_id`` do envelope = ``Organization.id`` interno.
        customer_id=organization_id,
        customer_name=src.get("customer_name"),
        organization_slug=src.get("organization_slug"),
        stream=DETECTION_EVENT_STREAM,
        event_type=DETECTION_EVENT_TYPE,
        mapping_version_id=None,
        # ``platform`` da ORIGEM (sobre quem é o achado) enquanto ``vendor`` é
        # "centralops" (quem produziu o achado).
        platform=src.get("platform") or DETECTION_EVENT_VENDOR,
        # Sem isto o evento sai com ``organization_id=None`` e o roteador casa
        # SOMENTE rotas globais: a rota criada pelo próprio tenant nunca receberia
        # a detecção dele. O caminho da scheduled query já pagou exatamente esse
        # bug e deixou o comentário; não se repete aqui.
        organization_id=organization_id,
        data_geography=src.get("data_geography"),
    )

    message = (
        f"Regra em voo '{_evidence_text(emit.rule_name)}' casou "
        f"({src.get('group_field') or 'sem group_by'}"
        f"={src.get('group_value') or '*'})"
    )

    normalized: dict[str, Any] = {
        # ── identidade OCSF ──────────────────────────────────────────────
        "class_uid": CLASS_UID_DETECTION_FINDING,
        "category_uid": CATEGORY_UID_FINDINGS,
        "activity_id": activity_id,
        # type_uid = class_uid * 100 + activity_id, como manda a spec.
        "type_uid": CLASS_UID_DETECTION_FINDING * 100 + activity_id,
        # MILISSEGUNDOS. Segundos aqui seria o erro de 1000x que este repo já
        # pagou uma vez em 16 mappings — e o Security Lake deriva a partição
        # deste campo.
        "time": now_ms,
        "severity_id": severity_id,
        "status_id": STATUS_ID["new"],
        "metadata": {
            "version": OCSF_VERSION,
            "product": {"name": "CentralOps", "vendor_name": "CentralOps"},
            "logged_time": now_ms,
        },
        # ── obrigatório da classe (manifesto 1.8.0) ──────────────────────
        "finding_info": {
            # A MESMA ``dedup_key`` da linha em ``detections``: é o que permite
            # ao destino correlacionar o alerta com o registro interno, e ao
            # suporte responder "este alerta é qual linha do banco?".
            "uid": emit.dedup_key,
            "title": _evidence_text(emit.rule_name),
            "desc": message,
            "created_time": now_ms,
            "types": ["inflight"],
        },
        "message": message,
        # ── contexto do produto ──────────────────────────────────────────
        "unmapped": {
            # Marca de auto-identificação: é ela que o guard de laço procura
            # quando este evento volta pela porta da frente como ``raw`` de um
            # push-ingest. Ver ``_is_self_emitted``.
            "centralops_detection": True,
            "detection_id": emit.detection_id,
            "dedup_key": emit.dedup_key,
            "rule_id": emit.rule_id,
            "rule_name": _evidence_text(emit.rule_name),
            "source": "inflight",
            "organization_id": organization_id,
            "integration_id": emit.integration_id,
            # PONTEIRO para o evento que casou — ver ``_event_source_pointer``
            # para o que ele NÃO carrega e por quê.
            "source_event_id": src.get("event_id"),
            "source_vendor": src.get("vendor"),
            "source_platform": src.get("platform"),
            "source_stream": src.get("stream"),
            "source_event_type": src.get("event_type"),
            "source_event_time": src.get("event_time"),
            "source_class_uid": src.get("event_class_uid"),
            "group_field": src.get("group_field"),
            "group_value": src.get("group_value"),
        },
    }

    # ``raw`` VAZIO, e é a decisão do parágrafo de ``_event_source_pointer``
    # materializada: nada do payload do cliente viaja neste evento.
    uid = (
        emit.detection_id
        if emit.detection_id is not None
        else _group_value_digest(emit.dedup_key)
    )
    return build_envelope(
        {}, normalized, ctx, vendor_msg_id=f"inflight-det-{uid}"
    )


def _dispatch_sync(envelopes: list[dict[str, Any]]) -> None:
    """Entrega o lote ao roteamento. SÍNCRONA, roda em thread.

    Duas razões para existir em vez de chamar ``_enqueue_dispatch`` direto:

    1. ``_enqueue_dispatch`` faz I/O SÍNCRONO (resolve rotas no banco, publica
       task Celery). Chamá-lo de dentro de ``flush_inflight`` — que roda no
       event loop do coletor — bloquearia o loop no ``finally`` do ciclo, que é
       a forma exata do poison-loop que R1 existe para impedir.
    2. É o ponto de monkeypatch dos testes, e mantém o import do ``pipeline``
       (que importa este módulo de volta) tardio e fora do escopo de módulo.

    O lote inteiro numa chamada só, e não uma por evento: ``_enqueue_dispatch``
    com ``routes=None`` RESOLVE AS ROTAS NO BANCO por chamada — emitir um a um
    seria uma consulta por alerta.
    """
    from ..pipeline import _enqueue_dispatch

    # ``enrich_skip_reason`` fica no default ``producer_unsupported``, e é a
    # verdade: este evento nasce DEPOIS do estágio de enriquecimento, não passou
    # por ele. Marcar é obrigatório — sem a marca, um evento sem contexto no
    # destino é indistinguível de um que foi enriquecido e não casou regra.
    _enqueue_dispatch(envelopes)


async def _emit_detection_events(
    acc: InflightAccumulator,
    emits: "tuple[DetectionEmit, ...]",
    organization_id: int,
    *,
    suppressed: int = 0,
) -> None:
    """Emite as detecções do ciclo como eventos OCSF 2004, roteadas. Best-effort.

    R3 É INVIOLÁVEL AQUI: esta função é chamada de dentro do flush, que roda no
    ``finally`` do ciclo de coleta. Ela NUNCA levanta — toda falha é CONTADA
    (``collector_inflight_errors_total{reason="emit_failed"}`` + breakdown por
    regra no observability_store) e logada, e o evento original do cliente já
    seguiu para o destino muito antes disto rodar. O emissor é um observador,
    como o matcher: nunca porteiro.

    1x por CICLO e em BULK, nunca por evento — a mesma forma que o
    ``scheduled_query`` usa. É isso que mantém R1 de pé com um caminho de saída
    novo.
    """
    from ..metrics import INFLIGHT_EVENTS_EMITTED, INFLIGHT_EVENTS_NOT_EMITTED

    if suppressed > 0:
        INFLIGHT_EVENTS_NOT_EMITTED.labels(reason="suppressed").inc(suppressed)

    cap = int(settings.INFLIGHT_EMIT_MAX_EVENTS_PER_CYCLE)
    lote: list[tuple[DetectionEmit, dict[str, Any]]] = []
    loop_guard = 0
    cycle_cap = 0
    # UM relógio para o ciclo inteiro: ``time`` e ``logged_time`` de todos os
    # eventos do mesmo flush têm de ser comparáveis entre si, e chamar
    # ``utcnow`` por evento produziria uma ordenação artificial no destino que
    # não corresponde a nada que aconteceu.
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    for emit in emits:
        if (emit.source or {}).get("self_emitted"):
            loop_guard += 1
            continue
        # ``>=`` com ``cap=0`` é KILL-SWITCH de emissão sem mexer na flag: nada
        # sai, tudo é contado em ``cycle_cap``, as Detections seguem gravadas.
        if len(lote) >= cap:
            cycle_cap += 1
            continue
        try:
            lote.append((emit, _build_detection_event(emit, organization_id, now_ms)))
        except Exception:  # noqa: BLE001 — a Detection já está gravada
            acc.count_error("emit_failed", emit.rule_id)
            logger.debug(
                "inflight: falha montando o evento de detecção (regra %s)",
                emit.rule_id, exc_info=True,
            )

    if loop_guard:
        INFLIGHT_EVENTS_NOT_EMITTED.labels(reason="loop_guard").inc(loop_guard)
        acc._warn_once(
            "loop_guard", None,
            "inflight: %d detecção(ões) deste ciclo casaram um evento que o "
            "PRÓPRIO produto emitiu — o evento novo NÃO foi emitido, para não "
            "abrir cascata. A Detection foi gravada normalmente. Se isto se "
            "repete, há um destino devolvendo evento à ingestão (ex.: rota "
            "apontada para o /api/ingest desta instalação, ou manager Wazuh "
            "que também é fonte coletada).",
            loop_guard,
        )
    if cycle_cap:
        INFLIGHT_EVENTS_NOT_EMITTED.labels(reason="cycle_cap").inc(cycle_cap)
        acc._warn_once(
            "cycle_cap", None,
            "inflight: teto de %d evento(s) de detecção por ciclo atingido — "
            "%d detecção(ões) foram GRAVADAS e não saíram como evento. As "
            "Detections estão íntegras; só a entrega ao destino foi cortada. "
            "Teto atingido costuma indicar regra de alta cardinalidade recém "
            "publicada (INFLIGHT_EMIT_MAX_EVENTS_PER_CYCLE).",
            cap, cycle_cap,
        )

    if not lote:
        return

    try:
        await asyncio.to_thread(_dispatch_sync, [env for _, env in lote])
    except Exception:  # noqa: BLE001 — R3: a coleta não cai por causa do alerta
        # ATRIBUÍDA, não só contada: "perdi 40 eventos" não diz qual regra parou
        # de chegar no SIEM. O ticket já carrega o ``rule_id``, então agrupar
        # aqui não custa nenhuma volta ao banco.
        for emit, _env in lote:
            acc.count_error("emit_failed", emit.rule_id)
        logger.exception(
            "inflight: falha ao despachar %d evento(s) de detecção (org %s) — "
            "as Detections estão GRAVADAS, o que se perdeu foi a entrega ao "
            "destino. Ver collector_inflight_errors_total{reason=\"emit_failed\"}",
            len(lote), organization_id,
        )
        return

    INFLIGHT_EVENTS_EMITTED.inc(len(lote))


async def flush_inflight(
    acc: Optional[InflightAccumulator], organization_id: Optional[int]
) -> None:
    """Persiste os matches do ciclo e emite as métricas. Best-effort.

    Chamada no ``finally`` do ciclo — cobre caminho feliz E caminho de exceção.
    Isso não é zelo: no data-plane default uma exceção no meio do ciclo NÃO
    solta as claims de dedupe, o retry re-busca os eventos e ``claim`` os
    descarta como duplicados. Sem flush aqui, os matches morreriam em memória
    sem log nem métrica.

    Além do instrumento OTel (``INFLIGHT_MATCHES``, no-op quando
    ``OTEL_ENABLED=False`` — o default da instalação padrão), espelha
    ``acc.matches``/``acc.overflow`` no ``observability_store`` (Redis,
    kind="rule") para a UI mostrar "disparos nas últimas 24h" por regra sem
    depender de um OTel Collector estar configurado.

    DUAS métricas, não uma: ``matches`` sozinho ao lado de uma lista de
    Detections muito menor pareceria produto quebrado, mas a divergência é
    ESTRUTURAL (dedup + teto de chaves por regra/ciclo + group_by não
    resolvido) — matches alto com ``overflow`` alto é o diagnóstico de que a
    cardinalidade do group_by estourou o teto, não um bug.

    Os ERROS seguem a mesma divisão, e é aqui que ela paga: o OTel recebe
    ``collector_inflight_errors_total{reason}`` exatamente como antes (soma do
    dict interno — zero mudança de cardinalidade), enquanto o breakdown
    ``err_{reason}`` por regra vai para o observability_store. Sem ele,
    "1200 group_by_unresolved" é um número que não aponta para nenhuma regra e
    o operador não tem o que corrigir.

    O espelhamento é best-effort mas não é MUDO: toda escrita que o store
    recusar é contada em ``collector_inflight_rule_metric_write_failures_total``
    e avisada em WARNING 1x por ciclo (ver ``_mirror_rule_metric``). Sem isso a
    perda era invisível dos dois lados — a chave não nascia na escrita e a
    leitura encontrava a AUSÊNCIA, que soma 0.0 sem erro e vira "a regra não
    disparou" na tela do operador.

    EMISSÃO (``INFLIGHT_EMIT_OCSF_EVENT``, OFF por default): cada Detection
    NOVA — nunca um bump dentro da janela de supressão — sai também como evento
    OCSF 2004 pelo roteamento normal. É best-effort no sentido forte de R3:
    falha ali é contada em ``emit_failed`` e logada, e não toca nem no evento do
    cliente (que já foi entregue muito antes) nem na Detection (que já está
    durável). Ver ``_emit_detection_events``.

    ``flush_lost`` conta a perda REAL, não ``len(acc.pending)``:
    ``DetectionRepository.record`` commita por chave, então uma falha no meio da
    escrita deixa parte das Detections DURÁVEIS no banco. Elas voltam em
    ``InflightFlushInterrupted.written_keys`` e saem da conta. Só o caminho em
    que a exceção nasce FORA do laço de escrita continua contando tudo — e ali
    o log diz explicitamente que o número é um teto, não uma medida.
    """
    if acc is None or organization_id is None:
        return

    from ..metrics import INFLIGHT_ERRORS, INFLIGHT_FLUSH_SECONDS, INFLIGHT_MATCHES

    # Tickets de emissão. ``_emits_known`` separa "nenhuma Detection nova" de
    # "não sei" — ver ``_coerce_emits``.
    _emits: tuple[DetectionEmit, ...] = ()
    _emits_known = False
    _suppressed = 0

    # Teto GLOBAL de escrita, ANTES de qualquer contabilidade: o que ele corta
    # sai de ``pending``, e é isso que mantém coerente todo o resto — o
    # ``flush_lost`` abaixo mede sobre ``pending``, e uma chave cortada aqui
    # contada lá viraria perda DUPLA na mesma série. O que ela perdeu já está
    # contado, e por outra razão. Ver ``_apply_flush_cap``.
    #
    # Guardado porque roda FORA do ``try`` que contabiliza ``flush_lost``: uma
    # exceção aqui pularia a escrita inteira E a contagem da perda, isto é, o
    # pior dos dois mundos — nenhuma Detection gravada e nenhuma série dizendo
    # que faltou algo. Falhando, o teto simplesmente não corta: escrever tudo é
    # LENTO, perder tudo em silêncio é o que este subsistema não pode fazer. A
    # direção da degradação é a decisão; o ``except`` só a executa.
    try:
        _apply_flush_cap(acc)
    except Exception:  # noqa: BLE001
        logger.exception(
            "inflight: teto global de flush falhou (org %s) — o flush segue SEM "
            "corte, com até %d Detection(s) a gravar em série",
            organization_id, len(acc.pending),
        )

    if acc.pending:
        _inicio = time.monotonic()
        try:
            _resultado = await asyncio.to_thread(
                _flush_sync, acc.pending, int(organization_id)
            )
            _emits, _emits_known = _coerce_emits(_resultado)
            if _emits_known:
                # Toda chave de ``pending`` que não virou ticket foi gravada como
                # BUMP dentro da janela de supressão da regra.
                _suppressed = max(0, len(acc.pending) - len(_emits))
        except Exception as exc:  # noqa: BLE001
            # A perda é CONTADA, não presumida — e ATRIBUÍDA. "Perdi 900
            # Detections" não diz QUAL regra parou de alertar; ``item["rule"]``
            # já carrega a regra compilada, então agrupar por ``rule_id`` aqui
            # não custa nenhuma volta ao banco.
            #
            # E CONTADA sobre o que de fato se perdeu: ``record`` commita por
            # chave, então numa falha no meio do laço as Detections anteriores
            # estão no banco. Elas chegam aqui em ``written_keys`` e saem da
            # conta. Antes, ``pending`` inteiro virava ``flush_lost`` e a série
            # reportava prejuízo maior que o real.
            medido = isinstance(exc, InflightFlushInterrupted)
            gravadas = frozenset(exc.written_keys) if medido else frozenset()
            if medido:
                # As Detections commitadas ANTES da falha são duráveis — o
                # alerta delas tem de sair. Calá-las por causa de um erro que
                # veio depois seria perder alerta exatamente no ciclo em que
                # algo já deu errado, que é quando o operador mais precisa dele.
                _emits, _emits_known = _coerce_emits(exc.emits)
                if _emits_known:
                    _suppressed = max(0, len(gravadas) - len(_emits))
            # Aritmética pura ANTES do laço guardado abaixo: é o número que vai
            # para o log, e ele não pode depender de a atribuição por regra ter
            # ido até o fim.
            perdidas = sum(1 for key in acc.pending if key not in gravadas)
            try:
                for dedup_key, item in acc.pending.items():
                    if dedup_key in gravadas:
                        continue
                    acc.count_error("flush_lost", int(item["rule"].rule_id))
            except Exception:  # noqa: BLE001
                # Best-effort dentro de best-effort: quem estoura aqui é o
                # CONTADOR do prejuízo, e ele não pode agravar o prejuízo
                # levantando dentro do ``finally`` do ciclo de coleta.
                logger.debug(
                    "inflight: falha atribuindo flush_lost por regra", exc_info=True
                )
            if medido:
                # ``len(acc.pending) - perdidas`` e não ``len(gravadas)``: as
                # duas contagens só coincidem quando todo ``written_key`` está
                # em ``pending``, e a soma no log tem de fechar sempre.
                logger.exception(
                    "inflight: flush interrompido — %d de %d Detection(s) já "
                    "estavam COMMITADAS (não são perda), %d perdida(s) (org %s)",
                    len(acc.pending) - perdidas, len(acc.pending), perdidas,
                    organization_id,
                )
            else:
                # DEGRADAÇÃO DECLARADA: a exceção não nasceu dentro do laço de
                # escrita (``_flush_sync`` substituído por um duplo, falha ao
                # despachar para a thread, ...), então não existe medida de
                # quantas foram gravadas. Conta tudo como perda — pessimismo do
                # lado certo, porque calar perda que houve é pior que reportar
                # perda que não houve — e o log diz que o número é um TETO, para
                # ninguém tratá-lo como medição.
                logger.exception(
                    "inflight: flush falhou por fora do laço de escrita — até "
                    "%d Detection(s) perdida(s) (org %s); o número é um TETO, "
                    "este caminho não sabe quantas já haviam sido gravadas",
                    perdidas, organization_id,
                )
        finally:
            # Medida nos DOIS caminhos, e é no de falha que ela importa: um
            # flush que estoura o soft-timeout só existe no ramo de exceção, e
            # medir só o caminho feliz esconderia exatamente o percentil que
            # manda elevar (ou baixar) o teto. Sem labels — ver o catálogo.
            # ``otel_metrics.record`` nunca levanta, então isto é seguro dentro
            # de um ``finally`` que roda no ``finally`` do ciclo de coleta.
            INFLIGHT_FLUSH_SECONDS.observe(time.monotonic() - _inicio)

    # ── A detecção SAINDO como evento OCSF 2004, roteada ────────────────
    # ANTES dos laços de métrica abaixo, e não depois: o ``emit_failed`` que a
    # emissão escreve em ``acc.errors`` é justamente o que aqueles laços levam
    # ao OTel e ao breakdown por regra. Invertida a ordem, a falha da entrega
    # ficaria contada só em memória e morreria com o ciclo.
    #
    # Flag OFF (o default) ⇒ nem os tickets são olhados: nenhum evento novo,
    # nenhuma série nova, nenhum comportamento novo numa instalação que não
    # pediu. Ver ``INFLIGHT_EMIT_OCSF_EVENT``.
    if _emits_known and settings.INFLIGHT_EMIT_OCSF_EVENT:
        try:
            await _emit_detection_events(
                acc, _emits, int(organization_id), suppressed=_suppressed
            )
        except Exception:  # noqa: BLE001
            # ``_emit_detection_events`` já é fechada por dentro (toda falha
            # conhecida é contada lá). Esta rede é para o que não se previu, e
            # existe por uma razão só: R3 — nada da emissão pode levantar do
            # ``finally`` do ciclo de coleta e mascarar a exceção que estava se
            # propagando.
            logger.exception(
                "inflight: emissão do evento de detecção falhou fora do "
                "caminho contado (org %s) — as Detections seguem gravadas",
                organization_id,
            )

    for rule_id, count in acc.matches.items():
        INFLIGHT_MATCHES.labels(rule_id=str(rule_id)).inc(count)
        _mirror_rule_metric(acc, "matches", rule_id, count)
    for rule_id, count in acc.overflow.items():
        _mirror_rule_metric(acc, "overflow", rule_id, count)
    for reason, by_rule in acc.errors.items():
        # DUAS superfícies, uma passada. OTel recebe SÓ ``reason``, somando o
        # dict interno: ``sum by (reason)`` fica idêntico ao de antes desta
        # mudança e a cardinalidade não se mexe. O breakdown por regra desce
        # para o observability_store, onde há TTL de 25h para envelhecer a
        # série de uma regra apagada — o que o OTLP não tem.
        if isinstance(by_rule, dict):
            total = sum(by_rule.values())
            per_rule = tuple(by_rule.items())
        else:
            # Forma legada (razão → int) escrita por call site fora deste
            # módulo — hoje ``pipeline.py`` com reason="matcher", que não é
            # atribuível a uma regra (a exceção veio do matcher, antes de saber
            # qual). Aceitar as duas formas aqui é o que impede um ``sum`` sobre
            # int de levantar DENTRO do ``finally`` do ciclo e mascarar a
            # exceção original que estivesse se propagando.
            total, per_rule = int(by_rule), ()
        if total:
            INFLIGHT_ERRORS.labels(reason=reason).inc(total)
        for rule_id, count in per_rule:
            _mirror_rule_metric(acc, f"err_{reason}", rule_id, count)
