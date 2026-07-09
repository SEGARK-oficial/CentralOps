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
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from ...db import database, models

logger = logging.getLogger(__name__)


_PRIMITIVE_TYPES = (str, int, float, bool, type(None))
_MAX_LIST_SAMPLES = 3  # quantos índices listamos em arrays
_MAX_PATHS_PER_EVENT = 500  # safety: evita explosão em payload patológico
_MAX_SAMPLE_VALUE_LEN = 200


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
    obj: Any, *, prefix: str = "", out: Optional[List[Tuple[str, Any]]] = None
) -> List[Tuple[str, Any]]:
    """Devolve lista ``[(path, value), ...]`` para nodes folha do raw.

    Folhas = primitivos. Dicts viram intermediários (recursão). Listas
    são amostradas nos primeiros ``_MAX_LIST_SAMPLES`` índices para
    capturar o shape sem explodir em arrays grandes.

    A representação dos paths usa ``.`` para chaves dict e ``[i]`` para
    índices de array — alinhada com o que JMESPath compreende.
    """
    if out is None:
        out = []
    if len(out) >= _MAX_PATHS_PER_EVENT:
        return out

    if isinstance(obj, dict):
        if not obj:
            out.append((prefix or "{}", obj))
            return out
        for key, value in obj.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            flatten_paths(value, prefix=child_prefix, out=out)
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
            flatten_paths(value, prefix=child_prefix, out=out)
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

    ``consumed_paths`` deve ser o conjunto que ``MappingEngine.apply``
    devolve em :class:`ApplyResult.consumed_paths` — embora aqueles
    sejam ``target`` paths (ex: ``"normalized.severity_id"``), não
    paths do raw. Evolução futura: a engine guarda a fonte original
    (JMESPath) também. Por enquanto consideramos como já consumido o
    "prefixo de chave de topo" do raw — abordagem conservadora para
    a 1ª iteração.

    Paths do namespace ``_`` são silenciosamente ignorados: entradas
    ``source:_*`` ou literais com prefixo ``_`` são campos virtuais
    produzidos por ops ``preprocess`` e não existem no raw.
    Ver docstring do módulo para detalhes.

    ``dsl_version`` é reservado para futura lógica v2-específica.
    Atualmente sem efeito comportamental.

    Retorna lista de pares (path, sample_value) para insert/upsert.
    """
    # dsl_version aceito para compatibilidade forward — sem uso hoje.
    _ = dsl_version

    consumed_top_keys: Set[str] = set()
    for cp in consumed_paths:
        # Engine devolve dois tipos de paths:
        # - "source:<jmespath>" — JMESPath original do source (preferido).
        # - "normalized.<target>" — target no envelope (fallback histórico).
        if cp.startswith("source:"):
            expr = cp[len("source:"):]
            # Namespace virtual do preprocess: "source:_*" — não é raw path.
            # Ignorar completamente sem tentar mapear para top-keys do raw.
            if expr.startswith("_"):
                continue
            # JMESPath: extrair top-keys de cada operando do `||` (OR).
            # Ex: "createdAt || raisedAt" → {"createdat", "raisedat"}.
            #     "managedAgent.name" → {"managedagent"}
            #     "[type]" → {"type"}
            for branch in expr.split("||"):
                # Remove brackets de array literal e espaços; pega o 1º segmento.
                token = branch.strip().lstrip("[").rstrip("]").strip()
                first = token.split(".", 1)[0].split("[", 1)[0].strip()
                if first:
                    consumed_top_keys.add(first.lower())
            continue

        # Caminho literal começando com "_": campo virtual do preprocess.
        # Não existe no raw — ignorar sem adicionar a consumed_top_keys.
        if cp.startswith("_"):
            continue

        if cp.startswith("normalized."):
            cp_tail = cp[len("normalized."):]
        else:
            cp_tail = cp
        first = cp_tail.split(".", 1)[0].split("[", 1)[0]
        if first:
            consumed_top_keys.add(first.lower())

    unknown: List[Tuple[str, Any]] = []
    for path, value in flatten_paths(raw):
        # Top-key do path (ex: "alert.threat.details[0].hash" → "alert").
        head = path.split(".", 1)[0].split("[", 1)[0].lower()
        if head in consumed_top_keys:
            continue
        unknown.append((path, value))
    return unknown


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
