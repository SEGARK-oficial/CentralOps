"""Fidelidade do "como saiu no fio", POR DESTINO — e a honestidade sobre ela.

``Destination.format(envelope)`` é o que mais se aproxima do payload entregue,
mas NÃO é o wire para todos os kinds, e a diferença importa: o cliente vai
comparar o que a tela mostra com o log do SIEM dele. Sem um rótulo por kind, o
produto MENTE em 4 dos 16 e é irreproduzível em 2.

Cinco níveis:

``exact``
    ``format()`` É o payload por item. O framing do LOTE (array JSON, NDJSON,
    envelope de requisição) vai descrito em ``note`` — o que falta é declarado,
    não escondido.

``nondeterministic``
    O formatter lê relógio, hostname e PID a cada chamada. A linha exibida
    NUNCA será byte-idêntica à entregue, nem em teste — o próprio teste de
    paridade do repo só prova igualdade congelando as três coisas.

``partial``
    Falta um pedaço SEMÂNTICO, não cosmético: em ``elastic_bulk`` falta a linha
    de ação que decide create-vs-index e o ``_id`` (ou seja, a idempotência); em
    ``otlp`` falta o ``ExportLogsServiceRequest`` com resource e scopeLogs.

``not_representable``
    A unidade do fio é o LOTE, não o evento: NDJSON do lote em gzip
    (``s3``), Parquet colunar (``security_lake``). Não existe "wire por evento"
    — e por isso estes NÃO devolvem preview nenhum. Mostrar um fragmento
    induziria exatamente a comparação errada.

``error``
    O formatter levantou. Alguns senders recusam envelope degenerado, e isso é
    informação diagnóstica legítima — não um bug do preview.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, NamedTuple

logger = logging.getLogger(__name__)

#: Teto do texto de wire gravado/exibido.
MAX_WIRE_BYTES = 16_384

FIDELITY_EXACT = "exact"
FIDELITY_NONDETERMINISTIC = "nondeterministic"
FIDELITY_PARTIAL = "partial"
FIDELITY_NOT_REPRESENTABLE = "not_representable"
FIDELITY_ERROR = "error"


class WireSpec(NamedTuple):
    fidelity: str
    encoding: str  # "json" | "ndjson" | "text" | "binary"
    note: str


#: EXAUSTIVA sobre os kinds do registry. Um kind novo SEM entrada aqui cairia
#: num default — e um default ``exact`` é a pior mentira possível. O teste
#: parametrizado sobre o registry é o que trava isso.
WIRE_FIDELITY: Dict[str, WireSpec] = {
    "jsonl": WireSpec(
        FIDELITY_EXACT, "ndjson",
        "uma linha JSON por evento; o arquivo é a concatenação das linhas",
    ),
    "kafka": WireSpec(
        FIDELITY_EXACT, "json",
        "corpo da mensagem; chave de partição e headers não aparecem aqui",
    ),
    "webhook": WireSpec(
        FIDELITY_EXACT, "json", "o corpo real é o array JSON do lote",
    ),
    "datadog": WireSpec(
        FIDELITY_EXACT, "json", "o corpo real é o array JSON do lote",
    ),
    "chronicle": WireSpec(
        FIDELITY_EXACT, "json", "o corpo real embrulha os eventos do lote",
    ),
    "sentinel": WireSpec(
        FIDELITY_EXACT, "json", "o corpo real é o array JSON do lote",
    ),
    "clickhouse": WireSpec(
        FIDELITY_EXACT, "ndjson",
        "insert JSONEachRow; o corpo é o lote concatenado. A linha reflete "
        "'payload' (o conteúdo) e 'row_shape' (a forma ao redor dele)",
    ),
    "nano": WireSpec(
        FIDELITY_EXACT, "ndjson",
        "insert JSONEachRow no ClickHouse do nano; a linha é sempre "
        '{"event": <ocsf>, "source_type": "<feed>"}',
    ),
    "crowdstrike_logscale": WireSpec(
        FIDELITY_EXACT, "json", "o corpo real é o array JSON do lote",
    ),
    "crowdstrike_ngsiem": WireSpec(
        FIDELITY_EXACT, "json", "o corpo real é o array JSON do lote",
    ),
    "splunk_hec": WireSpec(
        FIDELITY_EXACT, "json",
        "eventos HEC concatenados sem separador; este é um deles",
    ),
    "syslog_rfc3164": WireSpec(
        FIDELITY_NONDETERMINISTIC, "text",
        "timestamp, hostname e PID são recalculados a cada envio — a linha "
        "entregue difere desta nesses campos",
    ),
    "syslog_rfc5424": WireSpec(
        FIDELITY_NONDETERMINISTIC, "text",
        "timestamp, hostname e PID são recalculados a cada envio — a linha "
        "entregue difere desta nesses campos",
    ),
    "elastic_bulk": WireSpec(
        FIDELITY_PARTIAL, "ndjson",
        "falta a linha de ação do _bulk (create vs index e o _id), que é o que "
        "define a idempotência da escrita",
    ),
    "otlp": WireSpec(
        FIDELITY_PARTIAL, "json",
        "é um LogRecord isolado; o fio é um ExportLogsServiceRequest com "
        "resource e scopeLogs em volta",
    ),
    "s3": WireSpec(
        FIDELITY_NOT_REPRESENTABLE, "binary",
        "o objeto gravado é o LOTE inteiro em NDJSON comprimido (gzip) — não "
        "existe representação por evento",
    ),
    "security_lake": WireSpec(
        FIDELITY_NOT_REPRESENTABLE, "binary",
        "o objeto gravado é Parquet colunar (zstd) do LOTE — não existe "
        "representação por evento",
    ),
}


def spec_for(kind: str) -> WireSpec:
    """Spec do kind. Kind desconhecido vira ``not_representable`` SEM preview.

    Fail-closed deliberado: um kind que ninguém classificou não pode receber o
    benefício da dúvida num campo cujo propósito é dizer o quanto confiar.
    """
    return WIRE_FIDELITY.get(
        kind,
        WireSpec(
            FIDELITY_NOT_REPRESENTABLE, "binary",
            f"kind {kind!r} não classificado — sem garantia de fidelidade",
        ),
    )


def _stringify(value: Any, encoding: str) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        # ``replace`` e não ``ignore``: ``ignore`` remove o byte inválido em
        # silêncio, e o operador que está diagnosticando encoding precisa VER
        # que havia algo ali.
        return bytes(value).decode("utf-8", "replace")
    from ._fastjson import dumps_bytes

    return dumps_bytes(value).decode("utf-8")


def render(dest: Any, envelope: Mapping[str, Any], *, limit_bytes: int = MAX_WIRE_BYTES) -> Dict[str, Any]:
    """``{fidelity, encoding, note, text?, bytes, truncated}`` para um envelope.

    NUNCA levanta. Serializa IMEDIATAMENTE para ``str``: vários kinds devolvem
    uma cópia RASA (e ``splunk_hec`` devolve o próprio envelope aninhado), então
    guardar o objeto criaria aliasing com o payload que ainda vai ser entregue.

    ``not_representable`` NÃO devolve ``text``. É a postura estrita e correta:
    quando a unidade do fio é o lote, um fragmento por evento induz o operador a
    diffar contra a coisa errada.
    """
    kind = str(getattr(dest, "kind", "") or "")
    spec = spec_for(kind)
    out: Dict[str, Any] = {
        "fidelity": spec.fidelity,
        "encoding": spec.encoding,
        "note": spec.note,
        "bytes": 0,
        "truncated": False,
    }
    if spec.fidelity == FIDELITY_NOT_REPRESENTABLE:
        return out

    try:
        raw = dest.format(envelope)
        text = _stringify(raw, spec.encoding)
    except NotImplementedError:
        out["fidelity"] = FIDELITY_NOT_REPRESENTABLE
        out["note"] = "este destino não expõe formatter desacoplado"
        return out
    except Exception as exc:  # noqa: BLE001 — preview nunca derruba nada
        out["fidelity"] = FIDELITY_ERROR
        out["note"] = f"o formatter recusou este evento: {type(exc).__name__}"
        return out

    encoded = text.encode("utf-8")
    out["bytes"] = len(encoded)
    if len(encoded) > limit_bytes:
        # Corta por CARACTERE sobre o texto, nunca fatiando os bytes — fatiar
        # UTF-8 parte codepoint no meio.
        out["text"] = text[: max(0, limit_bytes // 4)]
        out["truncated"] = True
    else:
        out["text"] = text
    return out
