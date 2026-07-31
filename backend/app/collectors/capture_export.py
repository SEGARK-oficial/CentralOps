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
import re
from typing import Any, Dict, Iterable, Iterator, List, Mapping

# Nomes de campo tratados como PII no EXPORT (dado que deixa o sistema). Não é a
# mesma lista de SEGREDOS (esses já foram scrubbados na gravação do ring): aqui
# são identificadores pessoais/host que um relatório exportado não deve vazar.
PII_FIELD_NAMES: frozenset = frozenset(
    {
        "user", "username", "user_name", "srcuser", "dstuser", "targetusername",
        "email", "mail", "mailfrom", "mailto", "sender", "recipient",
        "hostname", "host", "computer", "computername", "dvchost", "collector_host",
        "src_ip", "dst_ip", "srcip", "dstip", "ip", "ipaddress", "client_ip",
        "src_mac", "dst_mac", "srcmac", "mac",
        "command_line", "commandline", "cmdline",
        "full_name", "fullname", "given_name", "surname",
        "phone", "phone_number", "cpf", "ssn",
    }
)

#: PII no bloco ``normalized``, por CAMINHO OCSF. A lista por NOME (acima) é
#: vendor-raw-shaped e não fala OCSF: tem ``command_line``/``cmdline``, mas o
#: OCSF usa ``cmd_line``; tem ``hostname``, mas o OCSF usa ``device.name`` e
#: ``src_endpoint.name``. Cruzando com os targets ``normalized.*`` dos mappings
#: default, a lista por nome cobria uma fração pequena — quem exportava uma
#: sessão levava command lines e hostnames EM CLARO num arquivo baixável.
PII_OCSF_PATHS: frozenset = frozenset(
    {
        "actor.user.name", "actor.user.uid", "actor.user.domain", "actor.user.email_addr",
        "process.user.name", "process.user.uid", "process.cmd_line",
        "process.parent_process.cmd_line", "process.file.path",
        "process.parent_process.file.path",
        "user.name", "user.uid", "user.full_name",
        "device.name", "device.hostname", "device.ip", "device.mac", "device.uid",
        "src_endpoint.ip", "src_endpoint.name", "src_endpoint.uid",
        "dst_endpoint.ip", "dst_endpoint.hostname",
        "url.url", "url.hostname", "url.text",
        "http_request.user_agent",
        "metadata.correlation_uid",
        "email.from", "email.to",
        "unmapped.src_ip", "unmapped.user_name", "unmapped.user_uid",
        "unmapped.user_agent", "unmapped.predecoder.hostname",
    }
)

_MASK = "[PII]"


def mask_pii(obj: Any, *, _force: bool = False, _path: str = "") -> Any:
    """Redação recursiva de PII (não muta o original).

    Duas correções sobre o comportamento anterior:

    1. NOME que casa + valor CONTÊINER não vira mais a string ``"[PII]"``. Antes,
       ``actor.user`` inteiro virava ``"[PII]"`` e levava junto ``uid``,
       ``domain`` e ``type`` — justamente o que o analista usa para
       correlacionar. Agora recorre mascarando as FOLHAS e preservando a
       estrutura.
    2. Casamento por CAMINHO no bloco ``normalized`` (``PII_OCSF_PATHS``), além
       do casamento por nome, que continua valendo para ``raw`` — onde os
       caminhos do vendor são desconhecíveis.
    """
    if isinstance(obj, Mapping):
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            key = k.lower() if isinstance(k, str) else ""
            child = f"{_path}.{k}" if _path else str(k)
            # ``normalized`` é a raiz dos caminhos OCSF — não entra na chave.
            in_ocsf = child == "normalized" or child.startswith("normalized.")
            ocsf_path = child[len("normalized."):] if child.startswith("normalized.") else child
            # Dentro de ``normalized`` o casamento por NOME só vale para
            # ESCALAR. Um contêiner ali é endereçado por CAMINHO, e deixar o
            # nome mandar destruiria a subárvore: ``user`` está na lista por
            # nome e colide com o contêiner OCSF ``actor.user`` — o `type`
            # (que não é PII e serve para triagem) sumiria junto.
            name_hit = key in PII_FIELD_NAMES and not (
                in_ocsf and isinstance(v, (Mapping, list))
            )
            hit = _force or name_hit or ocsf_path in PII_OCSF_PATHS
            if hit and not isinstance(v, (Mapping, list)):
                out[k] = _MASK
            elif hit:
                # Contêiner: mascara as folhas, PRESERVA a estrutura.
                out[k] = mask_pii(v, _force=True, _path=child)
            else:
                out[k] = mask_pii(v, _path=child)
        return out
    if isinstance(obj, list):
        return [mask_pii(v, _force=_force, _path=_path) for v in obj]
    return _MASK if _force else obj


# Colunas do CSV (ordem estável). O payload vai como JSON compacto numa coluna —
# o analista abre no Excel para ler desfecho/rota/destino; quem quer o payload
# estruturado usa NDJSON.
#: As 8 primeiras são CONGELADAS (há teste): quem já tem script consumindo o
#: export não pode quebrar. As novas vão ANEXADAS ao fim, e são ESCALARES —
#: o payload estruturado continua sendo assunto do NDJSON.
CSV_COLUMNS: List[str] = [
    "captured_at", "organization_id", "vendor", "outcome",
    "route_id", "destination_id", "detail", "event_json",
    # ── jornal ──
    "event_id", "stage", "payload_kind", "pii_redacted", "wire_fidelity",
]


#: Credencial embutida em URL (``https://user:senha@host``). O scrub geral do
#: repo NÃO pega isto — verifiquei: ele cobre token estilo Vault (``s.``/``b.``)
#: e nada mais. E ``detail`` carrega URL de sink rotineiramente, porque é o que
#: a mensagem de erro do sink traz.
_URL_CREDENTIALS = re.compile(r"(?<=://)([^/\s:@]+):([^/\s@]+)(?=@)")


def _mask_detail(detail: Any, *, mask: bool) -> Any:
    """``detail`` é TEXTO LIVRE (erro de sink), e antes só ``event`` era tratado.

    Máscara por NOME de campo não se aplica a texto livre — não há campo. O que
    se aplica é: (1) o scrub de segredo do repo, e (2) credencial em URL, que o
    scrub não cobre e é o vazamento concreto desta mensagem.

    NÃO tenta adivinhar PII em prosa. Uma heurística agressiva sobre texto livre
    produziria falso-positivo justamente na mensagem que o operador precisa ler
    para entender a falha.
    """
    if not mask or not isinstance(detail, str) or not detail:
        return detail
    out = _URL_CREDENTIALS.sub(r"\1:[REDACTED]", detail)
    try:
        from ..core.logging_config import scrub_secrets_in_value

        return scrub_secrets_in_value(out)
    except Exception:  # noqa: BLE001 — export nunca quebra por causa da máscara
        return out


def _row_for_csv(entry: Mapping[str, Any], *, mask: bool) -> Dict[str, Any]:
    event = entry.get("event") or {}
    if mask:
        event = mask_pii(event)
    meta = event.get("_centralops") if isinstance(event, Mapping) else None
    org = None
    if isinstance(meta, Mapping):
        org = meta.get("organization_id")
    wire = entry.get("wire") if isinstance(entry.get("wire"), Mapping) else None
    return {
        "captured_at": entry.get("captured_at"),
        "organization_id": org,
        "vendor": entry.get("vendor"),
        "outcome": entry.get("outcome") or "unknown",
        "route_id": entry.get("route_id"),
        "destination_id": entry.get("destination_id"),
        "detail": _mask_detail(entry.get("detail"), mask=mask),
        "event_json": json.dumps(event, separators=(",", ":"), default=str, ensure_ascii=False),
        "event_id": entry.get("event_id"),
        "stage": entry.get("stage") or "routed",
        "payload_kind": entry.get("payload_kind") or "envelope",
        "pii_redacted": bool(entry.get("pii_redacted")),
        # Só o NÍVEL no CSV — o texto do wire é payload, e payload no CSV vira
        # célula gigante. Quem quer o wire usa NDJSON.
        "wire_fidelity": (wire or {}).get("fidelity"),
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


def ndjson_line(entry: Mapping[str, Any], *, mask: bool, include_wire: bool = True) -> str:
    event = entry.get("event") or {}
    if mask:
        event = mask_pii(event)
    meta = event.get("_centralops") if isinstance(event, Mapping) else None
    record = {
        "captured_at": entry.get("captured_at"),
        # Paridade com o CSV — faltava, e sem ele o NDJSON não permite separar
        # por tenant sem reabrir o envelope.
        "organization_id": (meta or {}).get("organization_id") if isinstance(meta, Mapping) else None,
        "vendor": entry.get("vendor"),
        "outcome": entry.get("outcome") or "unknown",
        "route_id": entry.get("route_id"),
        "destination_id": entry.get("destination_id"),
        "detail": _mask_detail(entry.get("detail"), mask=mask),
        # ── jornal ──
        "event_id": entry.get("event_id"),
        "stage": entry.get("stage") or "routed",
        "payload_kind": entry.get("payload_kind") or "envelope",
        "pii_redacted": bool(entry.get("pii_redacted")),
        "destination_kind": entry.get("destination_kind"),
        "dest_config_version": entry.get("dest_config_version"),
        "capture": entry.get("_capture"),
        "event": event,
    }
    if include_wire and entry.get("wire") is not None:
        record["wire"] = entry["wire"]
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
