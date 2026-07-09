"""Mock interno de aiohttp para testes de coletores — substituto do ``aioresponses``.

Por que existe: o ``aioresponses`` foi abandonado em 0.7.8 e é incompatível com
``aiohttp >= 3.14`` (onde ``ClientResponse.stream_writer`` virou obrigatório),
o que prendia o projeto em ``aiohttp 3.13.5`` — versão com 11 CVEs conhecidas
(CVE-2026-*). Em vez de depender de outra lib de mock que pode morrer do mesmo
jeito, mantemos aqui um mock minúsculo e sob nosso controle, à prova do churn do
aiohttp.

API: espelha o subconjunto do ``aioresponses`` que os testes usam, de forma que a
migração seja apenas troca do import::

    from ._aiohttp_mock import aioresponses

    with aioresponses() as m:
        m.get(url_or_regex, payload={...}, status=200, headers={...})
        m.post(url_or_regex, payload={...})

Faz monkeypatch de ``aiohttp.ClientSession._request`` e devolve um response fake
leve (controle total → o ``stream_writer`` obrigatório do 3.14 é irrelevante).
Mocks casam por string de URL (igualdade de ``yarl.URL``, insensível à ordem de
query) ou por ``re.Pattern`` (via ``search``), e são consumidos na ORDEM de
registro (cada um uma vez, salvo ``repeat=True``).
"""

from __future__ import annotations

import json as _json
import re
from collections import namedtuple
from typing import Any, Dict, List, Optional, Pattern, Tuple, Union

import aiohttp
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

_Matcher = Union[str, Pattern[str]]

# Espelha aioresponses.RequestCall — os testes leem ``call.kwargs["params"]``.
RequestCall = namedtuple("RequestCall", ["args", "kwargs"])


class _MockResponse:
    """Response fake com a API que os coletores realmente consomem."""

    def __init__(
        self,
        *,
        method: str,
        url: URL,
        status: int = 200,
        payload: Any = None,
        body: Optional[Union[str, bytes]] = None,
        headers: Optional[Dict[str, str]] = None,
        reason: Optional[str] = None,
    ) -> None:
        self.method = method.upper()
        self._url = url
        self.status = status
        self._payload = payload
        self._body = body
        self.reason = reason or ("OK" if status < 400 else "Error")
        _h: CIMultiDict = CIMultiDict(headers or {})
        self.headers: CIMultiDictProxy = CIMultiDictProxy(_h)
        self.content_type = _h.get("Content-Type", "application/json")
        self.closed = False

    @property
    def url(self) -> URL:
        return self._url

    def _text(self) -> str:
        if self._body is not None:
            return self._body if isinstance(self._body, str) else self._body.decode()
        if self._payload is not None:
            return _json.dumps(self._payload)
        return ""

    async def json(self, *args: Any, **kwargs: Any) -> Any:
        if self._payload is not None:
            return self._payload
        return _json.loads(self._text() or "null")

    async def text(self, *args: Any, **kwargs: Any) -> str:
        return self._text()

    async def read(self) -> bytes:
        if isinstance(self._body, (bytes, bytearray)):
            return bytes(self._body)
        return self._text().encode()

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                request_info=aiohttp.RequestInfo(
                    self._url, self.method, self.headers, self._url
                ),
                history=(),
                status=self.status,
                message=self.reason,
                headers=self.headers,
            )

    def release(self) -> None:
        self.closed = True

    def close(self) -> None:
        self.closed = True

    async def __aenter__(self) -> "_MockResponse":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        self.release()
        return False


class _Registration:
    __slots__ = ("method", "matcher", "spec", "repeat", "remaining")

    def __init__(self, method: str, matcher: _Matcher, spec: Dict[str, Any], repeat: bool) -> None:
        self.method = method.upper()
        self.matcher = matcher
        self.spec = spec
        self.repeat = repeat
        self.remaining = 10**9 if repeat else 1

    def matches(self, method: str, url: URL) -> bool:
        if self.method != method.upper() or self.remaining <= 0:
            return False
        if isinstance(self.matcher, re.Pattern):
            return self.matcher.search(str(url)) is not None
        return URL(self.matcher) == url


class AiohttpMock:
    """Context manager que mocka ``aiohttp.ClientSession._request``."""

    def __init__(self) -> None:
        self._registrations: List[_Registration] = []
        self._original = None
        # Espelha aioresponses.requests: {(method, URL): [RequestCall, ...]}.
        self.requests: Dict[Tuple[str, URL], List[RequestCall]] = {}

    # ── registro de respostas (espelha aioresponses) ──
    def _add(self, method: str, url: _Matcher, *, repeat: bool = False, **spec: Any) -> None:
        self._registrations.append(_Registration(method, url, spec, repeat))

    def get(self, url: _Matcher, **kwargs: Any) -> None:
        self._add("GET", url, **kwargs)

    def post(self, url: _Matcher, **kwargs: Any) -> None:
        self._add("POST", url, **kwargs)

    def put(self, url: _Matcher, **kwargs: Any) -> None:
        self._add("PUT", url, **kwargs)

    def delete(self, url: _Matcher, **kwargs: Any) -> None:
        self._add("DELETE", url, **kwargs)

    def patch(self, url: _Matcher, **kwargs: Any) -> None:
        self._add("PATCH", url, **kwargs)

    # ── patch / unpatch ──
    def __enter__(self) -> "AiohttpMock":
        self._original = aiohttp.ClientSession._request
        mock = self

        async def _mock_request(self_session, method, str_or_url, **kwargs):  # noqa: ANN001
            url = URL(str_or_url)
            params = kwargs.get("params")
            if params:
                url = url.update_query(params)
            mock.requests.setdefault((method.upper(), url), []).append(
                RequestCall(args=(method, str_or_url), kwargs=dict(kwargs))
            )
            for reg in mock._registrations:
                if reg.matches(method, url):
                    if not reg.repeat:
                        reg.remaining -= 1
                    spec = reg.spec
                    if spec.get("exception") is not None:
                        raise spec["exception"]
                    return _MockResponse(
                        method=method,
                        url=url,
                        status=spec.get("status", 200),
                        payload=spec.get("payload"),
                        body=spec.get("body"),
                        headers=spec.get("headers"),
                        reason=spec.get("reason"),
                    )
            raise aiohttp.ClientConnectionError(
                f"AiohttpMock: nenhuma resposta registrada para {method} {url}"
            )

        aiohttp.ClientSession._request = _mock_request  # type: ignore[assignment]
        return self

    def __exit__(self, *exc: Any) -> bool:
        if self._original is not None:
            aiohttp.ClientSession._request = self._original  # type: ignore[assignment]
        return False


# Alias para migração de baixo atrito (call-sites idênticos ao aioresponses).
aioresponses = AiohttpMock
