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
