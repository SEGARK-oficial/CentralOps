"""Serialização de export de captura (CSV / NDJSON) + máscara de PII."""
from __future__ import annotations

import json

from backend.app.collectors import capture_export as ex


def _entry(**over):
    base = {
        "event": {
            "raw": {"srcuser": "svc_backup", "src_ip": "10.0.0.9", "eventID": "4624"},
            "_centralops": {"organization_id": 7, "vendor": "sophos"},
        },
        "vendor": "sophos",
        "captured_at": 1_714_000_100,
        "outcome": "dropped",
        "route_id": "r-noise",
        "destination_id": "d-splunk",
    }
    base.update(over)
    return base


def test_csv_starts_with_bom_for_excel():
    out = "".join(ex.iter_csv([_entry()]))
    assert out.startswith("﻿")


def test_csv_separator_follows_locale():
    assert ex.csv_separator_for_locale("pt-BR") == ";"
    assert ex.csv_separator_for_locale("es") == ";"
    assert ex.csv_separator_for_locale("en-US") == ","
    assert ex.csv_separator_for_locale(None) == ","


def test_csv_masks_pii_by_default():
    out = "".join(ex.iter_csv([_entry()], separator=";"))
    assert "svc_backup" not in out
    assert "10.0.0.9" not in out
    assert "[PII]" in out
    # campo não-PII sobrevive
    assert "4624" in out


def test_csv_can_disable_mask():
    out = "".join(ex.iter_csv([_entry()], mask=False))
    assert "svc_backup" in out


def test_csv_has_route_and_outcome_columns():
    out = "".join(ex.iter_csv([_entry()], separator=";"))
    header = out.splitlines()[0].lstrip("﻿")
    assert "route_id" in header and "outcome" in header
    # a linha traz a rota que dropou
    assert "r-noise" in out


def test_ndjson_one_line_per_event_and_masks():
    lines = [l for l in "".join(ex.iter_ndjson([_entry(), _entry()])).splitlines() if l]
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["outcome"] == "dropped"
    assert rec["route_id"] == "r-noise"
    assert rec["event"]["raw"]["srcuser"] == "[PII]"
    assert rec["event"]["raw"]["eventID"] == "4624"


def test_csv_signals_truncation_in_the_body():
    entries = [_entry() for _ in range(5)]
    out = "".join(ex.iter_csv(entries, max_rows=2))
    assert "truncated" in out
    # 1 BOM/header + 2 linhas + 1 aviso
    data_lines = [l for l in out.splitlines() if l and not l.startswith("captured_at") and not l.startswith("﻿captured_at")]
    assert any("truncated" in l for l in data_lines)


def test_ndjson_signals_truncation():
    entries = [_entry() for _ in range(5)]
    lines = [l for l in "".join(ex.iter_ndjson(entries, max_rows=2)).splitlines() if l]
    assert json.loads(lines[-1]).get("__truncated__") is True


def test_mask_pii_is_recursive_and_non_mutating():
    original = {"a": {"user": "alice", "keep": 1}, "list": [{"ip": "1.2.3.4"}]}
    masked = ex.mask_pii(original)
    assert masked["a"]["user"] == "[PII]"
    assert masked["a"]["keep"] == 1
    assert masked["list"][0]["ip"] == "[PII]"
    # original intacto
    assert original["a"]["user"] == "alice"


# ── máscara PII: OCSF por CAMINHO, estrutura preservada ───────────────

_OCSF = {
    "_centralops": {"organization_id": 7, "vendor": "sophos", "collector_host": "worker-01"},
    "normalized": {
        "class_uid": 1001,
        "actor": {"user": {"name": "ana.silva", "uid": "S-1-5-21-99", "type": "User"}},
        "process": {"cmd_line": "powershell.exe -enc SQBFAFgA", "pid": 4242},
        "device": {"name": "DESKTOP-ABC", "os": {"name": "Windows"}},
        "src_endpoint": {"ip": "10.0.0.5", "port": 443},
    },
    "raw": {"srcip": "10.0.0.5", "message": "ok"},
}


def test_ocsf_paths_are_masked() -> None:
    """A lista por NOME é vendor-raw-shaped e não fala OCSF: tem
    ``command_line``, mas o OCSF usa ``cmd_line``; tem ``hostname``, mas o OCSF
    usa ``device.name``. Sem os caminhos, quem exportava levava command lines e
    hostnames EM CLARO num arquivo baixável."""
    out = ex.mask_pii(_OCSF)
    n = out["normalized"]
    assert n["process"]["cmd_line"] == "[PII]"
    assert n["device"]["name"] == "[PII]"
    assert n["src_endpoint"]["ip"] == "[PII]"


def test_masking_preserves_structure_instead_of_collapsing_it() -> None:
    """Antes, ``actor.user`` inteiro virava a string ``"[PII]"`` e levava junto
    ``uid`` e ``type`` — justamente o que o analista usa para correlacionar."""
    out = ex.mask_pii(_OCSF)
    user = out["normalized"]["actor"]["user"]
    assert isinstance(user, dict), "a subárvore não pode virar string"
    assert user["name"] == "[PII]"
    assert user["uid"] == "[PII]"
    # O campo que NÃO é PII sobrevive intacto.
    assert user["type"] == "User"


def test_non_pii_siblings_survive() -> None:
    out = ex.mask_pii(_OCSF)
    n = out["normalized"]
    assert n["class_uid"] == 1001
    assert n["process"]["pid"] == 4242
    assert n["device"]["os"]["name"] == "Windows"
    assert n["src_endpoint"]["port"] == 443


def test_collector_host_is_masked() -> None:
    out = ex.mask_pii(_OCSF)
    assert out["_centralops"]["collector_host"] == "[PII]"


def test_raw_block_still_masked_by_field_name() -> None:
    """No ``raw`` os caminhos do vendor são desconhecíveis — lá o casamento por
    NOME continua sendo a única ferramenta."""
    out = ex.mask_pii(_OCSF)
    assert out["raw"]["srcip"] == "[PII]"
    assert out["raw"]["message"] == "ok"


def test_mask_does_not_mutate_the_original() -> None:
    import copy

    antes = copy.deepcopy(_OCSF)
    ex.mask_pii(_OCSF)
    assert _OCSF == antes


# ── contrato do export ────────────────────────────────────────────────


def test_first_eight_csv_columns_are_frozen() -> None:
    """Quem já tem script consumindo o export não pode quebrar: as colunas
    novas vão ANEXADAS ao fim."""
    assert ex.CSV_COLUMNS[:8] == [
        "captured_at", "organization_id", "vendor", "outcome",
        "route_id", "destination_id", "detail", "event_json",
    ]


def test_new_csv_columns_are_scalars() -> None:
    """Payload no CSV vira célula gigante — o estruturado é assunto do NDJSON."""
    entry = {
        "event": _OCSF, "vendor": "sophos", "captured_at": 1.0, "outcome": "delivered",
        "event_id": "e-1", "stage": "delivered", "payload_kind": "envelope",
        "pii_redacted": True,
        "wire": {"fidelity": "exact", "text": "x" * 5000, "encoding": "json"},
    }
    row = ex._row_for_csv(entry, mask=True)
    assert row["event_id"] == "e-1"
    assert row["stage"] == "delivered"
    assert row["pii_redacted"] is True
    # Só o NÍVEL do wire, nunca o texto.
    assert row["wire_fidelity"] == "exact"
    assert "x" * 5000 not in str(row)


def test_ndjson_has_organization_id_parity_with_csv() -> None:
    entry = {"event": _OCSF, "vendor": "sophos", "captured_at": 1.0, "outcome": "delivered"}
    rec = json.loads(ex.ndjson_line(entry, mask=True))
    assert rec["organization_id"] == 7


def test_ndjson_carries_the_wire_when_present() -> None:
    entry = {
        "event": _OCSF, "vendor": "x", "captured_at": 1.0, "outcome": "delivered",
        "wire": {"fidelity": "not_representable", "note": "lote em gzip"},
    }
    rec = json.loads(ex.ndjson_line(entry, mask=True))
    assert rec["wire"]["fidelity"] == "not_representable"
    # ``not_representable`` NÃO tem texto — mostrar fragmento induziria a
    # comparação errada.
    assert "text" not in rec["wire"]


def test_ndjson_omits_wire_when_absent() -> None:
    entry = {"event": _OCSF, "vendor": "x", "captured_at": 1.0, "outcome": "delivered"}
    rec = json.loads(ex.ndjson_line(entry, mask=True))
    assert "wire" not in rec


def test_detail_url_credentials_are_redacted() -> None:
    """``detail`` traz URL de sink rotineiramente (é o que a mensagem de erro
    do sink carrega). O scrub geral do repo NÃO pega credencial em URL —
    verificado: cobre token estilo Vault e nada mais."""
    entry = {
        "event": {}, "vendor": "x", "captured_at": 1.0, "outcome": "delivery_failed",
        "detail": "falha ao POSTar em https://user:senha123@sink.example/ingest",
    }
    row = ex._row_for_csv(entry, mask=True)
    assert "senha123" not in row["detail"]
    assert "sink.example" in row["detail"], "o host tem de sobreviver — é diagnóstico"


def test_detail_is_untouched_when_mask_is_off() -> None:
    entry = {
        "event": {}, "vendor": "x", "captured_at": 1.0, "outcome": "delivery_failed",
        "detail": "erro cru",
    }
    row = ex._row_for_csv(entry, mask=False)
    assert row["detail"] == "erro cru"
