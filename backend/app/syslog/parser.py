"""Parser syslog → JSON. Puro, sem I/O, nunca levanta em linha ruim.

Cobre o que chega de verdade em porta 514: RFC5424 (com structured-data),
RFC3164 (BSD, sem ano), CEF e LEEF embutidos na mensagem, e o framing
octet-count do TCP. Linha que não casa NENHUM formato NÃO é descartada —
vira ``format="unknown"`` com ``msg`` e ``raw`` intactos, porque o operador
precisa VER o que chegou para escrever o mapping; descartar aqui seria o
"tabela vazia silenciosa" do syslog.

Saída (estável, é o que o mapping ``custom_json.<stream>`` enxerga)::

    {
      "format": "rfc5424" | "rfc3164" | "unknown",
      "pri": 134, "facility": 16, "facility_name": "local0",
      "severity": 6, "severity_name": "informational",
      "version": 1,                      # 0 no RFC3164
      "timestamp": "2026-09-06T01:02:03.000Z" | None,
      "ts_inferred": False,              # True quando o ano foi assumido (3164)
      "host": "fw01", "app": "sshd", "procid": "1234", "msgid": "ID47",
      "sd": {"exampleSDID@32473": {"iut": "3", "eventSource": "App"}},
      "msg": "texto livre",
      "cef": {...} | "leef": {...}       # quando o msg carrega um deles
      "raw": "<134>1 ..."               # a linha como chegou (sem framing)
    }
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

FACILITY_NAMES = (
    "kern", "user", "mail", "daemon", "auth", "syslog", "lpr", "news", "uucp", "cron",
    "authpriv", "ftp", "ntp", "audit", "alert", "clock", "local0", "local1", "local2",
    "local3", "local4", "local5", "local6", "local7",
)
SEVERITY_NAMES = ("emergency", "alert", "critical", "error", "warning", "notice", "informational", "debug")

_PRI_RE = re.compile(r"^<(\d{1,3})>")
_RFC5424_RE = re.compile(
    r"^<(?P<pri>\d{1,3})>(?P<version>\d{1,2})\s+"
    r"(?P<ts>\S+)\s+(?P<host>\S+)\s+(?P<app>\S+)\s+(?P<procid>\S+)\s+(?P<msgid>\S+)\s+"
    r"(?P<rest>.*)$",
    re.S,
)
_RFC3164_RE = re.compile(
    r"^(?:<(?P<pri>\d{1,3})>)?"
    r"(?P<ts>[A-Z][a-z]{2}\s{1,2}\d{1,2}\s\d{2}:\d{2}:\d{2})\s+"
    r"(?P<host>\S+)\s+"
    r"(?P<rest>.*)$",
    re.S,
)
_TAG_RE = re.compile(r"^(?P<app>[A-Za-z0-9_./-]+)(?:\[(?P<pid>[^\]]+)\])?:\s?(?P<msg>.*)$", re.S)
_OCTET_RE = re.compile(r"^(\d{1,7})\s")
_MONTHS = {m: i for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}


def strip_octet_count(line: bytes) -> Tuple[bytes, Optional[int]]:
    """Framing ``<len> <msg>`` (RFC6587). Devolve (mensagem, len declarado)."""
    m = _OCTET_RE.match(line.decode("ascii", "replace")[:9])
    if not m:
        return line, None
    n = int(m.group(1))
    body = line[m.end():]
    return body[:n] if len(body) >= n else body, n


def _pri_fields(pri: Optional[int]) -> Dict[str, Any]:
    if pri is None or pri < 0 or pri > 191:
        return {"pri": None, "facility": None, "facility_name": None, "severity": None, "severity_name": None}
    fac, sev = divmod(pri, 8)
    return {
        "pri": pri, "facility": fac,
        "facility_name": FACILITY_NAMES[fac] if fac < len(FACILITY_NAMES) else str(fac),
        "severity": sev, "severity_name": SEVERITY_NAMES[sev],
    }


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_5424_ts(ts: str) -> Optional[str]:
    if ts == "-":
        return None
    try:
        return _iso(datetime.fromisoformat(ts.replace("Z", "+00:00")))
    except ValueError:
        return None


def _parse_3164_ts(ts: str, *, now: Optional[datetime] = None) -> Optional[str]:
    """``Sep  6 01:02:03`` não tem ano nem fuso: assume o ano corrente (ou o
    anterior, se a data ficar no futuro — virada de ano) e UTC."""
    try:
        mon, day, hms = ts.split()
        h, mi, s = (int(x) for x in hms.split(":"))
        now = now or datetime.now(timezone.utc)
        dt = datetime(now.year, _MONTHS[mon], int(day), h, mi, s, tzinfo=timezone.utc)
        if dt > now.replace(microsecond=0) and (dt - now).days > 1:
            dt = dt.replace(year=now.year - 1)
        return _iso(dt)
    except (ValueError, KeyError):
        return None


def parse_structured_data(rest: str) -> Tuple[Dict[str, Dict[str, str]], str]:
    """``[id k="v" k2="v2"][id2 ...] msg`` → (sd, msg). Honra os escapes do RFC
    (``\\"``, ``\\]``, ``\\\\``). ``-`` = sem SD."""
    if rest.startswith("-"):
        return {}, rest[1:].lstrip()
    sd: Dict[str, Dict[str, str]] = {}
    i = 0
    n = len(rest)
    while i < n and rest[i] == "[":
        j = i + 1
        # id
        k = j
        while k < n and rest[k] not in " ]":
            k += 1
        sd_id = rest[j:k]
        params: Dict[str, str] = {}
        i = k
        while i < n and rest[i] != "]":
            while i < n and rest[i] == " ":
                i += 1
            if i < n and rest[i] == "]":
                break
            eq = rest.find("=", i)
            if eq < 0:
                return sd, rest[i:]
            name = rest[i:eq]
            if eq + 1 >= n or rest[eq + 1] != '"':
                return sd, rest[i:]
            v = []
            p = eq + 2
            while p < n:
                ch = rest[p]
                if ch == "\\" and p + 1 < n and rest[p + 1] in '"]\\':
                    v.append(rest[p + 1]); p += 2; continue
                if ch == '"':
                    break
                v.append(ch); p += 1
            params[name] = "".join(v)
            i = p + 1
        sd[sd_id] = params
        i += 1  # ']'
    return sd, rest[i:].lstrip()


def _split_ext(ext: str, delim: str = " ") -> Dict[str, str]:
    """``k=v k2=v2`` do CEF/LEEF, tolerando espaços no valor: um novo par começa
    onde aparece ``<space>token=``."""
    out: Dict[str, str] = {}
    if not ext:
        return out
    if delim != " ":
        for part in ext.split(delim):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v
        return out
    pairs = re.split(r"\s+(?=[A-Za-z0-9_.]+=)", ext.strip())
    for part in pairs:
        if "=" in part:
            k, v = part.split("=", 1)
            out[k] = v.replace("\\=", "=").replace("\\\\", "\\")
    return out


def _cef_split(header: str, n: int) -> list:
    """Divide por ``|`` honrando ``\\|``; para após ``n`` campos (o resto é ext)."""
    fields, cur, i = [], [], 0
    while i < len(header) and len(fields) < n:
        ch = header[i]
        if ch == "\\" and i + 1 < len(header):
            cur.append(header[i + 1]); i += 2; continue
        if ch == "|":
            fields.append("".join(cur)); cur = []; i += 1; continue
        cur.append(ch); i += 1
    return fields + [header[i:]]


def parse_cef(msg: str) -> Optional[Dict[str, Any]]:
    idx = msg.find("CEF:")
    if idx < 0:
        return None
    parts = _cef_split(msg[idx + 4:], 7)
    if len(parts) < 8:
        return None
    version, vendor, product, pversion, sig, name, sev, ext = parts[:8]
    return {
        "version": version.strip(), "vendor": vendor, "product": product, "product_version": pversion,
        "signature_id": sig, "name": name, "severity": sev, "ext": _split_ext(ext),
    }


def parse_leef(msg: str) -> Optional[Dict[str, Any]]:
    idx = msg.find("LEEF:")
    if idx < 0:
        return None
    body = msg[idx + 5:]
    parts = body.split("|", 5)
    if len(parts) < 5:
        return None
    version, vendor, product, pversion, event_id = parts[:5]
    rest = parts[5] if len(parts) > 5 else ""
    delim = "\t"
    if version.strip().startswith("2") and rest[:1] and rest[:1] != "|":
        # LEEF 2.0: o 6º campo é o delimitador (1 char ou xHH) seguido de |
        d, sep, attrs = rest.partition("|")
        if sep:
            delim = chr(int(d[1:], 16)) if d.startswith("x") and len(d) > 1 else (d or "\t")
            rest = attrs
    return {
        "version": version.strip(), "vendor": vendor, "product": product, "product_version": pversion,
        "event_id": event_id, "ext": _split_ext(rest, delim),
    }


def parse_syslog_line(raw: Any, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Uma linha (bytes ou str, sem framing) → dict. Nunca levanta."""
    if isinstance(raw, (bytes, bytearray)):
        line = bytes(raw).decode("utf-8", "replace")
    else:
        line = str(raw)
    line = line.rstrip("\r\n")
    out: Dict[str, Any] = {"format": "unknown", "raw": line, "version": 0, "timestamp": None, "ts_inferred": False,
                           "host": None, "app": None, "procid": None, "msgid": None, "sd": {}, "msg": line}
    out.update(_pri_fields(None))

    m = _RFC5424_RE.match(line)
    if m and m.group("version") in ("1",):
        out.update(_pri_fields(int(m.group("pri"))))
        out["format"] = "rfc5424"
        out["version"] = 1
        out["timestamp"] = _parse_5424_ts(m.group("ts"))
        out["host"] = None if m.group("host") == "-" else m.group("host")
        out["app"] = None if m.group("app") == "-" else m.group("app")
        out["procid"] = None if m.group("procid") == "-" else m.group("procid")
        out["msgid"] = None if m.group("msgid") == "-" else m.group("msgid")
        sd, msg = parse_structured_data(m.group("rest"))
        out["sd"] = sd
        # BOM opcional do RFC
        out["msg"] = msg[1:] if msg.startswith("﻿") else msg
    else:
        m = _RFC3164_RE.match(line)
        if m:
            pri = int(m.group("pri")) if m.group("pri") else None
            out.update(_pri_fields(pri))
            out["format"] = "rfc3164"
            out["timestamp"] = _parse_3164_ts(m.group("ts"), now=now)
            out["ts_inferred"] = out["timestamp"] is not None
            out["host"] = m.group("host")
            rest = m.group("rest")
            t = _TAG_RE.match(rest)
            # ``CEF:0|...`` / ``LEEF:2.0|...`` no lugar do TAG: o "app" seria "CEF".
            # Mantém o corpo inteiro como msg para o parser de CEF/LEEF ver o cabeçalho.
            if t and t.group("app") not in ("CEF", "LEEF"):
                out["app"], out["procid"], out["msg"] = t.group("app"), t.group("pid"), t.group("msg")
            else:
                out["msg"] = rest
        else:
            pm = _PRI_RE.match(line)
            if pm:
                out.update(_pri_fields(int(pm.group(1))))
                out["msg"] = line[pm.end():]

    msg = out["msg"] or ""
    cef = parse_cef(msg)
    if cef:
        out["cef"] = cef
    else:
        leef = parse_leef(msg)
        if leef:
            out["leef"] = leef
    return out
