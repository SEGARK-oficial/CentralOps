"""Inventário de caminhos que uma organização DE FATO produz (``normalized.*``).

Nasceu no router de enriquecimento para o campo ``key.source`` e virou serviço
porque a correlação precisa da MESMA lista: ``group_by_field`` e ``where.field``
de uma regra em voo são texto livre, resolvem da raiz do envelope, e um path
sem raiz (``source.ip``) nunca resolve — o match é contado e a Detection nunca
nasce. O seletor que impede isso no enriquecimento é este inventário. Dois
routers, uma fonte: se a ordenação ou a exclusão de constantes mudar, muda
para os dois.

Além dos caminhos mapeados, ``ENVELOPE_LABEL_PATHS`` expõe os rótulos
``_centralops.*`` que o roteamento aceita em condição — são os únicos campos
fora de ``normalized.*`` que uma regra costuma referenciar, e vêm da MESMA
allowlist do motor de rotas para não existir uma segunda lista à mão.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

from sqlalchemy.orm import Session

from ..collectors.normalize.envelope import ENVELOPE_ROOTS
from ..collectors.routing.engine import ALLOWED_FIELDS as _ROUTING_LABELS
from ..db import models

#: Raízes válidas de um path que resolve sobre o envelope entregue. Reexportado
#: para que quem valida ``group_by``/``where`` não precise conhecer o módulo
#: de envelope.
ROOTS: Tuple[str, ...] = tuple(ENVELOPE_ROOTS)

#: ``_centralops.<label>`` para cada rótulo que o roteamento aceita em condição.
#: Ordenado para a resposta ser estável.
ENVELOPE_LABEL_PATHS: Tuple[str, ...] = tuple(
    sorted(f"_centralops.{label}" for label in _ROUTING_LABELS)
)


#: Fallback quando a org ainda não conectou nada.
#:
#: **Verificado contra os mappings que o produto entrega**, não escrito de
#: memória do schema OCSF. A primeira versão desta lista tinha 8 de 16 caminhos
#: que NENHUM mapping escreve (`file.hash.sha256`, `file.name`, `url.text`,
#: `container.uid`...) — e o widget existe justamente para impedir que o
#: operador escolha um caminho morto. Sugerir um caminho inexistente aqui é
#: pior que não sugerir nada: vem com aparência de validado.
#:
#: O teste `test_fallback_so_tem_caminho_que_algum_mapping_escreve` compara
#: esta lista com os `target` reais e falha se algum fantasma voltar.
COMMON_OCSF_KEY_PATHS: Tuple[str, ...] = (
    # Rede
    "normalized.src_endpoint.ip",
    "normalized.dst_endpoint.ip",
    "normalized.device.ip",
    "normalized.device.hostname",
    "normalized.dst_endpoint.hostname",
    "normalized.device.mac",
    # Identidade
    "normalized.actor.user.name",
    "normalized.user.name",
    "normalized.process.user.name",
    "normalized.actor.user.uid",
    # Arquivo e processo (o hash real é `process.file.hashes`, não `file.hash.*`)
    "normalized.process.file.hashes",
    "normalized.process.file.name",
    "normalized.process.name",
    # Web
    "normalized.url.hostname",
    "normalized.url.url",
    # Ativo
    "normalized.device.uid",
)


def rule_can_be_key(rule: Dict[str, Any]) -> bool:
    """Uma regra de mapping cujo alvo serve de CHAVE de enriquecimento?

    Regra puramente CONSTANTE não serve: o valor é o mesmo em todo evento
    daquele vendor, então casar por ele é casar tudo ou nada. É o caso do Base
    Event obrigatório (``class_uid``, ``category_uid``, ``metadata.version``),
    que TODO mapping escreve — e que, por isso mesmo, encabeçava a lista quando
    a ordenação era só "quantos mappings escrevem".

    Função nomeada em vez de condição inline porque é a invariante que o teste
    trava; embutida no laço, uma remoção acidental passava despercebida.
    """
    return not ("const" in rule and not rule.get("source"))


def mapped_normalized_paths(db: Session, org_id: int) -> Dict[str, Dict[str, Any]]:
    """Caminhos ``normalized.*`` que os mappings ATIVOS desta org escrevem.

    Espelha o gating de ``mappings.list_definitions`` (``only_active``): o
    catálogo de mappings é global por ``(vendor, event_type)``, mas só o vendor
    que a org de fato conectou produz evento — sugerir os outros seria oferecer
    campo que nunca vai existir nos dados dela.

    Percorre regras escalares e ``array_builder``, que também escrevem em
    ``normalized.*`` (``engine.py:562``).
    """
    active_platforms = {
        p
        for (p,) in db.query(models.Integration.platform)
        .filter(
            models.Integration.is_active.is_(True),
            models.Integration.organization_id == org_id,
        )
        .distinct()
        .all()
        if p
    }
    if not active_platforms:
        return {}

    defs = (
        db.query(models.MappingDefinition)
        .filter(models.MappingDefinition.vendor.in_(active_platforms))
        .all()
    )

    found: Dict[str, Dict[str, Any]] = {}
    for definition in defs:
        version = definition.current_version
        if version is None:
            continue
        try:
            doc = json.loads(version.rules or "{}")
        except (TypeError, ValueError):
            # Versão ilegível é problema DAQUELE mapping, não desta lista.
            continue
        rules = doc.get("rules") if isinstance(doc, dict) else doc
        if not isinstance(rules, list):
            continue
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            target = rule.get("target")
            if not isinstance(target, str) or not target.startswith("normalized."):
                continue
            if not rule_can_be_key(rule):
                continue
            entry = found.setdefault(target, {"rule_count": 0, "vendors": set()})
            entry["rule_count"] += 1
            entry["vendors"].add(definition.vendor)
    return found


#: Tokens que denunciam um campo IDENTIFICADOR — o que serve de chave. Derivado
#: dos ``key_kinds`` que os enrichers aceitam (ip, domain, url, file_hash, cve,
#: mac, user, container_id), não de gosto pessoal.
KEYISH_TOKENS: Tuple[str, ...] = (
    "ip", "hostname", "mac", "hash", "uid", "user", "url", "domain",
    "name", "cve", "email", "container",
)


def key_relevance(path: str) -> int:
    """Menor = mais provável de ser a chave. Usado só para ORDENAR a lista.

    Ordenar por popularidade sozinha põe o Base Event no topo: o campo que
    TODOS os mappings escrevem é justamente o metadado constante. Quem abre o
    seletor quer `src_endpoint.ip`, não `category_uid`.
    """
    # `unmapped.*` PRIMEIRO: é o balde do que ninguém modelou. Um
    # `unmapped.src_ip` até é um IP, mas perde para o `src_endpoint.ip`
    # modelado — checar "parece chave" antes empataria os dois no topo.
    if ".unmapped." in path:
        return 2
    folha = path.rsplit(".", 1)[-1].lower()
    return 0 if any(tok in folha for tok in KEYISH_TOKENS) else 1
