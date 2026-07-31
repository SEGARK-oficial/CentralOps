"""Corpo de UPDATE recusa campo desconhecido em vez de descartá-lo em silêncio.

O default do Pydantic (``extra="ignore"``) transforma "o schema esqueceu de
declarar um campo" em DADO PERDIDO, com HTTP 200. Duas ocorrências reais:

  * ``CollectorConfigUpdate`` sem ``dedupe_ttl_seconds`` — o operador ajustava o
    TTL de dedupe para 4h, recebia 200, e o efetivo continuava 24h;
  * ``PredefinedQueryUpdate``/``PredefinedQueryBase`` sem ``dialect``/
    ``spec_kind`` — a UI tinha seletor para os dois e a edição nunca persistia.

Nos dois casos a coluna, a UI e o runtime estavam certos; só o schema ficou para
trás. Com ``StrictUpdateModel`` o mesmo esquecimento vira 422 na primeira
requisição.

``SelfProfileUpdate`` é EXEMPLO CONTRÁRIO deliberado e está coberto aqui para
que ninguém o "conserte" por engano: lá o descarte silencioso é a defesa contra
mass-assignment, e recusar extras quebraria um cliente que devolve o objeto de
usuário inteiro no PATCH.
"""
from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import pytest
from pydantic import ValidationError

from backend.app.api import schemas, schemas_destinations, schemas_routes

#: Todos os corpos de update que carregam edição do operador.
STRICT_MODELS = [
    schemas.OrganizationUpdate,
    schemas.OrganizationRetentionConfigUpdate,
    schemas.IntegrationUpdate,
    schemas.AutoApprovePolicyUpdate,
    schemas.IdentityConfigUpdate,
    schemas.LocaleUpdate,
    schemas.UserUpdate,
    schemas.PredefinedQueryUpdate,
    schemas.EmailConfigUpdate,
    schemas.IntegrationCollectionFiltersUpdate,
    schemas.CollectorConfigUpdate,
    schemas.ServiceAccountUpdate,
    schemas_routes.RouteUpdate,
    schemas_destinations.DestinationUpdate,
]


@pytest.mark.parametrize("model", STRICT_MODELS, ids=lambda m: m.__name__)
def test_update_model_rejects_unknown_field(model) -> None:
    with pytest.raises(ValidationError) as exc:
        model(campo_que_nao_existe="x")
    assert "campo_que_nao_existe" in str(exc.value)


#: Os que são patch PARCIAL (todos os campos opcionais). ``AutoApprovePolicyUpdate``
#: e ``LocaleUpdate`` ficam de fora: têm campo obrigatório por design — são corpos
#: de um único valor, não patches.
PARTIAL_MODELS = [
    m
    for m in STRICT_MODELS
    if not any(f.is_required() for f in m.model_fields.values())
]


@pytest.mark.parametrize("model", PARTIAL_MODELS, ids=lambda m: m.__name__)
def test_partial_update_still_accepts_empty_body(model) -> None:
    """``forbid`` recusa o DESCONHECIDO, não o AUSENTE — o patch parcial
    (inclusive o vazio) tem de continuar válido."""
    assert model().model_dump(exclude_unset=True) == {}


def test_partial_models_are_the_majority() -> None:
    """Guarda contra a lista de parciais esvaziar por engano (ex.: alguém torna
    um campo obrigatório e o teste acima vira no-op silencioso)."""
    assert len(PARTIAL_MODELS) == len(STRICT_MODELS) - 2


def test_self_profile_update_deliberately_ignores_extras() -> None:
    """Exceção documentada: aqui o descarte é a defesa anti-mass-assignment.

    Se alguém aplicar ``StrictUpdateModel`` a este schema, este teste falha e
    aponta o teste de segurança que seria quebrado junto.
    """
    body = schemas.SelfProfileUpdate(
        display_name="Carol", role="admin", is_global=True, organization_id=999
    )
    dumped = body.model_dump(exclude_unset=True)
    assert dumped == {"display_name": "Carol"}
    assert "role" not in dumped
    assert "is_global" not in dumped


def test_collector_config_update_declares_the_field_that_regressed() -> None:
    body = schemas.CollectorConfigUpdate(dedupe_ttl_seconds=14_400)
    assert body.model_dump(exclude_unset=True) == {"dedupe_ttl_seconds": 14_400}


def test_predefined_query_update_declares_dialect_and_spec_kind() -> None:
    body = schemas.PredefinedQueryUpdate(dialect="kql", spec_kind="sigma")
    assert body.model_dump(exclude_unset=True) == {"dialect": "kql", "spec_kind": "sigma"}


def test_predefined_query_dialect_accepts_plugin_values() -> None:
    """``dialect`` é ``str``, não Literal: o conjunto é extensível por plugin de
    vendor (a UI monta as opções a partir de ``/query-capabilities``). Um
    Literal fechado rejeitaria o dialeto de um plugin novo."""
    assert schemas.PredefinedQueryUpdate(dialect="dialeto_de_plugin_novo").dialect == (
        "dialeto_de_plugin_novo"
    )


def test_predefined_query_spec_kind_is_a_closed_set() -> None:
    with pytest.raises(ValidationError):
        schemas.PredefinedQueryUpdate(spec_kind="nao_existe")
