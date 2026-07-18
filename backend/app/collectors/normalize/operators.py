"""Operadores nomeados da DSL de mapping (RF3.4).

Cada operador transforma um valor já resolvido (via ``source`` JMESPath
ou ``const``). Operadores são puros: recebem entrada, retornam saída,
sem efeitos colaterais. Erros viram :class:`OperatorError`.

Operadores cobertos:

- ``type_cast``: ``iso_to_epoch``, ``epoch_to_iso``, ``to_str``,
  ``to_int``, ``to_bool``, ``score_to_percent``, ``lowercase``,
  ``uppercase``, ``trim``, ``to_array``, ``dedup``,
  ``mitre_tactic_to_ocsf``.
- ``value_map``: dict de lookup (case-insensitive nas chaves string).
- ``default``: valor de fallback se entrada é ``None``.

A engine aplica os operadores na ordem fixa: ``default`` → ``pre_cast``
→ ``value_map`` → ``type_cast``. Essa ordem importa porque um valor
faltando deve primeiro receber o fallback, ser normalizado pelo
``pre_cast`` opcional e só então ser mapeado/cast.

Adicionando um novo cast
-------------------------
Basta usar ``@register_type_cast`` com nome único, ``description`` e
``signature``. O cast fica disponível imediatamente na DSL e no endpoint
``GET /api/mappings/normalize/type-casts``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

# OperatorError e o registry são definidos em registry.py para evitar
# dependência circular (registry não depende de operators).
from .registry import OperatorError  # noqa: F401 (re-export para callers legados)
from .registry import TYPE_CASTS as _TYPE_CAST
from .registry import TYPE_CAST_DESCRIPTORS
from .registry import register_type_cast

__all__ = [
    "OperatorError",
    "apply_type_cast",
    "apply_value_map",
    "apply_default",
    "TYPE_CAST_DESCRIPTORS",
]


# ── type_cast — casts originais ────────────────────────────────────────


# ── timestamp_t (OCSF) — a unidade canônica é MILISSEGUNDOS ───────────
#
# O OCSF tipa ``timestamp_t`` como milissegundos desde a epoch Unix. Todo
# campo temporal numérico do envelope normalizado (``time``, ``start_time``,
# ``end_time``, ``finding_info.*_time``, ``process.created_time``, …) DEVE
# sair daqui em ms — um consumidor OCSF conforme que receba segundos lê os
# eventos como janeiro de 1970.
#
# Heurística segundos-vs-ms (``_EPOCH_MS_THRESHOLD``)
# ---------------------------------------------------
# Quando o vendor já entrega o campo NUMÉRICO não há como saber a unidade
# pelo tipo — CrowdStrike/NinjaOne mandam segundos, CloudWatch/Defender
# mandam ms. Usamos o limiar clássico de 1e11:
#
#   |v| <  1e11  → interpretado como SEGUNDOS  (→ multiplica por 1000)
#   |v| >= 1e11  → interpretado como MILISSEGUNDOS (passthrough)
#
# Por que 1e11 é seguro nas duas pontas:
#   - Como segundos, 1e11 s ≈ ano 5138. Qualquer epoch-em-segundos real
#     (passado ou futuro plausível) cai ABAIXO do limiar.
#   - Como ms, 1e11 ms ≈ 1973-03-03. Qualquer epoch-em-ms real de um evento
#     de segurança cai ACIMA do limiar.
# A única zona ambígua é ms anteriores a 1973-03-03, que não existem em
# telemetria de segurança. ``abs()`` mantém o comportamento simétrico para
# datas pré-1970 (epoch negativa).
_EPOCH_MS_THRESHOLD = 100_000_000_000  # 1e11


def _coerce_epoch_millis(value: int | float) -> int:
    """Normaliza um epoch numérico de unidade desconhecida para ms.

    Ver a nota de heurística acima. Frações são preservadas na conversão
    segundos→ms (``1776954130.5`` → ``1776954130500``).
    """
    if abs(value) < _EPOCH_MS_THRESHOLD:
        return int(round(value * 1000))
    return int(value)


@register_type_cast(
    "iso_to_epoch",
    description=(
        "Converte ISO-8601 (com ou sem Z) para epoch em MILISSEGUNDOS (int), "
        "conforme timestamp_t do OCSF. Entradas numéricas passam pela "
        "heurística segundos-vs-ms (|v| < 1e11 = segundos → ×1000)."
    ),
    signature="str|int|float → int (epoch ms)",
)
def _iso_to_epoch(value: Any) -> int:
    """ISO-8601 (com ou sem ``Z``) → epoch em MILISSEGUNDOS (int)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Vendor já entregou epoch numérico — unidade desconhecida.
        return _coerce_epoch_millis(value)
    if not isinstance(value, str):
        raise OperatorError(
            f"iso_to_epoch espera string ISO-8601, recebeu {type(value).__name__}"
        )
    normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise OperatorError(f"iso_to_epoch: timestamp inválido {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(round(dt.timestamp() * 1000))


@register_type_cast(
    "epoch_to_iso",
    description=(
        "Converte epoch para ISO-8601 com sufixo Z. Inverso de iso_to_epoch: "
        "usa a MESMA heurística segundos-vs-ms (|v| < 1e11 = segundos)."
    ),
    signature="int|float|str → str",
)
def _epoch_to_iso(value: Any) -> str:
    """Epoch (segundos ou ms, detectado pela heurística) → ISO-8601 ``Z``.

    Simétrico a :func:`_iso_to_epoch`: um round-trip
    ``epoch_to_iso(iso_to_epoch(x)) == x`` (na precisão de segundos).
    """
    if isinstance(value, str):
        # Best-effort: alguns vendors devolvem epoch como string.
        try:
            value = float(value)
        except ValueError as exc:
            raise OperatorError(
                f"epoch_to_iso: string não-numérica {value!r}"
            ) from exc
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise OperatorError(
            f"epoch_to_iso espera número, recebeu {type(value).__name__}"
        )
    millis = _coerce_epoch_millis(value)
    dt = datetime.fromtimestamp(millis / 1000.0, tz=timezone.utc)
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


@register_type_cast(
    "to_str",
    description="Converte qualquer valor para string. None não é aceito — use default antes.",
    signature="any (non-None) → str",
)
def _to_str(value: Any) -> str:
    if value is None:
        raise OperatorError("to_str não aceita None — use default antes")
    return str(value)


@register_type_cast(
    "to_int",
    description="Converte str/float para int. bool é rejeitado explicitamente.",
    signature="str|int|float → int",
)
def _to_int(value: Any) -> int:
    if isinstance(value, bool):
        # bool é subclasse de int em Python; tratar explicitamente para
        # não acabar com ``True → 1`` mascarando bug de mapping.
        raise OperatorError("to_int não aceita bool — converta explicitamente")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError as exc:
            raise OperatorError(f"to_int: string não-numérica {value!r}") from exc
    raise OperatorError(f"to_int não suporta {type(value).__name__}")


@register_type_cast(
    "to_bool",
    description="Converte str/int/float para bool. Strings ambíguas levantam OperatorError.",
    signature="str|int|float|bool → bool",
)
def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y"}:
            return True
        if normalized in {"false", "0", "no", "n", ""}:
            return False
        raise OperatorError(f"to_bool: string ambígua {value!r}")
    raise OperatorError(f"to_bool não suporta {type(value).__name__}")


# ── type_cast — novos casts (Fase 1.1) ────────────────────────────────


@register_type_cast(
    "score_to_percent",
    description="Converte float [0,1] para int [0,100] (arredondado). Idempotente para int em [0,100]. None passa direto.",
    signature="float[0..1]|int[0..100] → int[0..100] | None → None",
)
def _score_to_percent(value: Any) -> Any:
    """float [0,1] → int [0,100].

    - None passa direto.
    - int já em [0,100] passa idempotente.
    - float é multiplicado por 100 e arredondado.
    - Valores fora de range levantam OperatorError.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise OperatorError("score_to_percent não aceita bool")
    if isinstance(value, int):
        if 0 <= value <= 100:
            return value
        raise OperatorError(
            f"score_to_percent: int {value!r} fora do intervalo [0, 100]"
        )
    if isinstance(value, float):
        if not (0.0 <= value <= 1.0):
            raise OperatorError(
                f"score_to_percent: float {value!r} fora do intervalo [0.0, 1.0]"
            )
        return round(value * 100)
    raise OperatorError(
        f"score_to_percent não suporta {type(value).__name__}"
    )


@register_type_cast(
    "lowercase",
    description="Converte string para minúsculas. None passa direto. Non-string levanta OperatorError.",
    signature="str → str | None → None",
)
def _lowercase(value: Any) -> Any:
    if value is None:
        return None
    if not isinstance(value, str):
        raise OperatorError(
            f"lowercase espera str, recebeu {type(value).__name__}"
        )
    return value.lower()


@register_type_cast(
    "uppercase",
    description="Converte string para maiúsculas. None passa direto. Non-string levanta OperatorError.",
    signature="str → str | None → None",
)
def _uppercase(value: Any) -> Any:
    if value is None:
        return None
    if not isinstance(value, str):
        raise OperatorError(
            f"uppercase espera str, recebeu {type(value).__name__}"
        )
    return value.upper()


@register_type_cast(
    "trim",
    description="Remove espaços no início e no fim da string. None passa direto. Non-string levanta OperatorError.",
    signature="str → str | None → None",
)
def _trim(value: Any) -> Any:
    if value is None:
        return None
    if not isinstance(value, str):
        raise OperatorError(
            f"trim espera str, recebeu {type(value).__name__}"
        )
    return value.strip()


@register_type_cast(
    "to_array",
    description="Encapsula scalar em lista. Lista passa direto. None retorna lista vazia.",
    signature="T → [T] | list[T] → list[T] | None → []",
)
def _to_array(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


@register_type_cast(
    "dedup",
    description="Remove duplicatas de lista preservando a ordem de primeira ocorrência. None passa direto. Non-list levanta OperatorError.",
    signature="list[T] → list[T] | None → None",
)
def _dedup(value: Any) -> Any:
    if value is None:
        return None
    if not isinstance(value, list):
        raise OperatorError(
            f"dedup espera list, recebeu {type(value).__name__}"
        )
    seen: set[Any] = set()
    result: list[Any] = []
    for item in value:
        # Itens unhashable (ex: dict) são tratados por id para não quebrar;
        # duplicatas exatas de dicts não são deduplicadas (comportamento
        # documentado: dedup é para tipos simples como str/int).
        try:
            if item not in seen:
                seen.add(item)
                result.append(item)
        except TypeError:
            # Unhashable: append sem dedup (ex: lista de dicts).
            result.append(item)
    return result


# ── mitre_tactic_to_ocsf ──────────────────────────────────────────────
#
# Sophos shape (entrada):
#   [{"tactic": {"id": "TA0001", "name": "Initial Access"}}, ...]
#
# OCSF shape (saída):
#   [{"tactics": [{"uid": "TA0001", "name": "Initial Access"}], "version": "16.1"}, ...]
#
# Validação estrita: cada entrada deve ser um dict com chave "tactic"
# cujo valor é um dict com "id" e "name" (ambos str).  Entradas com
# "tactic" = None são filtradas silenciosamente (Sophos às vezes emite
# táticas nulas em alertas de baixa fidelidade).


@register_type_cast(
    "mitre_tactic_to_ocsf",
    description="Converte lista de táticas MITRE no formato Sophos para o formato OCSF 16.1. None passa direto.",
    signature="list[{tactic:{id,name}}] → list[{tactics:[{uid,name}],version:'16.1'}] | None → None",
)
def _mitre_tactic_to_ocsf(value: Any) -> Any:
    """Transforma shape MITRE Sophos → OCSF.

    Args:
        value: None (passthrough) ou lista de dicts no formato Sophos.

    Returns:
        None se entrada for None; lista OCSF caso contrário.

    Raises:
        OperatorError: Se entrada não for lista, ou se algum item não
            corresponder ao schema esperado.
    """
    if value is None:
        return None
    if not isinstance(value, list):
        raise OperatorError(
            f"mitre_tactic_to_ocsf espera list, recebeu {type(value).__name__}"
        )
    if len(value) == 0:
        return []

    result: list[dict[str, Any]] = []
    for idx, item in enumerate(value):
        if not isinstance(item, dict):
            raise OperatorError(
                f"mitre_tactic_to_ocsf: item #{idx} deve ser dict, "
                f"recebeu {type(item).__name__}"
            )
        if "tactic" not in item:
            raise OperatorError(
                f"mitre_tactic_to_ocsf: item #{idx} não possui chave 'tactic'"
            )
        tactic = item["tactic"]
        # Tática nula → filtrar silenciosamente.
        if tactic is None:
            continue
        if not isinstance(tactic, dict):
            raise OperatorError(
                f"mitre_tactic_to_ocsf: item #{idx}.tactic deve ser dict ou None, "
                f"recebeu {type(tactic).__name__}"
            )
        tactic_id = tactic.get("id")
        tactic_name = tactic.get("name")
        if not isinstance(tactic_id, str) or not tactic_id:
            raise OperatorError(
                f"mitre_tactic_to_ocsf: item #{idx}.tactic.id deve ser string não-vazia, "
                f"recebeu {tactic_id!r}"
            )
        if not isinstance(tactic_name, str) or not tactic_name:
            raise OperatorError(
                f"mitre_tactic_to_ocsf: item #{idx}.tactic.name deve ser string não-vazia, "
                f"recebeu {tactic_name!r}"
            )
        result.append({
            "tactics": [{"uid": tactic_id, "name": tactic_name}],
            "version": "16.1",
        })
    return result


# ── apply_type_cast ────────────────────────────────────────────────────


def apply_type_cast(value: Any, cast_name: str) -> Any:
    """Aplica o cast nomeado. Levanta ``OperatorError`` se desconhecido."""
    fn = _TYPE_CAST.get(cast_name)
    if fn is None:
        raise OperatorError(
            f"type_cast desconhecido: {cast_name!r}. "
            f"Suportados: {sorted(_TYPE_CAST.keys())}"
        )
    return fn(value)


# ── value_map ─────────────────────────────────────────────────────────

def apply_value_map(value: Any, mapping: Mapping[Any, Any]) -> Any:
    """Lookup case-insensitive para chaves string; literal para o resto.

    Se a chave não está no mapping, devolve o valor original — quem
    chama decide se isso é erro (com ``required``) ou aceitável.

    Tolerância int/str:
        Quando o valor não é string e a lookup exata falha, o operador
        tenta ``str(value)`` como chave de fallback antes de devolver o
        passthrough.  Isso cobre o caso real do Sophos, onde ``severity``
        chega como ``int`` mas o ``value_map`` usa chaves de string
        (ex. ``"3": "high"``).

        A direção inversa (str → int) **não** é tentada — manter a
        assimetria torna o comportamento previsível.

        Exemplos::

            apply_value_map(3, {"3": "high"})          # → "high"
            apply_value_map(3, {3: "a", "3": "b"})     # → "a"  (exact wins)
            apply_value_map(None, {"3": "high"})        # → None (passthrough)
            apply_value_map(3.0, {"3.0": "x"})         # → "x"  (str(3.0))
    """
    if not isinstance(mapping, Mapping):
        raise OperatorError(
            f"value_map espera dict, recebeu {type(mapping).__name__}"
        )
    if isinstance(value, str):
        key = value.lower()
        # Construímos um dict normalizado on-the-fly. Para mappings
        # frequentes vale cachear via engine; aqui mantemos puro.
        for k, v in mapping.items():
            if isinstance(k, str) and k.lower() == key:
                return v
        return value
    # Lookup exata primeiro.
    _sentinel = object()
    result = mapping.get(value, _sentinel)
    if result is not _sentinel:
        return result
    # Fallback: tenta str(value) quando a chave exata não existe.
    if value is not None:
        result = mapping.get(str(value), _sentinel)
        if result is not _sentinel:
            return result
    return value


# ── default ───────────────────────────────────────────────────────────

def apply_default(value: Any, default: Any) -> Any:
    """Devolve ``default`` se ``value`` é ``None``."""
    return default if value is None else value
