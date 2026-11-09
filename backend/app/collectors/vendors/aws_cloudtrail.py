"""AWS CloudTrail — collector de cloud-audit via polling de bucket S3.

Vendor novo = 1 módulo, ZERO core. Cloud-audit é categoria canônica ausente;
CloudTrail é a fonte cloud #1. Exercita o caminho de **creds exóticas** (IAM key)
sobre o capability/credential model.

**Creds (reuso GENÉRICO das colunas — client_id/base_url/tenant_id/region são
genéricos):** ``client_id`` = ARN do papel cross-account (recomendado, ver
``collectors/auth/aws.py``) OU Access Key ID (legado, com ``secret_access_key`` no
store); ``base_url`` = nome do bucket S3, ``region`` = região AWS, ``tenant_id`` =
Account ID (compõe o prefixo; com papel, sai do próprio ARN se vazio). Sem coluna nova
→ zero-core. (Limite: org trails têm o OU ID no path — config single-account; documentar.)

**Objeto que não dá para ler NÃO é pulado.** Falha de leitura (permissão, KMS, rede)
derruba o ciclo: o cursor volta e o erro aparece na Saúde. Antes o objeto era pulado
e o watermark avançava — perda permanente com a Saúde verde. Só conteúdo corrompido
(gzip/JSON inválido, que reler não conserta) é pulado, e fica registrado no cursor em
``skipped_objects``.

**Coleta:** ``aioboto3`` (import lazy, modelo do destino ``s3.py``) — ``list_objects_v2``
sob o prefixo ``AWSLogs/<account>/CloudTrail/<region>/<YYYY>/<MM>/<DD>/`` (hoje+ontem
UTC, pois a partição é por hora de ENTREGA), baixa cada ``.json.gz``, descomprime e
emite cada item de ``Records[]``. Cursor: ``LastModified`` do último objeto + overlap
(~15min); dedupe por ``eventID`` (a AWS endossa: duplicados têm o mesmo eventID).
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict, List, Optional

from ..auth import aws as aws_auth
from ..base import BaseCollector
from ..metrics import API_LATENCY
import time

logger = logging.getLogger(__name__)

# Teto de OBJETOS S3 processados por ciclo. Cada objeto custa GET + gzip.decompress +
# json.loads (pesado), então o teto é por objeto, não por página de listagem. Sem ele,
# um catch-up drena a janela inteira num run e estoura o ``task_soft_time_limit`` (720s)
# → o pipeline reverte o cursor → loop sem progresso. Ao atingir, gravamos a POSIÇÃO
# (prefixo + última chave) e retomamos no próximo ciclo.
_MAX_OBJECTS_PER_CYCLE = 500

# Quantos objetos corrompidos pulados ficam registrados no cursor (os mais recentes).
_MAX_SKIPPED_RECORDED = 20

# Teto de dias de prefixo gerados num ciclo. Evita lista ilimitada de prefixos quando o
# cursor está MUITO atrasado (o watermark avança a cada ciclo até alcançar o presente).
_MAX_LOOKBACK_DAYS = 14


class AWSCloudTrailCollector(BaseCollector):
    """Pull de eventos CloudTrail de um bucket S3 (aioboto3)."""

    platform = "aws_cloudtrail"
    stream = "events"
    event_type = "aws_cloudtrail.event"

    def __init__(self, ctx) -> None:
        super().__init__(ctx)
        self._region: Optional[str] = None
        self._skipped: List[Dict[str, str]] = []

    @property
    def domain(self) -> str:
        return f"s3.{self._region}.amazonaws.com" if self._region else "s3.amazonaws.com"

    def _load_conn(self) -> Dict[str, Any]:
        """Creds (papel ou chave fixa) + config do bucket (sync, em thread)."""
        from ...db import database, models

        with database.SessionLocal() as db:
            integ = db.get(models.Integration, self.ctx.integration_id)
            if integ is None:
                raise RuntimeError(f"cloudtrail: integração {self.ctx.integration_id} não encontrada")
            try:
                auth = aws_auth.auth_for_integration(db, integ)
            except aws_auth.AwsAuthError as exc:
                raise RuntimeError(
                    f"cloudtrail: integração {self.ctx.integration_id} incompleta — {exc}"
                ) from None
            bucket = (integ.base_url or "").strip()
            region = (integ.region or "us-east-1").strip()
            account_id = (integ.tenant_id or "").strip() or (
                aws_auth.role_account_id(auth.role_arn) or ""
            )
            if not bucket or not account_id:
                raise RuntimeError(
                    f"cloudtrail: integração {self.ctx.integration_id} incompleta "
                    "(precisa do bucket e do Account ID)"
                )
            return {
                "auth": auth,
                "bucket": bucket,
                "region": region,
                "prefix_base": f"AWSLogs/{account_id}/CloudTrail/{region}/",
            }

    def _s3_client(self, conn: Dict[str, Any], client_kwargs: Dict[str, str]):
        """Seam mockável (modelo s3.py): import lazy + client aioboto3."""
        import aioboto3  # noqa: PLC0415 — import tardio proposital (mockabilidade)

        return aioboto3.Session().client("s3", **client_kwargs)

    async def collect(self) -> AsyncIterator[Dict[str, Any]]:
        conn = await asyncio.to_thread(self._load_conn)
        self._region = conn["region"]
        bucket = conn["bucket"]
        client_kwargs = await aws_auth.client_kwargs(conn["auth"], conn["region"])

        cursor = self.ctx.cursor or {}
        last_mod = _parse_iso(cursor.get("last_modified")) or _default_lookback_dt()
        overlap = last_mod - timedelta(minutes=15)  # cobre entrega atrasada / ts empatado
        latest = last_mod
        # Retomada MID-BACKLOG (teto do ciclo anterior): continua no MESMO prefixo, a
        # partir da última chave processada. Sem isto o cursor era só um watermark
        # gravado no FIM — um return no meio não retomava (daí o redesenho).
        resume_prefix: Optional[str] = cursor.get("prefix") or None
        resume_after: Optional[str] = cursor.get("start_after") or None

        # Prefixos de dia do watermark até hoje (antes: só hoje+ontem → um backlog de
        # >2 dias era silenciosamente PULADO). Do mais ANTIGO p/ o mais novo, p/ drenar
        # cronologicamente e a posição de retomada fazer sentido.
        prefixes = _date_prefixes(conn["prefix_base"], last_mod, datetime.now(timezone.utc))
        if resume_prefix and resume_prefix in prefixes:
            prefixes = prefixes[prefixes.index(resume_prefix):]  # pula os já drenados

        objects_done = 0
        self._skipped = list(cursor.get("skipped_objects") or [])
        async with self._s3_client(conn, client_kwargs) as s3:
            for prefix in prefixes:
                # ``StartAfter`` (chave, lexicográfico) em vez de ContinuationToken
                # (opaco/expirável): é o que torna a posição PERSISTÍVEL e resumível.
                start_after: Optional[str] = resume_after if prefix == resume_prefix else None
                resume_after = None  # só vale p/ o primeiro prefixo retomado
                while True:
                    await self.ctx.rate_limiter.acquire(self.ctx.integration_id, self.platform)
                    list_kwargs: Dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
                    if start_after:
                        list_kwargs["StartAfter"] = start_after

                    started = time.monotonic()
                    async with self.ctx.domain_limiter.slot(self.domain):
                        resp = await s3.list_objects_v2(**list_kwargs)
                    API_LATENCY.labels(vendor=self.platform, stream=self.stream).observe(
                        time.monotonic() - started
                    )

                    contents = resp.get("Contents", []) or []
                    if not contents:
                        break
                    for obj in contents:
                        key = obj["Key"]
                        lm = obj.get("LastModified")
                        start_after = key  # avança SEMPRE (mesmo se pulado) → progresso
                        if lm is None or lm <= overlap:
                            continue
                        records = await self._read_records(s3, bucket, key)
                        for ev in records:
                            yield ev
                        if lm > latest:
                            latest = lm
                        objects_done += 1
                        if self.ctx.bounded_per_cycle and objects_done >= _MAX_OBJECTS_PER_CYCLE:
                            # Teto do ciclo: grava a POSIÇÃO e retoma no próximo ciclo.
                            # O watermark NÃO avança — senão a retomada pularia os
                            # objetos ainda não lidos desta janela (perda de dado).
                            self.ctx.cursor = self._with_skipped({
                                "last_modified": last_mod.isoformat(),
                                "prefix": prefix,
                                "start_after": key,
                            })
                            # Sobrou backlog: sem este sinal, o watermark parado
                            # (justamente porque não avançamos) é indistinguível de
                            # um bucket sem objetos novos, e a Saúde do Pipeline
                            # continua verde enquanto o atraso cresce.
                            self.mark_cycle_capped()
                            logger.info(
                                "cloudtrail: teto de %d objetos/ciclo — retomando em "
                                "prefix=%s start_after=%s (integration=%s)",
                                _MAX_OBJECTS_PER_CYCLE, prefix, key, self.ctx.integration_id,
                            )
                            return

                    if not resp.get("IsTruncated"):
                        break

        # Drenou a janela inteira: só AQUI o watermark avança (e a posição é limpa).
        self.ctx.cursor = self._with_skipped({"last_modified": latest.isoformat()})

    def _with_skipped(self, cursor: Dict[str, Any]) -> Dict[str, Any]:
        """Anexa ao cursor os objetos corrompidos pulados (os mais recentes)."""
        if self._skipped:
            cursor["skipped_objects"] = self._skipped[-_MAX_SKIPPED_RECORDED:]
        return cursor

    async def _read_records(self, s3, bucket: str, key: str) -> List[Dict[str, Any]]:
        """Baixa o objeto ``.json.gz``, descomprime e devolve ``Records[]``.

        Duas falhas com destinos opostos:

        - **Não deu para LER** (permissão, ``kms:Decrypt``, rede, throttling): o
          objeto está bom e o próximo ciclo pode conseguir. Levanta — o ciclo falha,
          o cursor volta e o erro aparece na Saúde. Antes era pulado e o watermark
          avançava, perdendo o objeto para sempre com a Saúde verde. Exceção: objeto
          que sumiu entre listar e ler (``NoSuchKey``, ex.: lifecycle) — não há o que ler.
        - **Leu, mas o conteúdo é inválido** (gzip/JSON corrompido): reler não
          conserta, e levantar travaria a coleta para sempre neste objeto. Pula, loga
          em ERROR e registra em ``skipped_objects`` no cursor.
        """
        try:
            got = await s3.get_object(Bucket=bucket, Key=key)
            async with got["Body"] as body:
                raw = await body.read()
        except Exception as exc:  # noqa: BLE001 — reclassificado abaixo
            if _error_code(exc) == "NoSuchKey":
                logger.warning("cloudtrail: objeto %s sumiu antes da leitura — pulando", key)
                return []
            raise RuntimeError(
                f"cloudtrail: falha ao ler s3://{bucket}/{key} — "
                f"{aws_auth.describe_error(exc, operation='GetObject')}"
            ) from None
        try:
            payload = json.loads(gzip.decompress(raw))
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "cloudtrail: objeto %s corrompido (%s) — pulado e registrado no cursor "
                "(integration=%s)", key, type(exc).__name__, self.ctx.integration_id,
            )
            self._skipped.append({"key": key, "reason": type(exc).__name__})
            return []
        records = payload.get("Records") if isinstance(payload, dict) else None
        return records if isinstance(records, list) else []

    def extract_message_id(self, event: Dict[str, Any]) -> str:
        return str(event.get("eventID") or "")

    @classmethod
    def watermark_at(cls, cursor: Optional[Dict[str, Any]]) -> Optional[datetime]:
        """``last_modified`` — a hora de ENTREGA do último objeto S3 consumido.

        Não é a hora do evento na AWS (o CloudTrail entrega com minutos de atraso
        próprio), e sim até onde o bucket foi drenado. É a medida certa aqui: o
        que a Saúde precisa responder é se ESTE coletor está acompanhando o
        bucket, não qual é o SLA de entrega da AWS.

        Num ciclo capado o valor fica no início da janela, junto de
        ``prefix``/``start_after``, o que é proposital — enquanto sobrar objeto
        não lido, o atraso reportado é o do ponto realmente consumido.
        """
        return cls.watermark_from_iso(cursor, "last_modified")


def _error_code(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return str((response.get("Error") or {}).get("Code") or "")
    return ""


def _default_lookback_dt() -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=2)


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _date_prefixes(base_prefix: str, since: datetime, now: datetime) -> List[str]:
    """Prefixos de dia (UTC) do watermark até hoje, do mais ANTIGO p/ o mais novo.

    A partição CloudTrail é por data de ENTREGA. Antes gerávamos só hoje+ontem, então um
    backlog de mais de 2 dias (cursor atrasado, downtime, primeira carga) era
    silenciosamente PULADO — data-gap. Agora cobrimos do watermark até hoje, com 1 dia de
    folga p/ entrega atrasada e teto de ``_MAX_LOOKBACK_DAYS`` (o watermark avança a cada
    ciclo até alcançar o presente, então um cursor muito velho converge sem lista infinita).
    """
    start = min(since, now) - timedelta(days=1)  # folga p/ entrega atrasada
    days = (now.date() - start.date()).days
    days = max(0, min(days, _MAX_LOOKBACK_DAYS))
    return [
        f"{base_prefix}{d.year:04d}/{d.month:02d}/{d.day:02d}/"
        for d in (now - timedelta(days=delta) for delta in range(days, -1, -1))
    ]


# ── Self-registration (sem OAuth — IAM key) ────────────────────────────


async def _cloudtrail_refresher(integration_id: int) -> Dict[str, object]:
    """No-op p/ o framework: CloudTrail usa IAM key (assinatura SigV4 no boto3),
    não OAuth. O collector lê a key/secret do store no collect()."""
    return {"access_token": "", "expires_in": 3600}


async def _cloudtrail_probe(cfg: Dict[str, Any]):
    """Teste STATELESS pré-save: lista 1 objeto do bucket com as creds digitadas.

    Com papel, o teste já faz o AssumeRole de verdade — pega trust policy errada e
    External ID trocado antes de salvar."""
    from ..output.base import TestResult

    bucket = (cfg.get("base_url") or "").strip()
    region = (cfg.get("region") or "us-east-1").strip()
    if not bucket:
        return TestResult.failed("Informe o bucket S3.")
    t0 = time.perf_counter()
    try:
        auth = await asyncio.to_thread(aws_auth.auth_for_probe, cfg)
        client_kwargs = await aws_auth.client_kwargs(auth, region)
    except aws_auth.AwsAuthError as exc:
        return TestResult.failed(str(exc))
    try:
        import aioboto3  # noqa: PLC0415

        async with aioboto3.Session().client("s3", **client_kwargs) as s3:
            await s3.list_objects_v2(Bucket=bucket, MaxKeys=1)
        ms = (time.perf_counter() - t0) * 1000.0
        how = "papel assumido" if auth.mode == aws_auth.MODE_ROLE else "chave fixa"
        return TestResult.passed(f"Conexão OK ({how}) — acesso ao bucket validado.", latency_ms=ms)
    except Exception as exc:  # noqa: BLE001
        return TestResult.failed(
            f"Falha ao acessar o bucket: {aws_auth.describe_error(exc, operation='ListObjectsV2')}"
        )


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
            platform="aws_cloudtrail",
            display_name="AWS CloudTrail",
            category="Cloud-audit",
            description="AWS CloudTrail — eventos de API/management (polling do bucket S3).",
            auth_setup="aws_assume_role",
            icon_id="aws",
            docs_url="https://docs.aws.amazon.com/awscloudtrail/latest/userguide/cloudtrail-event-reference-record-contents.html",
            order=50,
            test_fn=_cloudtrail_probe,
            required_secrets=("secret_access_key",),
            capabilities=frozenset({"catalog", "auth:test", "collect:events"}),
            auth_fields=(
                AuthField(key="client_id", label="ARN do papel ou Access Key ID", type="string", required=True,
                          help_text="Recomendado: o ARN do papel criado pelo template "
                                    "(arn:aws:iam::<conta>:role/CentralOpsReadOnly). "
                                    "Access Key ID de usuário IAM ainda é aceito."),
                AuthField(key="secret_access_key", label="Secret Access Key", type="secret", required=False,
                          help_text="Só com Access Key ID. Deixe vazio ao usar papel."),
                AuthField(key="base_url", label="Bucket S3", type="string", required=True,
                          help_text="Nome do bucket onde o CloudTrail entrega os logs"),
                AuthField(key="region", label="Região AWS", type="string", required=True,
                          help_text="Ex: us-east-1"),
                AuthField(key="tenant_id", label="Account ID", type="string", required=False,
                          help_text="Conta dona dos logs (compõe AWSLogs/<conta>/CloudTrail/). "
                                    "Com papel, vazio = a conta do ARN."),
            ),
        )
    )

    register(
        CollectorRegistration(
            platform=AWSCloudTrailCollector.platform,
            stream=AWSCloudTrailCollector.stream,
            collector_cls=AWSCloudTrailCollector,
            refresh_fn=_cloudtrail_refresher,
            schedule=_td(minutes=5),
            queue=Q_BULK,
            task_name=T_COLLECT_BULK,
        )
    )


_register()
