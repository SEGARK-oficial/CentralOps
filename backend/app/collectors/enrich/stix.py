"""Helpers de STIX 2.1 compartilhados entre enrichers de threat intel.

Existe porque OpenCTI e TAXII entregam o MESMO formato: um Indicator STIX com um
`pattern` textual. Duplicar o parser nos dois enrichers garantiria divergência (um
ganharia um tipo de padrão que o outro não reconhece) e, pior, divergência
SILENCIOSA: um padrão não reconhecido vira zero linhas na tabela, não um erro.

O escopo é deliberadamente pequeno. Isto não é uma biblioteca STIX: é o mínimo
para transformar um Indicator numa linha de tabela de lookup.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Tuple

__all__ = [
    "STIX_TYPE_TO_KIND",
    "extract_observable_from_pattern",
    "is_expired",
    "parse_indicator",
]

#: Tipo STIX → ``key_kind`` da nossa DSL. Explícito porque um tipo não mapeado
#: deve ser IGNORADO, nunca adivinhado: indexar um ``email-addr`` como se fosse
#: ``domain`` produziria hit errado sem nenhum sinal de erro.
STIX_TYPE_TO_KIND: Mapping[str, str] = {
    "ipv4-addr": "ip",
    "ipv6-addr": "ip",
    "domain-name": "domain",
    "hostname": "domain",
    "url": "url",
    "file": "file_hash",
    "artifact": "file_hash",
    "mac-addr": "mac",
}

#: Padrão STIX de UM termo, que é o que sabemos casar contra um evento.
#: Cobre ``[ipv4-addr:value = '1.2.3.4']`` e ``[file:hashes.'SHA-256' = 'abc']``.
_SINGLE_TERM = re.compile(
    r"\s*\[\s*([a-z0-9-]+):(?:value|hashes\.'?[A-Za-z0-9-]+'?)\s*=\s*'([^']+)'\s*\]\s*"
)


def extract_observable_from_pattern(pattern: str) -> Optional[Tuple[str, str]]:
    """``(kind, valor)`` de um padrão STIX simples, ou ``None``.

    **Padrão composto (``AND``/``OR``) devolve ``None`` de propósito.** Casar um
    evento contra ele exigiria avaliar a expressão STIX inteira; avaliar só o
    primeiro termo daria hit errado em silêncio, que é pior que não casar. Quem
    precisa de expressão composta precisa de um motor de correlação, não de uma
    tabela de lookup.
    """
    m = _SINGLE_TERM.fullmatch(pattern or "")
    if not m:
        return None
    kind = STIX_TYPE_TO_KIND.get(m.group(1))
    if kind is None:
        return None
    return kind, m.group(2)


def is_expired(valid_until: Any, *, now: Optional[datetime] = None) -> bool:
    """O indicador passou da validade?

    Data ilegível devolve ``False``: descartar um indicador por causa de um campo
    mal formatado seria trocar um falso positivo por um falso NEGATIVO, e falso
    negativo em threat intel é o erro caro.
    """
    if not isinstance(valid_until, str) or not valid_until:
        return False
    try:
        exp = datetime.fromisoformat(valid_until.replace("Z", "+00:00"))
    except ValueError:
        return False
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp < (now or datetime.now(timezone.utc))


def parse_indicator(
    obj: Mapping[str, Any],
    *,
    min_confidence: int = 0,
    source_name: str = "taxii",
) -> Optional[Tuple[str, dict]]:
    """Indicator STIX 2.1 → ``(chave, linha)``, ou ``None`` para descartar.

    Descarta, nesta ordem: o que não é Indicator, o revogado, o expirado, o de
    confiança abaixo do piso, e o de padrão composto.

    Filtrar expirado e revogado AQUI, na carga, e não no evento, é a decisão que
    mais reduz falso positivo: intel vencida é a maior fonte deles num feed, e
    deixar isso para uma condição por regra significa depender de alguém lembrar
    de escrevê-la em toda regra nova.

    A chave sai em minúsculas porque a política declara ``normalize: ["lower"]``
    do lado do evento. As duas pontas TÊM que concordar: divergir aqui produz
    miss de 100% sem erro nenhum.
    """
    if obj.get("type") != "indicator":
        return None
    if obj.get("revoked") is True:
        return None
    if is_expired(obj.get("valid_until")):
        return None

    try:
        confidence = int(obj.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0
    if confidence < min_confidence:
        return None

    parsed = extract_observable_from_pattern(str(obj.get("pattern") or ""))
    if parsed is None:
        return None
    kind, value = parsed

    labels = [x for x in (obj.get("labels") or []) if isinstance(x, str)]
    phases = [
        p.get("phase_name")
        for p in (obj.get("kill_chain_phases") or [])
        if isinstance(p, Mapping) and p.get("phase_name")
    ]
    # `object_marking_refs` são ids (marking-definition--...), não os rótulos
    # TLP. Resolver exigiria buscar os objetos de marcação; expor o id cru seria
    # pior que omitir, então guardamos só a contagem como sinal de que há
    # marcação a respeitar.
    markings = obj.get("object_marking_refs") or []

    return value.strip().lower(), {
        "kind": kind,
        "stix_id": obj.get("id"),
        "indicator_name": obj.get("name"),
        "pattern_type": obj.get("pattern_type"),
        "confidence": confidence,
        "valid_from": obj.get("valid_from"),
        "valid_until": obj.get("valid_until"),
        "kill_chain_phases": phases,
        "labels": labels,
        "has_markings": bool(markings),
        "created": obj.get("created"),
        "modified": obj.get("modified"),
        "source": source_name,
    }
