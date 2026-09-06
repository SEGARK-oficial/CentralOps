"""Pacotes de regras embarcadas — o primeiro é **IOC → Detection** (W4.7).

CTI, detecção em voo e roteamento são um circuito, não três telas: a política
de enriquecimento marca o evento (``tags: ["ioc:ip"]``), a regra em voo
transforma a marca em ``Detection`` e a rota manda só o match ao SIEM. Este
módulo entrega a peça do meio pronta: três regras ``inflight`` sobre
``_centralops.enrichment_tags``, **desabilitadas** por padrão — ligar é decisão
do cliente, porque muda o que chega ao SIEM (política da ADR-0015 §7).

**Convenção de tags** (o contrato entre a política e o pacote):

=============  ======================================================
``ioc:ip``     endereço casou uma lista de bloqueio (table_cidr, TAXII…)
``ioc:hash``   hash de arquivo casou um indicador
``ioc:domain`` domínio/URL casou um indicador
=============  ======================================================

**Instalação idempotente e sem ressurreição.** O pacote é instalado uma vez
por organização e o nome do pacote fica em ``Organization.rule_packs_installed``.
Apagar uma regra do pacote NÃO a traz de volta no próximo boot — o marcador
diz "já instalei", não "estas regras existem". ``template_key`` na regra
identifica a origem para a UI e para pacotes futuros.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

IOC_PACK = "ioc-v1"

#: Cada item vira uma ``CorrelationRule`` ``eval_mode='inflight'``. ``where`` usa
#: o vocabulário único de operadores (``routing.engine.compare_values``):
#: ``contains`` sobre a lista de tags é substring do repr — por isso as tags
#: não podem ser prefixo umas das outras.
IOC_RULES: Sequence[Mapping[str, Any]] = (
    {
        "template_key": "ioc.ip_blocklist",
        "name": "[IOC] Endereço em lista de bloqueio",
        "description": (
            "Dispara quando o enriquecimento marcou o evento com a tag ioc:ip "
            "(IP de origem casou uma lista de bloqueio ou um indicador de ameaça). "
            "Agrupa por IP de origem. Regra do pacote IOC — desabilitada por padrão; "
            "habilite depois de conferir a política de enriquecimento."
        ),
        "severity_id": 4,
        "group_by_field": "normalized.src_endpoint.ip",
        "where": [{"field": "_centralops.enrichment_tags", "op": "contains", "value": "ioc:ip"}],
    },
    {
        "template_key": "ioc.malicious_hash",
        "name": "[IOC] Hash de arquivo malicioso",
        "description": (
            "Dispara quando o enriquecimento marcou o evento com a tag ioc:hash "
            "(hash de arquivo casou um indicador). Agrupa pelo host afetado. "
            "Regra do pacote IOC — desabilitada por padrão."
        ),
        "severity_id": 5,
        "group_by_field": "normalized.device.hostname",
        "where": [{"field": "_centralops.enrichment_tags", "op": "contains", "value": "ioc:hash"}],
    },
    {
        "template_key": "ioc.c2_domain",
        "name": "[IOC] Domínio de comando e controle",
        "description": (
            "Dispara quando o enriquecimento marcou o evento com a tag ioc:domain "
            "(domínio ou URL casou um indicador). Agrupa pelo host afetado. "
            "Regra do pacote IOC — desabilitada por padrão."
        ),
        "severity_id": 4,
        "group_by_field": "normalized.device.hostname",
        "where": [{"field": "_centralops.enrichment_tags", "op": "contains", "value": "ioc:domain"}],
    },
)

PACKS: Mapping[str, Sequence[Mapping[str, Any]]] = {IOC_PACK: IOC_RULES}


def installed_packs(org: Any) -> List[str]:
    raw = getattr(org, "rule_packs_installed", None)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(x) for x in data] if isinstance(data, list) else []


def install_pack(db: Any, org: Any, pack: str = IOC_PACK) -> int:
    """Instala ``pack`` na org se ainda não foi instalado. Devolve quantas regras
    criou (0 = já instalado). NÃO faz commit — o chamador decide."""
    from ...db import models

    rules = PACKS.get(pack)
    if rules is None:
        raise KeyError(f"pacote desconhecido: {pack!r}")
    already = installed_packs(org)
    if pack in already:
        return 0

    created = 0
    for spec in rules:
        exists = (
            db.query(models.CorrelationRule.id)
            .filter(
                models.CorrelationRule.organization_id == org.id,
                models.CorrelationRule.template_key == spec["template_key"],
            )
            .first()
        )
        if exists is not None:
            continue
        db.add(
            models.CorrelationRule(
                organization_id=org.id,
                name=spec["name"],
                description=spec["description"],
                enabled=False,
                severity_id=int(spec["severity_id"]),
                rule_type="threshold",
                eval_mode="inflight",
                emit_event=False,
                group_by_field=spec["group_by_field"],
                where_json=json.dumps(list(spec["where"]), separators=(",", ":")),
                template_key=spec["template_key"],
            )
        )
        created += 1
    org.rule_packs_installed = json.dumps(sorted({*already, pack}))
    db.flush()
    logger.info(
        "rule_pack: %r instalado na org %s (%d regra(s), desabilitadas)",
        pack, org.id, created, extra={"event": "rule_pack.installed", "pack": pack, "org_id": org.id},
    )
    return created


def install_pack_for_all_orgs(db: Any, pack: str = IOC_PACK) -> int:
    """Boot: instala o pacote nas orgs que ainda não o têm. Idempotente."""
    from ...db import models

    total = 0
    for org in db.query(models.Organization).all():
        try:
            total += install_pack(db, org, pack)
        except Exception:  # noqa: BLE001 — um pacote não pode impedir o boot
            logger.warning("rule_pack: falha ao instalar %r na org %s", pack, org.id, exc_info=True)
    return total
