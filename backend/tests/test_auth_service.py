from unittest import mock

from backend.app.services.auth import SophosAuthService


def test_authenticate_success():
    service = SophosAuthService("id", "secret")
    with mock.patch.object(service._http, "post") as mock_post:
        response = mock.Mock()
        response.json.return_value = {"access_token": "a", "refresh_token": "r"}
        response.raise_for_status.return_value = None
        mock_post.return_value = response

        tokens = service.authenticate()

    assert tokens["access_token"] == "a"
    assert tokens["refresh_token"] == "r"


def test_discover_region():
    service = SophosAuthService("id", "secret")
    with mock.patch.object(service._http, "get") as mock_get:
        response = mock.Mock()
        response.json.return_value = {
            "apiHosts": {"dataRegion": "https://api-eu01.central.sophos.com"}
        }
        response.raise_for_status.return_value = None
        mock_get.return_value = response

        region = service.discover_region("t")

    assert region == "eu01"
