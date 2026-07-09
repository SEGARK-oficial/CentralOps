"""Smoke test E2E da camada de normalização (Fase 2 do plano).

Verifica a cadeia completa SEM rede: load mapping atual do DB →
engine.apply → build_envelope → JSONL writer → leitura do arquivo
gerado → asserts no shape. Não exige Redis nem Celery — usa apenas
SQLite temporário e disco local.

Uso:

    APP_MASTER_KEY=test-master-key-for-centralops-suite-12345 \
    APP_ENV=test \
    DATABASE_URL=sqlite:////tmp/smoke_e2e.db \
    /private/tmp/centralops-venv/bin/python backend/scripts/smoke_normalize_e2e.py

Saída esperada:
- "OK: pipeline produz envelope canônico para os 5 streams"
- "OK: JSONL output contém todos os eventos normalizados"
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

# Usa SQLite temp por default — não impacta DB de dev.
if not os.environ.get("DATABASE_URL"):
    tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_db.close()
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp_db.name}"


from backend.app.collectors.normalize import ENVELOPE_SCHEMA_VERSION
from backend.app.collectors.normalize.engine import (
    apply_compiled,
    compile_rules,
    default_engine,
)
from backend.app.collectors.normalize.envelope import (
    EnvelopeContext,
    build_envelope,
    has_customer_id,
)
from backend.app.collectors.output.jsonl_writer import JSONLWriter
from backend.app.db import models
from backend.app.db.database import SessionLocal, initialize_database


SAMPLE_EVENTS = {
    ("sophos", "sophos.alert"): {
        "id": "alert-uuid-001",
        "createdAt": "2026-04-23T14:22:10Z",
        "severity": "critical",
        "type": "malware",
        "description": "Trojan.GenericKD detected",
        "managedAgent": {"id": "a1", "name": "WIN-DESKTOP", "type": "computer"},
        "person": {"id": "u1", "name": "alice"},
        "product": "Endpoint",
    },
    ("sophos", "sophos.case"): {
        "id": "case-001",
        "createdAt": "2026-04-23T10:00:00Z",
        "name": "Investigation",
        "severity": "high",
        "status": "investigating",
    },
    ("microsoft_defender", "defender.alert"): {
        "id": "da637-1",
        "lastUpdateDateTime": "2026-04-23T09:30:00Z",
        "title": "Suspicious sign-in",
        "severity": "high",
        "status": "new",
    },
    ("microsoft_defender", "defender.incident"): {
        "id": "inc-1",
        "lastUpdateDateTime": "2026-04-23T08:30:00Z",
        "displayName": "Multi-stage incident",
        "severity": "high",
        "status": "active",
    },
    ("ninjaone", "ninjaone.activity"): {
        "id": 100001,
        "activityTime": 1776940800,
        "activityType": "DEVICE_ALERT",
        "subject": "Device offline",
        "severity": "info",
        "device": {"id": 22, "systemName": "WIN10-FIN-03"},
    },
}


def _load_current(db, vendor: str, event_type: str):
    defn = (
        db.query(models.MappingDefinition)
        .filter_by(vendor=vendor, event_type=event_type)
        .one()
    )
    if defn.current_version_id is None:
        raise RuntimeError(
            f"({vendor}, {event_type}) sem MappingVersion ativo — seed falhou"
        )
    version = db.get(models.MappingVersion, defn.current_version_id)
    rules = json.loads(version.rules)
    return version.id, rules


async def main() -> int:
    initialize_database()

    envelopes = []
    with SessionLocal() as db:
        for (vendor, event_type), raw in SAMPLE_EVENTS.items():
            version_id, rules = _load_current(db, vendor, event_type)
            compiled = compile_rules(rules)
            applied = apply_compiled(compiled, raw)

            ctx = EnvelopeContext(
                vendor=vendor,
                integration_id=999,
                customer_id=42,
                stream=event_type.split(".", 1)[1] if "." in event_type else event_type,
                event_type=event_type,
                mapping_version_id=version_id,
            )
            env = build_envelope(raw, applied.output, ctx, vendor_msg_id=str(raw.get("id")))

            assert has_customer_id(env), f"{event_type} faltou customer_id"
            assert env["_centralops"]["vendor"] == vendor
            assert env["_centralops"]["event_type"] == event_type
            assert env["normalized"]["class_uid"] in {2004, 2005, 6003}
            assert isinstance(env["normalized"]["time"], int)
            assert env["raw"] == raw

            envelopes.append(env)
            print(
                f"  OK ({vendor:>20} | {event_type:<25} | "
                f"class_uid={env['normalized']['class_uid']} | "
                f"severity_id={env['normalized']['severity_id']})"
            )

    print(f"\nOK: pipeline produz envelope canônico para os {len(envelopes)} streams")

    # JSONL output
    out_dir = tempfile.mkdtemp(prefix="centralops_smoke_")
    writer = JSONLWriter(base_dir=out_dir)
    await writer.send_batch(envelopes)

    # Leia de volta e valide
    written_count = 0
    for vendor_dir in Path(out_dir).iterdir():
        for log_file in vendor_dir.glob("*.log"):
            with open(log_file) as fh:
                for line in fh:
                    parsed = json.loads(line)
                    assert {"_centralops", "normalized", "raw"}.issubset(parsed.keys())
                    assert parsed["_centralops"]["customer_id"] == 42
                    assert parsed["_centralops"]["schema_version"] == ENVELOPE_SCHEMA_VERSION
                    written_count += 1

    assert written_count == len(envelopes), f"esperado {len(envelopes)} eventos, lido {written_count}"
    print(f"OK: JSONL output contém todos os {written_count} eventos normalizados")
    print(f"     (dir: {out_dir})")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
