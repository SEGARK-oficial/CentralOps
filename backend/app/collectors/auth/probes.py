"""Probes de conexão STATELESS (creds cruas) — teste pré-save do catálogo.

Diferente dos *refreshers* (que leem credenciais do DB por ``integration_id``),
aqui o usuário ainda NÃO salvou a integração: validamos as credenciais que ele
acabou de digitar na tela de Nova Integração. Cada vendor expõe uma
``async fn(config: dict) -> TestResult`` referenciada em
``PlatformRegistration.test_fn`` (self-describing — adicionar um vendor traz o
próprio teste, sem tocar o router).

Reusa ``TestResult`` (o MESMO tipo dos destinos) para consistência de contrato.
Nunca vaza o secret na mensagem de erro.
"""

from __future__ import annotations

import time
from typing import Any, Dict

import aiohttp

from ..output.base import TestResult

# URLs de token OAuth (client_credentials).
_GRAPH_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
_GRAPH_SCOPE = "https://graph.microsoft.com/.default"
_NINJA_TOKEN_URL = "https://app.ninjarmm.com/ws/oauth/token"
_SOPHOS_TOKEN_URL = "https://id.sophos.com/api/v2/oauth2/token"

_TIMEOUT = aiohttp.ClientTimeout(total=20)


async def oauth_client_credentials_probe(
    token_url: str, client_id: str, client_secret: str, scope: str
) -> TestResult:
    """Tenta obter um token via ``grant_type=client_credentials``. 200 + token →
    credenciais válidas. Best-effort, nunca levanta; mensagem sem o secret."""
    if not client_id or not client_secret:
        return TestResult.failed("Informe client_id e client_secret.")
    t0 = time.perf_counter()
    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": scope,
    }
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(token_url, data=data) as r:
                body = await r.text()
                ms = (time.perf_counter() - t0) * 1000.0
                if r.status == 200 and "access_token" in body:
                    return TestResult.passed("Autenticação OK — credenciais válidas.", latency_ms=ms)
                # HTTP 400/401 = creds inválidas; outros = problema de conexão/endpoint.
                hint = "Verifique client_id/secret." if r.status in (400, 401) else "Verifique o endpoint/rede."
                return TestResult.failed(f"Falha de autenticação (HTTP {r.status}). {hint}")
    except Exception as exc:  # rede/DNS/timeout
        return TestResult.failed(f"Não foi possível conectar: {exc}")


# ── Probes por vendor (referenciados em PlatformRegistration.test_fn) ──────────


async def sophos_probe(cfg: Dict[str, Any]) -> TestResult:
    return await oauth_client_credentials_probe(
        _SOPHOS_TOKEN_URL, cfg.get("client_id", ""), cfg.get("client_secret", ""), "token"
    )


async def defender_probe(cfg: Dict[str, Any]) -> TestResult:
    tenant = (cfg.get("tenant_id") or "").strip()
    if not tenant:
        return TestResult.failed("Informe o Tenant ID (Azure AD).")
    return await oauth_client_credentials_probe(
        _GRAPH_TOKEN_URL.format(tenant=tenant),
        cfg.get("client_id", ""),
        cfg.get("client_secret", ""),
        _GRAPH_SCOPE,
    )


async def ninjaone_probe(cfg: Dict[str, Any]) -> TestResult:
    base = (cfg.get("base_url") or "https://app.ninjarmm.com").rstrip("/")
    return await oauth_client_credentials_probe(
        f"{base}/ws/oauth/token",
        cfg.get("client_id", ""),
        cfg.get("client_secret", ""),
        "monitoring management",
    )
