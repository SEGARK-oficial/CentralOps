"""Respostas SINTÉTICAS da GraphQL Analytics API da Cloudflare (``firewallEventsAdaptive``).

Forma copiada da documentação (developers.cloudflare.com/analytics/graphql-api:
tutorial "Querying Firewall Events", nó ``settings`` e página de erros): o corpo é
``{"data": {"viewer": {"zones": [...]}}, "errors": null}``. Zone IDs, rayNames,
regras e hosts são inventados; IPs vêm das faixas de documentação (RFC 5737 /
RFC 3849) e o domínio é ``example.com``. O "token" é uma string de teste que NÃO é
credencial de ninguém. Nada aqui vem de ambiente real.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ..vendors.cloudflare import _FIELD_PRIORITY

ZONE_A = "a" * 32
ZONE_B = "b" * 32
ZONE_C = "c" * 32

TOKEN = "cf-token-de-teste-nao-e-real-0000000000000000"

#: Todos os campos que o dataset expõe num plano com tudo ligado.
ALL_FIELDS: List[str] = list(_FIELD_PRIORITY) + ["clientRequestBytes", "edgeResponseBytes"]


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def ago(**delta: float) -> datetime:
    return now() - timedelta(**delta)


def event(
    ray: str,
    when: datetime,
    *,
    action: str = "block",
    source: str = "firewallcustom",
    rule_id: str = "7f3a91c2",
    description: str = "Bloqueia scanners conhecidos",
    match_index: int = 0,
    client_ip: str = "203.0.113.7",
    method: str = "GET",
) -> Dict[str, Any]:
    """Uma linha de ``firewallEventsAdaptive`` como a API a entrega (tipos incluídos:
    ``clientAsn`` vem como string; ``edgeResponseStatus`` como número)."""
    return {
        "datetime": iso(when),
        "rayName": ray,
        "action": action,
        "source": source,
        "ruleId": rule_id,
        "description": description,
        "kind": "firewall",
        "matchIndex": match_index,
        "rulesetId": "2c0d5e8b",
        "clientIP": client_ip,
        "clientIPClass": "noRecord",
        "clientAsn": "64500",
        "clientASNDescription": "EXAMPLE-AS",
        "clientCountryName": "BR",
        "clientRequestHTTPHost": "www.example.com",
        "clientRequestHTTPMethodName": method,
        "clientRequestHTTPProtocol": "HTTP/2",
        "clientRequestScheme": "https",
        "clientRequestPath": "/login",
        "clientRequestQuery": "?next=%2F",
        "clientRefererHost": "example.org",
        "userAgent": "Mozilla/5.0 (teste)",
        "edgeColoName": "GRU",
        "edgeResponseStatus": 403,
        "originResponseStatus": 0,
        "sampleInterval": 1,
        "ref": "",
    }


def settings_payload(
    *,
    enabled: bool = True,
    fields: Optional[List[str]] = None,
    max_duration: int = 259200,
    max_page_size: int = 10000,
    max_fields: int = 30,
    not_older_than: int = 2678400,
    zones: bool = True,
) -> Dict[str, Any]:
    """Resposta do nó ``settings`` (os números do exemplo da documentação)."""
    node = {
        "enabled": enabled,
        "availableFields": list(ALL_FIELDS if fields is None else fields),
        "maxDuration": max_duration,
        "maxNumberOfFields": max_fields,
        "maxPageSize": max_page_size,
        "notOlderThan": not_older_than,
    }
    return {
        "data": {"viewer": {"zones": [{"settings": {"firewallEventsAdaptive": node}}] if zones else []}},
        "errors": None,
    }


def events_payload(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"data": {"viewer": {"zones": [{"firewallEventsAdaptive": rows}]}}, "errors": None}


def errors_payload(message: str) -> Dict[str, Any]:
    return {"data": None, "errors": [{"message": message, "path": ["viewer", "zones", "0"]}]}


def verify_payload(status: str = "active") -> Dict[str, Any]:
    return {"result": {"id": "0" * 32, "status": status}, "success": True, "errors": [], "messages": []}


def zones_payload(ids: List[str], *, page: int = 1, total_pages: int = 1) -> Dict[str, Any]:
    return {
        "result": [{"id": z, "name": f"{z[:4]}.example.com", "status": "active"} for z in ids],
        "result_info": {"page": page, "per_page": 50, "total_pages": total_pages, "count": len(ids)},
        "success": True,
        "errors": [],
    }


#: Evento de referência (benchmark e mapping): bloqueio pelo WAF, como o COLETOR o
#: emite (a linha da API + ``zoneTag``).
BLOCK_EVENT: Dict[str, Any] = {**event("8a1b2c3d4e5f6a7b", datetime(2026, 11, 12, 10, 0, 3)), "zoneTag": ZONE_A}
