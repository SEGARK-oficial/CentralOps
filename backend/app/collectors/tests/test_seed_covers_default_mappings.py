"""Guard: TODO mapping default precisa estar no catálogo de seed.

Regressão recorrente (3 vezes: wazuh, depois o lote crowdstrike/entra/okta/cloudtrail,
depois veeam). Um vendor com JSON default em ``DEFAULT_MAPPING_FILES`` mas SEM tupla em
``seed_definitions`` (``db/database.py``) não ganha ``MappingDefinition`` no banco →
``_load_current_mapping`` devolve None → o pipeline manda **100% do stream** para
quarentena com ``missing_mapping``. Pior: não há endpoint para CRIAR uma
``MappingDefinition``, então o operador não conserta pela UI — só SQL cru ou redeploy.

O guard que existia era hardcoded por vendor (``test_wazuh_detection_mapping.py`` assere
a string do wazuh), então não pegava vendors novos. Este é GENÉRICO: qualquer par
(vendor, event_type) novo em ``DEFAULT_MAPPING_FILES`` quebra o CI até ser seedado.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..normalize.defaults import DEFAULT_MAPPING_FILES

_DB_SOURCE = Path(__file__).resolve().parents[2] / "db" / "database.py"
# ("vendor", "vendor.event_type", <class_uid>, "descrição")
_TUPLE_RE = re.compile(r'\(\s*"([a-z0-9_]+)"\s*,\s*"([a-z0-9_.]+)"\s*,\s*(\d+)\s*,')


def _seeded_pairs() -> set[tuple[str, str]]:
    """Extrai os pares (vendor, event_type) do bloco ``seed_definitions``."""
    src = _DB_SOURCE.read_text(encoding="utf-8")
    start = src.index("seed_definitions = [")
    end = src.index("\n            ]", start)
    return {(m.group(1), m.group(2)) for m in _TUPLE_RE.finditer(src[start:end])}


def test_every_default_mapping_is_seeded() -> None:
    """Sem a tupla de seed, a 1ª integração do vendor quarentena 100% dos eventos."""
    seeded = _seeded_pairs()
    assert seeded, "não consegui extrair seed_definitions de db/database.py"
    missing = sorted(set(DEFAULT_MAPPING_FILES) - seeded)
    assert not missing, (
        "vendors com mapping default mas SEM entrada em seed_definitions "
        f"(db/database.py) — quarentenariam 100% do stream: {missing}"
    )


def test_seed_class_uid_matches_the_default_mapping() -> None:
    """O class_uid seedado deve bater com o que o JSON default realmente emite —
    senão o catálogo anuncia uma classe OCSF e o pipeline emite outra."""
    import json

    src = _DB_SOURCE.read_text(encoding="utf-8")
    start = src.index("seed_definitions = [")
    end = src.index("\n            ]", start)
    defaults_dir = Path(__file__).resolve().parents[1] / "normalize" / "defaults"

    divergences: list[str] = []
    for m in _TUPLE_RE.finditer(src[start:end]):
        vendor, event_type, seeded_uid = m.group(1), m.group(2), int(m.group(3))
        fname = DEFAULT_MAPPING_FILES.get((vendor, event_type))
        if not fname:
            continue  # seedado sem default em disco — fora do escopo deste guard
        doc = json.loads((defaults_dir / fname).read_text(encoding="utf-8"))
        rules = doc.get("rules", doc) if isinstance(doc, dict) else doc
        emitted = next(
            (r.get("const") for r in rules if r.get("target") == "normalized.class_uid"),
            None,
        )
        if emitted is not None and emitted != seeded_uid:
            divergences.append(f"{vendor}/{event_type}: seed={seeded_uid} json={emitted}")
    assert not divergences, f"class_uid do seed diverge do mapping default: {divergences}"
