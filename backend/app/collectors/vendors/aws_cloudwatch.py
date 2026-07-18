"""AWS CloudWatch Logs — coletor de log-events via ``FilterLogEvents``.

Vendor novo = 1 módulo, ZERO core. Complementa o ``aws_cloudtrail`` (que é
cloud-AUDIT via bucket S3): aqui a fonte é o **serviço de logs** da AWS, onde
caem logs de aplicação/serviço (Lambda, ECS, API Gateway, RDS, VPC Flow, appliances
que exportam via agente). É a fonte "catch-all" da AWS.

**API oficial** — CloudWatch Logs ``FilterLogEvents`` (``logs.<region>.amazonaws.com``,
target ``Logs_20140328.FilterLogEvents``):
https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_FilterLogEvents.html

- Params: ``logGroupName`` (1..512), ``startTime``/``endTime`` (ms epoch, **inclusivos**),
  ``limit`` (1..10000, default 10000), ``nextToken``, ``startFromHead`` (default true =
  ordem ascendente por timestamp).
- Resposta: ``events[] {eventId, timestamp, ingestionTime, message, logStreamName}`` +
  ``nextToken``. Sem ``nextToken`` ⇒ paginação terminou. **Página parcial/vazia NÃO
  significa fim** — só a ausência de ``nextToken`` significa (a doc é explícita).
- ``nextToken`` expira em **24h** (cadência de 5 min ⇒ nunca esbarramos nisso).
- Cada página traz até 1 MB **ou** 10.000 eventos, o que vier primeiro.

Shape do evento: https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_FilteredLogEvent.html

**Creds — reuso GENÉRICO de colunas (zero-core, mesmo modelo do ``aws_cloudtrail``):**

===================  =========================================================
Coluna genérica      Uso aqui / justificativa
===================  =========================================================
``client_id``        IAM **Access Key ID** — é público (vai no header SigV4),
                     então cabe numa coluna não-secreta, igual ao CloudTrail.
``secret_access_key``**Secret Access Key** no store (``integration_secrets``),
(store, não coluna)  nunca em coluna.
``base_url``         **Log group name**. É o "endereço lógico" do recurso que o
                     coletor lê — mesmo papel que ``base_url`` cumpre nos vendors
                     HTTP (o alvo da leitura) e que o *bucket* cumpre no
                     CloudTrail. Evita coluna nova.
``region``           Região AWS — compõe o endpoint ``logs.<region>.amazonaws.com``
                     e é gravada no evento (a resposta da API não a traz).
``tenant_id``        AWS **Account ID** (OPCIONAL). Só enriquecimento
                     (``normalized.cloud.account.uid``) — a API não devolve o
                     account. Genérico por design (Defender usa p/ Azure tenant).
===================  =========================================================

**Cursor:** watermark de ``startTime`` em ms epoch + ``nextToken`` resumível.
Ver ``collect()`` para o contrato exato de cap-hit vs. drain.

**Enriquecimento do raw:** a resposta do ``FilterLogEvents`` é auto-contida demais —
não traz log group, região, conta nem timestamp legível. Sem isso o evento
normalizado não sabe de ONDE veio. Então anexamos ``logGroupName``/``awsRegion``/
``awsAccountId``/``eventTime`` ao dict cru (precedente: ``wazuh_detections`` injeta
``src["id"]`` do ``_id`` do doc). ``eventTime`` também resolve um limite da DSL: os
``type_cast`` disponíveis assumem epoch em **segundos**, e o CloudWatch entrega
**milissegundos** — converter aqui evita adicionar um cast novo ao core.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict, Optional

from ..base import BaseCollector
from ..metrics import API_LATENCY

logger = logging.getLogger(__name__)

# Teto de PÁGINAS do ``FilterLogEvents`` por ciclo. Cada página custa até 1 MB /
# 10k eventos, então drenar um backlog inteiro num run estoura o
# ``task_soft_time_limit`` (720s) → o pipeline reverte o cursor → poison-loop sem
# progresso (incidente de PROD). Ao atingir o teto gravamos o ``nextToken`` da
# PRÓXIMA página junto da janela (start/end) e retomamos no ciclo seguinte.
_MAX_PAGES_PER_CYCLE = 20

# ``limit`` do FilterLogEvents (máx. permitido pela API = 10000). 1000 mantém a
# página pequena o bastante para o ciclo ser previsível.
_PAGE_LIMIT = 1000

# Largura máxima da janela consultada num ciclo. Um cursor MUITO atrasado
# (downtime, primeira carga) é drenado em pedaços — o watermark avança a cada
# ciclo até alcançar o presente, sem query gigante.
_MAX_WINDOW_MS = 6 * 60 * 60 * 1000  # 6h

# Overlap aplicado ao abrir uma janela NOVA: o CloudWatch indexa por
# ``timestamp`` (hora do evento), mas a ingestão tem lag — um evento pode entrar
# com timestamp já dentro de uma janela consultada. O overlap re-consulta a borda;
# o dedupe por ``eventId`` (único, garantido pela AWS) descarta a repetição.
_OVERLAP_MS = 5 * 60 * 1000  # 5min

# Lookback inicial quando não há cursor.
_DEFAULT_LOOKBACK_MS = 60 * 60 * 1000  # 1h


class AWSCloudWatchCollector(BaseCollector):
    """Pull de log-events de um log group do CloudWatch Logs (aioboto3)."""

    platform = "aws_cloudwatch"
    stream = "events"
    event_type = "aws_cloudwatch.event"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._region: Optional[str] = None

    @property
    def domain(self) -> str:
        return f"logs.{self._region}.amazonaws.com" if self._region else "logs.amazonaws.com"

    def _load_conn(self) -> Dict[str, Any]:
        """Creds + config do log group (sync — chamado via ``asyncio.to_thread``)."""
        from ...db import database, models
        from ...services import integration_secrets

        with database.SessionLocal() as db:
            integ = db.get(models.Integration, self.ctx.integration_id)
            if integ is None:
                raise RuntimeError(
                    f"cloudwatch: integração {self.ctx.integration_id} não encontrada"
                )
            access_key_id = (integ.client_id or "").strip()
            secret = integration_secrets.read_secret(integ, "secret_access_key") or ""
            log_group = (integ.base_url or "").strip()
            region = (integ.region or "us-east-1").strip()
            account_id = (integ.tenant_id or "").strip()
            if not access_key_id or not secret or not log_group:
                raise RuntimeError(
                    f"cloudwatch: integração {self.ctx.integration_id} incompleta "
                    "(precisa de Access Key ID, Secret Access Key e Log Group)"
                )
            return {
                "access_key_id": access_key_id,
                "secret": secret,
                "log_group": log_group,
                "region": region,
                "account_id": account_id,
            }

    def _client(self, conn: Dict[str, Any]):
        """Seam mockável (modelo do ``aws_cloudtrail._s3_client``): import lazy +
        client ``aioboto3`` do serviço ``logs``."""
        import aioboto3  # noqa: PLC0415 — import tardio proposital (mockabilidade)

        return aioboto3.Session().client(
            "logs",
            aws_access_key_id=conn["access_key_id"],
            aws_secret_access_key=conn["secret"],
            region_name=conn["region"],
        )

    async def collect(self) -> AsyncIterator[Dict[str, Any]]:
        conn = await asyncio.to_thread(self._load_conn)
        self._region = conn["region"]
        log_group = conn["log_group"]

        cursor = self.ctx.cursor or {}
        resume_token: Optional[str] = cursor.get("next_token") or None

        if resume_token:
            # Retomada MID-BACKLOG: o ``nextToken`` só faz sentido colado à MESMA
            # janela que o originou — por isso a janela também é persistida.
            window_start = int(cursor.get("start_time_ms") or 0)
            window_end = int(cursor.get("end_time_ms") or 0)
            if window_end <= 0:  # cursor corrompido → recomeça janela limpa
                resume_token = None

        if not resume_token:
            watermark = int(cursor.get("start_time_ms") or 0) or (
                _now_ms() - _DEFAULT_LOOKBACK_MS
            )
            window_start = max(0, watermark - _OVERLAP_MS)
            # Janela fechada e FIXA no ciclo: paginar com ``endTime`` móvel
            # produziria páginas inconsistentes.
            window_end = min(_now_ms(), window_start + _MAX_WINDOW_MS)

        if window_end < window_start:  # relógio andou p/ trás — nada a fazer
            return

        next_token = resume_token
        pages = 0

        async with self._client(conn) as logs:
            while True:
                await self.ctx.rate_limiter.acquire(self.ctx.integration_id, self.platform)
                kwargs: Dict[str, Any] = {
                    "logGroupName": log_group,
                    "startTime": window_start,
                    "endTime": window_end,
                    "limit": _PAGE_LIMIT,
                    "startFromHead": True,  # ascendente (default da API, explícito)
                }
                if next_token:
                    kwargs["nextToken"] = next_token

                started = time.monotonic()
                async with self.ctx.domain_limiter.slot(self.domain):
                    resp = await logs.filter_log_events(**kwargs)
                API_LATENCY.labels(vendor=self.platform, stream=self.stream).observe(
                    time.monotonic() - started
                )

                for ev in resp.get("events") or []:
                    yield _enrich(ev, conn)

                # Contrato da API: página parcial/vazia NÃO indica fim; só a
                # AUSÊNCIA de ``nextToken`` indica.
                next_token = resp.get("nextToken") or None
                pages += 1
                if not next_token:
                    break

                if self.ctx.bounded_per_cycle and pages >= _MAX_PAGES_PER_CYCLE:
                    # ── TETO DO CICLO ────────────────────────────────────────
                    # Grava a POSIÇÃO resumível (token da PRÓXIMA página + janela
                    # que o originou). O watermark NÃO avança: gravar
                    # ``start_time_ms = window_end + 1`` aqui descartaria todas as
                    # páginas ainda não lidas desta janela (perda de dado).
                    self.ctx.cursor = {
                        "start_time_ms": window_start,
                        "end_time_ms": window_end,
                        "next_token": next_token,
                    }
                    logger.info(
                        "cloudwatch: teto de %d páginas/ciclo — retomando janela "
                        "[%s, %s] com nextToken (integration=%s, log_group=%s)",
                        _MAX_PAGES_PER_CYCLE, window_start, window_end,
                        self.ctx.integration_id, log_group,
                    )
                    return

        # Drenou a janela INTEIRA: só aqui o watermark avança (e o token some).
        # ``endTime`` é inclusivo, daí o +1 para não reprocessar a borda.
        self.ctx.cursor = {"start_time_ms": window_end + 1}

    def extract_message_id(self, event: Dict[str, Any]) -> str:
        """``eventId`` — a AWS garante unicidade por evento no CloudWatch Logs."""
        return str(event.get("eventId") or "")


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _enrich(event: Dict[str, Any], conn: Dict[str, Any]) -> Dict[str, Any]:
    """Anexa contexto que o ``FilterLogEvents`` não devolve + ``eventTime`` ISO.

    Cópia rasa — não mutamos o objeto devolvido pelo SDK.
    """
    out = dict(event)
    out["logGroupName"] = conn["log_group"]
    out["awsRegion"] = conn["region"]
    if conn.get("account_id"):
        out["awsAccountId"] = conn["account_id"]
    ts = event.get("timestamp")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        out["eventTime"] = (
            datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    return out


# ── Self-registration (sem OAuth — IAM key) ────────────────────────────


async def _cloudwatch_refresher(integration_id: int) -> Dict[str, object]:
    """No-op p/ o framework: CloudWatch Logs usa IAM key (SigV4 no boto3), não
    OAuth. O collector lê key/secret do store no ``collect()``."""
    return {"access_token": "", "expires_in": 3600}


async def _cloudwatch_probe(cfg: Dict[str, Any]):
    """Teste STATELESS pré-save: 1 chamada ``FilterLogEvents`` (limit=1) no log
    group informado, com as creds digitadas. Valida cred + permissão
    ``logs:FilterLogEvents`` + existência do log group de uma vez."""
    from ..output.base import TestResult

    log_group = (cfg.get("base_url") or "").strip()
    access_key_id = (cfg.get("client_id") or "").strip()
    secret = (cfg.get("secret_access_key") or "").strip()
    region = (cfg.get("region") or "us-east-1").strip()
    if not access_key_id or not secret or not log_group:
        return TestResult.failed("Informe Access Key ID, Secret Access Key e Log Group.")

    t0 = time.perf_counter()
    try:
        import aioboto3  # noqa: PLC0415

        session = aioboto3.Session()
        async with session.client(
            "logs",
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret,
            region_name=region,
        ) as logs:
            await logs.filter_log_events(
                logGroupName=log_group,
                startTime=_now_ms() - _DEFAULT_LOOKBACK_MS,
                limit=1,
            )
        ms = (time.perf_counter() - t0) * 1000.0
        return TestResult.passed(
            "Conexão OK — acesso ao log group validado.", latency_ms=ms
        )
    except Exception as exc:  # noqa: BLE001
        return TestResult.failed(
            f"Falha ao consultar o log group: {type(exc).__name__}"
        )


def _register() -> None:
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
            platform="aws_cloudwatch",
            display_name="AWS CloudWatch Logs",
            category="Cloud-audit",
            description=(
                "AWS CloudWatch Logs — log events de um log group "
                "(polling via FilterLogEvents)."
            ),
            icon_id="aws",
            docs_url="https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_FilterLogEvents.html",
            order=51,
            test_fn=_cloudwatch_probe,
            required_secrets=("secret_access_key",),
            capabilities=frozenset({"catalog", "auth:test", "collect:events"}),
            auth_fields=(
                AuthField(
                    key="client_id", label="Access Key ID", type="string", required=True,
                    help_text="IAM Access Key ID com permissão logs:FilterLogEvents",
                ),
                AuthField(
                    key="secret_access_key", label="Secret Access Key",
                    type="secret", required=True,
                ),
                AuthField(
                    key="base_url", label="Log Group", type="string", required=True,
                    help_text="Nome do log group, ex: /aws/lambda/minha-funcao",
                ),
                AuthField(
                    key="region", label="Região AWS", type="string", required=True,
                    help_text="Ex: us-east-1 (compõe logs.<region>.amazonaws.com)",
                ),
                AuthField(
                    key="tenant_id", label="Account ID", type="string", required=False,
                    help_text="AWS Account ID (opcional) — só enriquece o evento normalizado",
                ),
            ),
        )
    )

    register(
        CollectorRegistration(
            platform=AWSCloudWatchCollector.platform,
            stream=AWSCloudWatchCollector.stream,
            collector_cls=AWSCloudWatchCollector,
            refresh_fn=_cloudwatch_refresher,
            schedule=timedelta(minutes=5),
            queue=Q_BULK,
            task_name=T_COLLECT_BULK,
        )
    )


_register()
