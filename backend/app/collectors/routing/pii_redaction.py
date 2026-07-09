"""Redação de PII por ROTA (governança LGPD).

Motivação
---------
A minimização de dado (LGPD Art.6/Art.12): uma rota pode **mascarar/
pseudonimizar/remover** campos ANTES de entregá-los ao destino daquela rota — o
mesmo evento de origem chega **íntegro no lago** (rota sem redação) e
**mascarado no SIEM** (rota com redação).

Espelha o padrão declarativo de ``normalize/payload_reduction.py`` (compile/apply
+ navegação por path + marcador de proveniência), porém aplicado no **roteamento**
(per-route), não na normalização.

Contrato da DSL (coluna ``routes.pii_redaction``)
-------------------------------------------------
Um objeto ``{"version": 1, "rules": [...]}`` OU uma lista pura ``[...]`` (= v1).
Cada regra::

    {"path": "raw.user.email",     "action": "mask"}
    {"path": "raw.src.ip",         "action": "partial", "octets": 2}
    {"path": "normalized.actor",   "action": "hash", "salt": "..."}
    {"path": "raw.body.ssn",       "action": "drop_field"}

- ``path`` (str, OBRIGATÓRIO): dot-separado, ENRAIZADO em ``raw`` ou
  ``normalized`` APENAS. ``_centralops`` é PROIBIDO como alvo — carrega
  event_id/organization_id usados para idempotência, auditoria, lineage e
  apagamento; redigi-lo quebraria roteamento/dedupe/erasure.
- ``action`` ∈ ``{mask, hash, partial, drop_field}``.
  - ``mask``: substitui o valor INTEIRO (escalar ou subárvore) por um sentinela.
    ``fixed_len`` → largura constante (não vaza o tamanho original).
  - ``hash``: pseudonimização determinística ``"sha256:" + hex(salt+valor)`` —
    o destino correlaciona sem ver o valor. ``salt`` opcional (sem salt é
    reversível por dicionário em campos de baixa entropia — documentado).
  - ``partial``: revela uma fatia. Strings: ``keep_prefix``/``keep_suffix``.
    IP: ``octets`` (mantém os N primeiros grupos). FAIL-CLOSED: se fosse revelar
    tudo (ou tipo inesperado), cai para mask total.
  - ``drop_field``: REMOVE a chave (não vira null — null ainda denota existência).

A redação roda sobre uma CÓPIA profunda só do ramo mascarado (o ramo full
mantém a referência original — preserva byte-identidade do wazuh-default e do
lago). ``apply_pii_redaction`` retorna ``None`` quando nada mudou (caller reusa
o original sem custo de deepcopy).
"""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

__all__ = [
    "PiiRedactionError",
    "CompiledRedactionRule",
    "compile_pii_redaction",
    "validate_pii_redaction",
    "apply_pii_redaction",
    "ALLOWED_ROOTS",
    "ALLOWED_ACTIONS",
]

#: Raízes permitidas — só o corpo do evento. ``_centralops`` é blindado.
ALLOWED_ROOTS = frozenset({"raw", "normalized"})
ALLOWED_ACTIONS = frozenset({"mask", "hash", "partial", "drop_field"})
_MASK_SENTINEL = "[REDACTED]"


class PiiRedactionError(ValueError):
    """Spec de ``pii_redaction`` inválida (vira 422 na API de rotas)."""


@dataclass(frozen=True)
class CompiledRedactionRule:
    """Forma pré-validada de uma regra de redação."""

    path_str: str
    path_parts: Tuple[str, ...]
    action: str
    params: Mapping[str, Any]


def _extract_rules(spec: Any) -> list:
    if spec is None:
        return []
    if isinstance(spec, Mapping):
        rules = spec.get("rules", [])
    elif isinstance(spec, (list, tuple)):
        rules = spec
    else:
        raise PiiRedactionError("pii_redaction deve ser objeto {version,rules} ou lista")
    if not isinstance(rules, (list, tuple)):
        raise PiiRedactionError("pii_redaction.rules deve ser uma lista")
    return list(rules)


def compile_pii_redaction(spec: Any) -> Tuple[CompiledRedactionRule, ...]:
    """Valida e compila a spec de redação. Vazio quando ``spec`` é None/[].

    Raises:
        PiiRedactionError: shape inválido (path fora da allowlist, ação
        desconhecida, params inconsistentes).
    """
    compiled: list[CompiledRedactionRule] = []
    for idx, item in enumerate(_extract_rules(spec)):
        if not isinstance(item, Mapping):
            raise PiiRedactionError(f"pii_redaction[{idx}]: regra deve ser um objeto")

        path = item.get("path")
        if not isinstance(path, str) or not path.strip():
            raise PiiRedactionError(
                f"pii_redaction[{idx}]: 'path' obrigatório (string dot-separada)"
            )
        parts = tuple(p for p in path.split(".") if p)
        if not parts:
            raise PiiRedactionError(f"pii_redaction[{idx}]: 'path' inválido {path!r}")
        if parts[0] not in ALLOWED_ROOTS:
            raise PiiRedactionError(
                f"pii_redaction[{idx}] {path!r}: path deve começar com "
                f"{sorted(ALLOWED_ROOTS)} (não se redige _centralops)"
            )
        if len(parts) < 2:
            raise PiiRedactionError(
                f"pii_redaction[{idx}] {path!r}: aponte um campo dentro de "
                f"{parts[0]!r}, não a raiz inteira"
            )

        action = item.get("action")
        if action not in ALLOWED_ACTIONS:
            raise PiiRedactionError(
                f"pii_redaction[{idx}] {path!r}: 'action' deve ser "
                f"{sorted(ALLOWED_ACTIONS)}"
            )

        params = _validate_params(idx, path, action, item)
        compiled.append(
            CompiledRedactionRule(
                path_str=path, path_parts=parts, action=action, params=params
            )
        )
    return tuple(compiled)


def _validate_params(idx: int, path: str, action: str, item: Mapping[str, Any]) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    if action == "mask":
        fixed_len = item.get("fixed_len")
        if fixed_len is not None:
            if not isinstance(fixed_len, int) or isinstance(fixed_len, bool) or fixed_len <= 0:
                raise PiiRedactionError(
                    f"pii_redaction[{idx}] {path!r}: 'fixed_len' deve ser inteiro positivo"
                )
            params["fixed_len"] = fixed_len
        params["mask_char"] = _mask_char(idx, path, item)
    elif action == "hash":
        algo = item.get("algo", "sha256")
        if algo != "sha256":
            raise PiiRedactionError(
                f"pii_redaction[{idx}] {path!r}: 'algo' suportado apenas 'sha256' (v1)"
            )
        params["algo"] = algo
        salt = item.get("salt")
        if salt is not None:
            if not isinstance(salt, str):
                raise PiiRedactionError(f"pii_redaction[{idx}] {path!r}: 'salt' deve ser string")
            params["salt"] = salt
    elif action == "partial":
        octets = item.get("octets")
        kp = item.get("keep_prefix")
        ks = item.get("keep_suffix")
        if octets is not None:
            if not isinstance(octets, int) or isinstance(octets, bool) or octets <= 0:
                raise PiiRedactionError(
                    f"pii_redaction[{idx}] {path!r}: 'octets' deve ser inteiro positivo"
                )
            params["octets"] = octets
        else:
            kp = 0 if kp is None else kp
            ks = 0 if ks is None else ks
            for name, val in (("keep_prefix", kp), ("keep_suffix", ks)):
                if not isinstance(val, int) or isinstance(val, bool) or val < 0:
                    raise PiiRedactionError(
                        f"pii_redaction[{idx}] {path!r}: '{name}' deve ser inteiro >= 0"
                    )
            if kp == 0 and ks == 0:
                raise PiiRedactionError(
                    f"pii_redaction[{idx}] {path!r}: 'partial' exige octets, "
                    f"keep_prefix e/ou keep_suffix > 0"
                )
            params["keep_prefix"] = kp
            params["keep_suffix"] = ks
        params["mask_char"] = _mask_char(idx, path, item)
    # drop_field: sem params
    return params


def _mask_char(idx: int, path: str, item: Mapping[str, Any]) -> str:
    mc = item.get("mask_char", "*")
    if not isinstance(mc, str) or len(mc) != 1:
        raise PiiRedactionError(
            f"pii_redaction[{idx}] {path!r}: 'mask_char' deve ser 1 caractere"
        )
    return mc


def validate_pii_redaction(spec: Any) -> None:
    """Levanta ``PiiRedactionError`` (ValueError) se a spec é inválida — usado
    pela API de rotas para 422 no create/update."""
    compile_pii_redaction(spec)


def _navigate(obj: Any, parts: Tuple[str, ...]) -> Tuple[Optional[dict], Optional[str], str]:
    """Navega ``parts[:-1]`` sobre dicts. Retorna ``(parent, key, status)``:

    - ``"ok"``      → ``parent[key]`` é o folha a transformar (caminho 100% dict).
    - ``"blocked"`` → um segmento INTERMEDIÁRIO é um não-dict (ex.: lista) →
      ``parent[key]`` é esse valor não-navegável. PII pode estar DENTRO da lista,
      então o caller FAIL-CLOSED mascara a subárvore inteira (antes
      ``raw.users.email`` com ``users`` lista passava em CLARO).
    - ``"absent"``  → uma chave intermediária não existe → campo realmente ausente.
    """
    cursor = obj
    for key in parts[:-1]:
        if not isinstance(cursor, dict) or key not in cursor:
            return None, None, "absent"
        nxt = cursor[key]
        if not isinstance(nxt, dict):
            # intermediário não-dict (lista/escalar): não dá pra descer → bloqueado.
            return cursor, key, "blocked"
        cursor = nxt
    if not isinstance(cursor, dict) or parts[-1] not in cursor:
        return None, None, "absent"
    return cursor, parts[-1], "ok"


def _mask_value(rule: CompiledRedactionRule) -> str:
    fixed_len = rule.params.get("fixed_len")
    if fixed_len:
        return rule.params.get("mask_char", "*") * fixed_len
    return _MASK_SENTINEL


def _hash_value(rule: CompiledRedactionRule, value: Any) -> Tuple[Any, bool]:
    if isinstance(value, str) and value.startswith("sha256:"):
        return value, False  # idempotente: não re-hashear um pseudônimo já gerado
    salt = rule.params.get("salt", "")
    digest = hashlib.sha256((salt + str(value)).encode("utf-8")).hexdigest()
    return f"sha256:{digest}", True


def _partial_value(rule: CompiledRedactionRule, value: Any) -> Tuple[Any, bool]:
    mask_char = rule.params.get("mask_char", "*")
    octets = rule.params.get("octets")
    if octets is not None:
        if not isinstance(value, str):
            return _MASK_SENTINEL, True  # fail-closed
        sep = "." if "." in value else (":" if ":" in value else None)
        if sep is None:
            return _MASK_SENTINEL, True
        groups = value.split(sep)
        if octets >= len(groups):
            return _MASK_SENTINEL, True  # revelaria tudo → fail-closed
        kept = groups[:octets]
        masked = [mask_char for _ in groups[octets:]]
        return sep.join(kept + masked), True
    # keep_prefix / keep_suffix
    if not isinstance(value, str):
        return _MASK_SENTINEL, True
    kp = rule.params.get("keep_prefix", 0)
    ks = rule.params.get("keep_suffix", 0)
    if kp + ks >= len(value):
        return _MASK_SENTINEL, True  # revelaria tudo → fail-closed
    middle = mask_char * (len(value) - kp - ks)
    prefix = value[:kp] if kp else ""
    suffix = value[-ks:] if ks else ""
    return prefix + middle + suffix, True


def apply_pii_redaction(
    envelope: Mapping[str, Any],
    rules: Sequence[CompiledRedactionRule],
) -> Optional[Dict[str, Any]]:
    """Aplica a redação a uma CÓPIA profunda de ``envelope``.

    Returns:
        Novo dict redigido se ALGUMA regra mudou algo; ``None`` caso contrário
        (caller reusa o ``envelope`` original — sem deepcopy, preserva
        byte-identidade do ramo não-redigido).
    """
    if not rules or not isinstance(envelope, Mapping):
        return None

    # Copy-on-first-mutation (perf): NÃO deepcopy se nenhuma
    # regra resolve num campo presente — navega o original (read-only) p/ decidir;
    # só paga o deepcopy quando vai REALMENTE mascarar algo. Preserva a semântica
    # de "None quando nada mudou" (caller reusa o original — byte-idêntico).
    plan: list = []  # (rule, status) das regras que resolvem (ok|blocked)
    for rule in rules:
        _, _, status = _navigate(envelope, rule.path_parts)
        if status != "absent":
            plan.append((rule, status))
    if not plan:
        return None

    obj: Dict[str, Any] = copy.deepcopy(dict(envelope))
    applied: list[str] = []

    for rule, _status in plan:
        parent, key, status = _navigate(obj, rule.path_parts)
        if status == "absent" or parent is None or key is None:
            continue

        if status == "blocked":
            # FAIL-CLOSED: caminho entra numa lista (não navegável). Mascara a
            # subárvore inteira p/ não vazar PII escondida na lista.
            if rule.action == "drop_field":
                del parent[key]
                applied.append(f"{rule.path_str}:drop_field(blocked)")
            else:
                parent[key] = _MASK_SENTINEL
                applied.append(f"{rule.path_str}:mask(blocked-list)")
            continue

        if rule.action == "drop_field":
            del parent[key]
            applied.append(f"{rule.path_str}:drop_field")
            continue

        value = parent[key]
        if rule.action == "mask":
            new_value, changed = _mask_value(rule), True
        elif rule.action == "hash":
            new_value, changed = _hash_value(rule, value)
        elif rule.action == "partial":
            new_value, changed = _partial_value(rule, value)
        else:  # pragma: no cover — compile garante a allowlist
            continue

        if changed:
            parent[key] = new_value
            applied.append(f"{rule.path_str}:{rule.action}")

    if not applied:
        return None

    # Marcador de proveniência — o destino/analista sabe que a rota redigiu
    # campos. Não colide com _centralops_reduced (raw_reduction).
    obj["_centralops_redacted"] = applied
    return obj
