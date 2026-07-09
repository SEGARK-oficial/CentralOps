"""Testes para validação de X-Forwarded-For via TRUSTED_PROXIES_CIDRS.

Garante que ``get_client_ip`` aceita XFF somente quando o request veio
diretamente de um proxy confiável, prevenindo bypass de lockout via IP forjado.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from backend.app.services.audit import _ip_in_cidrs, get_client_ip


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_request(
    *,
    client_host: str | None,
    xff: str | None = None,
    x_real_ip: str | None = None,
) -> MagicMock:
    """Constrói um mock de ``fastapi.Request`` com os atributos necessários."""
    mock_request = MagicMock()

    if client_host is None:
        mock_request.client = None
    else:
        mock_request.client = MagicMock()
        mock_request.client.host = client_host

    headers: dict[str, str] = {}
    if xff is not None:
        headers["X-Forwarded-For"] = xff
    if x_real_ip is not None:
        headers["x-real-ip"] = x_real_ip

    mock_request.headers.get = lambda key, default="": headers.get(key, default)
    return mock_request


# ---------------------------------------------------------------------------
# Testes para get_client_ip
# ---------------------------------------------------------------------------

class TestGetClientIp:

    def test_ignores_xff_when_no_trusted_proxy_configured(self) -> None:
        """Sem TRUSTED_PROXIES_CIDRS: XFF presente deve ser ignorado, retorna direct."""
        request = _make_request(client_host="8.8.8.8", xff="1.2.3.4")

        with patch("backend.app.services.audit.settings") as mock_settings:
            mock_settings.TRUSTED_PROXIES_CIDRS = []
            result = get_client_ip(request)

        assert result == "8.8.8.8", f"Esperado direct IP 8.8.8.8, obtido {result!r}"

    def test_uses_xff_when_request_from_trusted_proxy(self) -> None:
        """Request de proxy confiável: usa primeiro IP do XFF."""
        request = _make_request(client_host="10.0.0.5", xff="1.2.3.4")

        with patch("backend.app.services.audit.settings") as mock_settings:
            mock_settings.TRUSTED_PROXIES_CIDRS = ["10.0.0.0/8"]
            result = get_client_ip(request)

        assert result == "1.2.3.4", f"Esperado XFF first IP 1.2.3.4, obtido {result!r}"

    def test_ignores_xff_when_request_from_untrusted_ip(self) -> None:
        """Request de IP não confiável: ignora XFF, retorna direct."""
        request = _make_request(client_host="8.8.8.8", xff="1.2.3.4")

        with patch("backend.app.services.audit.settings") as mock_settings:
            mock_settings.TRUSTED_PROXIES_CIDRS = ["10.0.0.0/8"]
            result = get_client_ip(request)

        assert result == "8.8.8.8", (
            f"IP externo 8.8.8.8 não é proxy confiável — XFF deve ser ignorado, obtido {result!r}"
        )

    def test_handles_multiple_xff_takes_first(self) -> None:
        """Com múltiplos IPs no XFF, deve usar somente o primeiro."""
        request = _make_request(client_host="172.16.0.1", xff="1.2.3.4, 5.6.7.8, 9.10.11.12")

        with patch("backend.app.services.audit.settings") as mock_settings:
            mock_settings.TRUSTED_PROXIES_CIDRS = ["172.16.0.0/12"]
            result = get_client_ip(request)

        assert result == "1.2.3.4", f"Esperado primeiro IP do XFF, obtido {result!r}"

    def test_handles_no_request_client(self) -> None:
        """Quando ``request.client`` é None, retorna None."""
        request = _make_request(client_host=None, xff="1.2.3.4")

        with patch("backend.app.services.audit.settings") as mock_settings:
            mock_settings.TRUSTED_PROXIES_CIDRS = []
            result = get_client_ip(request)

        assert result is None, f"Esperado None quando client é None, obtido {result!r}"

    def test_handles_no_request_client_with_trusted_proxies(self) -> None:
        """Sem client IP, não é possível validar proxy — retorna None."""
        request = _make_request(client_host=None, xff="1.2.3.4")

        with patch("backend.app.services.audit.settings") as mock_settings:
            mock_settings.TRUSTED_PROXIES_CIDRS = ["10.0.0.0/8"]
            result = get_client_ip(request)

        assert result is None

    def test_uses_direct_when_xff_empty_but_proxy_trusted(self) -> None:
        """Proxy confiável mas sem XFF: retorna direct."""
        request = _make_request(client_host="10.0.0.1", xff="")

        with patch("backend.app.services.audit.settings") as mock_settings:
            mock_settings.TRUSTED_PROXIES_CIDRS = ["10.0.0.0/8"]
            result = get_client_ip(request)

        # XFF vazio → first strip == "" → fallback para direct
        assert result == "10.0.0.1", f"Esperado fallback para direct, obtido {result!r}"

    def test_trusted_proxy_ipv4_cidr(self) -> None:
        """Testa CIDR IPv4 clássico."""
        request = _make_request(client_host="192.168.1.254", xff="203.0.113.5")

        with patch("backend.app.services.audit.settings") as mock_settings:
            mock_settings.TRUSTED_PROXIES_CIDRS = ["192.168.0.0/16"]
            result = get_client_ip(request)

        assert result == "203.0.113.5"

    def test_untrusted_proxy_adjacent_subnet(self) -> None:
        """IP adjacente à sub-rede confiável mas fora dela deve ser rejeitado."""
        request = _make_request(client_host="11.0.0.1", xff="1.2.3.4")

        with patch("backend.app.services.audit.settings") as mock_settings:
            mock_settings.TRUSTED_PROXIES_CIDRS = ["10.0.0.0/8"]
            result = get_client_ip(request)

        # 11.0.0.1 não está em 10.0.0.0/8
        assert result == "11.0.0.1", "IP fora do CIDR não deve ser tratado como proxy confiável"


# ---------------------------------------------------------------------------
# Testes para _ip_in_cidrs
# ---------------------------------------------------------------------------

class TestIpInCidrs:

    def test_ip_in_cidr_returns_true(self) -> None:
        assert _ip_in_cidrs("10.0.0.5", ["10.0.0.0/8"]) is True

    def test_ip_not_in_cidr_returns_false(self) -> None:
        assert _ip_in_cidrs("8.8.8.8", ["10.0.0.0/8"]) is False

    def test_multiple_cidrs_first_match(self) -> None:
        assert _ip_in_cidrs("172.16.5.5", ["10.0.0.0/8", "172.16.0.0/12"]) is True

    def test_empty_cidrs_returns_false(self) -> None:
        assert _ip_in_cidrs("1.2.3.4", []) is False

    def test_handles_invalid_ip(self) -> None:
        """IP inválido não deve causar exceção — retorna False."""
        result = _ip_in_cidrs("not_an_ip", ["10.0.0.0/8"])
        assert result is False

    def test_handles_invalid_cidr(self) -> None:
        """CIDR inválido na lista deve ser ignorado silenciosamente."""
        result = _ip_in_cidrs("10.0.0.1", ["not_a_cidr", "10.0.0.0/8"])
        assert result is True  # segundo CIDR é válido e inclui o IP

    def test_handles_all_invalid_cidrs(self) -> None:
        """Lista de CIDRs todos inválidos → False."""
        result = _ip_in_cidrs("10.0.0.1", ["garbage", "more_garbage"])
        assert result is False

    @pytest.mark.parametrize("ip,cidr,expected", [
        ("127.0.0.1", "127.0.0.0/8", True),
        ("192.168.1.1", "192.168.0.0/16", True),
        ("10.255.255.255", "10.0.0.0/8", True),
        ("11.0.0.0", "10.0.0.0/8", False),
        ("0.0.0.0", "0.0.0.0/0", True),
    ])
    def test_parametric_cidr_matching(self, ip: str, cidr: str, expected: bool) -> None:
        assert _ip_in_cidrs(ip, [cidr]) is expected
