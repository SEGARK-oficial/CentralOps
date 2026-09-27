"""Herança de enriquecimento na hierarquia de organizações (Enterprise).

A herança é por **materialização**, nunca por resolução: cada filha ganha a
PRÓPRIA versão de política e a PRÓPRIA linha de compartilhamento de fonte, e o
runtime continua lendo só a organização do ciclo (``load_policy_for_org`` pelo
ponteiro, ``_resolve_source`` pelo join de ``EnrichmentSourceOrg``). Um caminho
"se a filha não tiver, use o da matriz" faria a política de um tenant valer em
qualquer outro cuja carga falhasse — o vazamento que o desenho existe para não
ter.

O que este módulo acrescenta ao "aplicar às filhas" manual:

- **Modelo sincronizado** (``EnrichmentPolicy.template_sync``): publicar uma
  versão do modelo reaplica nas filhas, e uma filha que entra na subárvore
  recebe o modelo na hora. Sem isto, cada edição da matriz exigia reaplicar à
  mão, e uma filha nova ficava sem enriquecimento até alguém lembrar.
- **Ligar nas filhas** (``enable`` / ``template_enable_children``): a cópia
  nasce desligada por padrão — quem opera o cliente decide —, mas o MSP que
  quer UMA política para todos pode pedir que ela já nasça ligada.
- **Fonte para a subárvore** (``EnrichmentSource.share_with_descendants``): a
  credencial atende as filhas atuais E as futuras.

Nada aqui passa por cima da regra "uma política ativa por org": filha com
política própria ligada é ``overridden`` e fica de fora, sempre.

Vive fora do router porque o gancho de "filha nova" roda na criação da
organização (sync de parceiro), fora de qualquer requisição HTTP.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy.orm import Session

from ..collectors.enrich.dsl import compile_policy
from ..core import edition
from ..db import models

logger = logging.getLogger(__name__)

#: Estados de uma filha diante do modelo. ``blocked`` = falta tabela/fonte;
#: ``overridden`` = política própria ligada vence; ``up_to_date`` = já tem esta
#: versão; ``ready`` = pode receber; ``applied`` = recebeu nesta chamada.
TARGET_STATUSES = ("ready", "applied", "up_to_date", "blocked", "overridden")


def multi_tenant_enabled() -> bool:
    try:
        return bool(edition.feature_enabled("multi_tenant"))
    except Exception:  # noqa: BLE001 — edição ilegível = Community
        return False


# ── árvore ──────────────────────────────────────────────────────────────────


def descendant_org_ids(db: Session, root_org_id: int) -> List[int]:
    """Filhas DIRETAS e indiretas da matriz, sem ela própria.

    Caminha ``parent_organization_id`` em vez de ler ``org_closure``: a closure
    é materializada pelo EE e pode estar vazia numa base que acabou de ganhar a
    licença, e o modelo precisa funcionar no primeiro uso.
    """
    try:
        rows = db.query(
            models.Organization.id, models.Organization.parent_organization_id
        ).all()
    except Exception:  # pragma: no cover — defensivo
        return []
    children: Dict[Optional[int], List[int]] = {}
    for org_id, parent_id in rows:
        children.setdefault(parent_id, []).append(int(org_id))
    out: List[int] = []
    frontier = [int(root_org_id)]
    seen = {int(root_org_id)}
    while frontier:
        nxt: List[int] = []
        for org_id in frontier:
            for child in children.get(org_id, ()):
                if child in seen:
                    continue
                seen.add(child)
                out.append(child)
                nxt.append(child)
        frontier = nxt
    return sorted(out)


def ancestor_org_ids(db: Session, org_id: int) -> List[int]:
    """Cadeia de pais, do mais PRÓXIMO à raiz. À prova de ciclo."""
    out: List[int] = []
    seen = {int(org_id)}
    current = db.get(models.Organization, int(org_id))
    while current is not None and current.parent_organization_id is not None:
        parent_id = int(current.parent_organization_id)
        if parent_id in seen:
            break
        seen.add(parent_id)
        out.append(parent_id)
        current = db.get(models.Organization, parent_id)
    return out


# ── pré-requisitos por NOME na organização alvo ─────────────────────────────


def missing_tables(db: Session, org_id: int, compiled) -> Set[str]:
    referenced = {r.table for r in compiled.rules if r.table}
    if not referenced:
        return set()
    existing = {
        str(t.name)
        for t in db.query(models.EnrichmentTable)
        .filter(
            models.EnrichmentTable.organization_id == org_id,
            models.EnrichmentTable.name.in_(list(referenced)),
        )
        .all()
    }
    return referenced - existing


def missing_sources(db: Session, org_id: int, compiled) -> List[str]:
    """Fontes citadas pela política que a org NÃO enxerga.

    Enxergar = ser a dona OU estar na lista de compartilhamento — o mesmo join
    que ``runtime._resolve_source`` faz. Fonte desabilitada conta como
    existente: desligar temporariamente não pode impedir de publicar.
    """
    referenced = {r.source for r in compiled.rules if getattr(r, "source", None)}
    if not referenced:
        return []
    names = list(referenced)
    own = {
        str(s.name)
        for s in db.query(models.EnrichmentSource)
        .filter(
            models.EnrichmentSource.organization_id == org_id,
            models.EnrichmentSource.name.in_(names),
        )
        .all()
    }
    shared = {
        str(s.name)
        for s in db.query(models.EnrichmentSource)
        .join(
            models.EnrichmentSourceOrg,
            models.EnrichmentSourceOrg.source_id == models.EnrichmentSource.id,
        )
        .filter(
            models.EnrichmentSourceOrg.organization_id == org_id,
            models.EnrichmentSource.name.in_(names),
        )
        .all()
    }
    return sorted(referenced - own - shared)


def tables_without_version(db: Session, org_id: int, compiled) -> List[str]:
    """Tabelas que existem no alvo mas sem versão publicada: aviso, não bloqueio."""
    referenced = {r.table for r in compiled.rules if r.table}
    if not referenced:
        return []
    return sorted(
        str(t.name)
        for t in db.query(models.EnrichmentTable)
        .filter(
            models.EnrichmentTable.organization_id == org_id,
            models.EnrichmentTable.name.in_(list(referenced)),
        )
        .all()
        if not t.current_version_id
    )


# ── modelo da matriz ────────────────────────────────────────────────────────


@dataclass
class TargetState:
    """Estado de UMA filha diante do modelo (espelha ``TemplateApplyTarget``)."""

    organization_id: int
    organization_name: Optional[str] = None
    status: str = "ready"
    policy_id: Optional[str] = None
    policy_name: Optional[str] = None
    applied_version_id: Optional[str] = None
    missing_tables: List[str] = field(default_factory=list)
    missing_sources: List[str] = field(default_factory=list)
    tables_without_version: List[str] = field(default_factory=list)
    overriding_policy: Optional[str] = None
    #: Se a política herdada da filha está ligada (depois desta chamada).
    enabled: Optional[bool] = None


def inherited_policy(
    db: Session, org_id: int, template_name: str
) -> Optional[models.EnrichmentPolicy]:
    """A política da filha que carrega o modelo — casada por NOME.

    Nome, e não um id de origem, porque é o nome que o operador reconhece e o
    que a filha vê na lista. Guardar um vínculo por id obrigaria a criar a
    política antes de saber se ela vai existir, e deixaria órfã toda cópia feita
    à mão antes de o modelo existir.
    """
    return (
        db.query(models.EnrichmentPolicy)
        .filter(
            models.EnrichmentPolicy.organization_id == org_id,
            models.EnrichmentPolicy.name == template_name,
        )
        .first()
    )


def current_rules(
    db: Session, policy: models.EnrichmentPolicy
) -> Optional[Tuple[Any, Any, str]]:
    """``(documento, compilado, version_id)`` da versão vigente, ou ``None``."""
    if not policy.current_version_id:
        return None
    version = db.get(models.EnrichmentPolicyVersion, policy.current_version_id)
    if version is None:
        return None
    doc = json.loads(version.rules or "{}")
    return doc, compile_policy(doc), str(version.id)


def template_target(
    db: Session,
    template: models.EnrichmentPolicy,
    compiled,
    version_id: str,
    org_id: int,
) -> TargetState:
    org = db.get(models.Organization, org_id)
    name = str(getattr(org, "name", "") or "") or None
    herdada = inherited_policy(db, org_id, str(template.name))
    base = dict(
        organization_id=org_id,
        organization_name=name,
        policy_id=(str(herdada.id) if herdada else None),
        policy_name=(str(herdada.name) if herdada else None),
        enabled=(bool(herdada.enabled) if herdada else None),
    )

    # Política PRÓPRIA habilitada vence o modelo. É a regra de precedência, e
    # ela é observável: sem dizer "sobrescrita", aplicar o modelo aqui pareceria
    # ter funcionado e nada mudaria no runtime, porque vale uma política por
    # organização — a mais antiga habilitada.
    propria = (
        db.query(models.EnrichmentPolicy)
        .filter(
            models.EnrichmentPolicy.organization_id == org_id,
            models.EnrichmentPolicy.enabled.is_(True),
        )
        .order_by(models.EnrichmentPolicy.created_at.asc(), models.EnrichmentPolicy.id.asc())
        .first()
    )
    if propria is not None and (herdada is None or propria.id != herdada.id):
        return TargetState(status="overridden", overriding_policy=str(propria.name), **base)

    faltam_tabelas = sorted(missing_tables(db, org_id, compiled))
    faltam_fontes = missing_sources(db, org_id, compiled)
    sem_versao = tables_without_version(db, org_id, compiled)
    if faltam_tabelas or faltam_fontes:
        return TargetState(
            status="blocked",
            missing_tables=faltam_tabelas,
            missing_sources=faltam_fontes,
            tables_without_version=sem_versao,
            **base,
        )

    ja_aplicada: Optional[str] = None
    if herdada is not None and herdada.current_version_id:
        atual = db.get(models.EnrichmentPolicyVersion, herdada.current_version_id)
        if atual is not None and atual.derived_from_version_id == version_id:
            ja_aplicada = version_id
    return TargetState(
        status="up_to_date" if ja_aplicada else "ready",
        applied_version_id=ja_aplicada,
        tables_without_version=sem_versao,
        **base,
    )


def apply_template(
    db: Session,
    template: models.EnrichmentPolicy,
    doc: Any,
    compiled,
    version_id: str,
    org_ids: List[int],
    *,
    author_user_id: Optional[int],
    commit_message: str,
    enable: bool = False,
) -> Tuple[List[TargetState], List[TargetState]]:
    """Publica uma versão DERIVADA em cada filha. NÃO commita.

    A decisão é recalculada por filha aqui, não herdada de um preflight: entre
    ver a tela e clicar, alguém pode ter apagado a tabela que a regra cita.

    ``enable=False`` preserva o comportamento original: a cópia nova nasce
    DESLIGADA e a existente mantém o estado. ``enable=True`` liga a herdada —
    nunca por cima de uma política própria ligada (essa filha é ``overridden``
    e fica de fora), então a regra "uma ativa por org" continua de pé.
    """
    applied: List[TargetState] = []
    skipped: List[TargetState] = []
    rules_json = json.dumps(doc, sort_keys=True, separators=(",", ":"))

    for org_id in sorted(set(int(i) for i in org_ids)):
        alvo = template_target(db, template, compiled, version_id, org_id)
        herdada = inherited_policy(db, org_id, str(template.name))

        if alvo.status == "up_to_date" and enable and herdada is not None and not herdada.enabled:
            # Já tem a versão; o que faltava era só ligar.
            herdada.enabled = True
            db.flush()
            alvo.status = "applied"
            alvo.enabled = True
            applied.append(alvo)
            continue
        if alvo.status in ("blocked", "overridden", "up_to_date"):
            skipped.append(alvo)
            continue

        if herdada is None:
            herdada = models.EnrichmentPolicy(
                organization_id=org_id,
                name=str(template.name),
                description=template.description,
                enabled=False,
            )
            db.add(herdada)
            db.flush()

        ultima = (
            db.query(models.EnrichmentPolicyVersion)
            .filter(models.EnrichmentPolicyVersion.policy_id == herdada.id)
            .order_by(models.EnrichmentPolicyVersion.version_number.desc())
            .first()
        )
        versao = models.EnrichmentPolicyVersion(
            policy_id=herdada.id,
            version_number=(int(ultima.version_number) + 1) if ultima else 1,
            rules=rules_json,
            author_user_id=author_user_id,
            commit_message=commit_message,
            derived_from_version_id=version_id,
        )
        db.add(versao)
        db.flush()
        herdada.current_version_id = versao.id
        if enable and not herdada.enabled:
            herdada.enabled = True
        db.flush()

        alvo.policy_id = str(herdada.id)
        alvo.policy_name = str(herdada.name)
        alvo.applied_version_id = version_id
        alvo.enabled = bool(herdada.enabled)
        alvo.status = "applied"
        applied.append(alvo)

    return applied, skipped


def sync_template(
    db: Session,
    template: models.EnrichmentPolicy,
    *,
    org_ids: Optional[List[int]] = None,
    author_user_id: Optional[int] = None,
) -> Tuple[List[TargetState], List[TargetState]]:
    """Reaplica o modelo SINCRONIZADO nas filhas (todas, ou ``org_ids``).

    No-op (listas vazias) se a política não é modelo sincronizado, não tem
    versão vigente, ou a edição não é Enterprise. NÃO commita.
    """
    if not (
        multi_tenant_enabled()
        and bool(getattr(template, "is_template", False))
        and bool(getattr(template, "template_sync", False))
    ):
        return [], []
    atual = current_rules(db, template)
    if atual is None:
        return [], []
    doc, compiled, version_id = atual
    alvos = (
        org_ids
        if org_ids is not None
        else descendant_org_ids(db, int(template.organization_id))
    )
    version = db.get(models.EnrichmentPolicyVersion, version_id)
    number = int(getattr(version, "version_number", 0) or 0)
    return apply_template(
        db,
        template,
        doc,
        compiled,
        version_id,
        alvos,
        author_user_id=author_user_id,
        commit_message=f"sincronizado do modelo {template.name!s} (v{number})",
        enable=bool(getattr(template, "template_enable_children", False)),
    )


# ── fonte compartilhada com a subárvore ─────────────────────────────────────


class SourceNameClash(ValueError):
    def __init__(self, org_id: int, name: str) -> None:
        super().__init__(f"a organização {org_id} já enxerga outra fonte chamada {name!r}")
        self.org_id = org_id
        self.name = name


def _visible_source_clash(db: Session, source: models.EnrichmentSource, org_id: int) -> bool:
    """A org já enxerga OUTRA fonte com o mesmo nome? (resolução por nome)."""
    return (
        db.query(models.EnrichmentSource.id)
        .outerjoin(
            models.EnrichmentSourceOrg,
            models.EnrichmentSourceOrg.source_id == models.EnrichmentSource.id,
        )
        .filter(
            models.EnrichmentSource.name == source.name,
            models.EnrichmentSource.id != source.id,
            (models.EnrichmentSourceOrg.organization_id == org_id)
            | (models.EnrichmentSource.organization_id == org_id),
        )
        .first()
        is not None
    )


def share_source_with(
    db: Session,
    source: models.EnrichmentSource,
    org_ids: List[int],
    *,
    strict: bool,
) -> List[int]:
    """Garante a linha de compartilhamento de ``source`` em cada org. NÃO commita.

    ``strict=True`` (ação do operador) levanta :class:`SourceNameClash` se a
    org já enxerga outra fonte homônima — o runtime resolve por ``(org, nome)``
    e a escolha ficaria arbitrária. ``strict=False`` (gancho automático) pula a
    org e registra no log. Devolve as orgs que ganharam linha nova.
    """
    # A lista manual pode ter sido reescrita nesta mesma sessão (linhas ainda
    # pendentes); sem o flush a consulta não as vê e duplica a linha.
    db.flush()
    ja = {
        int(r.organization_id)
        for r in db.query(models.EnrichmentSourceOrg)
        .filter(models.EnrichmentSourceOrg.source_id == source.id)
        .all()
    }
    added: List[int] = []
    for org_id in sorted(set(int(i) for i in org_ids)):
        if org_id in ja:
            continue
        if _visible_source_clash(db, source, org_id):
            if strict:
                raise SourceNameClash(org_id, str(source.name))
            logger.warning(
                "enrich: fonte %r não compartilhada com a org %s — ela já enxerga "
                "outra fonte com esse nome",
                source.name, org_id,
                extra={"event": "enrich.source_share_clash", "org_id": org_id},
            )
            continue
        db.add(models.EnrichmentSourceOrg(source_id=source.id, organization_id=org_id))
        added.append(org_id)
    if added:
        db.flush()
    return added


# ── gancho: organização entrou na subárvore ─────────────────────────────────


def on_org_attached(db: Session, org_id: int) -> Dict[str, Any]:
    """Herda fontes e modelos sincronizados dos ancestrais. NÃO commita.

    Ordem importa: fontes PRIMEIRO, para o pré-requisito do modelo já enxergar
    a credencial compartilhada. Ancestral mais próximo primeiro: se dois níveis
    têm modelo com o mesmo nome, vale o da matriz imediata.
    """
    summary: Dict[str, Any] = {"sources": [], "policies": [], "skipped": []}
    if not multi_tenant_enabled():
        return summary
    ancestors = ancestor_org_ids(db, org_id)
    if not ancestors:
        return summary

    fontes = (
        db.query(models.EnrichmentSource)
        .filter(
            models.EnrichmentSource.organization_id.in_(ancestors),
            models.EnrichmentSource.share_with_descendants.is_(True),
        )
        .all()
    )
    for source in fontes:
        if share_source_with(db, source, [org_id], strict=False):
            summary["sources"].append(str(source.name))

    ordem = {oid: i for i, oid in enumerate(ancestors)}
    modelos = sorted(
        db.query(models.EnrichmentPolicy)
        .filter(
            models.EnrichmentPolicy.organization_id.in_(ancestors),
            models.EnrichmentPolicy.is_template.is_(True),
            models.EnrichmentPolicy.template_sync.is_(True),
        )
        .all(),
        key=lambda p: ordem.get(int(p.organization_id), 1 << 30),
    )
    nomes_vistos: Set[str] = set()
    for template in modelos:
        if str(template.name) in nomes_vistos:
            continue
        nomes_vistos.add(str(template.name))
        applied, skipped = sync_template(db, template, org_ids=[org_id])
        summary["policies"].extend(str(template.name) for _ in applied)
        summary["skipped"].extend(
            {"policy": str(template.name), "status": s.status} for s in skipped
        )
    return summary


def on_org_attached_safe(bind, org_id: int) -> None:
    """Roda :func:`on_org_attached` numa sessão PRÓPRIA e commita.

    Chamado DEPOIS do commit que criou/anexou a organização: uma falha aqui é
    revertida sozinha e NUNCA desfaz a criação da org — o enriquecimento não
    pode derrubar o sync de parceiro, que é a coleta.
    """
    try:
        with Session(bind=bind) as db:
            summary = on_org_attached(db, int(org_id))
            db.commit()
        if summary["sources"] or summary["policies"] or summary["skipped"]:
            logger.info(
                "enrich: org %s herdou fontes=%s políticas=%s puladas=%s",
                org_id, summary["sources"], summary["policies"], summary["skipped"],
                extra={"event": "enrich.inherited_on_attach", "org_id": org_id},
            )
    except Exception:  # noqa: BLE001
        logger.exception(
            "enrich: falha herdando enriquecimento para a org %s", org_id,
            extra={"event": "enrich.inherit_failed", "org_id": org_id},
        )
