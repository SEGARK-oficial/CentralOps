"""Serialização de eventos de captura para EXPORT (CSV / NDJSON).

Separado do router para ser testável sem HTTP. Duas garantias que a serialização
do ``routers/history.py`` (o único precedente de CSV no repo) NÃO dá e que são a
diferença entre "abre certo no Excel" e "abre com acento quebrado numa coluna só":

  * BOM UTF-8 no início do CSV — sem ele o Excel (Windows, pt-BR) lê UTF-8 como
    Latin-1 e ``Descrição`` vira ``DescriÃ§Ã£o``;
  * separador ``;`` para locales pt/es — o Excel dessas localidades usa ``;`` como
    separador de lista; com ``,`` o arquivo inteiro cai numa coluna.

Máscara de PII (``mask=True``, default do export): o dado está SAINDO do sistema
num arquivo baixável. O ring já teve SEGREDOS scrubbados na gravação
(``audit_buffer._redact``); aqui adicionamos a máscara de PII (usuário, host, IP,
e-mail, ...) por NOME de campo, recursiva sobre o payload do evento. Isto é
camada de export — no inspetor in-app o admin da própria org vê o dado cru.
"""
from __future__ import annotations

import csv
import io
import json
from typing import Any, Dict, Iterable, Iterator, List, Mapping

# Nomes de campo tratados como PII no EXPORT (dado que deixa o sistema). Não é a
# mesma lista de SEGREDOS (esses já foram scrubbados na gravação do ring): aqui
# são identificadores pessoais/host que um relatório exportado não deve vazar.
PII_FIELD_NAMES: frozenset = frozenset(
    {
        "user", "username", "user_name", "srcuser", "dstuser", "targetusername",
        "email", "mail", "mailfrom", "mailto", "sender", "recipient",
        "hostname", "host", "computer", "computername", "dvchost",
        "src_ip", "dst_ip", "srcip", "dstip", "ip", "ipaddress", "client_ip",
        "src_mac", "dst_mac", "srcmac", "mac",
        "command_line", "commandline", "cmdline",
        "full_name", "fullname", "given_name", "surname",
        "phone", "phone_number", "cpf", "ssn",
    }
)

_MASK = "[PII]"


def mask_pii(obj: Any) -> Any:
    """Redação recursiva de PII por NOME de campo (não muta o original)."""
    if isinstance(obj, Mapping):
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in PII_FIELD_NAMES:
                out[k] = _MASK
            else:
                out[k] = mask_pii(v)
        return out
    if isinstance(obj, list):
        return [mask_pii(v) for v in obj]
    return obj


# Colunas do CSV (ordem estável). O payload vai como JSON compacto numa coluna —
# o analista abre no Excel para ler desfecho/rota/destino; quem quer o payload
# estruturado usa NDJSON.
CSV_COLUMNS: List[str] = [
    "captured_at", "organization_id", "vendor", "outcome",
    "route_id", "destination_id", "detail", "event_json",
]


def _row_for_csv(entry: Mapping[str, Any], *, mask: bool) -> Dict[str, Any]:
    event = entry.get("event") or {}
    if mask:
        event = mask_pii(event)
    meta = event.get("_centralops") if isinstance(event, Mapping) else None
    org = None
    if isinstance(meta, Mapping):
        org = meta.get("organization_id")
    return {
        "captured_at": entry.get("captured_at"),
        "organization_id": org,
        "vendor": entry.get("vendor"),
        "outcome": entry.get("outcome") or "unknown",
        "route_id": entry.get("route_id"),
        "destination_id": entry.get("destination_id"),
        "detail": entry.get("detail"),
        "event_json": json.dumps(event, separators=(",", ":"), default=str, ensure_ascii=False),
    }


def csv_separator_for_locale(accept_language: str | None) -> str:
    """``;`` para pt/es (o Excel dessas localidades usa ``;`` como separador de
    lista), ``,`` caso contrário."""
    lang = (accept_language or "").strip().lower()[:2]
    return ";" if lang in ("pt", "es") else ","


# ── Serialização por-entrada (para o streamer async chamar item a item) ──────


def csv_header(separator: str) -> str:
    """BOM + linha de cabeçalho. Emitido UMA vez, antes das linhas."""
    buf = io.StringIO()
    csv.writer(buf, delimiter=separator, lineterminator="\n").writerow(CSV_COLUMNS)
    return "﻿" + buf.getvalue()  # BOM para o Excel ler UTF-8


def csv_row(entry: Mapping[str, Any], *, mask: bool, separator: str) -> str:
    row = _row_for_csv(entry, mask=mask)
    buf = io.StringIO()
    csv.writer(buf, delimiter=separator, lineterminator="\n").writerow(
        [row[c] if row[c] is not None else "" for c in CSV_COLUMNS]
    )
    return buf.getvalue()


def csv_truncation_notice(max_rows: int) -> str:
    return f"# truncated: limite de {max_rows} linhas atingido — refine o filtro ou use NDJSON\n"


def ndjson_line(entry: Mapping[str, Any], *, mask: bool) -> str:
    event = entry.get("event") or {}
    if mask:
        event = mask_pii(event)
    record = {
        "captured_at": entry.get("captured_at"),
        "vendor": entry.get("vendor"),
        "outcome": entry.get("outcome") or "unknown",
        "route_id": entry.get("route_id"),
        "destination_id": entry.get("destination_id"),
        "detail": entry.get("detail"),
        "event": event,
    }
    return json.dumps(record, separators=(",", ":"), default=str, ensure_ascii=False) + "\n"


def ndjson_truncation_notice(max_rows: int) -> str:
    return json.dumps({"__truncated__": True, "limit": max_rows}, separators=(",", ":")) + "\n"


# ── Serialização em lote (sync, para testes e chamadas não-streaming) ─────────


def iter_csv(
    entries: Iterable[Mapping[str, Any]],
    *,
    mask: bool = True,
    separator: str = ",",
    max_rows: int = 50_000,
) -> Iterator[str]:
    """Gera o CSV linha a linha (com BOM + cabeçalho). Sinaliza truncamento no
    CORPO (comentário final) quando ``max_rows`` é atingido — nunca em silêncio."""
    yield csv_header(separator)
    written = 0
    for entry in entries:
        if written >= max_rows:
            yield csv_truncation_notice(max_rows)
            return
        yield csv_row(entry, mask=mask, separator=separator)
        written += 1


def iter_ndjson(
    entries: Iterable[Mapping[str, Any]],
    *,
    mask: bool = True,
    max_rows: int = 50_000,
) -> Iterator[str]:
    """Gera NDJSON (uma linha JSON por evento). Estável para jq/replay."""
    written = 0
    for entry in entries:
        if written >= max_rows:
            yield ndjson_truncation_notice(max_rows)
            return
        yield ndjson_line(entry, mask=mask)
        written += 1
