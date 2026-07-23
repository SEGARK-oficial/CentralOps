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
    {"path": "rawData.lineage", "drop": true}           # REMOVE a chave
    {"path": "rawData", "keep_only": ["a", "b"]}        # mantém só estes filhos
    {"drop_nulls": true}                                # remove chaves nulas

- ``path`` (str): caminho dot-separado para o campo dentro do raw. Obrigatório
  em todas as ops EXCETO ``drop_nulls``, que é global. Um segmento ``[]``
  aplica a op a CADA item de uma lista (ex.: ``alerts[].evidences``).
- ``max_items`` (int > 0): se o alvo é lista e excede, mantém os N primeiros.
- ``max_bytes`` (int > 0): se o alvo é string e excede (UTF-8), clipa para N
  bytes (decode tolerante). O JSON externo permanece válido.
- ``drop`` (bool): REMOVE a chave inteira. É a primitiva que faltava — o
  equivalente ao ``del()`` do Vector, ao "Remove fields" do Cribl e ao ``drop``
  do Tenzir. Use para o lixo que sobra depois da extração (um
  ``rawData.lineage`` já parseado não serve a ninguém).
- ``keep_only`` (lista de str): keep-list — remove os IRMÃOS não listados sob
  ``path``. Equivale ao ``Allowlist_key`` do Fluent Bit. Mais robusto que
  enumerar drops quando o vendor adiciona campos novos.
- ``drop_nulls`` (bool, spec sem ``path``): remove recursivamente toda chave de
  valor ``None`` do raw inteiro.
- Pelo menos uma op é obrigatória por spec.

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
    "LIST_WILDCARD",
]

#: Segmento de path que significa "para CADA item desta lista".
LIST_WILDCARD = "[]"

#: Sentinela devolvida por uma leaf-op que pede a REMOÇÃO da chave.
_REMOVE = object()


@dataclass(frozen=True)
class CompiledReductionSpec:
    """Forma pré-validada de um item de ``raw_reduction``."""

    path_str: str
    path_parts: Tuple[str, ...]
    max_items: Optional[int]
    max_bytes: Optional[int]
    #: remove a chave inteira (primitiva de DROP).
    drop: bool = False
    #: keep-list: sob ``path``, remove os irmãos que não estejam aqui.
    keep_only: Optional[Tuple[str, ...]] = None
    #: spec GLOBAL (sem path): remove recursivamente chaves de valor None.
    drop_nulls: bool = False

    @property
    def is_global(self) -> bool:
        """True para specs que não têm alvo (hoje só ``drop_nulls``)."""
        return self.drop_nulls and not self.path_parts


def _split_path(path: str) -> Tuple[str, ...]:
    """``"alerts[].evidences"`` -> ``("alerts", "[]", "evidences")``.

    O sufixo ``[]`` vira um segmento próprio para :func:`_transform` distribuir
    o resto do path por cada item da lista. Sem essa separação o segmento
    literal ``"alerts[]"`` nunca casaria uma chave do documento.
    """
    parts: list[str] = []
    for segment in path.split("."):
        if not segment:
            continue
        while segment.endswith(LIST_WILDCARD):
            segment = segment[: -len(LIST_WILDCARD)]
            if segment:
                parts.append(segment)
            parts.append(LIST_WILDCARD)
            segment = ""
        if segment:
            parts.append(segment)
    return tuple(parts)


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
        drop_nulls = bool(item.get("drop_nulls") or False)
        path = item.get("path")

        # ``drop_nulls`` é a única op GLOBAL: vale para o raw inteiro e
        # dispensa ``path``.
        if drop_nulls and path is None:
            compiled.append(
                CompiledReductionSpec(
                    path_str="", path_parts=(), max_items=None, max_bytes=None,
                    drop_nulls=True,
                )
            )
            continue

        if not isinstance(path, str) or not path.strip():
            raise MappingDefinitionError(
                f"raw_reduction[{idx}]: 'path' obrigatório (string dot-separada)"
            )
        parts = _split_path(path)
        if not parts:
            raise MappingDefinitionError(
                f"raw_reduction[{idx}]: 'path' inválido {path!r}"
            )

        max_items = item.get("max_items")
        max_bytes = item.get("max_bytes")
        drop = bool(item.get("drop") or False)
        keep_only_raw = item.get("keep_only")

        keep_only: Optional[Tuple[str, ...]] = None
        if keep_only_raw is not None:
            if not isinstance(keep_only_raw, (list, tuple)) or not all(
                isinstance(k, str) and k for k in keep_only_raw
            ):
                raise MappingDefinitionError(
                    f"raw_reduction[{idx}] {path!r}: 'keep_only' deve ser lista de strings não-vazias"
                )
            keep_only = tuple(keep_only_raw)

        if max_items is None and max_bytes is None and not drop and keep_only is None and not drop_nulls:
            raise MappingDefinitionError(
                f"raw_reduction[{idx}] {path!r}: defina ao menos uma op "
                "('max_items', 'max_bytes', 'drop', 'keep_only' ou 'drop_nulls')"
            )
        # ``drop`` remove a chave inteira: qualquer outra op sobre o MESMO path
        # seria inalcançável. Erro explícito em vez de precedência silenciosa.
        if drop and (max_items is not None or max_bytes is not None or keep_only is not None):
            raise MappingDefinitionError(
                f"raw_reduction[{idx}] {path!r}: 'drop' é exclusivo — remove a chave, "
                "então combinar com max_items/max_bytes/keep_only não faz sentido"
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
                drop=drop,
                keep_only=keep_only,
                drop_nulls=drop_nulls,
            )
        )
    return tuple(compiled)


def _transform(node: Any, parts: Tuple[str, ...], leaf_op) -> Tuple[Any, bool]:
    """Reescreve ``node`` aplicando ``leaf_op`` no fim de ``parts``.

    Copy-on-write ESTRUTURAL: devolve ``(novo_node, mudou)``. Quando nada muda,
    devolve a REFERÊNCIA ORIGINAL — subárvores intocadas são compartilhadas e o
    raw original nunca é mutado. O custo é proporcional ao que mudou, não ao
    tamanho do documento (nada de deepcopy).

    Suporta o segmento ``[]``, que distribui o resto do path por cada item de
    uma lista — sem isso, blobs dentro de arrays (``alerts[].evidences``) ficam
    inalcançáveis, que era a limitação da versão anterior.

    ``leaf_op(value) -> novo_valor`` devolve :data:`_REMOVE` para apagar a
    chave, ou o próprio ``value`` quando não há o que fazer.
    """
    if not parts:
        return node, False

    head, rest = parts[0], parts[1:]

    if head == LIST_WILDCARD:
        if not isinstance(node, list):
            return node, False
        out: list = []
        changed = False
        for item in node:
            new_item, item_changed = _transform(item, rest, leaf_op)
            out.append(new_item)
            changed = changed or item_changed
        return (out, True) if changed else (node, False)

    if not isinstance(node, dict) or head not in node:
        return node, False

    if not rest:
        new_value = leaf_op(node[head])
        if new_value is node[head]:
            return node, False  # no-op: preserva a referência
        copy = dict(node)
        if new_value is _REMOVE:
            copy.pop(head, None)
        else:
            copy[head] = new_value
        return copy, True

    child, child_changed = _transform(node[head], rest, leaf_op)
    if not child_changed:
        return node, False
    copy = dict(node)
    copy[head] = child
    return copy, True


def _prune_nulls(node: Any) -> Tuple[Any, bool]:
    """Remove recursivamente chaves de valor ``None``. Mesmo contrato
    copy-on-write de :func:`_transform`."""
    if isinstance(node, dict):
        out: Dict[str, Any] = {}
        changed = False
        for key, value in node.items():
            if value is None:
                changed = True
                continue
            new_value, value_changed = _prune_nulls(value)
            out[key] = new_value
            changed = changed or value_changed
        return (out, True) if changed else (node, False)
    if isinstance(node, list):
        out_list: list = []
        changed = False
        for item in node:
            new_item, item_changed = _prune_nulls(item)
            out_list.append(new_item)
            changed = changed or item_changed
        return (out_list, True) if changed else (node, False)
    return node, False


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

    obj: Any = raw
    dropped: list[str] = []

    for spec in specs:
        # ── op GLOBAL: drop_nulls sem path ──────────────────────────────
        if spec.is_global:
            new_obj, changed = _prune_nulls(obj)
            if changed:
                obj = new_obj
                dropped.append("nulls")
            continue

        # ── ops com alvo: a leaf-op decide o novo valor da chave ────────
        marks: list[str] = []

        def _leaf(value: Any, _spec: CompiledReductionSpec = spec, _marks: list = marks) -> Any:
            if _spec.drop:
                _marks.append(f"{_spec.path_str}(dropped)")
                return _REMOVE
            if _spec.keep_only is not None and isinstance(value, Mapping):
                kept = {k: v for k, v in value.items() if k in _spec.keep_only}
                if len(kept) == len(value):
                    return value  # nada removido — preserva a referência
                _marks.append(f"{_spec.path_str}(kept {len(kept)}/{len(value)})")
                return kept
            if _spec.max_items is not None and isinstance(value, list):
                if len(value) > _spec.max_items:
                    _marks.append(f"{_spec.path_str}[+{len(value) - _spec.max_items} items]")
                    return value[: _spec.max_items]
                return value
            if _spec.max_bytes is not None and isinstance(value, str):
                encoded = value.encode("utf-8")
                if len(encoded) > _spec.max_bytes:
                    _marks.append(f"{_spec.path_str}(~{len(encoded) - _spec.max_bytes}B)")
                    return encoded[: _spec.max_bytes].decode("utf-8", "ignore")
                return value
            return value

        new_obj, changed = _transform(obj, spec.path_parts, _leaf)
        if changed:
            obj = new_obj
            dropped.extend(marks)

    if not dropped or obj is raw:
        return None

    # Marcador de proveniência — o destino/analista sabe que o raw foi podado.
    # Não colide com campos do vendor. Escrito numa cópia rasa: `obj` pode ser
    # o próprio `raw` se só ops no-op rodaram (guardado acima).
    out: Dict[str, Any] = dict(obj)
    out["_centralops_reduced"] = dropped
    return out
