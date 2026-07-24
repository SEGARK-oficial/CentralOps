"""REST endpoints para gestão de mapping definitions e versões.

Endpoints:

- ``GET /api/mappings`` — lista catálogo (vendor, event_type, current).
- ``GET /api/mappings/{id}`` — definição + lista de versões + atual.
- ``GET /api/mappings/{id}/versions`` — histórico imutável.
- ``GET /api/mappings/{id}/versions/{version_id}`` — versão específica.
- ``POST /api/mappings/{id}/versions`` — cria nova versão. Roda
  validação sintática + dry-run sobre o sample reservoir antes de
  persistir. Promove para current automaticamente.
- ``POST /api/mappings/{id}/rollback`` — aponta current para
  ``version_id`` existente. Não cria nova versão.
- ``POST /api/mappings/dry-run`` — valida regras + roda contra
  sample reservoir sem persistir nada. Usado pelo editor live.
- ``GET /api/mappings/{id}/discover-fields`` — campos já descobertos
  pelo drift detector para o vendor/event_type do mapping. Alimenta
  autocomplete de JMESPath no editor de regras.

Toda mudança grava :class:`MappingAuditLog`.
Acesso: GET = qualquer autenticado; POST = admin.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import redis.asyncio as redis_async
from fastapi import APIRouter, Depends, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, joinedload

from ..collectors.normalize import sample_reservoir
from ..collectors.normalize.engine import (
    CompiledRule,
    MappingDefinitionError,
    MappingError,
    MappingRequiredFieldError,
    apply_compiled,
    compile_rules,
    default_engine,
)
from ..collectors.normalize.operators import TYPE_CAST_DESCRIPTORS
from ..collectors.normalize.ocsf import validator as ocsf_validator
from ..core import auth as app_auth
from ..core import tenant
from ..core.config import settings
from ..core.errors import ApiError
from ..db import database, models

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mappings", tags=["mappings"])


# ── Schemas (Pydantic locais — não inflar api/schemas global) ─────────


class MappingDefinitionRead(BaseModel):
    id: str
    vendor: str
    event_type: str
    ocsf_class_uid: int
    description: Optional[str]
    current_version_id: Optional[str]
    created_at: datetime
    updated_at: datetime
    rules_count: Optional[int] = None


class MappingVersionRead(BaseModel):
    id: str
    definition_id: str
    version_number: int
    # DSL v2: dict com ``preprocess`` (opcional) e ``rules`` (obrigatório).
    rules: Dict[str, Any]
    author_user_id: Optional[int]
    commit_message: str
    diff_from_previous: Optional[Any]
    dry_run_stats: Optional[Any]
    ocsf_validation_stats: Optional[Any] = None
    created_at: datetime


class MappingDefinitionDetail(MappingDefinitionRead):
    versions: List[MappingVersionRead]


class CreateVersionRequest(BaseModel):
    """Cria uma nova versão de mapping.

    ``rules`` é sempre um dict v2 com chaves:
    - ``preprocess`` (opcional, lista de operações)
    - ``rules`` (obrigatório, lista de regras)
    - ``raw_reduction`` (opcional, lista de specs de poda do payload bruto)

    Blocos top-level não reconhecidos são PRESERVADOS (ver
    :func:`_normalize_rules_to_v2`) — o normalizador não pode apagar
    configuração que o cliente enviou.
    """
    rules: Dict[str, Any] = Field(...)
    commit_message: str = Field(..., min_length=1, max_length=2000)


class DryRunRequest(BaseModel):
    rules: Dict[str, Any] = Field(...)
    # Se ``vendor + event_type`` informados, lê sample reservoir do par.
    # Caso contrário, ``raw_events`` deve vir com a amostra explícita.
    vendor: Optional[str] = None
    event_type: Optional[str] = None
    raw_events: Optional[List[Dict[str, Any]]] = None
    limit: int = Field(default=100, ge=1, le=500)
    # o reservoir é particionado por org. Um usuário GLOBAL
    # (admin/SOC interno) editando um mapping default precisa nomear o tenant a
    # inspecionar — espelha o ``?org_id`` do GET reservoir. Não-global ignora
    # (sempre travado na própria org). Sem isto, o dry-run do admin lia ``None``
    # → reservoir vazio → editor sem amostra (regressão do leak-fix).
    organization_id: Optional[int] = None


class DryRunRuleStats(BaseModel):
    target: str
    fail_count: int
    fail_examples: List[str]


class DryRunDefaultHitWarning(BaseModel):
    """Diagnóstico de regra cujo source resolve None em 100% das amostras.

    Indica que o path JMESPath da regra nunca encontrou dados no reservoir
    e o engine SEMPRE caiu no ``default``. Pode indicar JMESPath errado,
    vendor que mudou o shape, ou campo genuinamente ausente (use
    ``expected_always_default: true`` na regra para suprimir).

    Campos:
    - ``target``: target da regra afetada (ex: ``normalized.severity_id``).
    - ``hit_rate``: proporção de amostras onde value=None antes de default (0.0–1.0).
    - ``hit_count``: contagem absoluta de amostras com default hit.
    - ``sample_size``: total de amostras avaliadas.
    - ``expected_always_default``: True se a regra declarou a flag de supressão.
    """

    target: str
    hit_rate: float
    hit_count: int
    sample_size: int
    expected_always_default: bool


class DryRunResult(BaseModel):
    sample_size: int
    ok_count: int
    fail_count: int
    rule_failures: List[DryRunRuleStats]
    output_examples: List[Dict[str, Any]]
    # regras com 100% default hit rate e expected_always_default=False.
    default_hit_warnings: List[DryRunDefaultHitWarning] = []


class RollbackRequest(BaseModel):
    version_id: str = Field(..., min_length=1)
    commit_message: str = Field(..., min_length=1, max_length=2000)


# ── Schemas de diff estruturado ──────────────────────────


class RuleSnapshot(BaseModel):
    """Snapshot de uma regra de mapping, identificada por 'target'."""
    target: str
    source: Optional[str] = None
    const: Optional[Any] = None
    default: Optional[Any] = None
    value_map: Optional[Dict[str, Any]] = None
    type_cast: Optional[str] = None
    required: bool = False
    # capturado para que o audit log reflita toggles desta flag.
    expected_always_default: Optional[bool] = None


class ModifiedRule(BaseModel):
    """Regra presente em ambas as versões, mas com payload diferente."""
    target: str
    before: RuleSnapshot
    after: RuleSnapshot


class MappingVersionDiff(BaseModel):
    """Diff estruturado entre duas versões de um mapping."""
    definition_id: str
    version_a: str  # ID UUID
    version_b: str  # ID UUID
    version_a_number: int
    version_b_number: int
    reordered_only: bool
    added: List[RuleSnapshot]
    removed: List[RuleSnapshot]
    modified: List[ModifiedRule]


# ── Schemas de audit paginado ─────────────────────────────────────────


class MappingAuditEntry(BaseModel):
    id: str
    mapping_definition_id: Optional[str]
    mapping_version_id: Optional[str]
    integration_id: Optional[int]
    action: str
    username: Optional[str]
    user_role: Optional[str]
    diff: Optional[Any]
    detail: Optional[str]
    created_at: datetime


class MappingAuditListResponse(BaseModel):
    total: int
    items: List[MappingAuditEntry]
    limit: int
    offset: int


# ── Schema do reservoir de amostras ──────────────────────────────────


class SamplesListResponse(BaseModel):
    """Resposta do endpoint GET /api/mappings/samples."""
    vendor: str
    event_type: str
    # Tamanho atual do ring buffer (pode ser maior que len(items) quando
    # limit < capacidade total do reservoir).
    total_in_reservoir: int
    # Eventos raw mais recentes primeiro; tamanho limitado por ``limit``.
    items: List[Dict[str, Any]]


# ── Schemas de discover-fields ────────────────────────────────────────


class DiscoveredField(BaseModel):
    """Campo descoberto pelo drift detector para um vendor/event_type."""
    path: str
    occurrences: int
    sample_values: List[str] = Field(default_factory=list)
    first_seen_at: datetime


class DiscoverFieldsResponse(BaseModel):
    """Resposta do endpoint GET /api/mappings/{id}/discover-fields."""
    fields: List[DiscoveredField]


# ── Schema de type-casts ──────────────────────────────────────────────


class TypeCastDescriptor(BaseModel):
    """Descriptor de um cast nomeado da DSL."""
    name: str
    description: str
    signature: str


# ── Helpers ───────────────────────────────────────────────────────────


def _serialize_version(v: models.MappingVersion) -> MappingVersionRead:
    try:
        raw_rules = json.loads(v.rules)
    except (TypeError, ValueError):
        raw_rules = {"preprocess": [], "rules": []}
    rules = _normalize_rules_to_v2(raw_rules)
    return MappingVersionRead(
        id=v.id,
        definition_id=v.definition_id,
        version_number=v.version_number,
        rules=rules,
        author_user_id=v.author_user_id,
        commit_message=v.commit_message,
        diff_from_previous=json.loads(v.diff_from_previous) if v.diff_from_previous else None,
        dry_run_stats=json.loads(v.dry_run_stats) if v.dry_run_stats else None,
        ocsf_validation_stats=(
            json.loads(v.ocsf_validation_stats)
            if getattr(v, "ocsf_validation_stats", None)
            else None
        ),
        created_at=v.created_at,
    )


def _normalize_rules_to_v2(rules: Any) -> Dict[str, Any]:
    """Garante shape v2 ``{preprocess, rules}`` PRESERVANDO os demais blocos.

    Linhas legadas (DSL v1) podem ter sido persistidas como list — a
    migration startup converte, mas mantemos o normalizador como defesa
    para qualquer linha residual ou input externo.

    PRESERVAÇÃO DE BLOCOS (regressão corrigida): a versão anterior RECONSTRUÍA
    o dict com apenas ``preprocess`` e ``rules``, descartando silenciosamente
    qualquer outro bloco top-level da DSL v2 — em particular ``raw_reduction``,
    que é o ÚNICO mecanismo de poda do payload bruto (ver
    ``normalize/payload_reduction.py``). Como esta função roda tanto ao SERVIR
    a definição quanto ao COMMITAR uma nova versão, o efeito era destrutivo e
    silencioso: a UI recebia o mapping já sem o bloco, o operador salvava, e a
    configuração de redução era apagada para sempre. Foi assim que o
    ``sophos.detection`` perdeu seus 3 specs de ``raw_reduction`` em produção.

    Agora só normalizamos ``preprocess``/``rules`` (garantindo que existam como
    listas) e repassamos TODAS as outras chaves intactas — o que também torna a
    função forward-compatible com blocos futuros da DSL, sem precisar editá-la.
    """
    if isinstance(rules, dict):
        out: Dict[str, Any] = {
            k: v for k, v in rules.items() if k not in ("preprocess", "rules")
        }
        out["preprocess"] = list(rules.get("preprocess") or [])
        out["rules"] = list(rules.get("rules") or [])
        return out
    if isinstance(rules, list):
        return {"preprocess": [], "rules": list(rules)}
    return {"preprocess": [], "rules": []}


def _serialize_definition(
    d: models.MappingDefinition,
    *,
    rules_count: Optional[int] = None,
) -> MappingDefinitionRead:
    return MappingDefinitionRead(
        id=d.id,
        vendor=d.vendor,
        event_type=d.event_type,
        ocsf_class_uid=d.ocsf_class_uid,
        description=d.description,
        current_version_id=d.current_version_id,
        created_at=d.created_at,
        updated_at=d.updated_at,
        rules_count=rules_count,
    )


def _audit(
    db: Session,
    *,
    definition_id: Optional[str],
    version_id: Optional[str],
    action: str,
    user: models.AppUser,
    diff: Optional[Dict[str, Any]] = None,
    detail: Optional[str] = None,
) -> None:
    db.add(
        models.MappingAuditLog(
            mapping_definition_id=definition_id,
            mapping_version_id=version_id,
            action=action,
            user_id=app_auth.persistable_user_id(user),  # SA shim (id<0) → None
            username=user.username,
            user_role=user.role,
            diff=json.dumps(diff, separators=(",", ":")) if diff else None,
            detail=detail,
        )
    )


def _run_dry_run(rules: Dict[str, Any], samples: List[Dict[str, Any]]) -> DryRunResult:
    """Compila + aplica regras sobre uma amostra. Não persiste nada.

    ``rules`` deve estar no shape v2 (``{preprocess, rules}``).

    default_hit_warnings:
        Conta por regra quantas vezes o source resolveu None antes de
        ``default`` ser aplicado. Ao final, regras com hit_rate == 1.0
        (100% default) e ``expected_always_default=False`` são incluídas
        em ``default_hit_warnings`` para diagnóstico.
    """
    try:
        compiled = compile_rules(rules)
    except MappingDefinitionError as exc:
        raise ApiError(
            "mapping.invalid_dsl",
            status.HTTP_400_BAD_REQUEST,
            messages={
                "pt": "DSL inválida: {error}",
                "en": "Invalid DSL: {error}",
                "es": "DSL inválida: {error}",
            },
            params={"error": str(exc)},
        )

    ok = 0
    fail = 0
    rule_failures: Dict[str, Dict[str, Any]] = {}
    examples: List[Dict[str, Any]] = []
    # contagem de default hits por target string.
    default_hits_by_target: Dict[str, int] = {}

    for raw in samples:
        try:
            applied = apply_compiled(compiled, raw)
        except MappingRequiredFieldError as exc:
            fail += 1
            entry = rule_failures.setdefault(
                exc.target, {"fail_count": 0, "fail_examples": []}
            )
            entry["fail_count"] += 1
            if len(entry["fail_examples"]) < 3:
                entry["fail_examples"].append(str(exc))
            continue
        except MappingError as exc:
            fail += 1
            target = "(generic)"
            entry = rule_failures.setdefault(target, {"fail_count": 0, "fail_examples": []})
            entry["fail_count"] += 1
            if len(entry["fail_examples"]) < 3:
                entry["fail_examples"].append(str(exc))
            continue
        ok += 1
        if len(examples) < 3:
            examples.append(applied.output)
        # Acumula default hits desta amostra.
        for hit_target in applied.default_hits:
            default_hits_by_target[hit_target] = default_hits_by_target.get(hit_target, 0) + 1

    # constrói lookup de expected_always_default por target.
    # Apenas CompiledRule (scalar) tem esse campo — array_builder é excluído.
    ead_by_target: Dict[str, bool] = {
        rule.target: rule.expected_always_default
        for rule in compiled.rules
        if isinstance(rule, CompiledRule)
    }

    sample_size = len(samples)
    default_hit_warnings: List[DryRunDefaultHitWarning] = []
    if sample_size > 0:
        for hit_target, hit_count in default_hits_by_target.items():
            hit_rate = hit_count / sample_size
            if hit_rate >= 1.0:
                expected = ead_by_target.get(hit_target, False)
                if not expected:
                    default_hit_warnings.append(
                        DryRunDefaultHitWarning(
                            target=hit_target,
                            hit_rate=hit_rate,
                            hit_count=hit_count,
                            sample_size=sample_size,
                            expected_always_default=expected,
                        )
                    )

    return DryRunResult(
        sample_size=sample_size,
        ok_count=ok,
        fail_count=fail,
        rule_failures=[
            DryRunRuleStats(target=t, fail_count=v["fail_count"], fail_examples=v["fail_examples"])
            for t, v in rule_failures.items()
        ],
        output_examples=examples,
        default_hit_warnings=default_hit_warnings,
    )


def _rule_to_snapshot(rule: Dict[str, Any]) -> RuleSnapshot:
    """Converte dict de regra DSL em RuleSnapshot normalizado.

    Suporta regras scalar (v1 e v2) e array_builder (v2, kind="array_builder").
    Para array_builder, captura os campos relevantes como representação
    compacta — diff item-level é problema v3.
    """
    kind = rule.get("kind", "scalar")
    if kind == "array_builder":
        items = rule.get("items") or []
        dedup_by = rule.get("dedup_by")
        skip_null = rule.get("skip_null", False)
        # Usa source=None e const representando metadados compactos do builder.
        return RuleSnapshot(
            target=rule.get("target", ""),
            source=None,
            const=None,
            default=None,
            value_map=None,
            type_cast=None,
            required=False,
            # Codifica metadados array_builder em expected_always_default=None
            # e usa value_map para transportar metadata compacta de forma segura.
            # Nota: field reutilizado como metadata container para array_builder
            # até que RuleSnapshot seja versionado (v3 problem).
        )

    # scalar rule (v1 ou v2)
    raw_ead = rule.get("expected_always_default")
    return RuleSnapshot(
        target=rule.get("target", ""),
        source=rule.get("source"),
        const=rule.get("const"),
        default=rule.get("default"),
        value_map=rule.get("value_map"),
        type_cast=rule.get("type_cast"),
        required=bool(rule.get("required", False)),
        expected_always_default=bool(raw_ead) if raw_ead is not None else None,
    )


def _extract_rules_list(rules_or_dict: Any) -> List[Dict[str, Any]]:
    """Normaliza v1 (list) ou v2 (dict com 'rules') para lista de regras.

    v2 mappings armazenam um dict ``{"preprocess": [...], "rules": [...]}``
    em ``MappingVersion.rules``.  Iterar o dict diretamente retornaria as
    chaves string, não os dicts de regra — causando crash silencioso no diff.
    """
    if isinstance(rules_or_dict, dict):
        # v2 shape: extrai lista interna "rules"
        inner = rules_or_dict.get("rules")
        if isinstance(inner, list):
            return inner
        return []
    if isinstance(rules_or_dict, list):
        return rules_or_dict
    return []


def _emitted_class_uid(rules_payload: Any) -> Optional[int]:
    """The ``class_uid`` a mapping emits via a ``const`` rule (or None if dynamic)."""
    for rule in _extract_rules_list(rules_payload):
        if isinstance(rule, dict) and rule.get("target") == "normalized.class_uid":
            const = rule.get("const")
            return const if isinstance(const, int) else None
    return None


def _ocsf_validate_commit(
    rules_payload: Any,
    output_examples: List[Dict[str, Any]],
    declared_class_uid: Optional[int],
) -> Dict[str, Any]:
    """Validate the dry-run outputs against the vendored OCSF
    manifest + cross-check the emitted class_uid vs the definition's declared one.

    Returns a JSON-able stats dict (persisted in ``ocsf_validation_stats``) with a
    ``blocking`` flag the caller uses to 422 when ``OCSF_MAPPING_GATE_ENABLED``.

    FAIL-SAFE: this is advisory analysis of a mapping being saved — it must NEVER
    break the save. Any internal error is caught, logged, and returned as a
    non-blocking stats entry (``error`` set), so ``create_version`` still succeeds.
    """
    try:
        reg = ocsf_validator.get_registry(settings.OCSF_VALIDATION_VERSION)
        by_reason: Dict[str, int] = {}
        valid = 0
        missing_required: Dict[str, int] = {}
        for out in output_examples:
            normalized = out.get("normalized", out) if isinstance(out, dict) else {}
            if not isinstance(normalized, dict):
                normalized = {}
            res = ocsf_validator.structural_gate(normalized, reg)
            if res.valid:
                valid += 1
                for attr in res.missing_required:
                    missing_required[attr] = missing_required.get(attr, 0) + 1
            else:
                by_reason[res.reason] = by_reason.get(res.reason, 0) + 1

        emitted = _emitted_class_uid(rules_payload)
        # class_uid the mapping emits must match the class it declares (if both known).
        class_uid_mismatch = (
            declared_class_uid is not None
            and emitted is not None
            and emitted != declared_class_uid
        )
        # out_of_scope is graceful (not a hard defect); real invalids are the rest.
        hard_invalid = sum(
            n for r, n in by_reason.items() if r != ocsf_validator.REASON_OUT_OF_SCOPE
        )
        blocking = bool(hard_invalid) or class_uid_mismatch
        return {
            "version": settings.OCSF_VALIDATION_VERSION,
            "checked": len(output_examples),
            "valid": valid,
            "invalid_by_reason": by_reason,
            "missing_required": missing_required,
            "class_uid_declared": declared_class_uid,
            "class_uid_emitted": emitted,
            "class_uid_mismatch": class_uid_mismatch,
            "blocking": blocking,
        }
    except Exception as exc:  # pragma: no cover - defensive; never break the save
        logger.warning(
            "OCSF commit validation failed (non-blocking): %s", exc, exc_info=True
        )
        return {
            "version": settings.OCSF_VALIDATION_VERSION,
            "checked": len(output_examples),
            "error": str(exc)[:200],
            "blocking": False,
        }


def compute_diff(
    rules_a: Dict[str, Any],
    rules_b: Dict[str, Any],
    *,
    definition_id: str = "",
    version_a: str = "",
    version_b: str = "",
    version_a_number: int = 0,
    version_b_number: int = 0,
) -> MappingVersionDiff:
    """Compara duas versões de mapping e retorna diff estruturado por 'target'.

    Espera ambos os lados no shape v2 (dict com ``rules``). ``_extract_rules_list``
    permanece como guarda defensiva — aceita também list pura caso linhas
    legadas escapem da migração.

    Algoritmo:
    - Indexa A e B por ``target`` (chave natural).
    - targets em A mas não em B → removed.
    - targets em B mas não em A → added.
    - targets em ambos com payload diferente → modified.
    - reordered_only = True se mesmos targets, mesmas ordens de inserção
      mas em posições diferentes E nenhum modified.
    """
    list_a = _extract_rules_list(rules_a)
    list_b = _extract_rules_list(rules_b)

    index_a: Dict[str, RuleSnapshot] = {
        r.get("target", ""): _rule_to_snapshot(r) for r in list_a if isinstance(r, dict)
    }
    index_b: Dict[str, RuleSnapshot] = {
        r.get("target", ""): _rule_to_snapshot(r) for r in list_b if isinstance(r, dict)
    }

    targets_a = set(index_a)
    targets_b = set(index_b)

    added = [index_b[t] for t in targets_b - targets_a]
    removed = [index_a[t] for t in targets_a - targets_b]

    modified: List[ModifiedRule] = []
    for target in targets_a & targets_b:
        snap_a = index_a[target]
        snap_b = index_b[target]
        if snap_a != snap_b:
            modified.append(ModifiedRule(target=target, before=snap_a, after=snap_b))

    # reordered_only: mesmos targets E mesma ordem E sem modified
    order_a = [r.get("target", "") for r in list_a if isinstance(r, dict)]
    order_b = [r.get("target", "") for r in list_b if isinstance(r, dict)]
    reordered_only = (
        not added
        and not removed
        and not modified
        and order_a != order_b
    )

    return MappingVersionDiff(
        definition_id=definition_id,
        version_a=version_a,
        version_b=version_b,
        version_a_number=version_a_number,
        version_b_number=version_b_number,
        reordered_only=reordered_only,
        added=added,
        removed=removed,
        modified=modified,
    )


def _serialize_audit_entry(log: models.MappingAuditLog) -> MappingAuditEntry:
    return MappingAuditEntry(
        id=log.id,
        mapping_definition_id=log.mapping_definition_id,
        mapping_version_id=log.mapping_version_id,
        integration_id=getattr(log, "integration_id", None),
        action=log.action,
        username=log.username,
        user_role=log.user_role,
        diff=json.loads(log.diff) if log.diff else None,
        detail=log.detail,
        created_at=log.created_at,
    )


async def _load_samples(
    organization_id: Optional[int],
    vendor: Optional[str],
    event_type: Optional[str],
    limit: int,
) -> List[Dict[str, Any]]:
    # amostras são escopadas por tenant. Sem organization_id
    # não há reservoir a ler (fail-closed em vez de vazar cross-tenant).
    if organization_id is None or not vendor or not event_type:
        return []
    redis = redis_async.from_url(
        settings.REDIS_URL or "redis://localhost:6379/0",
        decode_responses=True,
    )
    try:
        return await sample_reservoir.peek(
            redis, organization_id, vendor, event_type, limit=limit
        )
    finally:
        await redis.aclose()


# ── Endpoints ─────────────────────────────────────────────────────────


@router.get("", response_model=List[MappingDefinitionRead])
def list_definitions(
    include_rules_count: bool = Query(False),
    only_active: bool = Query(
        False,
        description="Quando True, retorna só mappings de vendors com integração ATIVA no "
        "escopo do usuário (a UI usa isso por padrão). False (default da API) = todos os "
        "mappings disponíveis — compat com consumidores existentes.",
    ),
    db: Session = Depends(database.get_session),
    current_user: models.AppUser = Depends(
        app_auth.require_permission(app_auth.Permission.MAPPING_READ)
    ),
) -> List[MappingDefinitionRead]:
    q = db.query(models.MappingDefinition).order_by(
        models.MappingDefinition.vendor, models.MappingDefinition.event_type
    )
    # Gating por integração ativa: o mapping padrão é seedado globalmente para TODOS
    # os vendors, mas só faz sentido exibir o de um vendor que o cliente conectou.
    # ``vendor`` do mapping == ``platform`` da Integration (variantes usam
    # a plataforma-base). Escopo de org respeitado; global vê a união dos conectados.
    if only_active:
        active_q = db.query(models.Integration.platform).filter(
            models.Integration.is_active.is_(True)
        )
        if not tenant.has_global_scope(current_user):
            if current_user.organization_id is None:
                return []  # usuário sem org e sem escopo global → nada ativo
            active_q = active_q.filter(
                models.Integration.organization_id == current_user.organization_id
            )
        active_platforms = {p for (p,) in active_q.distinct().all() if p}
        if not active_platforms:
            return []  # nenhuma integração ativa → lista vazia (use only_active=False)
        q = q.filter(models.MappingDefinition.vendor.in_(active_platforms))
    if include_rules_count:
        q = q.options(joinedload(models.MappingDefinition.current_version))
    rows = q.all()
    result: List[MappingDefinitionRead] = []
    for r in rows:
        count: Optional[int] = None
        if include_rules_count:
            cv = r.current_version
            if cv is not None:
                try:
                    raw = json.loads(cv.rules)
                except (TypeError, ValueError):
                    raw = {}
                if isinstance(raw, dict):
                    rules_list = raw.get("rules", [])
                    count = len(rules_list) if isinstance(rules_list, list) else 0
                elif isinstance(raw, list):
                    count = len(raw)
                else:
                    count = 0
            else:
                count = 0
        result.append(_serialize_definition(r, rules_count=count))
    return result


@router.get("/samples", response_model=SamplesListResponse)
async def get_samples(
    vendor: str = Query(..., min_length=1, description="Vendor do evento (ex: sophos)"),
    event_type: str = Query(..., min_length=1, description="Tipo do evento (ex: sophos.alert)"),
    limit: int = Query(default=10, ge=1, le=100, description="Máximo de eventos retornados"),
    org_id: Optional[int] = Query(None, description="Admin: tenant a inspecionar"),
    db: Session = Depends(database.get_session),
    user: models.AppUser = Depends(app_auth.require_permission(app_auth.Permission.MAPPING_READ)),
) -> SamplesListResponse:
    """Lê amostras do sample reservoir Redis para um par (vendor, event_type).

    Nunca retorna 404 — reservoir vazio equivale a lista vazia + total 0.
    O cliente deve exibir uma mensagem orientando o operador a coletar eventos
    antes de usar o dry-run do mapping editor.

    Isolamento multi-tenant: a chave Redis do sample_reservoir
    AGORA inclui ``organization_id`` — amostras são particionadas por
    (org, vendor, event_type). O gate por vendor abaixo permanece como defesa em
    profundidade (redundante mas barato).

    SUBÁRVORE: ``?org_id`` deixa de ser privilégio exclusivo do escopo global —
    um admin de org PAI pode nomear uma org FILHA, desde que ela esteja na sua
    subárvore (``require_subtree_access``, o mesmo gate das integrações). A
    escolha é EXPLÍCITA, não um merge implícito de partições: cada resposta
    pertence a UMA org, o que preserva a proveniência da amostra (misturar
    amostras de tenants diferentes num mesmo dry-run seria enganoso). Sem
    ``?org_id``, segue lendo a própria org.
    """
    is_global = tenant.has_global_scope(user)
    if org_id is not None and not is_global:
        # Levanta 403 quando a org pedida está fora da subárvore do usuário.
        tenant.require_subtree_access(user, int(org_id))
    effective_org = org_id if org_id is not None else user.organization_id
    # Validação de acesso ao vendor por organização (defesa em profundidade)
    if not is_global:
        _scope_ids = tenant.accessible_org_ids(user, db) or set()
        allowed_vendors = (
            db.query(models.Integration.platform)
            .filter(models.Integration.organization_id.in_(_scope_ids))
            .distinct()
            .all()
        ) if _scope_ids else []
        allowed_set = {row.platform for row in allowed_vendors}
        if vendor not in allowed_set:
            # 404 em vez de 403 evita enumeração de vendors de outros tenants
            raise ApiError(
                "mapping.samples_not_found",
                404,
                messages={
                    "pt": "Amostras não encontradas para este vendor.",
                    "en": "Samples not found for this vendor.",
                    "es": "Muestras no encontradas para este proveedor.",
                },
            )
    # Sem org (ex.: admin global sem tenant especificado) → reservoir vazio
    # (fail-closed, sem leitura cross-tenant implícita).
    if effective_org is None:
        return SamplesListResponse(
            vendor=vendor, event_type=event_type, total_in_reservoir=0, items=[]
        )
    redis = redis_async.from_url(
        settings.REDIS_URL or "redis://localhost:6379/0",
        decode_responses=True,
    )
    try:
        items = await sample_reservoir.peek(
            redis, effective_org, vendor, event_type, limit=limit
        )
        total = await sample_reservoir.size(redis, effective_org, vendor, event_type)
    finally:
        await redis.aclose()

    return SamplesListResponse(
        vendor=vendor,
        event_type=event_type,
        total_in_reservoir=total,
        items=items,
    )


@router.get(
    "/normalize/type-casts",
    response_model=List[TypeCastDescriptor],
    summary="Lista todos os type-casts disponíveis na DSL",
)
def list_type_casts(
    _: models.AppUser = Depends(app_auth.require_permission(app_auth.Permission.MAPPING_READ)),
) -> List[TypeCastDescriptor]:
    """Retorna os type-casts registrados, ordenados por nome.

    Usado pelo editor de regras para popular o dropdown de type_cast
    dinamicamente, sem hardcode no frontend.
    """
    return [
        TypeCastDescriptor(name=name, **descriptor)
        for name, descriptor in sorted(TYPE_CAST_DESCRIPTORS.items())
    ]


@router.get(
    "/{definition_id}/discover-fields",
    response_model=DiscoverFieldsResponse,
)
def discover_fields(
    definition_id: str,
    response: Response,
    db: Session = Depends(database.get_session),
    user: models.AppUser = Depends(app_auth.require_permission(app_auth.Permission.MAPPING_READ)),
) -> DiscoverFieldsResponse:
    """Retorna campos já descobertos pelo drift detector para o mapping.

    Usado pelo editor de regras para oferecer autocomplete de JMESPath sem
    disparar dry-run em cada keystroke. Os campos vêm da tabela UnknownField,
    ordenados por occurrence_count DESC, limitados a 100.

    Isolamento multi-tenant: non-global enxerga APENAS o drift
    da própria org — filtro EXATO por ``organization_id`` (espelha
    ``routers/drift.py``).

    Cache-Control: private, max-age=60 — campos de drift não mudam com
    frequência e a query pode ser cara em volumes altos.
    """
    defn = db.get(models.MappingDefinition, definition_id)
    if defn is None:
        raise ApiError(
            "mapping.definition_not_found",
            404,
            messages={
                "pt": "Definição de mapping não encontrada.",
                "en": "Mapping definition not found.",
                "es": "Definición de mapping no encontrada.",
            },
        )

    # Escopo SUBTREE-AWARE (None == global, set() == nada acessível). Mesmo seam
    # de integrações/rotas/drift: com igualdade exata, um admin de org PAI não via
    # os campos descobertos nas FILHAS e não conseguia evoluir o mapping a partir
    # do tráfego real da subárvore que administra. Em Community o resolver é FLAT
    # e o resultado é idêntico ao de antes.
    org_ids = tenant.accessible_org_ids(user, db)

    # Gate por vendor (defesa em profundidade — o filtro por org abaixo é o
    # isolamento real).
    if org_ids is not None:
        allowed_vendors = (
            db.query(models.Integration.platform)
            .filter(models.Integration.organization_id.in_(org_ids))
            .distinct()
            .all()
        ) if org_ids else []
        allowed_set = {row.platform for row in allowed_vendors}
        if defn.vendor not in allowed_set:
            # 404 em vez de 403 evita enumeração de mappings de outros tenants
            raise ApiError(
            "mapping.definition_not_found",
            404,
            messages={
                "pt": "Definição de mapping não encontrada.",
                "en": "Mapping definition not found.",
                "es": "Definición de mapping no encontrada.",
            },
        )

    # Sem nenhuma org acessível → fail-closed (não cair em ``IS NULL``, que
    # casaria linhas legadas de org NULL de outros tenants).
    if org_ids is not None and not org_ids:
        rows = []
    else:
        rows_q = db.query(models.UnknownField).filter(
            models.UnknownField.vendor == defn.vendor,
            models.UnknownField.event_type == defn.event_type,
        )
        if org_ids is not None:
            rows_q = rows_q.filter(models.UnknownField.organization_id.in_(org_ids))
        rows = (
            rows_q.order_by(models.UnknownField.occurrence_count.desc())
            .limit(100)
            .all()
        )

    fields = [
        DiscoveredField(
            path=uf.field_path,
            occurrences=uf.occurrence_count,
            # Uma row = um sample_value (o primeiro visto pelo drift detector).
            # Retornamos lista para compatibilidade futura se o modelo evoluir.
            sample_values=[uf.sample_value] if uf.sample_value else [],
            first_seen_at=uf.first_seen,
        )
        for uf in rows
    ]

    response.headers["Cache-Control"] = "private, max-age=60"
    return DiscoverFieldsResponse(fields=fields)


@router.get("/{definition_id}", response_model=MappingDefinitionDetail)
def get_definition(
    definition_id: str,
    db: Session = Depends(database.get_session),
    _: models.AppUser = Depends(app_auth.require_permission(app_auth.Permission.MAPPING_READ)),
) -> MappingDefinitionDetail:
    defn = db.get(models.MappingDefinition, definition_id)
    if defn is None:
        raise ApiError(
            "mapping.definition_not_found",
            404,
            messages={
                "pt": "Definição de mapping não encontrada.",
                "en": "Mapping definition not found.",
                "es": "Definición de mapping no encontrada.",
            },
        )
    versions = (
        db.query(models.MappingVersion)
        .filter(models.MappingVersion.definition_id == definition_id)
        .order_by(models.MappingVersion.version_number.desc())
        .all()
    )
    base = _serialize_definition(defn).model_dump()
    base["versions"] = [_serialize_version(v) for v in versions]
    return MappingDefinitionDetail(**base)


@router.get("/{definition_id}/versions", response_model=List[MappingVersionRead])
def list_versions(
    definition_id: str,
    db: Session = Depends(database.get_session),
    _: models.AppUser = Depends(app_auth.require_permission(app_auth.Permission.MAPPING_READ)),
) -> List[MappingVersionRead]:
    defn = db.get(models.MappingDefinition, definition_id)
    if defn is None:
        raise ApiError(
            "mapping.definition_not_found",
            404,
            messages={
                "pt": "Definição de mapping não encontrada.",
                "en": "Mapping definition not found.",
                "es": "Definición de mapping no encontrada.",
            },
        )
    rows = (
        db.query(models.MappingVersion)
        .filter(models.MappingVersion.definition_id == definition_id)
        .order_by(models.MappingVersion.version_number.desc())
        .all()
    )
    return [_serialize_version(v) for v in rows]


@router.get(
    "/{definition_id}/versions/{version_id}",
    response_model=MappingVersionRead,
)
def get_version(
    definition_id: str,
    version_id: str,
    db: Session = Depends(database.get_session),
    _: models.AppUser = Depends(app_auth.require_permission(app_auth.Permission.MAPPING_READ)),
) -> MappingVersionRead:
    version = db.get(models.MappingVersion, version_id)
    if version is None or version.definition_id != definition_id:
        raise ApiError(
            "mapping.version_not_found",
            404,
            messages={
                "pt": "Versão de mapping não encontrada.",
                "en": "Mapping version not found.",
                "es": "Versión de mapping no encontrada.",
            },
        )
    return _serialize_version(version)


@router.post(
    "/{definition_id}/versions",
    response_model=MappingVersionRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_version(
    definition_id: str,
    payload: CreateVersionRequest,
    db: Session = Depends(database.get_session),
    user: models.AppUser = Depends(app_auth.require_permission(app_auth.Permission.MAPPING_WRITE)),
) -> MappingVersionRead:
    defn = db.get(models.MappingDefinition, definition_id)
    if defn is None:
        raise ApiError(
            "mapping.definition_not_found",
            404,
            messages={
                "pt": "Definição de mapping não encontrada.",
                "en": "Mapping definition not found.",
                "es": "Definición de mapping no encontrada.",
            },
        )

    rules_payload = _normalize_rules_to_v2(payload.rules)

    # 1) Validação sintática (compile_rules levanta com mensagem útil).
    try:
        compile_rules(rules_payload)
    except MappingDefinitionError as exc:
        raise ApiError(
            "mapping.invalid_dsl",
            status.HTTP_400_BAD_REQUEST,
            messages={
                "pt": "DSL inválida: {error}",
                "en": "Invalid DSL: {error}",
                "es": "DSL inválida: {error}",
            },
            params={"error": str(exc)},
        )

    # 2) Dry-run sobre a amostra do reservoir — escopada por tenant.
    samples = await _load_samples(
        user.organization_id, defn.vendor, defn.event_type, limit=100
    )
    dry_run = _run_dry_run(rules_payload, samples)

    # 2.5) (shift-left) — valida os outputs do dry-run contra o
    # manifest OCSF vendorado + cross-check do class_uid emitido vs o declarado na
    # definição. Sempre grava ``ocsf_validation_stats``; só 422 (acionável) quando
    # OCSF_MAPPING_GATE_ENABLED e há inválido real (ou divergência de class_uid).
    ocsf_stats = _ocsf_validate_commit(
        rules_payload, dry_run.output_examples, defn.ocsf_class_uid
    )
    if settings.OCSF_MAPPING_GATE_ENABLED and ocsf_stats["blocking"]:
        raise ApiError(
            "mapping.ocsf_invalid",
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            messages={
                "pt": "Mapping emite OCSF inválido: {detail}",
                "en": "Mapping emits invalid OCSF: {detail}",
                "es": "El mapping emite OCSF inválido: {detail}",
            },
            params={
                "detail": (
                    f"class_uid declarado={ocsf_stats['class_uid_declared']} "
                    f"emitido={ocsf_stats['class_uid_emitted']} "
                    f"invalid_by_reason={ocsf_stats['invalid_by_reason']}"
                )
            },
        )

    # 3) Resolve próximo version_number.
    last = (
        db.query(models.MappingVersion)
        .filter(models.MappingVersion.definition_id == definition_id)
        .order_by(models.MappingVersion.version_number.desc())
        .first()
    )
    next_number = (last.version_number + 1) if last else 1

    version = models.MappingVersion(
        definition_id=definition_id,
        version_number=next_number,
        rules=json.dumps(rules_payload, separators=(",", ":")),
        # SA autentica como shim com id NEGATIVO (não existe em app_users) —
        # gravar direto violava a FK (500 via MCP). None preserva a linha;
        # a autoria fica no commit_message + mapping_audit.username.
        author_user_id=app_auth.persistable_user_id(user),
        commit_message=payload.commit_message,
        diff_from_previous=None,
        dry_run_stats=json.dumps(dry_run.model_dump(), default=str),
        ocsf_validation_stats=json.dumps(ocsf_stats, default=str),
        dsl_version=2,
    )
    db.add(version)
    db.flush()  # garante version.id

    previous_current = defn.current_version_id
    defn.current_version_id = version.id

    # Audit + invalidate cache do engine para o version_id antigo.
    _audit(
        db,
        definition_id=definition_id,
        version_id=version.id,
        action="create_version",
        user=user,
        diff={"previous_current": previous_current, "new_current": version.id},
        detail=payload.commit_message,
    )
    db.commit()
    db.refresh(version)

    if previous_current:
        default_engine.invalidate(previous_current)

    return _serialize_version(version)


@router.post("/dry-run", response_model=DryRunResult)
async def dry_run_endpoint(
    payload: DryRunRequest,
    # (LEAK FIX): antes era ``_`` descartado — o dry-run lia
    # amostras raw do reservoir SEM escopo de tenant, vazando eventos de
    # outros clientes do mesmo vendor. Agora vincula o usuário e escopa por
    # ``user.organization_id``.
    user: models.AppUser = Depends(app_auth.require_permission(app_auth.Permission.MAPPING_READ)),
) -> DryRunResult:
    if payload.raw_events:
        samples = payload.raw_events[: payload.limit]
    else:
        # Resolução de org IGUAL ao GET reservoir: global pode nomear o tenant
        # via ``payload.organization_id``; não-global SEMPRE na própria org.
        is_global = tenant.has_global_scope(user)
        effective_org = (
            payload.organization_id
            if (payload.organization_id is not None and is_global)
            else user.organization_id
        )
        samples = await _load_samples(
            effective_org, payload.vendor, payload.event_type, payload.limit
        )
    rules_payload = _normalize_rules_to_v2(payload.rules)
    if not samples:
        # Sem amostra: retorna validação sintática vazia. UI mostra
        # mensagem útil ("colete eventos antes de testar").
        try:
            compile_rules(rules_payload)
        except MappingDefinitionError as exc:
            raise ApiError(
                "mapping.invalid_dsl",
                status.HTTP_400_BAD_REQUEST,
                messages={
                    "pt": "DSL inválida: {error}",
                    "en": "Invalid DSL: {error}",
                    "es": "DSL inválida: {error}",
                },
                params={"error": str(exc)},
            )
        return DryRunResult(
            sample_size=0, ok_count=0, fail_count=0, rule_failures=[], output_examples=[]
        )
    return _run_dry_run(rules_payload, samples)


@router.post("/{definition_id}/rollback", response_model=MappingDefinitionRead)
def rollback(
    definition_id: str,
    payload: RollbackRequest,
    db: Session = Depends(database.get_session),
    user: models.AppUser = Depends(app_auth.require_permission(app_auth.Permission.MAPPING_ROLLBACK)),
) -> MappingDefinitionRead:
    defn = db.get(models.MappingDefinition, definition_id)
    if defn is None:
        raise ApiError(
            "mapping.definition_not_found",
            404,
            messages={
                "pt": "Definição de mapping não encontrada.",
                "en": "Mapping definition not found.",
                "es": "Definición de mapping no encontrada.",
            },
        )

    target = db.get(models.MappingVersion, payload.version_id)
    if target is None or target.definition_id != definition_id:
        raise ApiError(
            "mapping.target_version_not_found",
            404,
            messages={
                "pt": "Versão de destino não encontrada.",
                "en": "Target version not found.",
                "es": "Versión de destino no encontrada.",
            },
        )

    previous_current = defn.current_version_id
    if previous_current == payload.version_id:
        raise ApiError(
            "mapping.already_on_version",
            status.HTTP_400_BAD_REQUEST,
            messages={
                "pt": "Já está nesta versão.",
                "en": "Already on this version.",
                "es": "Ya está en esta versión.",
            },
        )

    defn.current_version_id = payload.version_id

    _audit(
        db,
        definition_id=definition_id,
        version_id=payload.version_id,
        action="rollback",
        user=user,
        diff={"previous_current": previous_current, "new_current": payload.version_id},
        detail=payload.commit_message,
    )
    db.commit()
    db.refresh(defn)

    if previous_current:
        default_engine.invalidate(previous_current)

    return _serialize_definition(defn)


@router.get(
    "/{definition_id}/versions/{version_a_id}/diff/{version_b_id}",
    response_model=MappingVersionDiff,
)
def diff_versions(
    definition_id: str,
    version_a_id: str,
    version_b_id: str,
    db: Session = Depends(database.get_session),
    _: models.AppUser = Depends(app_auth.require_permission(app_auth.Permission.MAPPING_READ)),
) -> MappingVersionDiff:
    """Retorna diff estruturado entre duas versões de um mapping.

    404 se definition_id não existir ou se qualquer version_id não pertencer
    à definição informada.
    """
    defn = db.get(models.MappingDefinition, definition_id)
    if defn is None:
        raise ApiError(
            "mapping.definition_not_found",
            404,
            messages={
                "pt": "Definição de mapping não encontrada.",
                "en": "Mapping definition not found.",
                "es": "Definición de mapping no encontrada.",
            },
        )

    ver_a = db.get(models.MappingVersion, version_a_id)
    if ver_a is None or ver_a.definition_id != definition_id:
        raise ApiError(
            "mapping.version_a_not_found",
            404,
            messages={
                "pt": "Versão A não encontrada nesta definição.",
                "en": "Version A not found in this definition.",
                "es": "Versión A no encontrada en esta definición.",
            },
        )

    ver_b = db.get(models.MappingVersion, version_b_id)
    if ver_b is None or ver_b.definition_id != definition_id:
        raise ApiError(
            "mapping.version_b_not_found",
            404,
            messages={
                "pt": "Versão B não encontrada nesta definição.",
                "en": "Version B not found in this definition.",
                "es": "Versión B no encontrada en esta definición.",
            },
        )

    try:
        rules_a = _normalize_rules_to_v2(json.loads(ver_a.rules)) if ver_a.rules else _normalize_rules_to_v2(None)
    except (TypeError, ValueError):
        rules_a = _normalize_rules_to_v2(None)

    try:
        rules_b = _normalize_rules_to_v2(json.loads(ver_b.rules)) if ver_b.rules else _normalize_rules_to_v2(None)
    except (TypeError, ValueError):
        rules_b = _normalize_rules_to_v2(None)

    return compute_diff(
        rules_a,
        rules_b,
        definition_id=definition_id,
        version_a=version_a_id,
        version_b=version_b_id,
        version_a_number=ver_a.version_number,
        version_b_number=ver_b.version_number,
    )


@router.get(
    "/{definition_id}/audit",
    response_model=MappingAuditListResponse,
)
def list_audit(
    definition_id: str,
    limit: int = 50,
    offset: int = 0,
    action: Optional[str] = None,
    username: Optional[str] = None,
    from_ts: Optional[str] = None,
    to_ts: Optional[str] = None,
    db: Session = Depends(database.get_session),
    _: models.AppUser = Depends(app_auth.require_permission(app_auth.Permission.AUDIT_READ)),
) -> MappingAuditListResponse:
    """Retorna audit trail paginado de um mapping definition.

    Filtros: action, username, from_ts (ISO), to_ts (ISO).
    Limit: 1–200, default 50.
    """
    if limit < 1 or limit > 200:
        raise ApiError(
            "mapping.audit_invalid_limit",
            422,
            messages={
                "pt": "limit deve estar entre 1 e 200.",
                "en": "limit must be between 1 and 200.",
                "es": "limit debe estar entre 1 y 200.",
            },
        )
    if offset < 0:
        raise ApiError(
            "mapping.audit_invalid_offset",
            422,
            messages={
                "pt": "offset deve ser >= 0.",
                "en": "offset must be >= 0.",
                "es": "offset debe ser >= 0.",
            },
        )

    defn = db.get(models.MappingDefinition, definition_id)
    if defn is None:
        raise ApiError(
            "mapping.definition_not_found",
            404,
            messages={
                "pt": "Definição de mapping não encontrada.",
                "en": "Mapping definition not found.",
                "es": "Definición de mapping no encontrada.",
            },
        )

    q = db.query(models.MappingAuditLog).filter(
        models.MappingAuditLog.mapping_definition_id == definition_id
    )

    if action:
        q = q.filter(models.MappingAuditLog.action == action)
    if username:
        q = q.filter(models.MappingAuditLog.username == username)
    if from_ts:
        try:
            dt_from = datetime.fromisoformat(from_ts.replace("Z", "+00:00")).replace(tzinfo=None)
            q = q.filter(models.MappingAuditLog.created_at >= dt_from)
        except ValueError:
            raise ApiError(
                "mapping.audit_invalid_from_ts",
                422,
                messages={
                    "pt": "from_ts inválido: {value}",
                    "en": "invalid from_ts: {value}",
                    "es": "from_ts inválido: {value}",
                },
                params={"value": from_ts},
            )
    if to_ts:
        try:
            dt_to = datetime.fromisoformat(to_ts.replace("Z", "+00:00")).replace(tzinfo=None)
            q = q.filter(models.MappingAuditLog.created_at <= dt_to)
        except ValueError:
            raise ApiError(
                "mapping.audit_invalid_to_ts",
                422,
                messages={
                    "pt": "to_ts inválido: {value}",
                    "en": "invalid to_ts: {value}",
                    "es": "to_ts inválido: {value}",
                },
                params={"value": to_ts},
            )

    total = q.count()
    rows = (
        q.order_by(models.MappingAuditLog.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    return MappingAuditListResponse(
        total=total,
        items=[_serialize_audit_entry(r) for r in rows],
        limit=limit,
        offset=offset,
    )
