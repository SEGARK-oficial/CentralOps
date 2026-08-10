"""REST do enriquecimento em stream (ADR-LOCAL-0002, Fase 2).

Três recursos, todos admin-only e **org-escopados**:

``GET /collectors/enrichment/enrichers``
    Catálogo self-describing lido do registry. É o que torna a galeria de fontes
    100% plugin-driven: adicionar um enricher é registrar um módulo, e o frontend
    não muda. Simétrico a ``/collectors/destination-types`` e ``/providers/platforms``.
    O endpoint existe desde o primeiro commit da API de propósito — o
    ``preprocess-ops`` do mapping foi documentado sem nunca existir
    (``normalize/preprocess.py:16`` vs ``routers/mappings.py:935``), e o custo disso
    foi um agente de IA chamando um endpoint fantasma.

``/collectors/enrichment/tables``
    Tabelas do cliente (CMDB, allowlist, plano de rede), com versões IMUTÁVEIS,
    ponteiro ``current_version_id``, rollback e diff.

``/collectors/enrichment/policies``
    Políticas versionadas. O corpo é validado por ``dsl.compile_policy`` no COMMIT —
    regra inválida é 422 no momento da escrita, nunca um evento processado pela
    metade em produção.

**Escopo é estrutural.** Diferente de rotas e destinos, aqui NÃO existe recurso
global: tabela e política pertencem sempre a uma organização. Um CMDB "global"
serviria o inventário de um cliente para outro — o oposto do que a feature promete.
Por isso ``organization_id`` é obrigatório e o admin escopado só enxerga a própria
subárvore.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..collectors.enrich import enrichers as _enrichers  # noqa: F401 — dispara registro
from ..collectors.enrich import registry as enrich_registry
from ..collectors.enrich.applier import ApplyStats, apply as apply_enrichment
from ..collectors.enrich.contract import EnrichmentConfigError
from ..collectors.enrich.dsl import compile_policy, describe_policy
from ..collectors.enrich.radix import parse_network
from ..collectors.enrich.runtime import DictLookupTable, TableResolution, estimate_bytes
from ..core import auth as app_auth
from ..core import tenant
from ..core.config import settings
from ..core.errors import ApiError
from ..core.tenant import has_global_scope
from ..db import database, models

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/collectors/enrichment", tags=["enrichment"])

#: Teto de linhas por versão de tabela. Não é limite de produto — é o guard que
#: impede subir um CSV que estouraria ``ENRICH_MAX_TABLE_BYTES`` e seria RECUSADO
#: silenciosamente no worker, longe de quem fez o upload.
MAX_TABLE_ROWS: int = 200_000


# ── schemas ─────────────────────────────────────────────────────────────────

class EnricherCatalogItem(BaseModel):
    name: str
    label: str
    category: str
    description: str
    icon_id: Optional[str] = None
    docs_url: Optional[str] = None
    tier: str
    order: int
    mode: str
    key_kinds: List[str]
    supports_bulk: bool
    p99_budget_ms: float
    suggested_ttl_s: int
    suggested_negative_ttl_s: int
    license: str
    redistributable: bool
    #: "none" | "internal" | "third_party". A UI DEVE exibir com destaque: é
    #: consentimento de privacidade, não detalhe técnico.
    egress: str
    required_secrets: List[str]
    output_fields: Dict[str, str]
    config_schema: Optional[Dict[str, Any]] = None


class TableCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    organization_id: Optional[int] = None
    description: Optional[str] = None
    match_mode: str = Field("exact", pattern="^(exact|cidr)$")
    key_kind: str = "ip"


class TableRead(BaseModel):
    id: str
    organization_id: int
    name: str
    description: Optional[str] = None
    match_mode: str
    key_kind: str
    current_version_id: Optional[str] = None
    entry_count: int = 0
    approx_bytes: int = 0


class TableVersionCommit(BaseModel):
    #: ``{chave: {campo: valor}}``. Para ``match_mode="cidr"`` a chave é um CIDR.
    rows: Dict[str, Dict[str, Any]]
    commit_message: str = Field(..., min_length=1, max_length=500)


class TableVersionRead(BaseModel):
    id: str
    version_number: int
    entry_count: int
    approx_bytes: int
    commit_message: str
    author_user_id: Optional[int] = None
    created_at: Optional[str] = None
    is_current: bool = False
    #: Linhas descartadas por CIDR inválido (só ``match_mode="cidr"``).
    invalid_rows: int = 0


class PolicyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    organization_id: Optional[int] = None
    description: Optional[str] = None


class PolicyRead(BaseModel):
    id: str
    organization_id: int
    name: str
    description: Optional[str] = None
    enabled: bool
    current_version_id: Optional[str] = None
    rule_count: int = 0


class PolicyVersionCommit(BaseModel):
    #: ``{"version": 1, "enrichment": [...]}`` ou a lista crua de regras.
    rules: Any
    commit_message: str = Field(..., min_length=1, max_length=500)


class PolicyVersionRead(BaseModel):
    id: str
    version_number: int
    commit_message: str
    author_user_id: Optional[int] = None
    created_at: Optional[str] = None
    is_current: bool = False
    summary: Optional[Dict[str, Any]] = None


class RollbackRequest(BaseModel):
    version_id: str


class DryRunRequest(BaseModel):
    rules: Any
    #: Envelope de exemplo. Aceita ``{_centralops, normalized, raw}`` completo.
    sample: Dict[str, Any]
    #: Tabelas simuladas ``{nome_da_regra: {chave: {campo: valor}}}`` — permite
    #: testar a política SEM publicar tabela nenhuma.
    tables: Dict[str, Dict[str, Any]] = Field(default_factory=dict)


class DryRunResponse(BaseModel):
    ok: bool
    summary: Dict[str, Any]
    enriched: Dict[str, Any]
    hits: Dict[str, int]
    misses: Dict[str, int]
    skipped: Dict[str, int]
    errors: Dict[str, int]
    #: Bytes que o enriquecimento acrescentaria a ESTE evento. É o número que
    #: liga a política ao card de custo — o operador vê o preço antes de publicar.
    bytes_added: int


# ── helpers de escopo ───────────────────────────────────────────────────────

def _db(db: Session = Depends(database.get_session)) -> Session:
    return db


def _resolve_scope(user: models.AppUser) -> tuple[bool, Optional[int]]:
    is_global = has_global_scope(user)
    raw = user.organization_id
    org_id: Optional[int] = int(raw) if raw is not None else None  # type: ignore[arg-type]
    return is_global, (org_id if not is_global else None)


def _resolve_target_org(user: models.AppUser, requested: Optional[int]) -> int:
    """Org do recurso a criar. **Nunca** ``None``: não existe tabela/política global.

    Um CMDB "global" serviria o inventário de um cliente para outro — a feature
    inteira existe para o oposto disso.
    """
    is_global, caller_org = _resolve_scope(user)
    org_id = requested if requested is not None else caller_org
    if org_id is None:
        raise ApiError(
            "enrichment.organization_required",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            messages={
                "pt": "organization_id é obrigatório: não existe tabela/política de enriquecimento global.",
                "en": "organization_id is required: there is no global enrichment table/policy.",
                "es": "organization_id es obligatorio: no existe tabla/política de enriquecimiento global.",
            },
        )
    if not is_global:
        tenant.require_subtree_access(user, int(org_id))
    return int(org_id)


def _assert_visible(row: Any, user: models.AppUser, kind: str) -> Any:
    """404 (não 403) quando fora do escopo — anti-enumeração, como em rotas."""
    if row is None:
        raise ApiError(
            f"enrichment.{kind}_not_found",
            status.HTTP_404_NOT_FOUND,
            messages={
                "pt": "Recurso de enriquecimento não encontrado.",
                "en": "Enrichment resource not found.",
                "es": "Recurso de enriquecimiento no encontrado.",
            },
        )
    is_global, _ = _resolve_scope(user)
    if not is_global and not tenant.can_access_subtree(user, int(row.organization_id)):
        raise ApiError(
            f"enrichment.{kind}_not_found",
            status.HTTP_404_NOT_FOUND,
            messages={
                "pt": "Recurso de enriquecimento não encontrado.",
                "en": "Enrichment resource not found.",
                "es": "Recurso de enriquecimiento no encontrado.",
            },
        )
    return row


def _visible_org_filter(query, model, user: models.AppUser, db: Session):
    """Filtro de listagem pelo escopo do usuário.

    Usa ``tenant.accessible_org_ids``, cujo contrato é: ``None`` = vê todas;
    ``set()`` = **não vê nenhuma**. O ``set()`` vazio precisa virar filtro
    impossível, não "sem filtro" — inverter isso é o formato clássico do vazamento
    cross-org (um admin escopado sem org enxergando tudo).
    """
    orgs = tenant.accessible_org_ids(user, db)
    if orgs is None:
        return query
    if not orgs:
        return query.filter(model.organization_id.is_(None))  # nenhuma linha
    return query.filter(model.organization_id.in_(sorted(orgs)))


def _bad_request(code: str, detail: str) -> ApiError:
    return ApiError(
        code,
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        messages={"pt": detail, "en": detail, "es": detail},
    )


# ── catálogo ────────────────────────────────────────────────────────────────

@router.get("/enrichers", response_model=List[EnricherCatalogItem])
def list_enrichers(
    user: models.AppUser = Depends(app_auth.require_admin_user),
) -> List[EnricherCatalogItem]:
    """Catálogo de fontes de enriquecimento. Plugin-driven, zero hardcode no front."""
    return [EnricherCatalogItem(**item) for item in enrich_registry.describe_all()]


# ── tabelas ─────────────────────────────────────────────────────────────────

def _table_read(db: Session, row: models.EnrichmentTable) -> TableRead:
    entry_count = 0
    approx = 0
    if row.current_version_id:
        v = (
            db.query(models.EnrichmentTableVersion)
            .filter(models.EnrichmentTableVersion.id == row.current_version_id)
            .first()
        )
        if v is not None:
            entry_count = int(v.entry_count or 0)
            approx = int(v.approx_bytes or 0)
    return TableRead(
        id=str(row.id),
        organization_id=int(row.organization_id),
        name=str(row.name),
        description=row.description,
        match_mode=str(row.match_mode),
        key_kind=str(row.key_kind),
        current_version_id=row.current_version_id,
        entry_count=entry_count,
        approx_bytes=approx,
    )


@router.get("/tables", response_model=List[TableRead])
def list_tables(
    user: models.AppUser = Depends(app_auth.require_admin_user),
    db: Session = Depends(_db),
) -> List[TableRead]:
    q = _visible_org_filter(db.query(models.EnrichmentTable), models.EnrichmentTable, user, db)
    return [_table_read(db, r) for r in q.order_by(models.EnrichmentTable.name).all()]


@router.post("/tables", response_model=TableRead, status_code=status.HTTP_201_CREATED)
def create_table(
    payload: TableCreate,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    db: Session = Depends(_db),
) -> TableRead:
    org_id = _resolve_target_org(user, payload.organization_id)
    exists = (
        db.query(models.EnrichmentTable)
        .filter(
            models.EnrichmentTable.organization_id == org_id,
            models.EnrichmentTable.name == payload.name,
        )
        .first()
    )
    if exists is not None:
        raise _bad_request(
            "enrichment.table_exists",
            f"já existe uma tabela chamada {payload.name!r} nesta organização",
        )
    row = models.EnrichmentTable(
        organization_id=org_id,
        name=payload.name,
        description=payload.description,
        match_mode=payload.match_mode,
        key_kind=payload.key_kind,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _table_read(db, row)


@router.get("/tables/{table_id}", response_model=TableRead)
def get_table(
    table_id: str,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    db: Session = Depends(_db),
) -> TableRead:
    row = _assert_visible(db.get(models.EnrichmentTable, table_id), user, "table")
    return _table_read(db, row)


@router.delete("/tables/{table_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_table(
    table_id: str,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    db: Session = Depends(_db),
) -> None:
    row = _assert_visible(db.get(models.EnrichmentTable, table_id), user, "table")
    # Recusa deletar tabela EM USO: a política referencia por NOME, então apagar
    # transformaria a regra num erro de carga silencioso a cada ciclo.
    in_use = _policies_referencing_table(db, int(row.organization_id), str(row.name))
    if in_use:
        raise _bad_request(
            "enrichment.table_in_use",
            f"tabela {row.name!r} é referenciada pela(s) política(s): {', '.join(in_use)}",
        )
    db.delete(row)
    db.commit()


def _policies_referencing_table(db: Session, org_id: int, table_name: str) -> List[str]:
    """Nomes das políticas cuja versão VIGENTE referencia a tabela."""
    names: List[str] = []
    policies = (
        db.query(models.EnrichmentPolicy)
        .filter(models.EnrichmentPolicy.organization_id == org_id)
        .all()
    )
    for p in policies:
        if not p.current_version_id:
            continue
        v = (
            db.query(models.EnrichmentPolicyVersion)
            .filter(models.EnrichmentPolicyVersion.id == p.current_version_id)
            .first()
        )
        if v is None:
            continue
        try:
            doc = json.loads(v.rules or "{}")
        except json.JSONDecodeError:
            continue
        rules = doc.get("enrichment", doc) if isinstance(doc, dict) else doc
        if isinstance(rules, list) and any(
            isinstance(r, dict) and r.get("table") == table_name for r in rules
        ):
            names.append(str(p.name))
    return names


@router.get("/tables/{table_id}/versions", response_model=List[TableVersionRead])
def list_table_versions(
    table_id: str,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    db: Session = Depends(_db),
    limit: int = Query(50, ge=1, le=500),
) -> List[TableVersionRead]:
    table = _assert_visible(db.get(models.EnrichmentTable, table_id), user, "table")
    rows = (
        db.query(models.EnrichmentTableVersion)
        .filter(models.EnrichmentTableVersion.table_id == table.id)
        .order_by(models.EnrichmentTableVersion.version_number.desc())
        .limit(limit)
        .all()
    )
    return [
        TableVersionRead(
            id=str(v.id),
            version_number=int(v.version_number),
            entry_count=int(v.entry_count or 0),
            approx_bytes=int(v.approx_bytes or 0),
            commit_message=str(v.commit_message),
            author_user_id=v.author_user_id,
            created_at=v.created_at.isoformat() if v.created_at else None,
            is_current=(str(v.id) == str(table.current_version_id)),
        )
        for v in rows
    ]


@router.post(
    "/tables/{table_id}/versions",
    response_model=TableVersionRead,
    status_code=status.HTTP_201_CREATED,
)
def commit_table_version(
    table_id: str,
    payload: TableVersionCommit,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    db: Session = Depends(_db),
) -> TableVersionRead:
    """Publica uma versão e aponta ``current_version_id`` para ela.

    Quatro gates ANTES de gravar — a alternativa é o worker recusar a tabela num
    log que ninguém lê, horas depois do upload:

    1. teto de linhas;
    2. teto de bytes (``ENRICH_MAX_TABLE_BYTES``) — o mesmo que o runtime aplica;
    3. valor de linha tem de ser objeto (``{campo: valor}``);
    4. em ``match_mode="cidr"``, as chaves são validadas e as inválidas CONTADAS.
       Não recusa o arquivo: 3 erros de digitação não podem derrubar 49.997 linhas.
    """
    table = _assert_visible(db.get(models.EnrichmentTable, table_id), user, "table")

    rows = payload.rows
    if len(rows) > MAX_TABLE_ROWS:
        raise _bad_request(
            "enrichment.table_too_many_rows",
            f"{len(rows)} linhas excede o teto de {MAX_TABLE_ROWS}",
        )
    bad_value = next(
        (k for k, v in rows.items() if not isinstance(v, dict)), None
    )
    if bad_value is not None:
        raise _bad_request(
            "enrichment.table_row_not_object",
            f"o valor da chave {bad_value!r} não é um objeto {{campo: valor}}",
        )

    invalid = 0
    if str(table.match_mode) == "cidr":
        invalid = sum(1 for k in rows if parse_network(str(k)) is None)
        if invalid and invalid == len(rows):
            raise _bad_request(
                "enrichment.table_no_valid_cidr",
                "nenhuma chave é um CIDR/IP válido — confira o arquivo",
            )

    approx = estimate_bytes(rows)
    max_bytes = int(getattr(settings, "ENRICH_MAX_TABLE_BYTES", 32 * 1024 * 1024))
    if approx > max_bytes:
        raise _bad_request(
            "enrichment.table_too_big",
            f"tabela ocuparia ~{approx} B, acima do teto de {max_bytes} B por worker. "
            "Reduza a tabela ou eleve ENRICH_MAX_TABLE_BYTES ciente de que o custo "
            "é multiplicado pela concorrência do worker.",
        )

    last = (
        db.query(models.EnrichmentTableVersion)
        .filter(models.EnrichmentTableVersion.table_id == table.id)
        .order_by(models.EnrichmentTableVersion.version_number.desc())
        .first()
    )
    version = models.EnrichmentTableVersion(
        table_id=table.id,
        version_number=(int(last.version_number) + 1) if last else 1,
        rows=json.dumps(rows, sort_keys=True, separators=(",", ":")),
        entry_count=len(rows) - invalid,
        approx_bytes=approx,
        # NUNCA o id cru: service account tem id NEGATIVO inexistente em app_users
        # (FK violation + auditoria perdida).
        author_user_id=app_auth.persistable_user_id(user),
        commit_message=payload.commit_message,
    )
    db.add(version)
    db.flush()
    table.current_version_id = version.id
    db.commit()
    db.refresh(version)
    return TableVersionRead(
        id=str(version.id),
        version_number=int(version.version_number),
        entry_count=int(version.entry_count),
        approx_bytes=int(version.approx_bytes),
        commit_message=str(version.commit_message),
        author_user_id=version.author_user_id,
        created_at=version.created_at.isoformat() if version.created_at else None,
        is_current=True,
        invalid_rows=invalid,
    )


@router.post("/tables/{table_id}/rollback", response_model=TableRead)
def rollback_table(
    table_id: str,
    payload: RollbackRequest,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    db: Session = Depends(_db),
) -> TableRead:
    """Re-aponta ``current_version_id`` para uma versão anterior.

    Não cria versão nova nem reescreve nada: o histórico é append-only, e é isso
    que faz o ponto-no-tempo do backfill continuar correto depois de um rollback.
    """
    table = _assert_visible(db.get(models.EnrichmentTable, table_id), user, "table")
    target = (
        db.query(models.EnrichmentTableVersion)
        .filter(
            models.EnrichmentTableVersion.id == payload.version_id,
            models.EnrichmentTableVersion.table_id == table.id,
        )
        .first()
    )
    if target is None:
        raise _bad_request(
            "enrichment.version_not_found",
            "versão não encontrada nesta tabela",
        )
    table.current_version_id = target.id
    db.commit()
    db.refresh(table)
    return _table_read(db, table)


# ── políticas ───────────────────────────────────────────────────────────────

def _policy_read(db: Session, row: models.EnrichmentPolicy) -> PolicyRead:
    rule_count = 0
    if row.current_version_id:
        v = (
            db.query(models.EnrichmentPolicyVersion)
            .filter(models.EnrichmentPolicyVersion.id == row.current_version_id)
            .first()
        )
        if v is not None:
            try:
                doc = json.loads(v.rules or "{}")
                rules = doc.get("enrichment", doc) if isinstance(doc, dict) else doc
                rule_count = len(rules) if isinstance(rules, list) else 0
            except json.JSONDecodeError:
                rule_count = 0
    return PolicyRead(
        id=str(row.id),
        organization_id=int(row.organization_id),
        name=str(row.name),
        description=row.description,
        enabled=bool(row.enabled),
        current_version_id=row.current_version_id,
        rule_count=rule_count,
    )


@router.get("/policies", response_model=List[PolicyRead])
def list_policies(
    user: models.AppUser = Depends(app_auth.require_admin_user),
    db: Session = Depends(_db),
) -> List[PolicyRead]:
    q = _visible_org_filter(
        db.query(models.EnrichmentPolicy), models.EnrichmentPolicy, user, db
    )
    return [_policy_read(db, r) for r in q.order_by(models.EnrichmentPolicy.name).all()]


@router.post("/policies", response_model=PolicyRead, status_code=status.HTTP_201_CREATED)
def create_policy(
    payload: PolicyCreate,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    db: Session = Depends(_db),
) -> PolicyRead:
    org_id = _resolve_target_org(user, payload.organization_id)
    if (
        db.query(models.EnrichmentPolicy)
        .filter(
            models.EnrichmentPolicy.organization_id == org_id,
            models.EnrichmentPolicy.name == payload.name,
        )
        .first()
        is not None
    ):
        raise _bad_request(
            "enrichment.policy_exists",
            f"já existe uma política chamada {payload.name!r} nesta organização",
        )
    row = models.EnrichmentPolicy(
        organization_id=org_id,
        name=payload.name,
        description=payload.description,
        enabled=False,  # criar não liga: publicar uma versão e habilitar são passos distintos
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _policy_read(db, row)


@router.post("/policies/{policy_id}/enable", response_model=PolicyRead)
def set_policy_enabled(
    policy_id: str,
    enabled: bool = Query(...),
    user: models.AppUser = Depends(app_auth.require_admin_user),
    db: Session = Depends(_db),
) -> PolicyRead:
    row = _assert_visible(db.get(models.EnrichmentPolicy, policy_id), user, "policy")
    if enabled and not row.current_version_id:
        # Habilitar sem versão vigente é o caso nº1 de suporte ("liguei e não faz
        # nada"): o worker loga e segue, mas ninguém lê o log do worker.
        raise _bad_request(
            "enrichment.policy_without_version",
            "publique uma versão antes de habilitar a política",
        )
    row.enabled = bool(enabled)
    db.commit()
    db.refresh(row)
    return _policy_read(db, row)


@router.post(
    "/policies/{policy_id}/versions",
    response_model=PolicyVersionRead,
    status_code=status.HTTP_201_CREATED,
)
def commit_policy_version(
    policy_id: str,
    payload: PolicyVersionCommit,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    db: Session = Depends(_db),
) -> PolicyVersionRead:
    """Compila a política e publica. **422 em regra inválida.**

    A validação acontece AQUI, na escrita, e não no worker: o compilador rejeita
    chave desconhecida, target fora de ``_centralops.enrichment``, enricher que não
    existe e kind que o enricher não resolve. É o oposto do fail-open silencioso de
    ``normalize/engine._compile_single_rule``, que ignora campo desconhecido sem erro.
    """
    row = _assert_visible(db.get(models.EnrichmentPolicy, policy_id), user, "policy")
    try:
        compiled = compile_policy(payload.rules)
    except EnrichmentConfigError as exc:
        raise _bad_request("enrichment.policy_invalid", str(exc)) from exc

    # Tabela referenciada precisa EXISTIR na org — senão a regra vira erro de
    # carga a cada ciclo, num log que ninguém lê.
    missing = _missing_tables(db, int(row.organization_id), compiled)
    if missing:
        raise _bad_request(
            "enrichment.table_missing",
            f"tabela(s) inexistente(s) nesta organização: {', '.join(sorted(missing))}",
        )

    last = (
        db.query(models.EnrichmentPolicyVersion)
        .filter(models.EnrichmentPolicyVersion.policy_id == row.id)
        .order_by(models.EnrichmentPolicyVersion.version_number.desc())
        .first()
    )
    version = models.EnrichmentPolicyVersion(
        policy_id=row.id,
        version_number=(int(last.version_number) + 1) if last else 1,
        rules=json.dumps(payload.rules, sort_keys=True, separators=(",", ":")),
        author_user_id=app_auth.persistable_user_id(user),
        commit_message=payload.commit_message,
    )
    db.add(version)
    db.flush()
    row.current_version_id = version.id
    db.commit()
    db.refresh(version)
    return PolicyVersionRead(
        id=str(version.id),
        version_number=int(version.version_number),
        commit_message=str(version.commit_message),
        author_user_id=version.author_user_id,
        created_at=version.created_at.isoformat() if version.created_at else None,
        is_current=True,
        summary=describe_policy(compiled),
    )


def _missing_tables(db: Session, org_id: int, compiled) -> set:
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


@router.get("/policies/{policy_id}/versions", response_model=List[PolicyVersionRead])
def list_policy_versions(
    policy_id: str,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    db: Session = Depends(_db),
    limit: int = Query(50, ge=1, le=500),
) -> List[PolicyVersionRead]:
    row = _assert_visible(db.get(models.EnrichmentPolicy, policy_id), user, "policy")
    versions = (
        db.query(models.EnrichmentPolicyVersion)
        .filter(models.EnrichmentPolicyVersion.policy_id == row.id)
        .order_by(models.EnrichmentPolicyVersion.version_number.desc())
        .limit(limit)
        .all()
    )
    return [
        PolicyVersionRead(
            id=str(v.id),
            version_number=int(v.version_number),
            commit_message=str(v.commit_message),
            author_user_id=v.author_user_id,
            created_at=v.created_at.isoformat() if v.created_at else None,
            is_current=(str(v.id) == str(row.current_version_id)),
        )
        for v in versions
    ]


@router.post("/policies/{policy_id}/rollback", response_model=PolicyRead)
def rollback_policy(
    policy_id: str,
    payload: RollbackRequest,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    db: Session = Depends(_db),
) -> PolicyRead:
    row = _assert_visible(db.get(models.EnrichmentPolicy, policy_id), user, "policy")
    target = (
        db.query(models.EnrichmentPolicyVersion)
        .filter(
            models.EnrichmentPolicyVersion.id == payload.version_id,
            models.EnrichmentPolicyVersion.policy_id == row.id,
        )
        .first()
    )
    if target is None:
        raise _bad_request(
            "enrichment.version_not_found", "versão não encontrada nesta política"
        )
    row.current_version_id = target.id
    db.commit()
    db.refresh(row)
    return _policy_read(db, row)


# ── dry-run ─────────────────────────────────────────────────────────────────

@router.post("/dry-run", response_model=DryRunResponse)
def dry_run(
    payload: DryRunRequest,
    user: models.AppUser = Depends(app_auth.require_admin_user),
) -> DryRunResponse:
    """Aplica a política a um evento de exemplo, SEM publicar nada.

    ``tables`` permite simular o conteúdo por regra, então dá para desenhar a
    política antes de existir tabela. Só regras LOCAIS são exercitadas: o seam
    remoto faria chamada externa (e gastaria cota de terceiro) num endpoint que o
    operador aciona a cada tecla — as remotas aparecem como ``skipped``, que é
    exatamente o que o aplicador reporta quando a resolução não responde.
    """
    try:
        compiled = compile_policy(payload.rules)
    except EnrichmentConfigError as exc:
        raise _bad_request("enrichment.policy_invalid", str(exc)) from exc

    tables = {
        rule_id: DictLookupTable(rows) for rule_id, rows in (payload.tables or {}).items()
    }
    envelope = json.loads(json.dumps(payload.sample))  # cópia profunda barata
    stats = ApplyStats()
    before = len(json.dumps(envelope, separators=(",", ":")))
    apply_enrichment(envelope, compiled.local_rules(), TableResolution(tables), stats)
    after = len(json.dumps(envelope, separators=(",", ":")))

    return DryRunResponse(
        ok=True,
        summary=describe_policy(compiled),
        enriched=envelope,
        hits=dict(stats.hits),
        misses=dict(stats.misses),
        skipped=dict(stats.skipped),
        errors=dict(stats.errors),
        bytes_added=max(after - before, 0),
    )
