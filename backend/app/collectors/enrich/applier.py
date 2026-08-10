"""Aplicador single-event — PURO (ADR-LOCAL-0002).

O ÚNICO código desta feature que roda por evento. Não faz I/O, não guarda estado
global, não loga, não emite métrica, não é ``async``. Um guard estrutural em CI
(``tests/test_adr_local_0002_enrich_applier.py``) reprova qualquer import proibido
neste módulo — a restrição vira mecânica em vez de convenção, porque um
``import httpx`` acrescentado aqui daqui a seis meses seria invisível numa revisão
de PR e custaria um round-trip por evento no gargalo do pipeline. É exatamente a
disciplina de ``inflight/matcher.py`` (ADR-0015, restrição R1), e pela mesma razão
medida: ``record_in`` fazia 4 pipelines Redis síncronos por evento (~0,8 ms/evento,
~8 s bloqueados num ciclo de 10k) antes de virar acumulador batched.

**Um aplicador, dois seams.** Este módulo é chamado tanto no seam local (por evento,
com as tabelas residentes) quanto no seam remoto (no flush, com o mapa já resolvido
em bulk). O que muda é apenas QUEM preencheu a :class:`~.contract.Resolution` — nunca
como o evento é escrito. Sem isso, E1 e E2 viram duas metades de feature com
comportamentos que divergem em produção.

**Contrato de falha.** Nada aqui levanta para o chamador em caso de dado torto: uma
regra que não consegue extrair chave é pulada, um valor inesperado do enricher é
ignorado. O evento SEMPRE segue. Enriquecimento é observador, nunca porteiro — e o
``try`` que fecha a rede de segurança é o do hot path, não daqui.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, MutableMapping, Optional, Sequence, Tuple

from ..normalize.predicates import evaluate_predicate
from .contract import Resolution
from .dsl import SOURCES_PATH, TAGS_PATH, CompiledEnrichRule, CompiledWhen

__all__ = ["ApplyStats", "apply", "evaluate_when", "normalize_key", "extract_key"]


@dataclass(slots=True)
class ApplyStats:
    """Contadores acumulados ao longo do ciclo.

    Mutável e reusado entre eventos DE PROPÓSITO: alocar um objeto de resultado por
    evento seria o maior custo desta feature no caminho quente. Quem instancia é o
    ``runtime``, que também é quem traduz isto em métrica — o aplicador não conhece
    OTel.
    """

    events_touched: int = 0
    hits: Dict[str, int] = field(default_factory=dict)
    misses: Dict[str, int] = field(default_factory=dict)
    skipped: Dict[str, int] = field(default_factory=dict)
    errors: Dict[str, int] = field(default_factory=dict)

    def _bump(self, bucket: Dict[str, int], rule_id: str) -> None:
        bucket[rule_id] = bucket.get(rule_id, 0) + 1


def normalize_key(value: str, normalizers: Sequence[str]) -> str:
    """Aplica a cadeia de normalizações à chave. Pura e sem regex compilada global.

    ``defang`` faz o caminho INVERSO do que o nome sugere à primeira vista: feeds de
    threat intel publicam indicadores *defanged* (``1.2.3[.]4``, ``hxxp://``) para não
    virarem link clicável, e o que chega do vendor no evento é a forma normal. Quem
    precisa converter é a TABELA, não o evento — mas a normalização mora aqui porque
    algumas fontes devolvem indicador defanged no corpo do próprio alerta.
    """
    out = value
    for norm in normalizers:
        if norm == "lower":
            out = out.lower()
        elif norm == "upper":
            out = out.upper()
        elif norm == "strip":
            out = out.strip()
        elif norm == "defang":
            out = (
                out.replace("[.]", ".")
                .replace("(.)", ".")
                .replace("[:]", ":")
                .replace("hxxp://", "http://")
                .replace("hxxps://", "https://")
            )
        elif norm == "strip_port":
            # IPv6 entre colchetes (``[::1]:443``) tem precedência: cortar no
            # primeiro ':' quebraria o endereço inteiro em silêncio.
            if out.startswith("["):
                close = out.find("]")
                if close > 0:
                    out = out[1:close]
            elif out.count(":") == 1:
                out = out.split(":", 1)[0]
    return out


def extract_key(envelope: Mapping[str, Any], rule: CompiledEnrichRule) -> Optional[str]:
    """Chave normalizada da regra para este evento, ou ``None``.

    ``None`` significa "esta regra não se aplica a este evento" — campo ausente,
    vazio, ou de tipo que não vira chave. NUNCA é erro: a esmagadora maioria dos
    eventos não tem todos os campos de todas as regras, e tratar ausência como falha
    encheria a métrica de erro com o caso normal.
    """
    try:
        raw = rule.key_source.search(envelope)
    except Exception:  # noqa: BLE001 — jmespath pode levantar em dado torto
        return None
    if raw is None:
        return None
    # Números viram chave (ex.: um ASN, um rule.level); dict/list não.
    if isinstance(raw, (dict, list, tuple, set, bool)):
        return None
    key = raw if isinstance(raw, str) else str(raw)
    key = normalize_key(key, rule.key_normalizers)
    key = key.strip()
    return key or None


def _current_tags(envelope: Mapping[str, Any]) -> Tuple[str, ...]:
    cur = _get_path(envelope, TAGS_PATH)
    if not isinstance(cur, list):
        return ()
    return tuple(t for t in cur if isinstance(t, str))


def evaluate_when(when: CompiledWhen, envelope: Mapping[str, Any]) -> bool:
    """Avalia o gate. Puro e nunca levanta.

    O teste de tag lê ``_centralops.enrichment_tags`` acumulado pelas regras
    ANTERIORES da mesma política — é isso que torna o encadeamento
    "regra A marca, regra B consome" funcional, e é a única forma de expressar
    condição composta (os predicados herdados são exatamente-uma-chave, sem
    ``and``/``or``).
    """
    if when.has_tags or when.lacks_tags:
        tags = _current_tags(envelope)
        if when.has_tags and not all(t in tags for t in when.has_tags):
            return False
        if when.lacks_tags and any(t in tags for t in when.lacks_tags):
            return False
        return True
    if when.predicate is not None:
        return evaluate_predicate(when.predicate, envelope)
    return True


def _get_path(root: Mapping[str, Any], path: Tuple[str, ...]) -> Any:
    cur: Any = root
    for part in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
        if cur is None:
            return None
    return cur


def _set_path(root: MutableMapping[str, Any], path: Tuple[str, ...], value: Any) -> bool:
    """Escreve ``value`` em ``path``, criando os dicts intermediários.

    Devolve False (sem escrever) quando um nó intermediário existe e NÃO é dict —
    sobrescrever um escalar por um dict corromperia dado do vendor em silêncio, que
    é pior que não enriquecer.
    """
    cur: Any = root
    for part in path[:-1]:
        nxt = cur.get(part)
        if nxt is None:
            nxt = {}
            cur[part] = nxt
        elif not isinstance(nxt, dict):
            return False
        cur = nxt
    cur[path[-1]] = value
    return True


def _add_tags(envelope: MutableMapping[str, Any], tags: Sequence[str]) -> None:
    """Acrescenta tags preservando ordem e sem duplicar.

    Lista e não set: a ordem é o que torna o valor legível na UI e estável no diff,
    e a cardinalidade é pequena por construção (vocabulário controlado)."""
    if not tags:
        return
    cur = _get_path(envelope, TAGS_PATH)
    if not isinstance(cur, list):
        cur = []
        if not _set_path(envelope, TAGS_PATH, cur):
            return
    for tag in tags:
        if tag not in cur:
            cur.append(tag)


def _add_source(envelope: MutableMapping[str, Any], rule: CompiledEnrichRule) -> None:
    """Registra proveniência: qual regra/enricher/tabela respondeu.

    Obrigatório, não opcional. Sem isto, um backfill point-in-time é inauditável
    (não dá para saber que versão de tabela respondeu) e um campo enriquecido é
    indistinguível de um campo do vendor no destino.
    """
    cur = _get_path(envelope, SOURCES_PATH)
    if not isinstance(cur, list):
        cur = []
        if not _set_path(envelope, SOURCES_PATH, cur):
            return
    entry = {"rule": rule.rule_id, "enricher": rule.enricher}
    if rule.table:
        entry["table"] = rule.table
    cur.append(entry)


def _write_outputs(
    envelope: MutableMapping[str, Any],
    rule: CompiledEnrichRule,
    value: Optional[Mapping[str, Any]],
    *,
    use_defaults: bool,
) -> bool:
    """Escreve os outputs. Devolve True se algo foi efetivamente escrito."""
    wrote = False
    for out in rule.outputs:
        if value is not None and out.from_field in value:
            payload = value[out.from_field]
        elif use_defaults and out.has_default:
            payload = out.default
        else:
            continue
        if payload is None:
            # Enricher devolvendo None para um campo é "não sei", não "é nulo".
            # Escrever null poluiria o payload e contaria bytes por nada.
            continue
        if not rule.overwrite:
            existing = _get_path(envelope, out.target_path)
            if existing is not None:
                # O dado que já está lá vence. É o inverso do default do Elastic
                # (`override: true`), e é a escolha certa aqui: o enriquecimento
                # nunca deve apagar o que o vendor afirmou.
                continue
        if isinstance(payload, (dict, list)):
            # Copiar ANTES de escrever. As três fontes de `payload` são objetos
            # COMPARTILHADOS: a linha da tabela residente (viva pelo ciclo inteiro,
            # todos os eventos com a mesma chave), o valor do lote remoto (todos os
            # eventos com a mesma chave) e `out.default` (constante compilada, viva
            # para sempre). Escrever a referência faria uma regra cujo target fica
            # SOB o de outra mutar o objeto compartilhado — o evento seguinte
            # herdaria o campo mesmo com o gate `when` falso, com `_sources` mentindo
            # sobre a origem; e dois outputs com o mesmo `from` produziriam envelope
            # auto-referente, cujo `dumps_bytes` derruba o lote inteiro no despacho.
            # Escalar (o caso comum: um site, uma criticidade, um score) não paga nada.
            payload = copy.deepcopy(payload)
        if _set_path(envelope, out.target_path, payload):
            wrote = True
    return wrote


def apply(
    envelope: MutableMapping[str, Any],
    rules: Sequence[CompiledEnrichRule],
    resolution: Resolution,
    stats: ApplyStats,
) -> bool:
    """Aplica as regras a UM envelope. Devolve True se o evento foi modificado.

    Caso comum — nenhuma regra casa — não aloca nada e devolve False.
    """
    if not rules:
        return False

    touched = False
    for rule in rules:
        # 1. gate declarativo. Falso ⇒ regra pulada, NADA escrito (nem tag de miss):
        #    "não se aplica" e "aplica e não achou" são estados diferentes, e
        #    colapsá-los tornaria a tag `*_unknown` inútil para roteamento.
        if rule.when is not None:
            try:
                if not evaluate_when(rule.when, envelope):
                    continue
            except Exception:  # noqa: BLE001 — predicado nunca derruba o evento
                stats._bump(stats.errors, rule.rule_id)
                continue

        # 2. chave
        key = extract_key(envelope, rule)
        if key is None:
            continue

        # 3. resolução — o único ponto que difere entre seam local e remoto
        try:
            answered, value = resolution.get(rule.rule_id, key)
        except Exception:  # noqa: BLE001 — Resolution é código nosso, mas o
            # aplicador é a última linha antes do evento: nada aqui derruba.
            stats._bump(stats.errors, rule.rule_id)
            if rule.on_error == "tag":
                _add_tags(envelope, ("enrich_degraded",))
                touched = True
            continue

        if not answered:
            # Esta resolução não responde por esta regra (ex.: o seam local
            # encontrando uma regra remota). NÃO é miss — contar como miss daria
            # 100% de miss_rate num enricher que funciona perfeitamente.
            stats._bump(stats.skipped, rule.rule_id)
            continue

        if value is None:
            stats._bump(stats.misses, rule.rule_id)
            if rule.on_miss == "skip":
                continue
            if rule.on_miss == "tag":
                _add_tags(envelope, tuple(f"{t}_unknown" for t in rule.tags) or ("enrich_unknown",))
                touched = True
                continue
            # on_miss == "default"
            if _write_outputs(envelope, rule, None, use_defaults=True):
                _add_source(envelope, rule)
                touched = True
            continue

        # 4. hit — resolve multiplicidade antes de escrever
        payload: Optional[Mapping[str, Any]] = value
        if isinstance(value, list):
            if rule.on_multi == "error":
                stats._bump(stats.errors, rule.rule_id)
                if rule.on_error == "tag":
                    _add_tags(envelope, ("enrich_ambiguous",))
                    touched = True
                continue
            if rule.on_multi == "array":
                # Cada output vira lista dos valores daquele campo. Mantém o
                # contrato "um target, um valor" sem inventar semântica de merge.
                merged: Dict[str, Any] = {}
                for out in rule.outputs:
                    vals = [
                        row[out.from_field]
                        for row in value
                        if isinstance(row, Mapping) and row.get(out.from_field) is not None
                    ]
                    if vals:
                        merged[out.from_field] = vals
                payload = merged or None
            else:  # "first"
                payload = next((r for r in value if isinstance(r, Mapping)), None)

        if payload is None or not isinstance(payload, Mapping):
            stats._bump(stats.misses, rule.rule_id)
            continue

        stats._bump(stats.hits, rule.rule_id)
        if _write_outputs(envelope, rule, payload, use_defaults=False):
            _add_tags(envelope, rule.tags)
            _add_source(envelope, rule)
            touched = True

    if touched:
        stats.events_touched += 1
    return touched
