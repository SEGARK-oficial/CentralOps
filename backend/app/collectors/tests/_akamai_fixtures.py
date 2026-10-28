"""Eventos SINTÉTICOS da SIEM Integration API da Akamai.

Forma copiada da documentação da Akamai (techdocs.akamai.com/siem-integration):
uma linha JSON por evento e, por último, a linha de contexto ``{total, offset,
limit?}``. Os campos de regra do ``attackData`` vão como a API os entrega:
pedaços base64 separados por ``;`` e URL-encoded. Hosts, IDs e IPs são
inventados (faixas de documentação 192.0.2.0/24 e 203.0.113.0/24, domínio
``example.com``). Nada aqui vem de ambiente real.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from ..vendors.akamai import decode_attack_data

HOST = "akab-test0000000000000-0000000000000000.luna.akamaiapis.net"
CONFIG_A = "14227"
CONFIG_B = "14228"

# Valores do ``testdata.json`` do SDK oficial (github.com/akamai/
# AkamaiOPEN-edgegrid-python, akamai/edgegrid/test/testdata.json): são de exemplo,
# publicados pela própria Akamai para validar a assinatura.
EG_HOST = "akaa-baseurl-xxxxxxxxxxx-xxxxxxxxxxxxx.luna.akamaiapis.net"
EG_CLIENT_TOKEN = "akab-client-token-xxx-xxxxxxxxxxxxxxxx"
EG_ACCESS_TOKEN = "akab-access-token-xxx-xxxxxxxxxxxxxxxx"
EG_CLIENT_SECRET = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx="
EG_NONCE = "nonce-xx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
EG_TIMESTAMP = "20140321T19:34:21+0000"

SECRET = "c2VjcmV0LWRlLXRlc3RlLW5hby1lLXJlYWw="
EDGERC = f"""[default]
host = {HOST}/
client_token = akab-ctoken-0000000000000000
client_secret = {SECRET}
access_token = akab-atoken-0000000000000000
max_body = 131072
"""


def encode_rules(values: List[str]) -> str:
    """Como a API entrega: base64 de cada item, ``;`` entre eles, URL-encoded."""
    joined = ";".join(base64.b64encode(v.encode("utf-8")).decode("ascii") for v in values)
    return quote(joined, safe="")


def event(
    request_id: str,
    start: int,
    *,
    action: str = "deny",
    method: str = "GET",
    config_id: str = CONFIG_A,
    policy_id: str = "qik1_26545",
    client_ip: str = "192.0.2.10",
    rules: Optional[List[str]] = None,
    messages: Optional[List[str]] = None,
) -> Dict[str, Any]:
    rules = rules if rules is not None else ["950004", "990011"]
    messages = messages if messages is not None else [
        "Cross-site Scripting (XSS) Attack",
        "Request Indicates an automated program explored the site",
    ]
    return {
        "type": "akamai_siem",
        "format": "json",
        "version": "1.0",
        "attackData": {
            "configId": config_id,
            "policyId": policy_id,
            "clientIP": client_ip,
            "appliedAction": action,
            "clientReputation": "",
            "rules": encode_rules(rules),
            "ruleVersions": encode_rules(["4" for _ in rules]),
            "ruleMessages": encode_rules(messages),
            "ruleTags": encode_rules(["WEB_ATTACK/XSS", "AUTOMATED/BOT"][: len(rules)]),
            "ruleData": encode_rules(["<script>alert(1)</script>", "curl/8.0"][: len(rules)]),
            "ruleSelectors": encode_rules(["ARGS:q", ""][: len(rules)]),
            "ruleActions": encode_rules([action, "alert"][: len(rules)]),
        },
        "httpMessage": {
            "requestId": request_id,
            "start": str(start),
            "protocol": "HTTP/1.1",
            "method": method,
            "host": "www.example.com",
            "port": "443",
            "path": "/search",
            "query": "q%3D%3Cscript%3E",
            "requestHeaders": quote(
                "User-Agent: Mozilla/5.0 (teste)\r\nAccept: */*\r\n", safe=""
            ),
            "status": "403",
            "bytes": "266",
            "tls": "tls1.3",
        },
        "geo": {
            "continent": "SA",
            "country": "BR",
            "city": "SAOPAULO",
            "regionCode": "SP",
            "asn": "64500",
        },
    }


def body(events: List[Dict[str, Any]], *, offset: str, limit: Optional[int] = None) -> str:
    """Corpo NDJSON: um evento por linha + a linha de contexto no fim."""
    meta: Dict[str, Any] = {"total": len(events), "offset": offset}
    if limit is not None:
        meta["limit"] = limit
    return "\n".join([*(json.dumps(e) for e in events), json.dumps(meta)]) + "\n"


#: Evento de referência para o benchmark e o mapping: XSS bloqueado, como o
#: COLETOR o emite (``attackData.decoded_rules`` e User-Agent já decodificados).
XSS_DENY = decode_attack_data(event("2a5e8f0c", 1761652800))


# Exemplo OFICIAL da resposta de ``GET /siem/v1/configs/{configId}`` (techdocs.akamai.com/
# siem-integration/reference/get-config, exemplo "Event lines"), só com o host trocado
# para example.com e sem ``responseHeaders``. É o contrato publicado: Account Protector
# (``userRiskData``), Bot Manager (``botData``), fingerprint TLS (``identity.ja4``) e três
# regras — duas ``alert`` e a de anomalia que decidiu ``deny``.
OFFICIAL_EXAMPLE_RAW: Dict[str, Any] = {'attackData': {'appliedAction': 'alert',
                'clientIP': '192.0.2.82',
                'configId': '14227',
                'policyId': 'qik1_26545',
                'ruleActions': 'YWxlcnQ%3d%3bYWxlcnQ%3d%3bZGVueQ%3d%3d',
                'ruleData': 'dGVsbmV0LmV4ZQ%3d%3d%3bdGVsbmV0LmV4ZQ%3d%3d%3bVmVjdG9yIFNjb3JlOiAxMCwgREVOWSB0aHJlc2hvbGQ6IDksIEFsZXJ0IFJ1bGVzOiA5NTAwMDI6OTUwMDA2LCBEZW55IFJ1bGU6ICwgTGFzdCBNYXRjaGVkIE1lc3NhZ2U6IFN5c3RlbSBDb21tYW5kIEluamVjdGlvbg%3d%3d',
                'ruleMessages': 'U3lzdGVtIENvbW1hbmQgQWNjZXNz%3bU3lzdGVtIENvbW1hbmQgSW5qZWN0aW9u%3bQW5vbWFseSBTY29yZSBFeGNlZWRlZCBmb3IgQ29tbWFuZCBJbmplY3Rpb24%3d',
                'ruleSelectors': 'QVJHUzpvcHRpb24%3d%3bQVJHUzpvcHRpb24%3d%3b',
                'ruleTags': 'T1dBU1BfQ1JTL1dFQl9BVFRBQ0svRklMRV9JTkpFQ1RJT04%3d%3bT1dBU1BfQ1JTL1dFQl9BVFRBQ0svQ09NTUFORF9JTkpFQ1RJT04%3d%3bQUtBTUFJL1BPTElDWS9DTURfSU5KRUNUSU9OX0FOT01BTFk%3d',
                'ruleVersions': 'NA%3d%3d%3bNA%3d%3d%3bMQ%3d%3d',
                'rules': 'OTUwMDAy%3bOTUwMDA2%3bQ01ELUlOSkVDVElPTi1BTk9NQUxZ'},
 'botData': {'botScore': '100', 'responseSegment': '3'},
 'clientData': {'appBundleId': 'com.mydomain.myapp',
                'appVersion': '1.23',
                'sdkVersion': '4.7.1',
                'telemetryType': '2'},
 'format': 'json',
 'geo': {'asn': '14618',
         'city': 'ASHBURN',
         'continent': '288',
         'country': 'US',
         'regionCode': 'VA'},
 'httpMessage': {'bytes': '266',
                 'host': 'www.example.com',
                 'method': 'GET',
                 'path': '/',
                 'port': '80',
                 'protocol': 'HTTP/1.1',
                 'query': 'option=com_jce%20telnet.exe',
                 'requestHeaders': 'User-Agent%3a%20BOT%2f0.1%20(BOT%20for%20JCE)%0d%0aAccept%3a%20text%2fhtml,application%2fxhtml+xml,application%2fxml%3bq%3d0.9,*%2f*%3bq%3d0.8%0d%0auniqueID%3a%20CR_H8%0d%0aAccept-Language%3a%20en-US,en%3bq%3d0.5%0d%0aAccept-Encoding%3a%20gzip,%20deflate%0d%0aConnection%3a%20keep-alive%0d%0aHost%3a%20www.example.com%0d%0aContent-Length%3a%200%0d%0a',
                 'requestId': '1158db1758e37bfe67b7c09',
                 'start': '1491303422',
                 'status': '200'},
 'identity': {'ja4': 't13d201100_2b729b4bf6f3_9e7b989ebec8',
              'tlsFingerprintV2': '46008b1582967146',
              'tlsFingerprintV3': '3~fe38c35477967146'},
 'type': 'akamai_siem',
 'userRiskData': {'allow': '0',
                  'emailDomain': 'example.com',
                  'general': 'duc_1h:10|duc_1d:30',
                  'originUserId': 'jsmith007',
                  'risk': 'udfp:1325gdg4g4343g/M|unp:74256/H',
                  'score': '75',
                  'status': '0',
                  'trust': 'ugp:US',
                  'username': 'jsmith@example.com',
                  'uuid': '964d54b7-0821-413a-a4d6-8131770ec8d5'},
 'version': '1.0'}
OFFICIAL_EXAMPLE = decode_attack_data(json.loads(json.dumps(OFFICIAL_EXAMPLE_RAW)))
