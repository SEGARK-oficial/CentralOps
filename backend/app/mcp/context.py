"""Principal da chamada MCP em curso — o analista dono da chave.

Montado pelo gateway (``/api/mcp``) depois de autenticar a PAT e conferir
``mcp.use``; fica num ``ContextVar`` durante a execução da ferramenta para
que peças sem acesso ao request (o cache de ack tokens, a auditoria) saibam
em nome de quem estão agindo.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class McpPrincipal:
    user_id: int
    username: str
    role: str
    token_id: int
    organization_id: int | None
    is_global: bool
    client_ip: str | None
    user_agent: str | None
    correlation_id: str

    @property
    def actor_key(self) -> str:
        """Chave estável do ator para estado por-analista (ack tokens).

        O id de usuário serve tanto para pessoas (positivo) quanto para o shim
        de service account (negativo, ver ``core.auth``): ambos são únicos.
        """
        return f"u{self.user_id}"


current_principal: ContextVar[McpPrincipal | None] = ContextVar(
    "centralops_mcp_principal", default=None
)


def require_principal() -> McpPrincipal:
    principal = current_principal.get()
    if principal is None:  # pragma: no cover — invariante do gateway
        raise RuntimeError("MCP tool executed outside of an authenticated call")
    return principal


__all__ = ["McpPrincipal", "current_principal", "require_principal"]
