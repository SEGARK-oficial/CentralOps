"""Detector de campos desconhecidos (RF3.6).

Compara os paths presentes no payload raw com os paths consumidos por
um mapping. O delta vai para a tabela ``unknown_fields`` para que a UI
de Drift Explorer liste, agregue e ofereça ações ("ignorar",
"criar mapping pra esse campo").

Por que sampling 1:N e não inline em todo evento: enumerar paths +
upsert em DB para cada evento custa caro em throughput alvo de 5k ev/s.
A taxa de detecção converge rapidamente — um campo novo aparece em
muitos eventos e a primeira amostra basta.

Estratégia:

1. ``flatten_paths(raw)``: walk recursivo dict/list → conjunto de
   paths dot/bracket-notation (ex: ``alert.threat.details[0].hash``).
2. Subtrair os ``consumed_paths`` reportados pelo engine.
3. Para cada path restante, fazer upsert em ``unknown_fields`` (insert
   ou increment ``occurrence_count`` + atualizar ``last_seen``).

O writer é síncrono (DB) e chamado via ``asyncio.to_thread`` pelo
pipeline para não bloquear o event loop.

Namespace ``_`` (preprocess):
-----------------------------------------
Ops ``preprocess`` podem extrair e materializar campos em um namespace
virtual prefixado com ``_`` (ex: ``_processed.parsedAlert.fields.mailFrom``).
Esses paths NÃO existem no payload raw — eles são artefatos produzidos
pelo engine durante o processamento. O drift detector IGNORA qualquer
``consumed_paths`` que comece com ``source:_`` ou diretamente com ``_``,
garantindo que fields virtuais do preprocess nunca apareçam como "unknown"
para os analistas. Esse comportamento é defensivo: funciona mesmo quando
nenhuma op preprocess existe na versão atual do mapping.

``dsl_version``:
--------------------------
O parâmetro ``dsl_version`` é aceito por ``compute_unknown_paths`` e
``record_unknown_fields`` e repassado para futura lógica v2-específica.
Atualmente sem efeito comportamental — reservado para uso futuro.
"""

from __future__ import annotations

import logging
import random
import re
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from ...db import database, models

logger = logging.getLogger(__name__)


_PRIMITIVE_TYPES = (str, int, float, bool, type(None))
_MAX_LIST_SAMPLES = 3  # quantos índices listamos em arrays
_MAX_PATHS_PER_EVENT = 500  # safety: evita explosão em payload patológico
_MAX_SAMPLE_VALUE_LEN = 200
# Teto de profundidade do walk. Não existia: um payload cíclico ou absurdamente
# aninhado levantava RecursionError num ponto do pipeline que NÃO está sob
# try/except, abortando o ciclo de coleta.
_MAX_DEPTH = 32


# ── Parsing de JMESPath para paths do raw ────────────────────────────────────
#
# O engine devolve ``source:<jmespath>`` com a expressão ORIGINAL da regra. Para
# saber o que o mapping realmente lê, precisamos extrair dela os paths do raw.
# Isto é um extrator deliberadamente pequeno (não um parser JMESPath completo):
# cobre o que aparece em mapping de verdade — path pontuado, identificador entre
# aspas, ``||`` de fallback, índice/flatten de array, multiselect e chamada de
# função — e, em qualquer construção que não reconheça, ERRA PARA O LADO DE
# reportar drift a mais, nunca de esconder campo.
_STRING_LITERAL_RE = re.compile(r"'(?:[^'\\]|\\.)*'")
_QUOTED_IDENT_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')
_BARE_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _extract_source_paths(expr: str) -> Set[str]:
    """Paths do raw lidos por uma expressão JMESPath.

    ``"managedAgent.name"``        -> {"managedAgent.name"}
    ``"timestamp || \\"@timestamp\\""`` -> {"timestamp", "@timestamp"}
    ``"rule.mitre.id[0]"``         -> {"rule.mitre.id"}
    ``"[type]"``                   -> {"type"}
    ``"join(', ', tags)"``         -> {"tags"}

    O caso do identificador entre aspas era um bug de falso positivo: o parser
    anterior fazia ``split('.')`` no texto cru, então ``"@timestamp"`` virava a
    top-key literal ``'"@timestamp"'`` (COM aspas), que nunca casava com a chave
    ``@timestamp`` do raw — e o campo aparecia no Drift Explorer como
    desconhecido embora o mapping o consumisse.
    """
    found: Set[str] = set()

    def _walk(text: str) -> None:
        segments: List[str] = []

        def _flush() -> None:
            if segments:
                found.add(".".join(segments))
                segments.clear()

        i, n = 0, len(text)
        while i < n:
            ch = text[i]
            if ch == '"':
                m = _QUOTED_IDENT_RE.match(text, i)
                if not m:
                    _flush()
                    break
                segments.append(m.group(1).replace('\\"', '"'))
                i = m.end()
            elif ch == "_" or ch.isalpha():
                m = _BARE_IDENT_RE.match(text, i)
                assert m is not None
                i = m.end()
                if i < n and text[i] == "(":
                    # Nome de função (join, length, to_string...): não é path.
                    # Os ARGUMENTOS são, e vêm no bloco de parênteses abaixo.
                    _flush()
                    continue
                segments.append(m.group(0))
            elif ch == ".":
                i += 1
            elif ch in "[(":
                close = "]" if ch == "[" else ")"
                depth, j = 1, i + 1
                while j < n and depth:
                    if text[j] == ch:
                        depth += 1
                    elif text[j] == close:
                        depth -= 1
                    j += 1
                inner = text[i + 1 : j - 1] if depth == 0 else text[i + 1 : n]
                stripped = inner.strip()
                if ch == "[" and (stripped == "" or stripped == "*" or stripped.isdigit()):
                    # Índice ou flatten: o path continua sendo o mesmo array.
                    pass
                else:
                    # Multiselect, filtro ou argumentos de função: paths próprios.
                    _flush()
                    _walk(inner)
                i = j
            else:
                _flush()
                i += 1
        _flush()

    for branch in expr.split("||"):
        _walk(_STRING_LITERAL_RE.sub(" ", branch))
    return {p for p in found if p}


class _ConsumedIndex:
    """Decide se um path do raw já é lido pelo mapping.

    Substitui a comparação por CHAVE DE TOPO, que era a causa raiz de o Drift
    Explorer não listar campos aninhados. Antes, consumir ``data.win.system.eventID``
    marcava a top-key ``data`` inteira como conhecida e cegava TODO ``data.*`` —
    num alerta Wazuh típico isso suprimia 34 de 46 folhas. Pior: o TARGET OCSF
    também virava top-key, então criar a regra ``normalized.message`` escondia o
    campo ``message`` do raw, e cada regra nova aumentava a cegueira.

    Agora o casamento é por PATH:
      * exato — ``rule.level`` cobre ``rule.level``;
      * por prefixo — ``data`` (subárvore inteira, típico de passthrough) cobre
        ``data.win.x``, mas ``data.win`` NÃO cobre ``data.winlog``;
      * índices de array são normalizados — ``rule.mitre.id`` cobre
        ``rule.mitre.id[0]``.

    Fallback histórico: mapping cujo engine só devolveu ``normalized.<target>``
    (sem nenhum ``source:``) mantém a supressão por top-key. Sem isso, um mapping
    legado passaria a reportar o raw inteiro como drift no primeiro deploy.
    """

    __slots__ = ("_exact", "_legacy_top_keys", "_has_sources")

    def __init__(self, consumed_paths: Iterable[str]) -> None:
        self._exact: Set[str] = set()
        self._legacy_top_keys: Set[str] = set()
        self._has_sources = False

        targets: List[str] = []
        for cp in consumed_paths:
            if cp.startswith("source:"):
                expr = cp[len("source:") :]
                # Namespace virtual do preprocess ("source:_*"): não existe no raw.
                if expr.startswith("_"):
                    continue
                paths = _extract_source_paths(expr)
                if paths:
                    self._has_sources = True
                    self._exact.update(p.lower() for p in paths)
                continue
            if cp.startswith("_"):
                continue
            targets.append(cp)

        if not self._has_sources:
            # Só targets: sem informação de origem, conservador por top-key.
            for cp in targets:
                tail = cp[len("normalized.") :] if cp.startswith("normalized.") else cp
                head = tail.split(".", 1)[0].split("[", 1)[0]
                if head:
                    self._legacy_top_keys.add(head.lower())

    @staticmethod
    def _normalize(path: str) -> str:
        """Remove índices de array: ``rule.mitre.id[0].x`` -> ``rule.mitre.id.x``."""
        return _ARRAY_INDEX_RE.sub("", path).lower()

    def covers(self, path: str) -> bool:
        norm = self._normalize(path)
        if self._legacy_top_keys:
            head = norm.split(".", 1)[0]
            if head in self._legacy_top_keys:
                return True
        if norm in self._exact:
            return True
        # Prefixo: alguma origem consome a subárvore que contém este path?
        idx = norm.rfind(".")
        while idx > 0:
            norm = norm[:idx]
            if norm in self._exact:
                return True
            idx = norm.rfind(".")
        return False

    def is_empty(self) -> bool:
        return not self._exact and not self._legacy_top_keys


_ARRAY_INDEX_RE = re.compile(r"\[\d*\]")


# ── Janela de aprendizado (auto-discovery de fontes novas) ───────────────────
#
# Contador PROCESSO-LOCAL de quantos eventos já vimos por (org, vendor, event_type).
# Enquanto abaixo da janela, forçamos a captura de drift a 100% para que uma fonte
# recém-apontada (ex.: um agente começando a mandar syslog de FortiGate) apareça no
# Drift Explorer com o schema inteiro logo nos primeiros eventos — em vez de gotejar
# sob a amostragem estacionária. É best-effort/efêmero (reinicia com o processo):
# o dedupe real e a idempotência ficam no upsert de ``record_unknown_fields`` (o
# ``occurrence_count`` no DB é a fonte da verdade, não este contador).
_seen_counts: Dict[Tuple[Optional[int], str, str], int] = {}
# Teto de chaves rastreadas — impede crescimento ilimitado da memória sob muitas
# combinações (org × vendor × event_type). Cheio: chaves novas caem direto na
# amostragem (sem boost), degradação graciosa e sem custo.
_MAX_TRACKED_KEYS = 10_000


def should_capture(
    vendor: str,
    event_type: str,
    organization_id: Optional[int],
    sample_rate: float,
    *,
    learning_events: int = 0,
) -> bool:
    """Decide se ESTE evento alimenta a detecção de drift.

    Combina (a) a janela de aprendizado — 100% nos primeiros ``learning_events`` de
    uma combinação NOVA — com (b) a amostragem estacionária ``sample_rate``. Chamada
    no thread do event loop (single-threaded), então o contador dispensa lock.
    """
    if learning_events > 0:
        key = (organization_id, vendor, event_type)
        seen = _seen_counts.get(key)
        if seen is None:
            # Combinação nova: só começa a rastrear se ainda há orçamento de chaves.
            if len(_seen_counts) < _MAX_TRACKED_KEYS:
                _seen_counts[key] = 1
                return True
            # Mapa cheio — sem boost; cai na amostragem estacionária abaixo.
        elif seen < learning_events:
            _seen_counts[key] = seen + 1
            return True
        # Passou da janela: mantém a chave (barata) e cai na amostragem.
    return sample_rate > 0 and random.random() < sample_rate


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        # Antes de int — Python trata bool como int.
        return "bool"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "number"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _truncate_sample(value: Any) -> str:
    """Representação curta para a coluna ``sample_value``."""
    if value is None:
        return "null"
    if isinstance(value, _PRIMITIVE_TYPES):
        s = str(value)
    elif isinstance(value, list):
        s = f"[array len={len(value)}]"
    elif isinstance(value, dict):
        s = f"{{object keys={len(value)}}}"
    else:
        s = repr(value)
    if len(s) > _MAX_SAMPLE_VALUE_LEN:
        return s[: _MAX_SAMPLE_VALUE_LEN - 3] + "..."
    return s


def flatten_paths(
    obj: Any,
    *,
    prefix: str = "",
    out: Optional[List[Tuple[str, Any]]] = None,
    is_covered: Optional[Callable[[str], bool]] = None,
    depth: int = 0,
) -> List[Tuple[str, Any]]:
    """Devolve lista ``[(path, value), ...]`` para nodes folha do raw.

    Folhas = primitivos. Dicts viram intermediários (recursão). Listas
    são amostradas nos primeiros ``_MAX_LIST_SAMPLES`` índices para
    capturar o shape sem explodir em arrays grandes.

    A representação dos paths usa ``.`` para chaves dict e ``[i]`` para
    índices de array — alinhada com o que JMESPath compreende.

    ``is_covered`` é a PODA: recebe o path corrente e, devolvendo True, corta a
    subárvore inteira. Existe por dois motivos, não um só. O óbvio é custo. O
    que morde de verdade é o teto ``_MAX_PATHS_PER_EVENT``: sem poda ele era
    gasto enumerando folhas JÁ MAPEADAS, e num evento gordo (Sysmon via Wazuh,
    ~520 folhas sob ``data``) o orçamento acabava antes de chegar às chaves
    novas no fim do documento — o campo novo ficava invisível justamente nos
    payloads em que mais importa. Com a poda, o teto passa a valer só para
    folhas DESCONHECIDAS.

    ``depth`` guarda contra ``RecursionError`` em payload cíclico/patológico:
    o walk roda dentro do laço de coleta e uma exceção aqui derrubava o ciclo
    inteiro (``compute_unknown_paths`` é chamado FORA do try/except do
    ``record_unknown_fields``).
    """
    if out is None:
        out = []
    if len(out) >= _MAX_PATHS_PER_EVENT or depth > _MAX_DEPTH:
        return out
    if prefix and is_covered is not None and is_covered(prefix):
        return out

    if isinstance(obj, dict):
        if not obj:
            out.append((prefix or "{}", obj))
            return out
        for key, value in obj.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            flatten_paths(value, prefix=child_prefix, out=out, is_covered=is_covered, depth=depth + 1)
            if len(out) >= _MAX_PATHS_PER_EVENT:
                return out
        return out

    if isinstance(obj, list):
        if not obj:
            out.append((f"{prefix}[]" if prefix else "[]", obj))
            return out
        sample = obj[:_MAX_LIST_SAMPLES]
        for idx, value in enumerate(sample):
            child_prefix = f"{prefix}[{idx}]" if prefix else f"[{idx}]"
            flatten_paths(value, prefix=child_prefix, out=out, is_covered=is_covered, depth=depth + 1)
            if len(out) >= _MAX_PATHS_PER_EVENT:
                return out
        return out

    out.append((prefix, obj))
    return out


def compute_unknown_paths(
    raw: Mapping[str, Any],
    consumed_paths: Iterable[str],
    *,
    dsl_version: int = 1,
) -> List[Tuple[str, Any]]:
    """Diff entre paths do raw e paths consumidos pelo mapping.

    ``consumed_paths`` é o que ``MappingEngine.apply`` devolve em
    :class:`ApplyResult.consumed_paths`, e traz DOIS tipos de entrada:

      * ``source:<jmespath>`` — a expressão ORIGINAL lida do raw (preferida);
      * ``normalized.<target>`` — o target no envelope (fallback histórico).

    Enquanto houver ao menos um ``source:``, o casamento é por PATH (exato,
    por prefixo de subárvore e com índices de array normalizados). A comparação
    por CHAVE DE TOPO, que valia antes, só sobrevive para mappings que não
    produziram nenhum ``source:`` — ver :class:`_ConsumedIndex`.

    Isto muda o resultado de forma deliberada e grande: campos aninhados sob uma
    top-key parcialmente mapeada (``data.win.*``, ``rule.mitre.*`` num alerta
    Wazuh) passam a aparecer no Drift Explorer. Ver as notas de blast radius em
    ``routers/pipeline_health.py`` — ``mapped_field_ratio`` cai quando a
    detecção melhora, sem que nada tenha piorado no pipeline.

    Paths do namespace ``_`` são silenciosamente ignorados: entradas
    ``source:_*`` ou literais com prefixo ``_`` são campos virtuais
    produzidos por ops ``preprocess`` e não existem no raw.

    ``dsl_version`` é reservado para futura lógica v2-específica.
    Atualmente sem efeito comportamental.

    Retorna lista de pares (path, sample_value) para insert/upsert.
    """
    # dsl_version aceito para compatibilidade forward — sem uso hoje.
    _ = dsl_version

    index = _ConsumedIndex(consumed_paths)
    # A poda acontece DENTRO do walk: subárvore coberta nem é enumerada, então o
    # teto _MAX_PATHS_PER_EVENT passa a orçar apenas folhas desconhecidas.
    return flatten_paths(raw, is_covered=index.covers)


def record_unknown_fields(
    *,
    vendor: str,
    event_type: str,
    raw: Mapping[str, Any],
    consumed_paths: Iterable[str],
    organization_id: Optional[int] = None,
    dsl_version: int = 1,
) -> int:
    """Persiste campos desconhecidos via batch upsert (elimina N+1).

    Antes: N SELECTs individuais (um por campo desconhecido).
    Agora:  1 SELECT IN cobrindo todos os campos do evento + bulk add_all.

    ``dsl_version`` é repassado a ``compute_unknown_paths`` e reservado
    para futura lógica v2-específica. Sem efeito comportamental
    na versão atual.

    Devolve a quantidade de paths efetivamente registrados (inserts +
    updates). Falhas de DB são logadas e o pipeline segue.
    """
    deltas = compute_unknown_paths(raw, consumed_paths, dsl_version=dsl_version)
    if not deltas:
        return 0

    now = datetime.utcnow()
    paths_to_check = [p for p, _ in deltas]
    delta_map = {p: v for p, v in deltas}

    try:
        with database.SessionLocal() as db:
            # 1 SELECT IN em vez de N SELECTs individuais (elimina N+1).
            existing_rows = db.scalars(
                select(models.UnknownField).where(
                    models.UnknownField.vendor == vendor,
                    models.UnknownField.event_type == event_type,
                    models.UnknownField.field_path.in_(paths_to_check),
                    # dedupe/upsert escopado por tenant.
                    models.UnknownField.organization_id == organization_id,
                )
            ).all()
            existing_by_path = {row.field_path: row for row in existing_rows}

            written = 0
            new_rows: List[models.UnknownField] = []
            for path in paths_to_check:
                value = delta_map[path]
                if path in existing_by_path:
                    row = existing_by_path[path]
                    row.occurrence_count += 1
                    row.last_seen = now
                    # Não sobrescreve sample_value se já há um — primeiro
                    # exemplo costuma ser representativo, e o engenheiro
                    # já investigou se reapareceu.
                    written += 1
                else:
                    new_rows.append(
                        models.UnknownField(
                            vendor=vendor,
                            event_type=event_type,
                            field_path=path,
                            organization_id=organization_id,
                            sample_value=_truncate_sample(value),
                            sample_type=_type_name(value),
                            occurrence_count=1,
                            first_seen=now,
                            last_seen=now,
                            status="new",
                        )
                    )
                    written += 1

            if new_rows:
                db.add_all(new_rows)  # bulk insert — menos round-trips
            db.commit()
            return written
    except SQLAlchemyError as exc:
        logger.warning(
            "drift: falha ao registrar unknown_fields vendor=%s event_type=%s: %s",
            vendor, event_type, exc,
        )
        return 0
