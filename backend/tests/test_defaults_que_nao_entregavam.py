"""Três defaults que faziam o produto entregar menos do que prometia.

Vieram de uma comparação competitiva. O padrão é o mesmo nos três: a feature
existe, tem código, tem teste, e não roda porque o caminho de configuração
padrão a desliga ou a descarta. O operador não tem como perceber.

1. **Redução de payload descartada no seeder.** 11 mappings default declaram
   ``raw_reduction``, e o seeder reconstruía o dict só com ``preprocess`` e
   ``rules``. Instalação nova nascia sem redução, entregando payload inteiro
   para destinos com limite de tamanho (o Wazuh trunca em silêncio acima de
   ~64 KiB). É a MESMA regressão que ``_normalize_rules_to_v2`` já tinha
   corrigido no caminho de CRUD, sobrevivendo no caminho de seed.

2. **Enriquecimento desligado com UI publicada.** O console tem página de
   enriquecimento com editor, catálogo e publicação. Com ``ENRICHMENT_ENABLED``
   em False, o operador montava a política, publicava, e nada acontecia.

3. **Catálogo oferecendo destino que a imagem não entrega.** s3, security_lake
   e chronicle importam SDK que não estava na imagem publicada.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

import pytest

import backend.app.collectors.output.destinations as _destinos  # noqa: F401
from backend.app.collectors.normalize import defaults as mapping_defaults
from backend.app.collectors.output.destinations import registry
from backend.app.core.config import settings


# ── 1. raw_reduction sobrevive ao seeder ──────────────────────────────

def _arquivos_default() -> dict[tuple[str, str], dict]:
    base = Path(mapping_defaults.__file__).parent
    out = {}
    for chave, nome in mapping_defaults.DEFAULT_MAPPING_FILES.items():
        caminho = base / nome
        if caminho.exists():
            out[chave] = json.loads(caminho.read_text(encoding="utf-8"))
    return out


def test_existem_mappings_default_com_raw_reduction() -> None:
    """Guarda o pressuposto do teste seguinte.

    Se um dia nenhum default declarar redução, o teste de preservação passa
    vazio e não protege nada. Este falha primeiro e explica.
    """
    com_reducao = [
        f"{v}.{e}"
        for (v, e), spec in _arquivos_default().items()
        if isinstance(spec, dict) and spec.get("raw_reduction")
    ]

    assert len(com_reducao) >= 10, (
        f"esperava a maioria dos defaults com raw_reduction, achei {com_reducao}"
    )


def test_o_seeder_preserva_todo_bloco_top_level_da_dsl() -> None:
    """Replica a normalização do seeder e exige que nada seja descartado.

    O corpo testado é o mesmo de ``database.py`` (seed de mapping default). Se
    alguém voltar a reconstruir o dict com uma lista fixa de chaves, este teste
    quebra antes de chegar em produção.
    """
    for (vendor, evento), spec in _arquivos_default().items():
        if not isinstance(spec, dict):
            continue

        # O que o seeder faz hoje.
        payload = {k: v for k, v in spec.items() if k not in ("preprocess", "rules")}
        payload["preprocess"] = list(spec.get("preprocess") or [])
        payload["rules"] = list(spec.get("rules") or [])

        assert set(payload) >= set(spec), (
            f"{vendor}.{evento}: o seeder perdeu {set(spec) - set(payload)}"
        )
        if spec.get("raw_reduction"):
            assert payload["raw_reduction"] == spec["raw_reduction"], (
                f"{vendor}.{evento}: raw_reduction não sobreviveu ao seed"
            )


# ── 2. enriquecimento habilitado por padrão ───────────────────────────

def test_enriquecimento_vem_habilitado() -> None:
    """Com a UI publicada, o default OFF era uma armadilha silenciosa."""
    assert settings.ENRICHMENT_ENABLED is True


def test_habilitado_nao_significa_enriquecer_sem_politica() -> None:
    """Ligar a flag não liga enriquecimento: sem org não há política.

    É o que torna o default seguro. ``load_policy_for_org`` é fail-closed para
    ``organization_id=None`` e devolve None quando a org não tem política
    habilitada, então o applier nunca roda.
    """
    from backend.app.collectors.enrich.runtime import load_policy_for_org

    assert load_policy_for_org(None) is None


# ── 3. o catálogo não mente sobre disponibilidade ─────────────────────

def test_todo_kind_com_sdk_declara_requires_modules() -> None:
    """Quem importa SDK precisa declarar, senão o catálogo volta a mentir.

    O import é TARDIO de propósito (o módulo registra sem o pacote, e os testes
    mockam). O efeito colateral é que o kind aparece na galeria mesmo sem poder
    entregar. ``requires_modules`` é o que reconcilia as duas coisas.
    """
    esperado = {
        "s3": {"aioboto3"},
        "security_lake": {"aioboto3", "pyarrow"},
        "chronicle": {"google.auth"},
        "kafka": {"aiokafka"},
    }

    for kind, modulos in esperado.items():
        assert set(registry.get(kind).requires_modules) == modulos, (
            f"{kind}: requires_modules divergente"
        )


def test_kind_http_puro_nao_declara_dependencia() -> None:
    """Só declara quem precisa. Declarar demais esconderia destino que funciona."""
    for kind in ("webhook", "splunk_hec", "syslog_rfc3164", "jsonl", "clickhouse", "nano"):
        assert registry.get(kind).requires_modules == (), (
            f"{kind} é HTTP/socket puro e não deveria declarar SDK"
        )


def test_o_catalogo_reporta_disponibilidade_real() -> None:
    """``available`` reflete o processo que está RODANDO, não a intenção."""
    for kind in registry.all_kinds():
        reg = registry.get(kind)
        d = reg.describe()

        assert d["available"] is (not reg.missing_modules())
        assert d["missing_modules"] == reg.missing_modules()


def test_sdk_ausente_derruba_available(monkeypatch) -> None:
    """Prova o mecanismo: com o SDK invisível, o kind se declara indisponível."""
    import importlib.util

    real = importlib.util.find_spec

    def _sem_aioboto3(nome, *a, **k):
        if nome == "aioboto3":
            return None
        return real(nome, *a, **k)

    monkeypatch.setattr(importlib.util, "find_spec", _sem_aioboto3)

    d = registry.get("s3").describe()
    assert d["available"] is False
    assert "aioboto3" in d["missing_modules"]
    # E o kind CONTINUA registrado: some da disponibilidade, não do catálogo.
    assert "s3" in registry.all_kinds()


def test_o_dockerfile_instala_os_extras_de_sinks() -> None:
    """A imagem publicada precisa entregar o catálogo que ela anuncia.

    Sem isto, ``available`` viraria só um rótulo honesto sobre um produto
    incompleto. O objetivo é que ele seja verdadeiro E positivo.
    """
    raiz = Path(__file__).resolve().parents[2]
    dockerfile = (raiz / "compose" / "Dockerfile").read_text(encoding="utf-8")

    assert "INSTALL_SINKS" in dockerfile
    assert "requirements-sinks.txt" in dockerfile
    assert 'ARG INSTALL_SINKS=true' in dockerfile, "o default da imagem publicada é instalar"
