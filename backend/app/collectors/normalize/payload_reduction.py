"""Redução declarativa do payload do vendor antes do dispatch (RF3.x, multi-destino).

Motivação
---------
O pipeline é multi-vendor e multi-destino. Destinos têm limites de tamanho
de mensagem distintos — o caso agudo é o Wazuh (``analysisd`` trunca
silenciosamente eventos > ~64 KiB; ``OS_MAXSTR`` = 65536), e o Wazuh Indexer
(OpenSearch) rejeita termos keyword > 32766 bytes. Detecções que embarcam
blobs grandes (ex.: ``processedData`` do Microsoft Graph dentro de uma
detecção Sophos XDR) estouram esses limites e perdem dados a jusante.

Separação Core × integração
----------------------------
Este módulo é **genérico**: ele apenas sabe como podar listas e strings num
``raw`` mantendo JSON válido. **QUAIS** campos podar é conhecimento do vendor
e fica declarado no mapping (DSL v2, bloco top-level ``raw_reduction``), nunca
no Core. Assim, lógica específica de vendor não vaza para o produto.

Contrato da DSL (``raw_reduction``)
-----------------------------------
Lista de specs, cada um::

    {"path": "processedData", "max_bytes": 16384}      # clipa string longa
    {"path": "mitreAttacks",  "max_items": 50}          # limita lista
    {"path": "rawData.raw",   "max_bytes": 16384}       # path dot-separado

- ``path`` (str, obrigatório): caminho dot-separado para o campo dentro do
  raw. Navega apenas dicts intermediários (não entra em listas).
- ``max_items`` (int > 0, opcional): se o alvo é lista e excede, mantém os
  N primeiros + um marcador de itens descartados.
- ``max_bytes`` (int > 0, opcional): se o alvo é string e excede (UTF-8),
  clipa para N bytes (decode tolerante). O JSON externo permanece válido —
  o valor é apenas uma string mais curta.
- Pelo menos um de ``max_items``/``max_bytes`` é obrigatório.

A redução é aplicada DEPOIS da normalização (o engine roda as regras sobre o
raw COMPLETO — fidelidade preservada), sobre uma cópia, e só afeta o ``raw``
embutido no envelope/dispatch. ``apply_raw_reduction`` retorna ``None`` quando
nada foi reduzido, para o caller reusar o raw original sem custo de cópia.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .exceptions import MappingDefinitionError

__all__ = [
    "CompiledReductionSpec",
    "compile_raw_reduction",
    "apply_raw_reduction",
]


@dataclass(frozen=True)
class CompiledReductionSpec:
    """Forma pré-validada de um item de ``raw_reduction``."""

    path_str: str
    path_parts: Tuple[str, ...]
    max_items: Optional[int]
    max_bytes: Optional[int]


def compile_raw_reduction(raw_reduction: Any) -> Tuple[CompiledReductionSpec, ...]:
    """Valida e compila o bloco ``raw_reduction`` da DSL v2.

    Returns:
        Tupla de :class:`CompiledReductionSpec`. Vazia se ``raw_reduction`` é
        ``None`` (campo opcional).

    Raises:
        MappingDefinitionError: shape inválido.
    """
    if raw_reduction is None:
        return ()
    if not isinstance(raw_reduction, (list, tuple)):
        raise MappingDefinitionError("'raw_reduction' deve ser uma lista de specs")

    compiled: list[CompiledReductionSpec] = []
    for idx, item in enumerate(raw_reduction):
        if not isinstance(item, Mapping):
            raise MappingDefinitionError(
                f"raw_reduction[{idx}]: item deve ser um objeto"
            )
        path = item.get("path")
        if not isinstance(path, str) or not path.strip():
            raise MappingDefinitionError(
                f"raw_reduction[{idx}]: 'path' obrigatório (string dot-separada)"
            )
        parts = tuple(p for p in path.split(".") if p)
        if not parts:
            raise MappingDefinitionError(
                f"raw_reduction[{idx}]: 'path' inválido {path!r}"
            )

        max_items = item.get("max_items")
        max_bytes = item.get("max_bytes")
        if max_items is None and max_bytes is None:
            raise MappingDefinitionError(
                f"raw_reduction[{idx}] {path!r}: defina 'max_items' e/ou 'max_bytes'"
            )
        for name, val in (("max_items", max_items), ("max_bytes", max_bytes)):
            if val is not None and (not isinstance(val, int) or isinstance(val, bool) or val <= 0):
                raise MappingDefinitionError(
                    f"raw_reduction[{idx}] {path!r}: '{name}' deve ser inteiro positivo"
                )

        compiled.append(
            CompiledReductionSpec(
                path_str=path,
                path_parts=parts,
                max_items=max_items,
                max_bytes=max_bytes,
            )
        )
    return tuple(compiled)


def _navigate_readonly(
    obj: Any, parts: Tuple[str, ...]
) -> Tuple[Optional[dict], Optional[str]]:
    """Navega ``parts[:-1]`` sobre dicts (somente leitura); retorna (parent_dict, last_key).

    Retorna (None, None) se o caminho intermediário não existe ou passa por
    algo que não é dict. Só navega dicts — não entra em listas.
    """
    cursor = obj
    for key in parts[:-1]:
        if not isinstance(cursor, dict) or key not in cursor:
            return None, None
        cursor = cursor[key]
    if not isinstance(cursor, dict):
        return None, None
    return cursor, parts[-1]


def _copy_path(root: Dict[str, Any], parts: Tuple[str, ...]) -> Tuple[Dict[str, Any], Optional[dict], Optional[str]]:
    """Copy-on-write: copia só os dicts ao longo de ``parts``.

    Retorna ``(new_root, parent_copy, last_key)`` onde ``new_root`` é uma
    cópia rasa do root com o caminho até ``parts[-2]`` também copiado
    (shallow em cada nível). ``parent_copy`` é o dict que contém
    ``last_key`` e pode ser mutado sem afetar o ``raw`` original.

    Segurança: apenas os dicts no caminho são copiados; folhas (str, list,
    int) são compartilhadas, mas substituímos a referência em ``parent_copy``
    em vez de mutá-las in-place, então o ``raw`` original permanece intocado.
    """
    # Primeiro nível: cópia rasa do root.
    new_root: Dict[str, Any] = dict(root)
    cursor: Dict[str, Any] = new_root

    for key in parts[:-1]:
        if not isinstance(cursor, dict) or key not in cursor:
            return new_root, None, None
        child = cursor[key]
        if not isinstance(child, dict):
            return new_root, None, None
        child_copy: Dict[str, Any] = dict(child)
        cursor[key] = child_copy
        cursor = child_copy

    last_key = parts[-1]
    if not isinstance(cursor, dict):
        return new_root, None, None
    return new_root, cursor, last_key


def apply_raw_reduction(
    raw: Mapping[str, Any],
    specs: Sequence[CompiledReductionSpec],
) -> Optional[Dict[str, Any]]:
    """Aplica as reduções a uma CÓPIA de ``raw``, mantendo JSON válido.

    Estratégia copy-on-write: navega o ``raw`` original em somente-leitura
    para detectar se alguma redução dispara; só então copia os dicts ao
    longo do caminho (``_copy_path``) e substitui a referência. Custo por
    spec: O(profundidade do path) em vez de O(total_nodes) do deepcopy.
    O raw original NUNCA é mutado — cada redução que dispara substitui a
    referência na cópia rasa do nó pai.

    Returns:
        Um novo dict reduzido se ALGUMA redução foi aplicada; ``None`` caso
        contrário (caller reusa o ``raw`` original sem custo de cópia).
    """
    if not specs or not isinstance(raw, Mapping):
        return None

    # Pré-triagem: verifica (somente leitura) quais specs realmente disparam.
    # Evita copiar qualquer coisa quando nada precisa ser reduzido.
    firing: list[CompiledReductionSpec] = []
    for spec in specs:
        parent_ro, key_ro = _navigate_readonly(raw, spec.path_parts)
        if parent_ro is None or key_ro is None or key_ro not in parent_ro:
            continue
        value = parent_ro[key_ro]
        if spec.max_items is not None and isinstance(value, list) and len(value) > spec.max_items:
            firing.append(spec)
        elif spec.max_bytes is not None and isinstance(value, str):
            if len(value.encode("utf-8")) > spec.max_bytes:
                firing.append(spec)

    if not firing:
        return None  # nada a fazer — sem cópia

    # Ao menos uma spec dispara: constrói o dict de saída com copy-on-write.
    # Cada _copy_path parte do ``obj`` (que evolui com as cópias anteriores),
    # criando cópias rasas apenas dos dicts no caminho da mutação.
    obj: Dict[str, Any] = dict(raw)  # cópia rasa do root
    dropped: list[str] = []

    for spec in firing:
        new_root, parent, key = _copy_path(obj, spec.path_parts)
        if parent is None or key is None or key not in parent:
            continue  # caminho sumiu após cópia anterior (não deve ocorrer)
        obj = new_root
        value = parent[key]

        if spec.max_items is not None and isinstance(value, list) and len(value) > spec.max_items:
            removed = len(value) - spec.max_items
            parent[key] = value[: spec.max_items]  # list slice = novo objeto
            dropped.append(f"{spec.path_str}[+{removed} items]")
        elif spec.max_bytes is not None and isinstance(value, str):
            encoded = value.encode("utf-8")
            if len(encoded) > spec.max_bytes:
                removed = len(encoded) - spec.max_bytes
                parent[key] = encoded[: spec.max_bytes].decode("utf-8", "ignore")
                dropped.append(f"{spec.path_str}(~{removed}B)")

    if not dropped:
        return None

    # Marcador de proveniência — o destino/analista sabe que o raw foi podado
    # para caber no limite de mensagem. Não colide com campos do vendor.
    obj["_centralops_reduced"] = dropped
    return obj
