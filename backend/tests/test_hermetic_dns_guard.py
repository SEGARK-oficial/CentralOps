"""Guard de DNS hermético da suíte (``backend/conftest.py::_hermetic_reserved_dns``).

Sem ele, ``indexer.example.com`` só falha rápido onde o resolvedor responde
NXDOMAIN. No build da imagem nos runners self-hosted (out/2026) o nome ficava
pendurado até o timeout do provider: o sweep compilado foi a 48 min (job
cancelado no teto de 50) e o teste de rate limit reprovou o release 2.13.0.

Estes testes travam o comportamento pelos três caminhos que a suíte usa de fato
— ``socket``, httpx e aiohttp (``loop.getaddrinfo``) — e garantem que o guard
não engole nome comum.
"""

from __future__ import annotations

import asyncio
import socket
import time

import httpx
import pytest

#: Folga generosa: o guard responde em microssegundos; o defeito leva 30 s.
_FAST_S = 2.0

_RESERVED = (
    "indexer.example.com",
    "manager.example.com",
    "example.com",
    "a.example.net",
    "example.org",
    "INDEXER.EXAMPLE.COM.",  # caixa e ponto final de FQDN
    "wazuh.test",
    "nada.invalid",
    "algo.example",
)


@pytest.mark.parametrize("host", _RESERVED)
def test_reserved_names_fail_immediately_like_nxdomain(host):
    started = time.monotonic()
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo(host, 443)
    assert time.monotonic() - started < _FAST_S


def test_bytes_hostname_is_guarded_too():
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo(b"indexer.example.com", 9200)


def test_httpx_connection_to_a_reserved_host_fails_fast():
    """O caminho do provider Wazuh (httpx síncrono, timeout de 30 s)."""
    started = time.monotonic()
    with pytest.raises(httpx.ConnectError):
        httpx.get("https://indexer.example.com:9200/", timeout=30.0)
    assert time.monotonic() - started < _FAST_S


def test_event_loop_resolution_is_guarded_too():
    """O caminho do aiohttp: ``loop.getaddrinfo`` chama ``socket.getaddrinfo`` em
    thread. Sem ``aiodns`` no lock, é por aqui que todo coletor resolve."""

    async def _resolve():
        return await asyncio.get_running_loop().getaddrinfo("wazuh.example.com", 55000)

    started = time.monotonic()
    with pytest.raises(socket.gaierror):
        asyncio.run(_resolve())
    assert time.monotonic() - started < _FAST_S


@pytest.mark.parametrize("host", ("localhost", "127.0.0.1"))
def test_loopback_still_resolves(host):
    """testcontainers e servidores locais de teste dependem disto. Nenhum dos
    dois passa por DNS externo, então o teste não fica lento num runner com o
    resolvedor pendurado."""
    assert socket.getaddrinfo(host, 80)


@pytest.mark.parametrize(
    ("host", "reserved"),
    (
        ("indexer.example.com", True),
        ("example.com", True),
        ("x.test", True),
        # Parecem reservados mas não são: casa o nome inteiro ou um sufixo de RÓTULO.
        ("notexample.com", False),
        ("example.company", False),
        ("mytest", False),
        ("api.contoso.com", False),
        (None, False),
    ),
)
def test_classifier_matches_whole_labels_only(host, reserved):
    """Testado sem rede: perguntar ao DNS real por ``notexample.com`` reintroduziria
    a espera que este guard existe para eliminar."""
    from backend.conftest import _is_reserved_dns_name

    assert _is_reserved_dns_name(host) is reserved
