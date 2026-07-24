"""Veeam Backup & Replication — collector de SESSÕES de job (backup/replica/restore).

Vendor novo = 1 módulo, ZERO core. Backup é o último bastião de ransomware: sessão
falhada / job desabilitado / restore inesperado são sinais de SOC de primeira ordem
que hoje morrem no console do VBR.

**API oficial (VBR REST API, porta 9419):**

- Token:   ``POST {base}/api/oauth2/token`` — ``grant_type=password`` +
  ``username``/``password`` (form-urlencoded), header ``x-api-version`` OBRIGATÓRIO
  (sem ele a request falha). Resposta: ``access_token`` / ``expires_in`` /
  ``refresh_token``.
- Sessões: ``GET {base}/api/v1/sessions`` com ``Authorization: Bearer <token>`` +
  ``x-api-version``. Filtros/paginação usados aqui: ``createdAfterFilter`` (date-time),
  ``orderColumn=CreationTime``, ``orderAsc=true``, ``skip``, ``limit`` (default 200).
  Envelope: ``{"data": [...], "pagination": {"total","count","skip","limit"}}``.
  Ordem de aplicação da API: filtro/sort → ``skip`` → ``limit``.

Docs: https://helpcenter.veeam.com/docs/backup/vbr_rest/rest_api_reference.html
      https://helpcenter.veeam.com/references/vbr/13/rest/1.3-rev1/tag/Sessions/index.html
      https://helpcenter.veeam.com/docs/backup/vbr_rest/skip.html

**Por que o token é obtido DENTRO do collect() (e o ``refresh_fn`` é no-op):** o VBR
é ON-PREM, por instância — não há IdP compartilhado. O ``base_url`` (``https://
vbr.local:9419``) e, principalmente, a política de TLS (``verify_ssl``: certificado
auto-assinado é o default de fábrica do VBR) são POR INTEGRAÇÃO, e o caminho genérico
de refresh do ``oauth_cache`` não carrega política de TLS por integração — um
refresher "de verdade" ali quebraria em todo VBR com cert auto-assinado. Então este
collector é AUTO-CONTIDO como o ``wazuh_detections.py``: lê creds do store, faz o
password-grant e monta o header ``Bearer`` ele mesmo, honrando ``verify_ssl``. O
``access_token`` do VBR vive ~15min > o ``task_soft_time_limit`` (720s) de um ciclo,
então 1 token por ciclo basta e o ``refresh_token`` é dispensável.

**Creds (reuso GENÉRICO das colunas — zero-core):** ``base_url`` = URL do servidor VBR,
``client_id`` = username (``DOMÍNIO\\usuario`` para conta de domínio), segredo
``password`` no store, ``region`` = valor do header ``x-api-version`` (a rev muda por
versão do VBR: 12.x → ``1.1-rev*``/``1.2-rev*``, 13.x → ``1.3-rev1``), ``verify_ssl``
na coluna homônima (modelo do Wazuh).

**Cursor:** watermark temporal + offset resumível — ``{"created_after": <iso>,
"skip": <int>}``. Como a coleção é ordenada por ``CreationTime`` ASC, o par
(janela, skip) é uma posição estável e persistível. Dedupe por ``id`` da sessão.

**Limitação conhecida:** o filtro é por ``CreationTime``, então uma sessão ainda em
execução é coletada UMA vez (no estado ``Working``) e não é re-emitida ao terminar —
o watermark já passou dela. Aceitável: o valor de SOC está no volume de sessões
concluídas; um stream de "sessões finalizadas" exigiria um segundo cursor por
``endedAfterFilter`` (fora de escopo deste stream).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict, Optional
from urllib.parse import urlparse

from ..base import BaseCollector
from ..metrics import API_LATENCY
from ._rate_limit import VendorRateLimitedError

logger = logging.getLogger(__name__)

_PAGE_SIZE = 200  # ``limit`` default da API de sessões.

# Rev do header ``x-api-version``. Sem o header a request falha (não é opcional).
# Default conservador: ``1.2-rev0`` (VBR 12.2+) — servidores mais novos aceitam revs
# anteriores, o inverso não. Sobrescrevível por integração no campo ``region``.
_DEFAULT_API_VERSION = "1.2-rev0"
_API_VERSIONS = ("1.1-rev0", "1.1-rev1", "1.1-rev2", "1.2-rev0", "1.2-rev1", "1.3-rev1")

# Sobreposição da janela ao FECHAR o ciclo: ``createdAfterFilter`` é EXCLUSIVO, então
# sessões com o mesmo ``creationTime` do watermark seriam perdidas. Recuamos alguns
# segundos e deixamos a dedupe por ``id`` absorver a borda re-buscada.
_WATERMARK_OVERLAP_SECONDS = 2

# Teto de páginas por CICLO Celery (50 × 200 = 10.000 sessões/ciclo). Sem este guard,
# um backlog grande (primeira carga de um VBR com meses de histórico, ou retomada pós-
# downtime) é drenado num ÚNICO run — o while abaixo pagina até exaurir o servidor —
# estourando o ``task_soft_time_limit`` (720s). No soft-timeout o pipeline REVERTE o
# cursor para ``cursor_before`` e solta todas as claims → poison-loop sem progresso
# (o coletor deixa de coletar). Ao atingir o teto gravamos a POSIÇÃO RESUMÍVEL
# (mesma janela ``created_after`` + ``skip`` da PRÓXIMA página) e devolvemos o slot do
# worker. O watermark NÃO avança aqui: avançá-lo zeraria o ``skip`` e descartaria as
# páginas ainda não lidas desta janela (perda de dado). Espelha o guard do
# ``wazuh_detections.py`` / ``sophos_detections.py``.
_MAX_PAGES_PER_CYCLE = 50


class VeeamRateLimitedError(VendorRateLimitedError):
    def __init__(self, retry_after: int) -> None:
        super().__init__(retry_after, vendor="veeam")


class VeeamSessionsCollector(BaseCollector):
    """Pull de sessões do VBR (OAuth2 password grant + skip/limit)."""

    platform = "veeam"
    stream = "sessions"
    event_type = "veeam.session"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._base: Optional[str] = None
        self._host: Optional[str] = None

    @property
    def domain(self) -> str:
        return self._host or "veeam-vbr"

    # ── Conexão (creds do store, colunas genéricas) ────────────────────

    def _load_conn(self) -> Dict[str, Any]:
        """base_url + username (client_id) + password (store) + api_version + TLS."""
        from ...core.url_policy import normalize_service_url
        from ...db import database, models
        from ...services import integration_secrets

        with database.SessionLocal() as db:
            integ = db.get(models.Integration, self.ctx.integration_id)
            if integ is None:
                raise RuntimeError(f"veeam: integração {self.ctx.integration_id} não encontrada")
            base = normalize_service_url(integ.base_url or "")
            if not base:
                raise RuntimeError(
                    f"veeam: integração {self.ctx.integration_id} sem base_url "
                    "(ex.: https://vbr.local:9419)"
                )
            username = (integ.client_id or "").strip()
            password = integration_secrets.read_secret(integ, "password") or ""
            if not username or not password:
                raise RuntimeError(
                    f"veeam: integração {self.ctx.integration_id} sem credenciais "
                    "(usuário no client_id + segredo 'password')"
                )
            verify_ssl = integ.verify_ssl if integ.verify_ssl is not None else True
            return {
                "base_url": base.rstrip("/"),
                "username": username,
                "password": password,
                "api_version": (integ.region or "").strip() or _DEFAULT_API_VERSION,
                "verify_ssl": bool(verify_ssl),
            }

    # ── OAuth2 password grant (auto-contido, honra verify_ssl) ─────────

    async def _fetch_token(self, conn: Dict[str, Any]) -> str:
        url = f"{conn['base_url']}/api/oauth2/token"
        ssl_opt: Any = None if conn["verify_ssl"] else False
        started = time.monotonic()
        async with self.ctx.domain_limiter.slot(self.domain):
            async with self.ctx.session.post(
                url,
                data={
                    "grant_type": "password",
                    "username": conn["username"],
                    "password": conn["password"],
                },
                headers={
                    "x-api-version": conn["api_version"],
                    "Accept": "application/json",
                },
                ssl=ssl_opt,
            ) as resp:
                resp.raise_for_status()
                payload = await resp.json()
        API_LATENCY.labels(vendor=self.platform, stream="oauth2").observe(
            time.monotonic() - started
        )
        token = (payload or {}).get("access_token") or ""
        if not token:
            raise RuntimeError("veeam: resposta de token sem access_token")
        return token

    # ── Coleta ─────────────────────────────────────────────────────────

    async def collect(self) -> AsyncIterator[Dict[str, Any]]:
        conn = await asyncio.to_thread(self._load_conn)
        self._base = conn["base_url"]
        self._host = urlparse(self._base).hostname or "veeam-vbr"
        ssl_opt: Any = None if conn["verify_ssl"] else False

        token = await self._fetch_token(conn)
        headers = {
            "Authorization": f"Bearer {token}",
            "x-api-version": conn["api_version"],
            "Accept": "application/json",
        }
        sessions_url = f"{self._base}/api/v1/sessions"

        cursor = self.ctx.cursor or {}
        created_after: str = cursor.get("created_after") or _default_lookback_iso()
        skip = _as_int(cursor.get("skip"))
        latest = created_after
        saw_any = False
        pages = 0
        # Ciclo RETOMADO (skip>0 veio do cursor). Se a 1ª página vier vazia, o offset
        # persistido estourou o ``total`` — a retenção do VBR podou sessões entre ciclos.
        # Isso é diferente de "janela vazia": precisamos re-sincronizar do início.
        resumed = skip > 0

        while True:
            # Teto por ciclo: grava a posição RESUMÍVEL (mesma janela + skip da
            # PRÓXIMA página) e encerra o run. NÃO caímos na escrita final de
            # watermark lá embaixo — ela zeraria o skip e pularia o restante da
            # janela (ver _MAX_PAGES_PER_CYCLE).
            if self.ctx.bounded_per_cycle and pages >= _MAX_PAGES_PER_CYCLE:
                self.ctx.cursor = {"created_after": created_after, "skip": skip}
                # Sobrou backlog: ``created_after`` não avança até a janela drenar,
                # e um VBR quieto produz exatamente o mesmo watermark parado. Só o
                # par (teto, atraso) separa os dois na Saúde do Pipeline.
                self.mark_cycle_capped()
                logger.info(
                    "veeam sessions: teto de %d páginas/ciclo atingido — retomando em "
                    "created_after=%s skip=%d (integration=%s)",
                    _MAX_PAGES_PER_CYCLE, created_after, skip, self.ctx.integration_id,
                )
                return

            await self.ctx.rate_limiter.acquire(self.ctx.integration_id, self.platform)

            params = {
                "createdAfterFilter": created_after,
                "orderColumn": "CreationTime",
                "orderAsc": "true",
                "skip": str(skip),
                "limit": str(_PAGE_SIZE),
            }

            started = time.monotonic()
            async with self.ctx.domain_limiter.slot(self.domain):
                async with self.ctx.session.get(
                    sessions_url, params=params, headers=headers, ssl=ssl_opt
                ) as resp:
                    if resp.status == 429:
                        retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                        await self.ctx.rate_limiter.backoff(self.platform, retry_after)
                        raise VeeamRateLimitedError(retry_after)
                    resp.raise_for_status()
                    payload = await resp.json()

            API_LATENCY.labels(vendor=self.platform, stream=self.stream).observe(
                time.monotonic() - started
            )

            data = (payload or {}).get("data") or []
            if not data:
                if resumed and not saw_any:
                    # Offset estourado (retenção podou o histórico): re-sincroniza a
                    # janela do início em vez de tratá-la como drenada.
                    logger.info(
                        "veeam sessions: offset %d estourou o total (retenção?) — "
                        "re-sincronizando created_after=%s (integration=%s)",
                        skip, created_after, self.ctx.integration_id,
                    )
                    skip = 0
                break

            for item in data:
                latest = _max_iso(latest, item.get("creationTime"))
                saw_any = True
                yield item

            skip += len(data)
            pages += 1

            if len(data) < _PAGE_SIZE:
                break
            total = ((payload or {}).get("pagination") or {}).get("total")
            if isinstance(total, int) and skip >= total:
                break

        # Drenou a janela: só AQUI o watermark avança e o offset é zerado. O recuo de
        # ``_WATERMARK_OVERLAP_SECONDS`` compensa o filtro EXCLUSIVO (a borda
        # re-buscada é deduplicada por ``id``). Sem eventos, o watermark fica PARADO —
        # recuar num ciclo vazio faria a janela andar para trás indefinidamente.
        self.ctx.cursor = {
            "created_after": _with_overlap(latest) if saw_any else created_after,
            "skip": 0,
        }

    def extract_message_id(self, event: Dict[str, Any]) -> str:
        return str(event.get("id") or "")

    @classmethod
    def watermark_at(cls, cursor: Optional[Dict[str, Any]]) -> Optional[datetime]:
        """``created_after`` — o ``createdAfterFilter`` enviado ao VBR.

        Já vem recuado de ``_WATERMARK_OVERLAP_SECONDS`` pela escrita final; o
        atraso reportado é, portanto, conservador por alguns segundos. Preferimos
        assim: superestimar um pouco o atraso é inofensivo, subestimar é o defeito
        que este indicador existe para corrigir.
        """
        return cls.watermark_from_iso(cursor, "created_after")


def _default_lookback_iso() -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=1)
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _max_iso(current: str, candidate: Any) -> str:
    """Maior de dois ISO-8601 comparando DATETIME, não string.

    Comparação lexicográfica quebra aqui: o VBR devolve ``creationTime`` com fração
    de segundo e offset (``...T02:10:00.000+02:00``) enquanto o watermark que geramos
    não tem fração — ``"02:10:00.000Z" > "13:38:22Z"`` é ``False`` embora possa ser
    posterior. Um watermark que nunca avança = re-coleta infinita da mesma janela.
    """
    cand_dt = _parse_iso(candidate)
    if cand_dt is None:
        return current
    cur_dt = _parse_iso(current)
    if cur_dt is None or cand_dt > cur_dt:
        return (
            cand_dt.astimezone(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    return current


def _with_overlap(value: str) -> str:
    """Recua ``_WATERMARK_OVERLAP_SECONDS`` de um ISO-8601 (passthrough se ilegível)."""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = (dt - timedelta(seconds=_WATERMARK_OVERLAP_SECONDS)).astimezone(timezone.utc)
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _parse_retry_after(value: str | None) -> int:
    if not value:
        return 5
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 5


# ── Self-registration (refresher no-op — token obtido no collect()) ────


async def _veeam_refresher(integration_id: int) -> Dict[str, object]:
    """No-op p/ o framework. O VBR é on-prem e por instância: o password-grant precisa
    do ``verify_ssl`` daquela integração, que o caminho genérico do ``oauth_cache`` não
    carrega. O token é obtido no ``collect()`` (ver docstring do módulo)."""
    return {"access_token": "", "expires_in": 3600}


async def _veeam_probe(cfg: Dict[str, Any]):
    """Teste STATELESS pré-save: password-grant + ``GET /api/v1/sessions?limit=1``."""
    import aiohttp

    from ..output.base import TestResult

    base = (cfg.get("base_url") or "").rstrip("/")
    username = (cfg.get("client_id") or "").strip()
    password = cfg.get("password") or ""
    api_version = (cfg.get("region") or "").strip() or _DEFAULT_API_VERSION
    verify = cfg.get("verify_ssl")
    ssl_opt: Any = None if (verify is None or bool(verify)) else False
    if not base or not username or not password:
        return TestResult.failed("Informe a URL do servidor VBR, o usuário e a senha.")

    t0 = time.perf_counter()
    timeout = aiohttp.ClientTimeout(total=20)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{base}/api/oauth2/token",
                data={"grant_type": "password", "username": username, "password": password},
                headers={"x-api-version": api_version, "Accept": "application/json"},
                ssl=ssl_opt,
            ) as r:
                if r.status != 200:
                    hint = (
                        "Verifique usuário/senha."
                        if r.status in (400, 401, 403)
                        else f"Verifique a URL e o x-api-version ({api_version})."
                    )
                    return TestResult.failed(f"Falha na autenticação (HTTP {r.status}). {hint}")
                token = (await r.json()).get("access_token") or ""
            if not token:
                return TestResult.failed("Autenticação sem access_token na resposta.")
            async with session.get(
                f"{base}/api/v1/sessions",
                params={"limit": "1"},
                headers={
                    "Authorization": f"Bearer {token}",
                    "x-api-version": api_version,
                    "Accept": "application/json",
                },
                ssl=ssl_opt,
            ) as r2:
                ms = (time.perf_counter() - t0) * 1000.0
                if r2.status == 200:
                    return TestResult.passed(
                        "Conexão OK — token válido e sessões acessíveis.", latency_ms=ms
                    )
                return TestResult.failed(
                    f"Autenticou, mas /api/v1/sessions falhou (HTTP {r2.status}). "
                    f"Verifique o x-api-version ({api_version}) e as permissões do usuário."
                )
    except Exception as exc:  # noqa: BLE001
        return TestResult.failed(f"Não foi possível conectar: {exc}")


def _register() -> None:
    from datetime import timedelta as _td

    from ..queues import Q_BULK, T_COLLECT_BULK
    from ..registry import (
        AuthField,
        CollectorRegistration,
        PlatformRegistration,
        register,
        register_platform,
    )

    register_platform(
        PlatformRegistration(
            platform="veeam",
            display_name="Veeam Backup & Replication",
            category="Backup",
            description=(
                "Veeam Backup & Replication — sessões de job (backup, réplica, restore) "
                "via REST API do servidor VBR."
            ),
            icon_id="veeam",
            docs_url="https://helpcenter.veeam.com/docs/backup/vbr_rest/rest_api_reference.html",
            order=60,
            test_fn=_veeam_probe,
            required_secrets=("password",),
            capabilities=frozenset({"catalog", "auth:test", "collect:sessions"}),
            auth_fields=(
                AuthField(key="base_url", label="URL do servidor VBR", type="url", required=True,
                          help_text="Host + porta da REST API (ex: https://vbr.local:9419)"),
                AuthField(key="client_id", label="Usuário", type="string", required=True,
                          help_text="Usuário do VBR. Conta de domínio: DOMINIO\\usuario"),
                AuthField(key="password", label="Senha", type="secret", required=True),
                AuthField(key="region", label="Versão da API (x-api-version)", type="select",
                          required=False, options=_API_VERSIONS,
                          help_text=(
                              "Header exigido em toda request. VBR 12.x: 1.1-rev*/1.2-rev*; "
                              f"VBR 13.x: 1.3-rev1. Vazio = {_DEFAULT_API_VERSION}."
                          )),
                AuthField(key="verify_ssl", label="Verificar SSL", type="bool", required=False,
                          help_text="Desative se o VBR usa o certificado auto-assinado de fábrica"),
            ),
        )
    )

    register(
        CollectorRegistration(
            platform=VeeamSessionsCollector.platform,
            stream=VeeamSessionsCollector.stream,
            collector_cls=VeeamSessionsCollector,
            refresh_fn=_veeam_refresher,
            schedule=_td(minutes=5),
            queue=Q_BULK,
            task_name=T_COLLECT_BULK,
        )
    )


_register()
