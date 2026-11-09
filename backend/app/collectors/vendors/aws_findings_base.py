"""Motor comum dos coletores de ACHADOS da AWS (Security Hub, GuardDuty).

Os dois serviços são regionais e respondem "o que mudou entre A e B" por
``UpdatedAt``. Um achado é atualizado ao longo da vida (GuardDuty soma ocorrências,
Security Hub muda workflow/arquivamento), e cada atualização é um evento novo para
o SOC — por isso o ID de dedupe é ``Id|UpdatedAt``, não só ``Id``.

**Cursor por região**, com JANELA FECHADA no ciclo (paginar com ``End`` móvel daria
páginas inconsistentes)::

    {"regions": {"us-east-1": {"updated_at": "...",            # watermark
                               "window_start": "...",          # só no teto
                               "window_end": "...",            # só no teto
                               "next_token": "..."}}}          # só no teto

**Sobreposição.** O índice dos dois serviços é eventualmente consistente: um achado
com ``UpdatedAt`` de 10:00 pode aparecer na consulta só às 10:05. A janela começa
``_OVERLAP`` antes do watermark e, ao drenar, o watermark vai para
``max(maior UpdatedAt visto, fim da janela − _LATENESS)``. Sem isso (o defeito que o
Entra/Defender tinham), achado que aparece atrasado nunca é lido. O que for relido
morre no dedupe.

**Teto.** ``_MAX_PAGES_PER_CYCLE`` páginas por ciclo, somando todas as regiões. No
teto, a região corrente grava a posição exata (janela + token) e as que faltam
seguem do watermark delas no próximo ciclo.

**Regiões.** Lista explícita ou ``*`` (``ec2:DescribeRegions`` — as habilitadas na
conta).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from ..auth import aws as aws_auth
from ..base import BaseCollector
from ..metrics import API_LATENCY
from .aws_cloudtrail import parse_regions

logger = logging.getLogger(__name__)

_MAX_PAGES_PER_CYCLE = 50
_OVERLAP = timedelta(minutes=15)
_LATENESS = timedelta(minutes=15)
_DEFAULT_LOOKBACK = timedelta(hours=24)


def parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class AwsRegionalFindingsCollector(BaseCollector):
    """Base: subclasses implementam ``_service``, ``_fetch_page`` e o ID."""

    stream = "findings"
    #: Nome do serviço no boto (``securityhub`` | ``guardduty``).
    _service = ""
    _label = "findings"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._region: Optional[str] = None

    @property
    def domain(self) -> str:
        return f"{self._service}.{self._region or 'us-east-1'}.amazonaws.com"

    # ── configuração ────────────────────────────────────────────────────

    def _load_conn(self) -> Dict[str, Any]:
        from ...db import database, models

        with database.SessionLocal() as db:
            integ = db.get(models.Integration, self.ctx.integration_id)
            if integ is None:
                raise RuntimeError(f"{self._label}: integração {self.ctx.integration_id} não encontrada")
            try:
                auth = aws_auth.auth_for_integration(db, integ)
            except aws_auth.AwsAuthError as exc:
                raise RuntimeError(
                    f"{self._label}: integração {self.ctx.integration_id} incompleta — {exc}"
                ) from None
            return {"auth": auth, "regions": parse_regions(integ.region, default=("us-east-1",))}

    def _client(self, service: str, client_kwargs: Dict[str, str]):
        """Seam mockável: client aioboto3 (import tardio)."""
        import aioboto3  # noqa: PLC0415

        return aioboto3.Session().client(service, **client_kwargs)

    async def _resolve_regions(self, conn: Dict[str, Any]) -> List[str]:
        if conn["regions"] != ["*"]:
            return list(conn["regions"])
        kwargs = await aws_auth.client_kwargs(conn["auth"], "us-east-1")
        async with self._client("ec2", kwargs) as ec2:
            resp = await ec2.describe_regions(AllRegions=False)
        return sorted(r["RegionName"] for r in resp.get("Regions") or [] if r.get("RegionName"))

    # ── contrato das subclasses ─────────────────────────────────────────

    async def _fetch_page(
        self, client, region: str, start: datetime, end: datetime, token: Optional[str]
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:  # pragma: no cover — abstrato
        raise NotImplementedError

    def _updated_at(self, item: Dict[str, Any]) -> Optional[datetime]:
        return parse_ts(item.get("UpdatedAt"))

    def extract_message_id(self, event: Dict[str, Any]) -> str:
        fid = event.get("Id")
        return f"{fid}|{event.get('UpdatedAt', '')}" if fid else ""

    # ── coleta ──────────────────────────────────────────────────────────

    async def collect(self) -> AsyncIterator[Dict[str, Any]]:
        conn = await asyncio.to_thread(self._load_conn)
        regions = await self._resolve_regions(conn)
        cursor = self.ctx.cursor or {}
        states: Dict[str, Dict[str, Any]] = {
            r: dict(s) for r, s in (cursor.get("regions") or {}).items() if isinstance(s, dict)
        }
        now = datetime.now(timezone.utc)
        pages = 0

        for region in regions:
            self._region = region
            state = states.get(region, {})
            watermark = parse_ts(state.get("updated_at")) or (now - _DEFAULT_LOOKBACK)
            token = state.get("next_token") or None
            start = parse_ts(state.get("window_start")) if token else None
            end = parse_ts(state.get("window_end")) if token else None
            if not (token and start and end):
                token = None
                start, end = watermark - _OVERLAP, now
            newest = watermark

            kwargs = await aws_auth.client_kwargs(conn["auth"], region)
            async with self._client(self._service, kwargs) as client:
                while True:
                    await self.ctx.rate_limiter.acquire(self.ctx.integration_id, self.platform)
                    started = time.monotonic()
                    async with self.ctx.domain_limiter.slot(self.domain):
                        items, token = await self._fetch_page(client, region, start, end, token)
                    API_LATENCY.labels(vendor=self.platform, stream=self.stream).observe(
                        time.monotonic() - started
                    )
                    for item in items:
                        ts = self._updated_at(item)
                        if ts is not None and ts > newest:
                            newest = ts
                        yield item
                    pages += 1
                    if not token:
                        break
                    if self.ctx.bounded_per_cycle and pages >= _MAX_PAGES_PER_CYCLE:
                        # Teto: posição exata desta região; o watermark NÃO avança
                        # (avançar pularia as páginas ainda não lidas da janela).
                        states[region] = {
                            "updated_at": iso(watermark),
                            "window_start": iso(start),
                            "window_end": iso(end),
                            "next_token": token,
                        }
                        self.ctx.cursor = {"regions": states}
                        self.mark_cycle_capped()
                        logger.info(
                            "%s: teto de %d páginas/ciclo em %s (integration=%s)",
                            self._label, _MAX_PAGES_PER_CYCLE, region, self.ctx.integration_id,
                        )
                        return

            # Janela drenada: o watermark vai para o mais novo visto ou para o fim
            # da janela menos a tolerância de indexação — o que for maior.
            states[region] = {"updated_at": iso(max(newest, end - _LATENESS))}

        self.ctx.cursor = {"regions": states}

    @classmethod
    def watermark_at(cls, cursor: Optional[Dict[str, Any]]) -> Optional[datetime]:
        """A região MAIS ATRASADA — é ela que diz se o coletor acompanha."""
        marks = [
            parse_ts(s.get("updated_at"))
            for s in ((cursor or {}).get("regions") or {}).values()
            if isinstance(s, dict)
        ]
        marks = [m for m in marks if m is not None]
        # UTC naive, como o resto da frota (a Saúde subtrai de ``utcnow()``).
        return min(marks).astimezone(timezone.utc).replace(tzinfo=None) if marks else None
