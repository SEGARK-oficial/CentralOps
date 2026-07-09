"""Formatadores de wire dos kinds LEGADOS — FONTE ÚNICA.

Este módulo é a **superfície de import única** da formatação (canônico → wire)
dos três kinds legados — ``syslog_rfc3164``, ``syslog_rfc5424`` e ``jsonl``.
Antes, a formatação vivia *inline* dentro do ``send_batch`` de cada sender
e o JSONL era re-implementado no módulo de destino — abrindo espaço para
divergência entre o caminho de **envio** e o ``Destination.format()``.

A função é a fonte ÚNICA do wire. Tanto o **envio** (``*_sender.send_batch`` /
``JSONLWriter.send_batch``) quanto o **destino** (``Destination.format``)
consomem a MESMA função — UMA definição, dois consumidores. O ``send_batch`` NÃO
duplica mais a lógica.

Por que rfc3164/rfc5424 ficam definidas no sender e são *re-exportadas* aqui
-----------------------------------------------------------------------------
``format_rfc3164``/``format_rfc5424`` leem fontes não-determinísticas
(``datetime``/``socket``/``os``) do namespace do *sender*, que os
wire-contract tests congelam por monkeypatch (``rfc3164_sender.datetime`` etc.).
Mover a definição para cá rebindaria esse alvo e quebraria o congelamento — sem
ganho de desacoplamento real, já que continua sendo UMA definição. Então este
módulo as **re-exporta** (mesmo objeto) e fica como ponto de import canônico.

``format_jsonl`` (a única que estava genuinamente DUPLICADA — inline no
``JSONLWriter`` *e* como ``_jsonl_format`` no destino) é definida AQUI, e ambos
os consumidores passam a importá-la.

Fronteira format ↔ framing
---------------------------
A formatação produz a **linha** do wire; o **framing** (LF para rfc3164/jsonl,
octet-counting para rfc5424) continua sendo responsabilidade do ``send_batch``.
``format_rfc3164``/``format_rfc5424`` retornam ``str`` sem ``\\n`` final;
``format_jsonl`` retorna ``bytes`` sem ``\\n`` final.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from ..normalize.severity_map import pri_for_event
from .rfc3164_sender import APP_NAME, PRI_INFO, format_rfc3164
from .syslog_sender import format_rfc5424

__all__ = [
    "format_rfc3164",
    "format_rfc5424",
    "format_jsonl",
    "pri_for_event",
    "APP_NAME",
    "PRI_INFO",
]


def format_jsonl(event: Mapping[str, Any]) -> bytes:
    """Formata um evento em uma linha NDJSON (compacta, UTF-8, sem ``\\n``).

    FONTE ÚNICA do wire JSONL: compact separators ``(",", ":")``,
    ``ensure_ascii=False`` (UTF-8 bruto), ``default=str`` (tipos não-nativos →
    ``str()``). O framing LF é responsabilidade do ``JSONLWriter.send_batch``.
    """
    return json.dumps(
        event, separators=(",", ":"), default=str, ensure_ascii=False
    ).encode("utf-8")
