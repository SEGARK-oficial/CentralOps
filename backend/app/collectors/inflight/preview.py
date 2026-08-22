"""Preview de regra contra amostras reais, sem persistir nada (ADR-0015, Fase 3).

O cliente escreve as próprias regras. Isso torna a AUTORIA um produto: se ele
não consegue saber se a regra funciona antes de salvar, ela fica muda por um dos
onze motivos silenciosos da feature e ele conclui que o produto não funciona.

Este módulo responde "minha regra casaria alguma coisa?" — e, quando não casa,
POR QUE não. Um preview que devolvesse só "0 de 100" cobriria dois dos onze
motivos e reproduziria o silêncio que a fase inteira combate, apenas mais cedo.

Vive no Core (e não no EE) porque depende do reservoir, do motor de normalização
e do matcher — todos Core. O EE só expõe a rota.

SEIS INVARIANTES, nenhuma opcional:

(a) FRESCOR. O reservoir NÃO tem TTL: é ``LPUSH``+``LTRIM``, sem ``expire``. Uma
    integração parada há três meses devolve 100 eventos de três meses atrás,
    indistinguíveis de tráfego vivo. Por isso a resposta SEMPRE carrega a janela
    temporal das amostras — sem ela, "casou 40 de 100" pode ser arqueologia.

(b) REDIS FORA ≠ RESERVOIR VAZIO. Usa ``peek_strict``, que propaga a exceção.

(c) PARIDADE DE ENVELOPE — o risco número um. O reservoir guarda o raw NU,
    pré-normalização, mas o matcher avalia paths enraizados no ENVELOPE. Avaliar
    a saída crua do ``peek`` faria toda cláusula ``raw.*`` e ``_centralops.*``
    resolver ``None`` — "0 de 100" para uma regra perfeita. E há a armadilha mais
    sutil: o envelope de produção carrega ``applied.reduced_raw or raw_event``,
    ou seja o raw TRIMADO quando o mapping tem ``raw_reduction``. Um preview que
    usasse o raw nu enxergaria campos que em produção foram cortados e diria
    "funciona" para uma regra que nunca dispara. A reconstrução aqui espelha
    ``pipeline.py`` linha a linha, e há teste de paridade guardando isso.

(d) O PREVIEW NUNCA ESCREVE. Usa ``evaluate_ruleset`` (matcher puro) e NUNCA
    ``flush_inflight``. Se reusasse o flush, um autor testando trinta vezes
    injetaria disparos fantasma no contador de 24h e contaminaria justamente o
    sinal que o kill switch usa.

(e) CAP SERVER-SIDE. O frontend não é fonte de verdade sobre limite.

(f) VOCABULÁRIO POR MODO. Compilar SEMPRE com o compilador em voo era a mentira
    que restava: uma regra ``eval_mode='batch'`` com ``in``, ``nin`` ou
    ``exists`` — os três operadores que só o inflight conhece — passava aqui
    reportando "casou 25 de 25", e em produção ``correlation_engine`` acha o
    operador fora de ``_OPS``, devolve False no PRIMEIRO filtro e descarta o
    item inteiro. Regra verde que nunca dispara: o silêncio que esta ADR existe
    para matar, produzido pela própria ferramenta de diagnóstico. Por isso
    ``evaluate_preview`` recebe o ``eval_mode`` e valida o vocabulário do modo
    PEDIDO. Antes de acreditar num preview batch, porém, leia
    ``BATCH_PREVIEW_CAVEATS``: ramificar o operador conserta o sintoma MENOR.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Teto de amostras avaliadas por chamada. Um ``LRANGE``, uma chave, um mapping.
PREVIEW_DEFAULT_LIMIT = 25
PREVIEW_MAX_LIMIT = 50

#: Valores observados devolvidos por cláusula, para o autor ver o que o campo
#: de fato contém. Poucos e truncados: o objetivo é diagnosticar a regra, não
#: expor o payload do cliente.
_MAX_OBSERVED = 3
_OBSERVED_MAXLEN = 120

#: Modo assumido quando o chamador não diz qual — preserva o comportamento dos
#: chamadores que passam só ``(envelopes, where_json)``.
PREVIEW_DEFAULT_EVAL_MODE = "inflight"

#: Domínio FECHADO de modos. Espelha ``CorrelationRule.eval_mode``.
PREVIEW_EVAL_MODES = frozenset({"inflight", "batch"})

#: O QUE O PREVIEW EM MODO BATCH **NÃO** É. Vive no código, e não só num ADR,
#: porque é a limitação que o operador precisa ler no momento em que confia no
#: resultado.
#:
#: Ramificar o vocabulário (invariante (f)) conserta o sintoma MENOR — a regra
#: batch aprovada com um operador que o motor batch não conhece. O sintoma MAIOR
#: continua de pé e é ESTRUTURAL: este módulo avalia o ENVELOPE do pipeline
#: (``_centralops`` / ``normalized`` / ``raw``, reconstruído por
#: ``build_preview_envelope`` a partir do reservoir), enquanto o motor batch
#: avalia LINHAS CRUAS de query federada, que não têm envelope nenhum. Uma
#: cláusula ``raw.user`` casa 25/25 aqui e resolve ``None`` lá — com operadores
#: 100% compartilhados, sem erro, sem log, sem nada.
#:
#: Em uma frase: o preview batch é aproximação de VOCABULÁRIO, NUNCA de FONTE.
#: Fazê-lo aproximar a fonte exigiria rodar a query federada e está fora de
#: escopo; o que não está fora de escopo é DIZER isso — um preview que se
#: apresenta como veredito e é palpite é pior que preview nenhum.
BATCH_PREVIEW_CAVEATS = (
    "Fonte diferente: este preview avalia o envelope do pipeline "
    "(_centralops/normalized/raw) montado a partir do reservoir; o motor batch "
    "avalia linhas cruas da query federada. Uma cláusula 'raw.user' pode casar "
    "aqui e resolver None lá. A aproximação é de VOCABULÁRIO, não de FONTE.",
    "'ne'/'nin' são mais ESTRITOS aqui: o compilador em voo auto-injeta um "
    "'exists' para o path da cláusula negativa, enquanto o motor batch mantém a "
    "vacuidade (campo ausente CASA). Logo este preview reporta MENOS "
    "casamentos do que o batch produziria.",
    "Comparação diferente: aqui 'eq' é igualdade nativa (o inteiro 1 não casa a "
    "string \"1\"); o motor batch compara os dois lados via str(). Um campo "
    "numérico contra valor textual casa no batch e não aqui.",
)


@dataclass(frozen=True)
class ClauseVerdict:
    """Diagnóstico de UMA cláusula sobre o conjunto de amostras.

    A separação entre ``path_resolved`` e ``matched`` é o que torna o preview
    útil: ``path_resolved=0`` significa "o campo não existe onde você apontou"
    (path errado, ou atravessando array, ou trimado pelo mapping);
    ``path_resolved=100, matched=0`` significa "o campo existe e o valor não
    bate". São problemas diferentes e correções diferentes.
    """

    index: int
    field_path: str
    op: str
    path_resolved: int
    matched: int
    observed: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PreviewResult:
    """Veredito do preview, sempre carimbado com o modo em que foi produzido.

    ``eval_mode`` é ecoado INCLUSIVE nos retornos precoces de ``invalid`` e
    ``empty``, e isso não é cosmético: sem ele a UI diz "regra inválida" sem
    poder dizer "inválida para QUAL vocabulário" — que é literalmente a próxima
    pergunta do operador, porque ``exists`` é legítimo em voo e inexistente no
    batch, e a MESMA regra é válida ou não conforme o radio button.

    ``caveats`` é PROPRIEDADE derivada e não campo: o texto é função do modo, e
    ``eval_mode`` precisa permanecer o último campo do dataclass para que
    qualquer construção existente continue válida. Ver ``BATCH_PREVIEW_CAVEATS``
    para o que ele diz e por quê.
    """

    state: str  # "ok" | "empty" | "unavailable" | "invalid"
    sample_count: int = 0
    matched: int = 0
    oldest_event_time: Optional[str] = None
    newest_event_time: Optional[str] = None
    clauses: list[ClauseVerdict] = field(default_factory=list)
    reason: Optional[str] = None
    eval_mode: str = PREVIEW_DEFAULT_EVAL_MODE

    @property
    def caveats(self) -> tuple[str, ...]:
        """Limitações que a UI DEVE renderizar junto com este resultado.

        Vazio no modo em voo — lá o preview avalia a mesma estrutura, com o
        mesmo comparador, que o pipeline avalia; não há aproximação a confessar.
        """
        return BATCH_PREVIEW_CAVEATS if self.eval_mode == "batch" else ()


def _truncate(value: Any) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= _OBSERVED_MAXLEN else text[:_OBSERVED_MAXLEN] + "…"


def _iso(epoch_like: Any) -> Optional[str]:
    """``normalized["time"]`` (epoch ms do OCSF) → ISO UTC."""
    try:
        num = float(epoch_like)
    except (TypeError, ValueError):
        return None
    if num <= 0:
        return None
    # OCSF ``time`` é em MILISSEGUNDOS.
    return datetime.fromtimestamp(num / 1000.0, tz=timezone.utc).isoformat()


def build_preview_envelope(
    raw_event: dict,
    *,
    vendor: str,
    integration_id: int,
    organization_id: int,
    organization_name: Optional[str],
    # Opcional para não quebrar chamador existente, mas o preview MENTE sem
    # ele quando o destino deriva rótulo por evento: a linha sai
    # ``unresolved`` na tela e com o slug em produção.
    organization_slug: Optional[str] = None,
    customer_id: Optional[str],
    stream: str,
    event_type: str,
    mapping_version_id: Optional[int],
    rules: Any,
    dsl_version: Any,
    data_geography: Optional[str] = None,
) -> Optional[dict]:
    """Reconstrói o envelope EXATAMENTE como o pipeline o monta.

    Espelha ``pipeline.py``: normaliza com ``default_engine.apply``, monta o
    ``EnvelopeContext`` com os mesmos campos, e — crítico — passa
    ``applied.reduced_raw or raw_event`` para ``build_envelope``, que é o raw
    TRIMADO quando o mapping define ``raw_reduction``. Também propaga
    ``degraded_fields``.

    Qualquer divergência aqui envenena o preview inteiro: ele passaria a avaliar
    uma estrutura que não é a que roda em produção, e todo veredito seria sobre
    um evento que não existe. Guardado por ``test_adr0015_preview_envelope_parity``.

    ``None`` quando a normalização falha (o evento iria para quarentena em
    produção, logo nunca chegaria ao matcher).
    """
    import time

    from ..normalize.engine import default_engine
    from ..normalize.envelope import EnvelopeContext, build_envelope
    from ..state.dedupe import compute_message_id

    try:
        applied = default_engine.apply(
            mapping_version_id,
            rules,
            raw_event,
            dsl_version=dsl_version,
            ingest_time_epoch=int(time.time() * 1000),
        )
    except Exception:  # noqa: BLE001 — em produção isto vira quarentena
        return None

    ctx = EnvelopeContext(
        vendor=vendor,
        integration_id=integration_id,
        customer_id=customer_id,
        customer_name=organization_name,
        organization_slug=organization_slug,
        stream=stream,
        event_type=event_type,
        mapping_version_id=mapping_version_id,
        organization_id=organization_id,
        data_geography=data_geography,
    )
    envelope = build_envelope(
        # MESMA expressão do pipeline. Ver invariante (c) no docstring do módulo.
        applied.reduced_raw or raw_event,
        applied.output,
        ctx,
        vendor_msg_id=compute_message_id(raw_event),
    )
    if getattr(applied, "ingest_fallback_targets", None):
        envelope["_centralops"]["degraded_fields"] = list(
            applied.ingest_fallback_targets
        )
    return envelope


def evaluate_preview(
    envelopes: list[dict],
    where_json: str,
    *,
    eval_mode: str = PREVIEW_DEFAULT_EVAL_MODE,
) -> PreviewResult:
    """Avalia ``where_json`` sobre envelopes já reconstruídos. PURA.

    Separada da carga de propósito: permite testar o diagnóstico sem Redis nem
    banco, e mantém o caminho de I/O isolado.

    ``eval_mode`` é KEYWORD-ONLY por construção (``*``): há chamadores POSICIONAIS
    de ``(envelopes, where_json)`` e um terceiro parâmetro posicional os
    quebraria calado — ou, pior, engoliria um argumento no lugar errado.

    O modo decide o VOCABULÁRIO cobrado da regra: ``inflight`` usa
    ``INFLIGHT_ALLOWED_OPS`` (10 operadores), ``batch`` usa o ``_OPS`` do próprio
    ``services/correlation_engine`` (7). É o que impede o preview de aprovar uma
    regra batch com ``in``/``nin``/``exists`` — ver invariante (f).

    Não decide, porém, a FONTE nem o comparador: em modo batch o veredito segue
    sendo produzido sobre o envelope e com o comparador em voo. Isso é
    aproximação, é deliberado, e está declarado em ``BATCH_PREVIEW_CAVEATS`` —
    leia antes de tratar um "casou 25 de 25" em modo batch como promessa.
    """
    from ...services.correlation_engine import _OPS as BATCH_ALLOWED_OPS
    from .matcher import CompiledRuleSet, _resolve, evaluate_ruleset
    from .runtime import compile_rule, validate_where_json

    if eval_mode not in PREVIEW_EVAL_MODES:
        # Domínio FECHADO, e cair no default em silêncio seria devolver um
        # veredito do vocabulário errado carimbado com o modo pedido — a mesma
        # mentira que este parâmetro veio matar, só que mais difícil de ver.
        raise ValueError(f"eval_mode desconhecido: {eval_mode!r}")

    class _Shim:
        id = 0
        name = "(preview)"
        severity_id = 4
        suppression_window_seconds = 3600
        group_by_field = None

        def __init__(self, wj: str) -> None:
            self.where_json = wj

    clauses_raw, reason = validate_where_json(where_json)
    if reason is None and eval_mode == "batch":
        # Vocabulário lido do PRÓPRIO motor batch, nunca de uma cópia local:
        # uma cópia envelheceria em silêncio no dia em que ``_OPS`` mudasse, e
        # o preview voltaria a divergir do que roda — em sentido novo.
        #
        # Sobre as cláusulas CRUAS, e não sobre as compiladas: o compilador em
        # voo AUTO-INJETA ``exists`` para todo path de ``ne``/``nin``, então
        # cobrar o vocabulário do resultado da compilação reprovaria por
        # ``unknown_op`` uma regra cujo autor jamais escreveu ``exists``.
        #
        # ``unknown_op`` é razão JÁ EXISTENTE em ``runtime.REJECT_REASONS``: o
        # enum é fechado porque vira label de métrica, e um reason novo criado
        # aqui multiplicaria série sem que ninguém percebesse.
        if any(str(c.get("op") or "eq") not in BATCH_ALLOWED_OPS for c in clauses_raw):
            reason = "unknown_op"

    if reason is None:
        rule, reason = compile_rule(_Shim(where_json))
    else:
        rule = None
    if rule is None:
        # A regra não compila: dizer QUAL razão é mais útil que qualquer
        # contagem, e é o mesmo vocabulário fechado que o 422 da escrita usa.
        return PreviewResult(state="invalid", reason=reason, eval_mode=eval_mode)

    if not envelopes:
        return PreviewResult(state="empty", eval_mode=eval_mode)

    # Diagnóstico POR CLÁUSULA. Avaliar a regra inteira só devolveria o total, e
    # o total não diz onde consertar.
    verdicts: list[ClauseVerdict] = []
    for idx, clause in enumerate(rule.clauses):
        resolved = matched = 0
        observed: list[str] = []
        single = CompiledRuleSet(
            rules=(rule.__class__(
                rule_id=0, name="c", severity_id=4,
                suppression_window_seconds=3600, group_by_path=None,
                clauses=(clause,),
            ),),
            share_paths=False,
        )
        for env in envelopes:
            value = _resolve(env, clause.path)
            if value is not None:
                resolved += 1
                if len(observed) < _MAX_OBSERVED:
                    text = _truncate(value)
                    if text not in observed:
                        observed.append(text)
            if evaluate_ruleset(env, single):
                matched += 1
        verdicts.append(
            ClauseVerdict(
                index=idx,
                field_path=".".join(clause.path),
                op=clause.op,
                path_resolved=resolved,
                matched=matched,
                observed=observed,
            )
        )

    full = CompiledRuleSet(rules=(rule,), share_paths=False)
    total_matched = sum(1 for env in envelopes if evaluate_ruleset(env, full))

    times = [
        t for t in (_iso((e.get("normalized") or {}).get("time")) for e in envelopes)
        if t
    ]
    return PreviewResult(
        state="ok",
        sample_count=len(envelopes),
        matched=total_matched,
        oldest_event_time=min(times) if times else None,
        newest_event_time=max(times) if times else None,
        clauses=verdicts,
        eval_mode=eval_mode,
    )
