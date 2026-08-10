"""Compilador da política de enriquecimento (ADR-LOCAL-0002, §4).

A DSL é **declarativa em JSON**, reusando JMESPath para leitura e os predicados já
existentes em ``normalize/predicates.py``. A escolha foi medida contra as
alternativas e está registrada no ADR; o resumo é que a superfície de execução aqui
é **zero** (não há ``eval``/``exec`` em nenhum ponto do caminho) e a alternativa mais
citada, ``cel-python``, custa 211,9 µs/op contra 4,222 µs/op desta abordagem — a
5.000 EPS o CEL satura um core inteiro avaliando UMA condição por evento.

**Diferença deliberada em relação ao compilador de mapping.**
``normalize/engine._compile_single_rule`` NÃO rejeita chave desconhecida em regra
escalar: um campo novo é simplesmente ignorado, sem erro. Isso significa que um
worker numa versão anterior processa a regra pela metade e não avisa ninguém — falha
silenciosa, num produto de segurança. Aqui, chave desconhecida é **422**.

Compilação é 1× por versão de política (cacheada por ``(policy_id, version)`` no
runtime), nunca por evento.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from ..normalize.dotpath import compile_source
from ..normalize.predicates import CompiledPredicate, compile_predicate
from .contract import KEY_KINDS, EnrichmentConfigError
from . import registry

#: Raiz ÚNICA de escrita. Ver ADR §3.1: escrever em ``normalized.*`` faria
#: ``_centralops.ocsf_valid`` (etiquetado em ``pipeline.py:1255``) descrever um
#: payload que não é o entregue.
TARGET_ROOT: str = "_centralops.enrichment."

#: Chave especial: as tags vão para uma lista, não para um path livre.
TAGS_PATH: Tuple[str, ...] = ("_centralops", "enrichment_tags")

#: Path da proveniência — que enricher/versão respondeu por este evento.
#: Obrigatório para auditoria e para backfill point-in-time ser explicável.
SOURCES_PATH: Tuple[str, ...] = ("_centralops", "enrichment", "_sources")

_RULE_KEYS: frozenset[str] = frozenset(
    {
        "id", "enricher", "table", "source", "key", "when", "outputs", "tags",
        "on_miss", "on_multi", "on_error", "overwrite", "mode",
        "ttl_s", "negative_ttl_s", "entry_ttl_s",
    }
)
_KEY_KEYS: frozenset[str] = frozenset({"source", "kind", "normalize"})
_OUTPUT_KEYS: frozenset[str] = frozenset({"from", "target", "default"})

ON_MISS = frozenset({"skip", "default", "tag"})
#: ``on_multi`` — o que fazer quando a tabela tem mais de uma linha para a chave.
#: Sem isso o comportamento é indefinido, que é exatamente o buraco que Vector
#: fecha errando em >1 linha e Elastic fecha com ``max_matches``.
ON_MULTI = frozenset({"first", "error", "array"})
ON_ERROR = frozenset({"skip", "tag"})
#: Normalizações aplicáveis à chave ANTES do lookup. Fechado: uma normalização
#: desconhecida faria a chave não bater com a tabela — miss silencioso de 100%.
KEY_NORMALIZERS = frozenset({"lower", "upper", "strip", "defang", "strip_port"})

#: Teto de regras por política. Não é limite de produto — é o guard contra uma
#: política patológica multiplicar o custo por evento sem ninguém perceber.
MAX_RULES: int = 64


@dataclass(frozen=True, slots=True)
class CompiledWhen:
    """Gate de uma regra. Ou delega ao predicado compartilhado, ou testa TAGS.

    **Por que `has_tag`/`lacks_tag` existem em vez de reusar o predicado `in`.**
    ``normalize/predicates`` implementa ``in`` como *escalar ∈ lista-de-literais*
    (``predicates.py:200``: ``result in pred.literal``). Sobre
    ``_centralops.enrichment_tags``, que é uma LISTA, isso avalia
    ``["ti_known"] in ("ti_known",)`` → **False**, sempre. Ou seja: o vocabulário
    herdado é estruturalmente incapaz de expressar "tem a tag X".

    Isso não é detalhe: o ADR define encadeamento por tag como o ÚNICO mecanismo de
    condição composta (os predicados são exatamente-uma-chave, sem ``and``/``or``).
    Sem este par, a regra "só consulte a API paga para o que o feed local já marcou"
    — que é o controle de custo do enriquecimento remoto — não é escrevível.

    Alterar a semântica de ``in`` em ``normalize/predicates`` seria a correção
    errada: aquele módulo é compartilhado com o engine de mapping e tem contrato
    ("Type-strict membership") do qual outras regras dependem.
    """

    predicate: Optional[CompiledPredicate] = None
    #: TODAS precisam estar presentes.
    has_tags: Tuple[str, ...] = ()
    #: NENHUMA pode estar presente.
    lacks_tags: Tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CompiledOutput:
    """Um campo a escrever. ``target_path`` já vem TOKENIZADO para não pagar um
    ``str.split`` por evento por output (mesma razão de ``inflight.CompiledClause``)."""

    from_field: str
    target_path: Tuple[str, ...]
    default: Any = None
    has_default: bool = False


@dataclass(frozen=True, slots=True)
class CompiledEnrichRule:
    """Regra pronta para avaliar. Imutável e sem referência ao ORM."""

    rule_id: str
    enricher: str
    table: Optional[str]
    #: Nome da ``EnrichmentSource`` (instância configurada) desta org. Citado por
    #: NOME, nunca com a config embutida: ``secret_ref`` é o próprio ciphertext,
    #: então aceitá-lo no corpo da regra deixaria um admin usar a credencial de
    #: outra org. O runtime resolve o nome DENTRO da org do ciclo.
    source: Optional[str]
    #: resolvedor JMESPath/dot-path já compilado (fast-path quando ``a.b.c``).
    key_source: Any
    key_source_str: str
    key_kind: str
    key_normalizers: Tuple[str, ...]
    when: Optional[CompiledWhen]
    outputs: Tuple[CompiledOutput, ...]
    tags: Tuple[str, ...]
    on_miss: str
    on_multi: str
    on_error: str
    overwrite: bool
    mode: str
    ttl_s: Optional[int]
    negative_ttl_s: Optional[int]
    entry_ttl_s: Optional[int]


@dataclass(frozen=True, slots=True)
class CompiledPolicy:
    """Política compilada + as decisões que valem para o conjunto.

    ``has_remote`` é pré-calculado porque o runtime precisa dele ANTES de processar
    o lote (para decidir se abre orçamento e cliente HTTP); descobrir isso varrendo
    as regras a cada flush seria trabalho repetido por lote.
    """

    rules: Tuple[CompiledEnrichRule, ...]
    version: int = 1

    @property
    def has_remote(self) -> bool:
        return any(r.mode == "remote" for r in self.rules)

    @property
    def has_local(self) -> bool:
        return any(r.mode == "local" for r in self.rules)

    def local_rules(self) -> Tuple[CompiledEnrichRule, ...]:
        return tuple(r for r in self.rules if r.mode == "local")

    def remote_rules(self) -> Tuple[CompiledEnrichRule, ...]:
        return tuple(r for r in self.rules if r.mode == "remote")


#: Discriminadores aceitos em ``when``. Os 4 primeiros são delegados a
#: ``normalize/predicates``; os 2 últimos são desta DSL (ver :class:`CompiledWhen`).
_WHEN_KEYS: frozenset[str] = frozenset(
    {"exists", "equals", "in", "not", "has_tag", "lacks_tag"}
)


def _as_tag_tuple(raw: Any, where: str) -> Tuple[str, ...]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)) or not raw:
        raise EnrichmentConfigError(
            f"{where}: valor deve ser string ou lista não-vazia de strings"
        )
    tags = tuple(str(t).strip() for t in raw if str(t).strip())
    if not tags:
        raise EnrichmentConfigError(f"{where}: nenhuma tag válida")
    return tags


def _compile_when(raw: Any, where: str) -> Optional[CompiledWhen]:
    """Compila o gate da regra. ``None`` = regra sempre avaliada."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise EnrichmentConfigError(f"{where}.when: deve ser objeto")

    # O gate era o ÚNICO objeto da DSL sem esta recusa — regra, key, output e
    # política já a fazem. Sem ela, `{"exists": ..., "lacks_tags": [...]}` (plural
    # digitado por engano) compilava com o gate reduzido a `exists`, e a condição
    # que o operador escreveu sumia sem 422, log ou aviso: fail-OPEN no compilador
    # que o ADR vende como fail-closed, no campo que decide egresso.
    _reject_unknown(raw, _WHEN_KEYS, f"{where}.when")

    present = _WHEN_KEYS & set(raw.keys())
    if not present:
        raise EnrichmentConfigError(
            f"{where}.when: nenhuma chave válida. Esperado uma de {sorted(_WHEN_KEYS)}"
        )
    if len(present) > 1:
        raise EnrichmentConfigError(
            f"{where}.when: múltiplas chaves {sorted(present)}. "
            "Um gate tem EXATAMENTE uma chave — condição composta se escreve "
            "encadeando regras (uma produz a tag, outra a consome)."
        )

    kind = next(iter(present))
    if kind == "has_tag":
        return CompiledWhen(has_tags=_as_tag_tuple(raw["has_tag"], f"{where}.when.has_tag"))
    if kind == "lacks_tag":
        return CompiledWhen(lacks_tags=_as_tag_tuple(raw["lacks_tag"], f"{where}.when.lacks_tag"))
    try:
        return CompiledWhen(predicate=compile_predicate(raw))
    except EnrichmentConfigError:
        raise
    except Exception as exc:  # noqa: BLE001 — MappingDefinitionError e afins
        raise EnrichmentConfigError(f"{where}.when: {exc}") from exc


def _reject_unknown(got: Mapping[str, Any], allowed: frozenset, where: str) -> None:
    """422 em chave desconhecida. É o oposto do fail-open do compilador de mapping."""
    unknown = set(got) - allowed
    if unknown:
        raise EnrichmentConfigError(
            f"{where}: chave(s) desconhecida(s) {sorted(unknown)}. "
            f"Permitidas: {sorted(allowed)}"
        )


def _compile_target(raw: Any, where: str) -> Tuple[str, ...]:
    """Valida e tokeniza o ``target``.

    A restrição de raiz é a regra de projeto mais importante da DSL, então a
    mensagem de erro explica o PORQUÊ — quem topa com ela quase sempre está
    tentando escrever em ``normalized.*``, que é a coisa intuitiva e errada.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise EnrichmentConfigError(f"{where}: 'target' deve ser string não-vazia")
    target = raw.strip()
    if not target.startswith(TARGET_ROOT):
        raise EnrichmentConfigError(
            f"{where}: 'target' deve começar com {TARGET_ROOT!r}, recebeu {target!r}. "
            "Escrever em 'normalized.*' ou 'raw.*' é PROIBIDO: o gate OCSF "
            "(pipeline.py:1255) já etiquetou `_centralops.ocsf_valid` sobre aquele "
            "objeto, e mutá-lo depois faria o rótulo descrever um payload que não é "
            "o entregue."
        )
    parts = tuple(p for p in target.split(".") if p)
    if len(parts) < 3:
        raise EnrichmentConfigError(
            f"{where}: 'target' {target!r} precisa de ao menos um campo sob "
            f"{TARGET_ROOT!r}"
        )
    if parts[:2] == SOURCES_PATH[:2] and parts[2].startswith("_"):
        raise EnrichmentConfigError(
            f"{where}: campos com '_' sob {TARGET_ROOT!r} são reservados ao runtime "
            f"(proveniência). Recebeu {target!r}."
        )
    return parts


def _compile_key(raw: Any, enricher_name: str, where: str) -> Tuple[Any, str, str, Tuple[str, ...]]:
    if not isinstance(raw, Mapping):
        raise EnrichmentConfigError(f"{where}: 'key' deve ser objeto")
    _reject_unknown(raw, _KEY_KEYS, f"{where}.key")

    source_raw = raw.get("source")
    if not isinstance(source_raw, str) or not source_raw.strip():
        raise EnrichmentConfigError(f"{where}.key: 'source' deve ser string não-vazia")
    try:
        resolver = compile_source(source_raw.strip())
    except Exception as exc:  # noqa: BLE001 — jmespath levanta tipos variados
        raise EnrichmentConfigError(
            f"{where}.key.source: expressão inválida {source_raw!r}: {exc}"
        ) from exc

    kind = raw.get("kind")
    if kind not in KEY_KINDS:
        raise EnrichmentConfigError(
            f"{where}.key: 'kind' inválido {kind!r}. Válidos: {sorted(KEY_KINDS)}"
        )

    reg = registry.require(enricher_name)
    if kind not in reg.caps.key_kinds:
        raise EnrichmentConfigError(
            f"{where}: enricher {enricher_name!r} não resolve chave do tipo {kind!r}. "
            f"Suportados: {sorted(reg.caps.key_kinds)}"
        )

    norms_raw = raw.get("normalize") or ()
    if isinstance(norms_raw, str):
        norms_raw = (norms_raw,)
    if not isinstance(norms_raw, (list, tuple)):
        raise EnrichmentConfigError(f"{where}.key: 'normalize' deve ser lista de strings")
    norms = tuple(str(n) for n in norms_raw)
    unknown_norm = set(norms) - KEY_NORMALIZERS
    if unknown_norm:
        raise EnrichmentConfigError(
            f"{where}.key.normalize: desconhecido(s) {sorted(unknown_norm)}. "
            f"Válidos: {sorted(KEY_NORMALIZERS)}"
        )
    return resolver, source_raw.strip(), str(kind), norms


def _compile_rule(raw: Any, index: int, seen_ids: set) -> CompiledEnrichRule:
    where = f"enrichment[{index}]"
    if not isinstance(raw, Mapping):
        raise EnrichmentConfigError(f"{where}: regra deve ser objeto")
    _reject_unknown(raw, _RULE_KEYS, where)

    rule_id = raw.get("id")
    if not isinstance(rule_id, str) or not rule_id.strip():
        raise EnrichmentConfigError(f"{where}: 'id' é obrigatório (string não-vazia)")
    rule_id = rule_id.strip()
    if rule_id in seen_ids:
        # Ids duplicados quebrariam a Resolution (que chaveia por rule_id) e a
        # métrica por regra, de formas que só apareceriam em produção.
        raise EnrichmentConfigError(f"{where}: 'id' duplicado na política: {rule_id!r}")
    seen_ids.add(rule_id)

    enricher = raw.get("enricher")
    if not isinstance(enricher, str) or not enricher.strip():
        raise EnrichmentConfigError(f"{where}: 'enricher' é obrigatório")
    enricher = enricher.strip()
    reg = registry.require(enricher)

    key_source, key_source_str, key_kind, key_norms = _compile_key(
        raw.get("key"), enricher, where
    )

    when = _compile_when(raw.get("when"), where)

    outputs_raw = raw.get("outputs")
    if not isinstance(outputs_raw, (list, tuple)) or not outputs_raw:
        raise EnrichmentConfigError(f"{where}: 'outputs' é obrigatório e não pode ser vazio")
    outputs: list[CompiledOutput] = []
    seen_targets: set = set()
    for j, out_raw in enumerate(outputs_raw):
        ow = f"{where}.outputs[{j}]"
        if not isinstance(out_raw, Mapping):
            raise EnrichmentConfigError(f"{ow}: deve ser objeto")
        _reject_unknown(out_raw, _OUTPUT_KEYS, ow)
        from_field = out_raw.get("from")
        if not isinstance(from_field, str) or not from_field.strip():
            raise EnrichmentConfigError(f"{ow}: 'from' deve ser string não-vazia")
        from_field = from_field.strip()
        known = reg.output_fields
        if known and from_field not in known:
            raise EnrichmentConfigError(
                f"{ow}: enricher {enricher!r} não devolve o campo {from_field!r}. "
                f"Devolve: {sorted(known)}"
            )
        target_path = _compile_target(out_raw.get("target"), ow)
        if target_path in seen_targets:
            raise EnrichmentConfigError(
                f"{ow}: dois outputs da mesma regra escrevem em "
                f"{'.'.join(target_path)!r}; o segundo venceria em silêncio"
            )
        seen_targets.add(target_path)
        outputs.append(
            CompiledOutput(
                from_field=from_field,
                target_path=target_path,
                default=out_raw.get("default"),
                has_default="default" in out_raw,
            )
        )

    tags_raw = raw.get("tags") or ()
    if isinstance(tags_raw, str):
        tags_raw = (tags_raw,)
    if not isinstance(tags_raw, (list, tuple)):
        raise EnrichmentConfigError(f"{where}: 'tags' deve ser lista de strings")
    tags = tuple(str(t).strip() for t in tags_raw if str(t).strip())

    on_miss = str(raw.get("on_miss", "skip"))
    if on_miss not in ON_MISS:
        raise EnrichmentConfigError(f"{where}: 'on_miss' inválido {on_miss!r}. Válidos: {sorted(ON_MISS)}")
    if on_miss == "default" and not any(o.has_default for o in outputs):
        raise EnrichmentConfigError(
            f"{where}: on_miss='default' mas nenhum output declara 'default' — "
            "a regra não escreveria nada no miss, que é o mesmo que 'skip' com "
            "aparência de configuração ativa"
        )
    on_multi = str(raw.get("on_multi", "first"))
    if on_multi not in ON_MULTI:
        raise EnrichmentConfigError(f"{where}: 'on_multi' inválido {on_multi!r}. Válidos: {sorted(ON_MULTI)}")
    on_error = str(raw.get("on_error", "skip"))
    if on_error not in ON_ERROR:
        raise EnrichmentConfigError(f"{where}: 'on_error' inválido {on_error!r}. Válidos: {sorted(ON_ERROR)}")

    mode = str(raw.get("mode") or reg.caps.mode)
    if mode not in ("local", "remote"):
        raise EnrichmentConfigError(f"{where}: 'mode' inválido {mode!r}")
    if mode == "local" and reg.caps.mode == "remote":
        raise EnrichmentConfigError(
            f"{where}: enricher {enricher!r} é remoto (faz I/O por chave) e não pode "
            "rodar em mode='local' — o aplicador por evento é puro por contrato "
            "(guard de CI). Use mode='remote'."
        )

    def _opt_int(field_name: str) -> Optional[int]:
        v = raw.get(field_name)
        if v is None:
            return None
        try:
            iv = int(v)
        except (TypeError, ValueError) as exc:
            raise EnrichmentConfigError(f"{where}: {field_name!r} deve ser inteiro") from exc
        if iv < 0:
            raise EnrichmentConfigError(f"{where}: {field_name!r} não pode ser negativo")
        return iv

    table = raw.get("table")
    if table is not None and (not isinstance(table, str) or not table.strip()):
        raise EnrichmentConfigError(f"{where}: 'table' deve ser string não-vazia quando presente")

    source = raw.get("source")
    if source is not None and (not isinstance(source, str) or not source.strip()):
        raise EnrichmentConfigError(
            f"{where}: 'source' deve ser string não-vazia quando presente"
        )
    # Enricher que exige segredo não roda sem uma fonte configurada. Recusar AQUI,
    # no commit, em vez de deixar levantar no ciclo: o operador que escreveu a
    # regra vê o 422 com o nome do que falta; a falha em runtime só apareceria
    # como evento sem contexto, horas depois, para outra pessoa.
    if getattr(reg, "required_secrets", ()) and not source:
        raise EnrichmentConfigError(
            f"{where}: o enricher {enricher!r} exige credencial, então a regra "
            f"precisa de 'source' apontando uma fonte configurada desta organização "
            f"(segredo exigido: {', '.join(reg.required_secrets)})."
        )

    return CompiledEnrichRule(
        rule_id=rule_id,
        enricher=enricher,
        table=table.strip() if isinstance(table, str) else None,
        source=source.strip() if isinstance(source, str) else None,
        key_source=key_source,
        key_source_str=key_source_str,
        key_kind=key_kind,
        key_normalizers=key_norms,
        when=when,
        outputs=tuple(outputs),
        tags=tags,
        on_miss=on_miss,
        on_multi=on_multi,
        on_error=on_error,
        overwrite=bool(raw.get("overwrite", False)),
        mode=mode,
        ttl_s=_opt_int("ttl_s"),
        negative_ttl_s=_opt_int("negative_ttl_s"),
        entry_ttl_s=_opt_int("entry_ttl_s"),
    )


def compile_policy(raw: Any) -> CompiledPolicy:
    """Compila uma política inteira. Levanta :class:`EnrichmentConfigError` (→422).

    Aceita o documento ``{"version": 1, "enrichment": [...]}`` ou a lista crua de
    regras — a segunda forma existe só para o dry-run da UI/MCP, onde o usuário
    cola as regras sem o envelope.
    """
    if isinstance(raw, (list, tuple)):
        rules_raw: Sequence[Any] = raw
        version = 1
    elif isinstance(raw, Mapping):
        _reject_unknown(raw, frozenset({"version", "enrichment"}), "política")
        rules_raw = raw.get("enrichment") or ()
        if not isinstance(rules_raw, (list, tuple)):
            raise EnrichmentConfigError("'enrichment' deve ser lista de regras")
        try:
            version = int(raw.get("version", 1))
        except (TypeError, ValueError) as exc:
            raise EnrichmentConfigError("'version' deve ser inteiro") from exc
    else:
        raise EnrichmentConfigError(
            f"política deve ser objeto ou lista, recebeu {type(raw).__name__}"
        )

    if len(rules_raw) > MAX_RULES:
        raise EnrichmentConfigError(
            f"política tem {len(rules_raw)} regras; o teto é {MAX_RULES}. "
            "O custo por evento é linear no número de regras."
        )

    seen_ids: set = set()
    rules = tuple(_compile_rule(r, i, seen_ids) for i, r in enumerate(rules_raw))
    return CompiledPolicy(rules=rules, version=version)


def describe_policy(policy: CompiledPolicy) -> Dict[str, Any]:
    """Resumo legível — alimenta o dry-run e o diff da UI."""
    return {
        "version": policy.version,
        "rule_count": len(policy.rules),
        "has_local": policy.has_local,
        "has_remote": policy.has_remote,
        "rules": [
            {
                "id": r.rule_id,
                "enricher": r.enricher,
                "table": r.table,
                "source": r.source,
                "mode": r.mode,
                "key": {"source": r.key_source_str, "kind": r.key_kind},
                "targets": [".".join(o.target_path) for o in r.outputs],
                "tags": list(r.tags),
                "on_miss": r.on_miss,
                "on_multi": r.on_multi,
                "on_error": r.on_error,
            }
            for r in policy.rules
        ],
    }
