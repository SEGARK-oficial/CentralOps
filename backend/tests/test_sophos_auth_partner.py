"""Unit tests for the Sophos Partner-mode additions to ``SophosAuthService``."""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import MagicMock

import httpx
import pytest

from backend.app.services.auth import SophosAuthService


# ── _whoami / discover_identity ──────────────────────────────────────


def _make_service_with_get(get_responses: list[Dict[str, Any]]) -> SophosAuthService:
    service = SophosAuthService("cid", "csec")
    fake_responses = []
    for body in get_responses:
        resp = MagicMock(spec=httpx.Response)
        resp.json.return_value = body
        resp.raise_for_status = MagicMock()
        fake_responses.append(resp)
    service._http.get = MagicMock(side_effect=fake_responses)  # type: ignore[attr-defined]
    return service


def test_discover_identity_partner():
    service = _make_service_with_get([
        {
            "id": "p-uuid",
            "idType": "partner",
            "apiHosts": {"global": "https://api.central.sophos.com"},
        }
    ])
    identity = service.discover_identity("AT")
    assert identity == {
        "id": "p-uuid",
        "id_type": "partner",
        "api_hosts": {"global": "https://api.central.sophos.com"},
    }


def test_discover_identity_organization():
    service = _make_service_with_get([
        {"id": "o-uuid", "idType": "organization", "apiHosts": {"global": "g"}}
    ])
    identity = service.discover_identity("AT")
    assert identity["id_type"] == "organization"


def test_discover_identity_tenant_includes_data_region():
    service = _make_service_with_get([
        {
            "id": "t-uuid",
            "idType": "tenant",
            "apiHosts": {
                "global": "https://api.central.sophos.com",
                "dataRegion": "https://api-eu03.central.sophos.com",
            },
        }
    ])
    identity = service.discover_identity("AT")
    assert identity["api_hosts"]["dataRegion"] == "https://api-eu03.central.sophos.com"


# ── list_partner_tenants pagination ──────────────────────────────────


def _paged_response(items: list[dict], total: int) -> Dict[str, Any]:
    return {"items": items, "pages": {"total": total, "current": 1}}


def test_list_partner_tenants_iterates_all_pages():
    service = SophosAuthService("cid", "csec")
    page1 = _paged_response(
        items=[{"id": "t1", "name": "T1", "dataRegion": "us02", "apiHost": "h1", "dataGeography": "US"}],
        total=3,
    )
    page2 = _paged_response(items=[{"id": "t2", "name": "T2"}], total=3)
    page3 = _paged_response(items=[{"id": "t3", "name": "T3"}], total=3)
    responses = []
    for body in (page1, page2, page3):
        r = MagicMock(spec=httpx.Response)
        r.json.return_value = body
        r.raise_for_status = MagicMock()
        responses.append(r)
    service._http.get = MagicMock(side_effect=responses)  # type: ignore[attr-defined]

    tenants = list(service.list_partner_tenants("AT", "partner-uuid"))
    assert [t["id"] for t in tenants] == ["t1", "t2", "t3"]
    # First call must include pageTotal=true; subsequent calls don't.
    first_call_kwargs = service._http.get.call_args_list[0].kwargs  # type: ignore[attr-defined]
    assert first_call_kwargs["params"]["pageTotal"] == "true"
    second_call_kwargs = service._http.get.call_args_list[1].kwargs  # type: ignore[attr-defined]
    assert "pageTotal" not in second_call_kwargs["params"]


def test_list_partner_tenants_uses_x_partner_id_header():
    service = SophosAuthService("cid", "csec")
    body = _paged_response(items=[{"id": "t1", "name": "T1"}], total=1)
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = body
    resp.raise_for_status = MagicMock()
    service._http.get = MagicMock(return_value=resp)  # type: ignore[attr-defined]

    list(service.list_partner_tenants("AT", "partner-uuid"))
    headers = service._http.get.call_args.kwargs["headers"]  # type: ignore[attr-defined]
    assert headers["X-Partner-ID"] == "partner-uuid"
    assert headers["Authorization"] == "Bearer AT"


def test_list_partner_tenants_empty():
    service = SophosAuthService("cid", "csec")
    body = _paged_response(items=[], total=1)
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = body
    resp.raise_for_status = MagicMock()
    service._http.get = MagicMock(return_value=resp)  # type: ignore[attr-defined]
    assert list(service.list_partner_tenants("AT", "p-uuid")) == []


def test_list_partner_tenants_requires_partner_id():
    service = SophosAuthService("cid", "csec")
    with pytest.raises(ValueError):
        list(service.list_partner_tenants("AT", ""))


def test_list_organization_tenants_uses_x_organization_id_header():
    service = SophosAuthService("cid", "csec")
    body = _paged_response(items=[{"id": "t1", "name": "T1"}], total=1)
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = body
    resp.raise_for_status = MagicMock()
    service._http.get = MagicMock(return_value=resp)  # type: ignore[attr-defined]

    list(service.list_organization_tenants("AT", "org-uuid"))
    headers = service._http.get.call_args.kwargs["headers"]  # type: ignore[attr-defined]
    assert headers["X-Organization-ID"] == "org-uuid"


# ── discover_tenants dispatcher ──────────────────────────────────────


def test_discover_tenants_partner_path():
    service = SophosAuthService("cid", "csec")
    body = _paged_response(items=[{"id": "t1", "name": "A"}, {"id": "t2", "name": "B"}], total=1)
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = body
    resp.raise_for_status = MagicMock()
    service._http.get = MagicMock(return_value=resp)  # type: ignore[attr-defined]

    identity = {"id": "p-uuid", "id_type": "partner", "api_hosts": {}}
    tenants = list(service.discover_tenants("AT", identity))
    assert len(tenants) == 2


def test_discover_tenants_tenant_returns_self():
    service = SophosAuthService("cid", "csec")
    identity = {
        "id": "t-uuid",
        "id_type": "tenant",
        "api_hosts": {"dataRegion": "https://api-eu03.central.sophos.com"},
    }
    tenants = list(service.discover_tenants("AT", identity))
    assert len(tenants) == 1
    assert tenants[0]["id"] == "t-uuid"
    assert tenants[0]["dataRegion"] == "eu03"


def test_discover_tenants_unknown_id_type_raises():
    service = SophosAuthService("cid", "csec")
    with pytest.raises(ValueError):
        list(service.discover_tenants("AT", {"id": "x", "id_type": "mystery", "api_hosts": {}}))
