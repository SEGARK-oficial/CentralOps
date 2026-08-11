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
from ..collectors.enrich.contract import EnrichContext, EnrichmentConfigError
from ..collectors.enrich.dsl import compile_policy, describe_policy
from ..collectors.enrich.radix import parse_network
from ..collectors.enrich.runtime import DictLookupTable, TableResolution, estimate_bytes
from ..core import auth as app_auth
from ..core import tenant
from ..core import edition
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


class SourceCreate(BaseModel):
    """Instância configurada de um enricher, escopada à organização.

    ``secret`` é WRITE-ONLY e vem em texto claro **uma única vez**: o servidor o
    cifra e guarda o ciphertext em ``secret_ref``. O cliente jamais recebe a
    referência de volta (ver :class:`SourceRead`) — como o cofre não conhece
    organização (``core.secrets.backend.decrypt(ciphertext)``), devolver o
    ciphertext permitiria a um admin colá-lo noutra org e usar a credencial alheia.
    """

    name: str = Field(..., min_length=1, max_length=120)
    enricher: str = Field(..., min_length=1, max_length=120)
    organization_id: Optional[int] = None
    description: Optional[str] = None
    config: Dict[str, Any] = Field(default_factory=dict)
    secret: Optional[str] = Field(None, max_length=8192)
    enabled: bool = True
    #: Orgs FILHAS que também usam esta fonte. A org dona entra sempre, não
    #: precisa ser repetida aqui. Lista com item exige a feature ``multi_tenant``.
    shared_organization_ids: List[int] = Field(default_factory=list)


class SourceUpdate(BaseModel):
    description: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    #: ``None`` = mantém o segredo atual; string vazia = REMOVE.
    secret: Optional[str] = Field(None, max_length=8192)
    enabled: Optional[bool] = None
    #: ``None`` mantém a lista atual; lista substitui (a dona é preservada).
    shared_organization_ids: Optional[List[int]] = None


class SourceRead(BaseModel):
    id: str
    organization_id: int
    name: str
    enricher: str
    description: Optional[str] = None
    config: Dict[str, Any] = Field(default_factory=dict)
    #: Booleano, NUNCA o ``secret_ref``. Mesmo padrão de
    #: ``Integration.manager_api_password_configured``.
    secret_configured: bool = False
    enabled: bool = True
    #: Filhas que usam a fonte (sem a dona). Editável depois da criação.
    shared_organization_ids: List[int] = Field(default_factory=list)


class SourceTestResult(BaseModel):
    ok: bool
    #: Mensagem pronta para a UI. Em falha, o motivo real do provedor.
    message: str
    #: Quantos registros a fonte devolveu na sondagem, quando aplicável.
    sample_count: Optional[int] = None
    #: Amostra pequena do que veio, para o operador conferir o formato.
    sample: Optional[Dict[str, Any]] = None
    elapsed_ms: Optional[float] = None


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


def _encrypt_secret(plaintext: str) -> str:
    """Cifra a credencial. O que vai para o banco é o ciphertext, nunca o claro.

    Mesmo caminho de ``destinations.py`` (``get_default_backend().encrypt(...)``).
    A cifragem acontece SÓ aqui, no servidor: o cliente manda o segredo em claro
    uma única vez e nunca o recebe de volta — o cofre não conhece organização
    (``core.secrets.backend.decrypt(ciphertext)``), então devolver o ciphertext
    deixaria um admin colá-lo noutra org e usar a credencial do vizinho.
    """
    from ..core import secrets as secrets_mod

    return secrets_mod.get_default_backend().encrypt(plaintext)


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


# ── fontes configuradas (instâncias de enricher por org) ────────────────────

def _source_read(db: Session, row: Any) -> SourceRead:
    """Serializa SEM o ``secret_ref``. Ver :class:`SourceRead`."""
    try:
        cfg = json.loads(row.config or "{}")
    except Exception:  # noqa: BLE001 — config torta não pode derrubar a listagem
        cfg = {}
    shared = [
        o.organization_id
        for o in db.query(models.EnrichmentSourceOrg)
        .filter(models.EnrichmentSourceOrg.source_id == row.id)
        .all()
        if o.organization_id != row.organization_id
    ]
    return SourceRead(
        id=row.id,
        organization_id=row.organization_id,
        name=row.name,
        enricher=row.enricher,
        description=row.description,
        config=cfg,
        secret_configured=bool(row.secret_ref),
        enabled=bool(row.enabled),
        shared_organization_ids=sorted(shared),
    )


def _sync_source_orgs(
    db: Session,
    row: Any,
    user: models.AppUser,
    shared_ids: Optional[List[int]],
) -> None:
    """Reescreve a lista de orgs da fonte. A dona nunca sai.

    Três recusas, nesta ordem:

    1. **Compartilhar exige Enterprise.** Sem ``multi_tenant`` a fonte atende só a
       dona. Não é trava artificial: o escopo de subárvore que faz a matriz
       enxergar as filhas já é EE, então em CE não existe nem como escolhê-las.
    2. **Cada org da lista precisa estar na subárvore de quem edita.** Sem isso um
       admin de MSP compartilharia a própria credencial com um tenant de outra
       árvore, e o join do runtime passaria a servi-la.
    3. **Nome único por org.** O runtime resolve por ``(org, nome)``; duas fontes
       homônimas visíveis para a mesma org tornariam a escolha arbitrária.
    """
    if shared_ids is None:
        return
    wanted = {int(i) for i in shared_ids if int(i) != int(row.organization_id)}

    if wanted and not edition.feature_enabled("multi_tenant"):
        raise ApiError(
            "enrichment.source_sharing_requires_enterprise",
            status.HTTP_403_FORBIDDEN,
            messages={
                "pt": "Compartilhar uma fonte entre organizações exige a edição Enterprise. Na Community cada fonte atende uma organização.",
                "en": "Sharing a source across organizations requires the Enterprise edition. In Community each source serves one organization.",
                "es": "Compartir una fuente entre organizaciones requiere la edición Enterprise. En Community cada fuente atiende a una organización.",
            },
        )

    for org_id in sorted(wanted):
        tenant.require_subtree_access(user, org_id, db)
        clash = (
            db.query(models.EnrichmentSource)
            .join(
                models.EnrichmentSourceOrg,
                models.EnrichmentSourceOrg.source_id == models.EnrichmentSource.id,
            )
            .filter(
                models.EnrichmentSourceOrg.organization_id == org_id,
                models.EnrichmentSource.name == row.name,
                models.EnrichmentSource.id != row.id,
            )
            .first()
        )
        if clash is not None:
            raise _bad_request(
                "enrichment.source_name_clash",
                f"a organização {org_id} já enxerga outra fonte chamada {row.name!r}",
            )

    db.query(models.EnrichmentSourceOrg).filter(
        models.EnrichmentSourceOrg.source_id == row.id
    ).delete(synchronize_session=False)
    for org_id in sorted(wanted | {int(row.organization_id)}):
        db.add(models.EnrichmentSourceOrg(source_id=row.id, organization_id=org_id))


def _validate_source_config(enricher: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Valida a config contra o ``config_schema`` do enricher — 422 no commit.

    Validar aqui, e não no ciclo, é o que faz uma URL inválida ou um campo
    obrigatório ausente falharem para QUEM ESCREVEU a config. O mesmo schema
    aplica o guard de egresso (``normalize_service_url`` no validator de ``url``
    do OpenCTI), então SSRF é recusado antes de virar linha no banco.
    """
    try:
        reg = enrich_registry.require(enricher)
    except Exception as exc:  # noqa: BLE001
        raise _bad_request(
            "enrichment.unknown_enricher",
            f"enricher {enricher!r} não existe; veja GET /enrichment/enrichers",
        ) from exc
    # Defesa em profundidade: a credencial entra SÓ pelo campo ``secret``, que o
    # servidor cifra. Se um enricher (ou um plugin EE amanhã) reintroduzir um
    # ``*_secret_ref`` no schema, aceitá-lo pela config reabriria o buraco
    # cross-tenant — ``core.secrets`` decifra qualquer ciphertext sem olhar org,
    # então o admin da Org A colaria o blob da Org B e usaria a credencial dela.
    offending = sorted(k for k in cfg if "secret" in k.lower())
    if offending:
        raise _bad_request(
            "enrichment.secret_in_config",
            f"campo(s) {offending} não podem vir na config — use o campo 'secret', "
            f"que o servidor cifra e vincula a esta organização",
        )
    schema = getattr(reg, "config_schema", None)
    if schema is None:
        return dict(cfg)
    try:
        return schema(**cfg).model_dump(mode="json")
    except Exception as exc:  # noqa: BLE001 — ValidationError e ValueError do guard
        raise _bad_request("enrichment.invalid_source_config", str(exc)) from exc


@router.get("/sources", response_model=List[SourceRead])
def list_sources(
    user: models.AppUser = Depends(app_auth.require_admin_user),
    db: Session = Depends(_db),
) -> List[SourceRead]:
    q = _visible_org_filter(db.query(models.EnrichmentSource), models.EnrichmentSource, user, db)
    return [_source_read(db, r) for r in q.order_by(models.EnrichmentSource.name).all()]


@router.post("/sources", response_model=SourceRead, status_code=status.HTTP_201_CREATED)
def create_source(
    payload: SourceCreate,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    db: Session = Depends(_db),
) -> SourceRead:
    org_id = _resolve_target_org(user, payload.organization_id)
    exists = (
        db.query(models.EnrichmentSource)
        .filter(
            models.EnrichmentSource.organization_id == org_id,
            models.EnrichmentSource.name == payload.name,
        )
        .first()
    )
    if exists is not None:
        raise _bad_request(
            "enrichment.source_exists",
            f"já existe uma fonte chamada {payload.name!r} nesta organização",
        )
    cfg = _validate_source_config(payload.enricher, payload.config)
    reg = enrich_registry.require(payload.enricher)
    if getattr(reg, "required_secrets", ()) and not payload.secret:
        raise _bad_request(
            "enrichment.secret_required",
            f"o enricher {payload.enricher!r} exige credencial "
            f"({', '.join(reg.required_secrets)})",
        )
    row = models.EnrichmentSource(
        organization_id=org_id,
        name=payload.name,
        enricher=payload.enricher,
        description=payload.description,
        config=json.dumps(cfg),
        secret_ref=_encrypt_secret(payload.secret) if payload.secret else None,
        enabled=payload.enabled,
    )
    db.add(row)
    db.flush()  # precisa do id antes de gravar a lista de orgs
    _sync_source_orgs(db, row, user, payload.shared_organization_ids)
    db.commit()
    db.refresh(row)
    return _source_read(db, row)


@router.patch("/sources/{source_id}", response_model=SourceRead)
def update_source(
    source_id: str,
    payload: SourceUpdate,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    db: Session = Depends(_db),
) -> SourceRead:
    row = _assert_visible(db.get(models.EnrichmentSource, source_id), user, "source")
    if payload.description is not None:
        row.description = payload.description
    if payload.config is not None:
        row.config = json.dumps(_validate_source_config(row.enricher, payload.config))
    if payload.secret is not None:
        # "" = remover explicitamente; None (ausente) = manter o que já está lá.
        row.secret_ref = _encrypt_secret(payload.secret) if payload.secret else None
    if payload.enabled is not None:
        row.enabled = payload.enabled
    _sync_source_orgs(db, row, user, payload.shared_organization_ids)
    db.commit()
    db.refresh(row)
    return _source_read(db, row)


@router.post("/sources/{source_id}/test", response_model=SourceTestResult)
def test_source(
    source_id: str,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    db: Session = Depends(_db),
) -> SourceTestResult:
    """Sonda a fonte de verdade: credencial, endpoint e formato do retorno.

    Existe porque hoje a única forma de descobrir que a chave está errada é ver
    eventos saindo sem contexto, horas depois, no destino. O erro do provedor
    (401, DNS, TLS, schema GraphQL divergente) fica no log do worker, longe de
    quem cadastrou a credencial.

    Roda em modo REDUZIDO: uma página, poucos registros. Não persiste nada e não
    toca em tráfego real, então é seguro apertar o botão quantas vezes quiser.
    """
    import asyncio
    import time

    row = _assert_visible(db.get(models.EnrichmentSource, source_id), user, "source")
    try:
        reg = enrich_registry.require(row.enricher)
    except Exception as exc:  # noqa: BLE001
        return SourceTestResult(ok=False, message=f"enricher {row.enricher!r} não existe: {exc}")

    try:
        cfg = json.loads(row.config or "{}")
    except Exception as exc:  # noqa: BLE001
        return SourceTestResult(ok=False, message=f"config não é JSON válido: {exc}")

    # Sondagem barata: 1 página curta. Sem isto, "testar" numa instância grande
    # baixaria a base inteira e o botão viraria um DoS contra o próprio cliente.
    probe_cfg = dict(cfg)
    probe_cfg.setdefault("page_size", 5)
    probe_cfg["max_pages"] = 1

    ctx = EnrichContext(
        organization_id=int(row.organization_id),
        config=probe_cfg,
        secret_ref=row.secret_ref,
    )
    started = time.monotonic()
    try:
        instance = reg.factory(probe_cfg)
    except Exception as exc:  # noqa: BLE001 — ValidationError do config_schema
        return SourceTestResult(ok=False, message=f"config recusada: {exc}")

    try:
        if hasattr(instance, "load"):
            table = asyncio.run(instance.load(ctx))
            rows = getattr(table, "_rows", {}) or {}
            first = next(iter(rows.items()), None)
            return SourceTestResult(
                ok=True,
                message="Conexão ok.",
                sample_count=len(rows),
                sample={first[0]: first[1]} if first else None,
                elapsed_ms=(time.monotonic() - started) * 1000.0,
            )
        # Enricher remoto: resolve uma chave sabidamente inexistente. O que
        # importa é a credencial ser aceita, não haver resultado.
        probe_key = "8.8.8.8"
        resolved = asyncio.run(instance.resolve([probe_key], ctx))
        return SourceTestResult(
            ok=True,
            message="Credencial aceita pelo provedor.",
            sample_count=len(resolved or {}),
            sample={probe_key: (resolved or {}).get(probe_key)},
            elapsed_ms=(time.monotonic() - started) * 1000.0,
        )
    except PermissionError as exc:
        return SourceTestResult(ok=False, message=f"Credencial recusada: {exc}")
    except Exception as exc:  # noqa: BLE001 — o motivo real ajuda mais que 500
        return SourceTestResult(
            ok=False,
            message=f"{type(exc).__name__}: {exc}",
            elapsed_ms=(time.monotonic() - started) * 1000.0,
        )


@router.delete("/sources/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_source(
    source_id: str,
    user: models.AppUser = Depends(app_auth.require_admin_user),
    db: Session = Depends(_db),
) -> None:
    row = _assert_visible(db.get(models.EnrichmentSource, source_id), user, "source")
    # Mesma disciplina do delete de tabela: apagar uma fonte em uso quebraria a
    # regra em silêncio a cada ciclo, e o operador só descobriria pelo evento sem
    # contexto no destino.
    users = _policies_referencing_source(db, row.organization_id, row.name)
    if users:
        raise _bad_request(
            "enrichment.source_in_use",
            f"fonte {row.name!r} é usada pela(s) política(s): {', '.join(users)}",
        )
    db.delete(row)
    db.commit()


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


def _policies_referencing_source(db: Session, org_id: int, source_name: str) -> List[str]:
    """Nomes das políticas cuja versão VIGENTE cita a fonte configurada."""
    return _policies_referencing_field(db, org_id, "source", source_name)


def _policies_referencing_table(db: Session, org_id: int, table_name: str) -> List[str]:
    """Nomes das políticas cuja versão VIGENTE referencia a tabela."""
    return _policies_referencing_field(db, org_id, "table", table_name)


def _policies_referencing_field(
    db: Session, org_id: int, field: str, value: str
) -> List[str]:
    """Políticas da org cuja versão vigente tem alguma regra com ``field == value``.

    O filtro por ``organization_id`` não é otimização: uma política de OUTRA org
    que cite o mesmo nome não pode contar como "em uso" aqui, senão o delete
    passaria a vazar a existência de recurso alheio pela mensagem de erro.
    """
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
            isinstance(r, dict) and r.get(field) == value for r in rules
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
