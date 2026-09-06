"""Parser syslog (W3.2) e classificador por conteúdo (W3.3).

Fixtures ANONIMIZADAS com a forma real de cada vendor. O que se prova: o parser
nunca descarta (linha ruim vira ``format=unknown`` com o texto intacto), o
structured-data do RFC5424 faz ida e volta com escapes, o framing octet-count
do TCP é respeitado, e os seis detectores de fábrica classificam o próprio
formato — e SÓ ele.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.app.collectors import classify
from backend.app.syslog.parser import parse_cef, parse_leef, parse_structured_data, parse_syslog_line, strip_octet_count

NOW = datetime(2026, 9, 6, 12, 0, 0, tzinfo=timezone.utc)

FIXTURES = {
    "fortigate": '<134>date=2026-09-06 time=01:02:03 devname="fw-edge" devid="FG100FTK00000000" logid="0000000013" type="traffic" subtype="forward" level="notice" srcip=10.0.5.7 dstip=203.0.113.9 action="deny"',
    "paloalto": "<14>Sep  6 01:02:03 pa-vm-01 1,2026/09/06 01:02:03,0009C100000,TRAFFIC,end,2049,2026/09/06 01:02:03,10.0.5.7,203.0.113.9,0.0.0.0,0.0.0.0,rule-1,,,ssl,vsys1,trust,untrust",
    "cisco_asa": "<166>Sep  6 01:02:03 asa-01 %ASA-6-302013: Built inbound TCP connection 12345 for outside:203.0.113.9/443 (203.0.113.9/443) to inside:10.0.5.7/51234 (10.0.5.7/51234)",
    "pfsense": "<134>Sep  6 01:02:03 pf-01 filterlog[8231]: 5,,,1000000103,igb0,match,block,in,4,0x0,,64,0,0,DF,6,tcp,60,203.0.113.9,10.0.5.7,4422,22,0,S",
    "linux_auth": "<86>Sep  6 01:02:03 srv-01 sshd[912]: Failed password for invalid user admin from 203.0.113.9 port 4422 ssh2",
    "windows_vector": '<14>1 2026-09-06T01:02:03Z ws-01 Microsoft-Windows-Security-Auditing 4624 - - {"EventID":4624,"TargetUserName":"alice"}',
}


# ── parser ─────────────────────────────────────────────────────────────────

def test_rfc5424_structured_data_roundtrip():
    line = ('<165>1 2026-09-06T01:02:03.123Z fw01 evntslog 1234 ID47 '
            '[exampleSDID@32473 iut="3" eventSource="App \\"x\\"" eventID="1011"][ex2 a="b\\]c" b="x\\\\y"] ﻿An application event')
    e = parse_syslog_line(line)
    assert e["format"] == "rfc5424" and e["version"] == 1
    assert (e["facility"], e["facility_name"], e["severity"], e["severity_name"]) == (20, "local4", 5, "notice")
    assert e["timestamp"] == "2026-09-06T01:02:03.123Z"
    assert (e["host"], e["app"], e["procid"], e["msgid"]) == ("fw01", "evntslog", "1234", "ID47")
    assert e["sd"] == {"exampleSDID@32473": {"iut": "3", "eventSource": 'App "x"', "eventID": "1011"}, "ex2": {"a": "b]c", "b": "x\\y"}}
    assert e["msg"] == "An application event"  # BOM removido
    assert e["raw"] == line


def test_rfc5424_campos_nil_viram_none():
    e = parse_syslog_line("<13>1 - - - - - - só a mensagem")
    assert e["format"] == "rfc5424" and e["timestamp"] is None and e["host"] is None and e["sd"] == {}
    assert e["msg"] == "só a mensagem"


def test_rfc3164_infere_ano_e_marca_isso():
    e = parse_syslog_line("<34>Sep  6 01:02:03 host1 sshd[912]: Failed password", now=NOW)
    assert e["format"] == "rfc3164" and e["version"] == 0
    assert e["timestamp"] == "2026-09-06T01:02:03.000Z" and e["ts_inferred"] is True
    assert (e["host"], e["app"], e["procid"]) == ("host1", "sshd", "912")
    assert e["msg"] == "Failed password"
    assert (e["facility_name"], e["severity_name"]) == ("auth", "critical")


def test_rfc3164_data_no_futuro_cai_no_ano_anterior():
    e = parse_syslog_line("<34>Dec 30 23:59:59 h app: x", now=datetime(2026, 1, 2, tzinfo=timezone.utc))
    assert e["timestamp"].startswith("2025-12-30")


def test_linha_sem_formato_nao_e_descartada():
    e = parse_syslog_line("isto não é syslog")
    assert e["format"] == "unknown" and e["msg"] == "isto não é syslog" and e["pri"] is None
    e = parse_syslog_line(b"<13>s\xc3\xb3 pri e texto \xff")
    assert e["format"] == "unknown" and e["pri"] == 13 and e["msg"].startswith("só pri e texto")
    assert parse_syslog_line("")["msg"] == ""


def test_cef_com_escapes_e_valores_com_espaco():
    e = parse_syslog_line("<134>Sep  6 01:02:03 fw CEF:0|Vendor\\|X|Prod|1.0|100|Name with space|5|src=10.0.0.1 dst=10.0.0.2 msg=hello world act=deny fname=a\\=b")
    c = e["cef"]
    assert c["vendor"] == "Vendor|X" and c["name"] == "Name with space" and c["severity"] == "5"
    assert c["ext"] == {"src": "10.0.0.1", "dst": "10.0.0.2", "msg": "hello world", "act": "deny", "fname": "a=b"}
    assert e["app"] is None  # "CEF" não é o app


def test_leef_2_com_delimitador_declarado():
    e = parse_syslog_line("<13>1 - - - - - - LEEF:2.0|Vendor|Prod|1.0|EV1|x09|src=1.1.1.1\tdst=2.2.2.2")
    assert e["leef"]["event_id"] == "EV1" and e["leef"]["ext"] == {"src": "1.1.1.1", "dst": "2.2.2.2"}
    assert parse_leef("LEEF:1.0|V|P|1|E|src=1\tdst=2")["ext"] == {"src": "1", "dst": "2"}
    assert parse_cef("nada") is None and parse_leef("nada") is None


def test_structured_data_malformado_nao_levanta():
    sd, msg = parse_structured_data('[bad param] resto')
    assert isinstance(sd, dict) and "resto" in msg or msg


def test_octet_count_framing():
    body, n = strip_octet_count(b"11 <1>1 hello extra")
    assert n == 11 and body == b"<1>1 hello "
    assert strip_octet_count(b"<1>1 sem framing") == (b"<1>1 sem framing", None)


# ── classificador ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", sorted(classify.FACTORY_DETECTORS))
def test_cada_detector_de_fabrica_casa_so_o_proprio_formato(name):
    clf = classify.compile_classifier({"rules": [{"when": f"@{name}", "stream": "hit"}]}, default_stream="miss")
    for other, line in FIXTURES.items():
        got = clf.classify(parse_syslog_line(line, now=NOW))
        assert got == ("hit" if other == name else "miss"), (name, other, got)


def test_primeira_regra_que_casa_vence_e_default_cobre_o_resto():
    clf = classify.compile_classifier({"rules": [
        {"when": "@linux_auth", "stream": "auth"},
        {"when": "facility == `4`", "stream": "auth-generic"},
    ]}, default_stream="other")
    assert clf.classify(parse_syslog_line(FIXTURES["linux_auth"], now=NOW)) == "auth"
    assert clf.classify(parse_syslog_line("garbage")) == "other"
    assert clf.rules == (("@linux_auth", "auth"), ("facility == `4`", "auth-generic"))


def test_explain_mostra_o_rastro_por_regra():
    clf = classify.compile_classifier({"rules": [{"when": "@cisco_asa", "stream": "asa"}]}, default_stream="d")
    ex = clf.explain(parse_syslog_line(FIXTURES["cisco_asa"], now=NOW))
    assert ex["stream"] == "asa" and ex["matched_rule"] is True and ex["trace"][0]["matched"] is True
    ex = clf.explain(parse_syslog_line("x"))
    assert ex["stream"] == "d" and ex["matched_rule"] is False


@pytest.mark.parametrize("cfg,msg", [
    ({"rules": [{"when": "@nao_existe", "stream": "s"}]}, "detector de fábrica desconhecido"),
    ({"rules": [{"when": "msg ==", "stream": "s"}]}, "JMESPath inválido"),
    ({"rules": [{"when": "@fortigate"}]}, "'stream' obrigatório"),
    ({"rules": "x"}, "lista"),
    ({"rules": [{"when": "a", "stream": "s"}] * 51}, "50 regras"),
])
def test_config_invalida_falha_na_escrita_nao_no_servidor(cfg, msg):
    with pytest.raises(classify.ClassifierError, match=msg):
        classify.compile_classifier(cfg)


def test_jmespath_com_tipo_inesperado_nao_levanta_na_avaliacao():
    clf = classify.compile_classifier({"rules": [{"when": "contains(msg, 'x')", "stream": "s"}]}, default_stream="d")
    assert clf.classify({"msg": 123}) == "d"
    assert clf.classify({}) == "d"


def test_streams_referenced():
    assert classify.streams_referenced({"rules": [{"when": "a", "stream": "b"}, {"when": "c", "stream": "a"}], "default_stream": "z"}) == ["a", "b", "z"]
    assert classify.streams_referenced(None) == []
