"""Pai de AWS Organization: lista as contas e diz como montar cada integração filha.

Um pai (``kind=organization``) não coleta nada — o scheduler o pula. Ele existe
para:

1. **Descobrir as contas** com ``organizations:ListAccounts``, a partir de um papel
   na conta de gerenciamento ou numa conta administradora delegada.
2. **Dizer como cada filha nasce** (``child_spec``). É a única regra por fonte, e
   mora aqui (Core, aberto) para o EE só orquestrar — aprovação, licença, criação
   e desativação por drift — sem conhecer AWS:

   - **CloudTrail** (trail de Organization): os logs de TODAS as contas estão num
     bucket central, legível pelo papel do PAI. A filha reusa o papel do pai e só
     troca o Account ID (``AWSLogs/o-…/<conta>/``, descoberto pelo coletor).
   - **GuardDuty / Security Hub**: cada conta responde pelos próprios achados. A
     filha assume o papel de MESMO NOME (e path) na conta membro — é o que o
     StackSet do template cria em todas as contas.

   As filhas ficam na MESMA organização do CentralOps que o pai: contas de uma AWS
   Organization são do mesmo cliente, e é isso que permite um External ID único
   (o da organização) servir para o papel em todas elas.

Credencial do pai: papel com External ID (``collectors/auth/aws.py``). Chave fixa
NÃO é aceita aqui: as filhas não teriam segredo próprio.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import re
from typing import Any, Dict, List, Optional

from ..base import BaseProvider, HealthResult

logger = logging.getLogger(__name__)

#: plataforma do pai → (plataforma da filha, estratégia de credencial)
ORG_PLATFORMS: Dict[str, tuple] = {
    "aws_cloudtrail_org": ("aws_cloudtrail", "parent_role"),
    "aws_guardduty_org": ("aws_guardduty", "member_role"),
    "aws_securityhub_org": ("aws_securityhub", "member_role"),
}

_ROLE_ARN_PARTS = re.compile(r"^arn:(aws[a-z-]*):iam::(\d{12}):role/(.+)$")


def _run(coro):
    """Executa uma corrotina a partir de código síncrono (routers sync, Celery).

    Com um loop já rodando NESTA thread, ``asyncio.run`` levantaria; nesse caso a
    corrotina roda numa thread própria."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def member_role_arn(parent_role_arn: str, account_id: str) -> str:
    """O papel de mesmo nome e path na conta membro."""
    m = _ROLE_ARN_PARTS.match((parent_role_arn or "").strip())
    if not m:
        raise ValueError("o pai precisa de um ARN de papel (arn:aws:iam::<conta>:role/<nome>)")
    return f"arn:{m.group(1)}:iam::{account_id}:role/{m.group(3)}"


class AwsOrganizationProvider(BaseProvider):
    platform = "aws_organization"

    # ── capacidades ────────────────────────────────────────────────────

    def capabilities(self) -> List[str]:
        return ["discover:children", "partner:sync_tenants", "auth:test"]

    # ── AWS ────────────────────────────────────────────────────────────

    def _auth(self):
        from ...collectors.auth import aws as aws_auth
        from ...db import database, models

        with database.SessionLocal() as db:
            integ = db.get(models.Integration, self.integration.id)
            if integ is None:
                raise RuntimeError(f"integração {self.integration.id} não encontrada")
            return aws_auth.auth_for_integration(db, integ)

    def _client(self, client_kwargs: Dict[str, str]):
        """Seam mockável: client ``organizations`` (aioboto3, import tardio)."""
        import aioboto3  # noqa: PLC0415

        return aioboto3.Session().client("organizations", **client_kwargs)

    async def _list_accounts(self, max_pages: Optional[int] = None) -> List[Dict[str, Any]]:
        from ...collectors.auth import aws as aws_auth

        auth = await asyncio.to_thread(self._auth)
        kwargs = await aws_auth.client_kwargs(auth, "us-east-1")
        out: List[Dict[str, Any]] = []
        token: Optional[str] = None
        pages = 0
        async with self._client(kwargs) as org:
            while True:
                params: Dict[str, Any] = {"MaxResults": 20}
                if token:
                    params["NextToken"] = token
                resp = await org.list_accounts(**params)
                out.extend(resp.get("Accounts") or [])
                token = resp.get("NextToken")
                pages += 1
                if not token or (max_pages is not None and pages >= max_pages):
                    return out

    def discover_tenants(self) -> List[Dict[str, Any]]:
        """Contas ATIVAS da Organization, no formato que o sync de filhas consome
        (``id`` + ``name``). Conta suspensa ou saindo da org não vira filha — e,
        se já era, o drift do sync a desativa."""
        accounts = _run(self._list_accounts())
        out: List[Dict[str, Any]] = []
        for acc in accounts:
            state = str(acc.get("State") or acc.get("Status") or "").upper()
            if state and state != "ACTIVE":
                continue
            acct_id = str(acc.get("Id") or "").strip()
            if not acct_id:
                continue
            out.append({"id": acct_id, "name": acc.get("Name") or acct_id, "email": acc.get("Email")})
        return out

    def child_spec(self, account_id: str, name: str) -> Dict[str, Any]:
        """Campos da integração filha para uma conta (o EE só aplica)."""
        child_platform, strategy = ORG_PLATFORMS[self.integration.platform]
        parent_role = (self.integration.client_id or "").strip()
        # Valida o ARN mesmo quando a filha reusa o papel do pai: o modo
        # Organization não aceita chave fixa (não há segredo para copiar às filhas).
        member = member_role_arn(parent_role, account_id)
        client_id = parent_role if strategy == "parent_role" else member
        spec: Dict[str, Any] = {
            "platform": child_platform,
            "name": f"{name} ({account_id})",
            "client_id": client_id,
            "tenant_id": account_id,
            # Todas as regiões: o coletor descobre as do bucket (CloudTrail) ou as
            # habilitadas na conta (achados). Ajustável depois na filha.
            "region": "*",
            "base_url": (self.integration.base_url or None) if strategy == "parent_role" else None,
        }
        return spec

    # ── saúde ──────────────────────────────────────────────────────────

    def test_connection(self) -> HealthResult:
        from ...collectors.auth import aws as aws_auth

        try:
            accounts = _run(self._list_accounts(max_pages=1))
        except Exception as exc:  # noqa: BLE001
            return HealthResult(
                status="error",
                details={"error": aws_auth.describe_error(exc, operation="ListAccounts")},
            )
        return HealthResult(status="healthy", details={"accounts_sample": len(accounts)})

    def health_check(self) -> HealthResult:
        return self.test_connection()

    def on_created(self) -> None:
        """Dispara a descoberta assíncrona das contas (EE). Na Community o pai é
        criado mas nenhuma filha nasce — o router registra o motivo."""
        from ...core import ee_hooks

        dispatch = ee_hooks.get_partner_sync_dispatcher()
        if dispatch is None:
            logger.info(
                "AWS Organization: criação de filhas é Enterprise; nada a disparar "
                "para integration_id=%s (Community)", self.integration.id,
            )
            return
        dispatch(self.integration.id)

    def close(self) -> None:
        return None
