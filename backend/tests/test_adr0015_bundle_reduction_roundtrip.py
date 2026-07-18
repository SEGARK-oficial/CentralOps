"""Config-bundle preserva as alavancas de redução no round-trip (ADR-0015, Fase 0).

O export/import de config é caminho de disaster-recovery e de GitOps. Os 5 campos
de redução (``protect_detection``, ``sample_percent``, ``suppress_key``,
``suppress_allow``, ``suppress_window_s``) existiam em ``models.Route`` e eram
consumidos no dispatch, mas o bundle não os serializava.

O modo de falha era pior que "campo ausente": depois que ``RouteRead`` passou a
declarar os 5 campos COM DEFAULT, um export que não os popula emite os defaults
como se fossem os valores reais. Um restore então:
  * zera a economia configurada (``sample_percent`` volta a 100), e
  * repõe ``protect_detection=True``, descartando um opt-out consciente do
    operador — silenciosamente, com o bundle parecendo íntegro.

E sem as comparações no cálculo de drift, mudar SÓ uma alavanca no bundle seria
classificado como "unchanged" e o import viraria no-op silencioso.
"""

from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import inspect
import types
from datetime import datetime, timezone

import pytest

from backend.app.api.schemas_routes import RouteRead
from backend.app.routers import config_bundle

_REDUCTION_FIELDS = (
    "protect_detection",
    "sample_percent",
    "suppress_key",
    "suppress_allow",
    "suppress_window_s",
)


def _row(**over):
    """``Route``-like mínimo para ``_route_row_to_read`` (lê getattr + JSON str)."""
    base = dict(
        id=7,
        name="r",
        priority=100,
        condition="{}",
        action="route",
        destination_ids="[]",
        is_final=True,
        enabled=True,
        canary_percent=100,
        transform_ref=None,
        pii_redaction=None,
        organization_id=1,
        created_at=datetime(2026, 7, 18, tzinfo=timezone.utc),
        updated_at=datetime(2026, 7, 18, tzinfo=timezone.utc),
        # os 5 em valores NÃO-default, para que um serializer que os ignore
        # produza saída visivelmente diferente.
        protect_detection=False,
        sample_percent=25,
        suppress_key="src_ip,user",
        suppress_allow=3,
        suppress_window_s=120,
    )
    base.update(over)
    return types.SimpleNamespace(**base)


def test_route_read_declares_the_reduction_fields():
    for f in _REDUCTION_FIELDS:
        assert f in RouteRead.model_fields, f"RouteRead sem campo de redução: {f}"


def test_export_preserves_non_default_reduction_values():
    """O cerne: valores reais da rota, não os defaults do schema."""
    read = config_bundle._route_row_to_read(_row())
    assert read.protect_detection is False
    assert read.sample_percent == 25
    assert read.suppress_key == "src_ip,user"
    assert read.suppress_allow == 3
    assert read.suppress_window_s == 120


def test_export_does_not_silently_reset_protect_detection():
    """Regressão direta: ``protect_detection=False`` é opt-out CONSCIENTE.

    Um export que o repõe para ``True`` parece "seguro", mas descarta uma decisão
    do operador sem avisar — e o bundle continua parecendo íntegro.
    """
    read = config_bundle._route_row_to_read(_row(protect_detection=False))
    assert read.protect_detection is False, (
        "export repôs protect_detection para o default, descartando o opt-out"
    )


@pytest.mark.parametrize("field", _REDUCTION_FIELDS)
def test_import_paths_carry_every_reduction_field(field: str):
    """Guard estrutural sobre os 3 caminhos de escrita do importador.

    ``add`` (criação), ``update`` (upsert) e o cálculo de ``changed`` (drift)
    precisam TODOS conhecer cada campo. Faltar no ``add`` perde a config no
    restore; faltar no ``changed`` faz o import virar no-op silencioso.
    """
    src = inspect.getsource(config_bundle)
    # add + update passam ``campo=route.campo``; o drift compara ``existing_route.campo``.
    assert src.count(f"{field}=route.{field}") >= 2, (
        f"{field} não é repassado nos dois caminhos de escrita (add/update) do import"
    )
    assert f"existing_route.{field}" in src, (
        f"{field} ausente do cálculo de drift — mudar só este campo no bundle "
        "seria classificado como 'unchanged' e o import não aplicaria nada"
    )
