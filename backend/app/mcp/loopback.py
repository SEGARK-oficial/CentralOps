"""Client HTTP que as ferramentas MCP usam para falar com a API — in-process.

Por que loopback e não chamada direta ao service layer
-------------------------------------------------------
Os routers REST são a fronteira de autorização do CentralOps: é neles que
vivem ``require_permission``, o escopo de organização (``core.tenant``), os
tetos de página e a trilha de auditoria. Reimplementar 57 ferramentas por
cima dos services duplicaria cada uma dessas checagens — e é exatamente a
duplicação que, um dia, concede pelo MCP algo que o REST negaria.

Então cada ferramenta chama o REST. Só que sem socket: ``httpx.ASGITransport``
entrega a requisição ao próprio app FastAPI, no mesmo processo, passando por
todos os middlewares (correlação, locale, auditoria) e dependências. A
autenticação da chamada interna é um **nonce de uso único** que
``core.auth`` reconhece apenas dentro do contexto da chamada da ferramenta
(ver ``core.auth.inprocess_principal``): o principal é o mesmo que já foi
autenticado na borda ``/api/mcp`` (argon2 + rate limit + scopes), sem
re-verificar o hash nem consumir o orçamento de rate limit uma segunda vez.

Em testes o transporte é injetável (``httpx.MockTransport``), o que mantém
os testes de contrato das ferramentas idênticos aos do servidor externo.
"""

from __future__ import annotations

from typing import Any, Mapping

import httpx

#: ``base_url`` sintética do loopback. O host não resolve em lugar nenhum de
#: propósito: nunca pode virar uma chamada de rede.
LOOPBACK_BASE_URL = "http://centralops.loopback/api"

#: Teto de tempo de UMA chamada REST interna. O ``wait_for_backfill_job``
#: faz várias em sequência dentro do próprio teto dele.
LOOPBACK_TIMEOUT_S = 60.0


class CentralOpsAPIError(RuntimeError):
    def __init__(self, status_code: int, message: str, body: Any = None):
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code
        self.body = body


class LoopbackClient:
    """``get``/``post`` sobre um ``httpx.AsyncClient`` com transporte injetado."""

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport,
        headers: Mapping[str, str],
        base_url: str = LOOPBACK_BASE_URL,
        timeout: float = LOOPBACK_TIMEOUT_S,
    ):
        self._transport = transport
        self._headers = dict(headers)
        self._headers.setdefault("Accept", "application/json")
        self._base_url = base_url
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "LoopbackClient":
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            headers=self._headers,
            transport=self._transport,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        return await self._request("GET", path, params=params)

    async def post(
        self,
        path: str,
        json: Any | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        return await self._request("POST", path, json=json, params=params)

    async def _request(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        json: Any | None = None,
    ) -> Any:
        assert self._client is not None, "Use 'async with' to manage the client lifecycle"
        cleaned_params = (
            {k: v for k, v in params.items() if v is not None} if params else None
        )
        response = await self._client.request(
            method, path, params=cleaned_params, json=json
        )
        if response.status_code >= 400:
            self._raise_for_status(response)
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        body: Any
        try:
            body = response.json()
        except ValueError:
            body = response.text

        message = "request failed"
        if isinstance(body, dict):
            detail = body.get("detail")
            if isinstance(detail, str):
                message = detail
            elif isinstance(detail, dict) and "message" in detail:
                message = str(detail["message"])
        elif isinstance(body, str) and body:
            message = body[:500]

        if response.status_code == 401:
            # No loopback um 401 significa que a credencial do analista deixou
            # de valer ENTRE a borda e a ferramenta (revogada/expirada agora).
            message = (
                "The analyst API key was rejected mid-call: it may have been "
                "revoked or expired. Generate a new key at <host>/settings/tokens "
                "and update the MCP client configuration."
            )
        elif response.status_code == 403:
            message = (
                f"{message}\n"
                "Hint: the MCP server grants nothing beyond the analyst's own REST "
                "permissions. Either the role lacks this permission or the API key "
                "was issued with restricted scopes that exclude it."
            )
        raise CentralOpsAPIError(response.status_code, message, body=body)


__all__ = ["LoopbackClient", "CentralOpsAPIError", "LOOPBACK_BASE_URL", "LOOPBACK_TIMEOUT_S"]
