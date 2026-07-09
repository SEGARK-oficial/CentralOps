"""Kind ``security_lake`` — Amazon Security Lake (OCSF Parquet).

Destino data-lake nativo OCSF da AWS. Diferente do sink ``s3`` (NDJSON cru), aqui
o lote vira **Parquet OCSF** (compressão zstd) gravado sob o layout de partição
que o Security Lake exige para uma *custom source*:

    ext/{source}/region={region}/accountId={account}/eventDay={YYYYMMDD}/{hash}.parquet

``eventDay`` deriva do campo OCSF ``time`` (epoch ms) do 1º evento — não do relógio
de envio — para que a partição seja estável (idempotência via ``{hash}``, sha1 dos
``event_id``s do lote → mesma key → sobrescreve, sem duplicar).

**Pré-requisito de deploy:** a *custom source* precisa estar registrada no
Security Lake (define o schema OCSF e provisiona o bucket/role). O CentralOps
ENVIA o Parquet OCSF-normalizado; o schema estrito é o da custom source.

**Mockabilidade (crítico):** ``pyarrow`` e ``aioboto3`` são importados TARDIAMENTE,
isolados em ``_to_parquet()`` e ``_client()``. O módulo importa SEM essas libs no
venv. Os testes sobrescrevem ambos os seams — nunca importam as libs reais.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, List, Literal, Mapping, Optional

from pydantic import BaseModel, Field

from ..base import DeliveryResult, RejectedEvent, TestResult
from .registry import DestinationConfig, DestinationRegistration, register
from .s3 import _classify_error, _event_id

logger = logging.getLogger(__name__)

KIND = "security_lake"

_NO_PYARROW_MSG = "pyarrow não instalado — instale: pip install -r requirements-sinks.txt"
_NO_BOTO_MSG = "aioboto3 não instalado — instale: pip install -r requirements-sinks.txt"


def _batch_hash(batch: List[Mapping[str, Any]]) -> str:
    """sha1 dos ``event_id``s do lote (ordem) — nome determinístico (idempotência)."""
    h = hashlib.sha1()
    for i, ev in enumerate(batch):
        h.update((_event_id(ev) or f"#{i}").encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


class SecurityLakeConfig(BaseModel):
    """Schema de config do destino Security Lake. Credencial NÃO aqui (secret_ref)."""

    bucket: str = Field(description="Bucket do Security Lake (aws-security-data-lake-<region>-…)")
    account_id: str = Field(description="AWS account id (partição accountId=)")
    region: str = Field(default="us-east-1", description="Região AWS do lake")
    source: str = Field(default="centralops", description="Nome da custom source (ext/<source>/)")
    access_key_id: Optional[str] = Field(default=None, description="AWS access key id (par da secret)")
    use_iam_role: bool = Field(default=False, description="Usar IAM role do host (sem credencial explícita)")
    compression: Literal["zstd", "snappy"] = Field(default="zstd", description="Compressão Parquet")


class SecurityLakeClient:
    """Cliente Security Lake (OCSF Parquet) — satisfaz o protocolo ``Destination``."""

    kind: str = KIND

    def __init__(
        self,
        bucket: str,
        *,
        account_id: str,
        region: str = "us-east-1",
        source: str = "centralops",
        access_key_id: Optional[str] = None,
        secret_access_key: Optional[str] = None,
        use_iam_role: bool = False,
        compression: str = "zstd",
    ) -> None:
        self._bucket = bucket
        self._account_id = account_id
        self._region = region
        self._source = (source or "centralops").strip("/")
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._use_iam_role = use_iam_role
        self._compression = compression if compression in ("zstd", "snappy") else "zstd"

    # ── Credencial / SDK (seams mockáveis) ───────────────────────────────

    def _has_credentials(self) -> bool:
        if self._use_iam_role:
            return True
        return bool(self._access_key_id and self._secret_access_key)

    def _client(self) -> Any:
        """Async CM do client S3 (``aioboto3`` lazy). Testes sobrescrevem."""
        try:
            import aioboto3  # noqa: PLC0415 — import tardio (mockabilidade)
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(_NO_BOTO_MSG) from exc
        session = aioboto3.Session()
        kwargs: dict[str, Any] = {"region_name": self._region}
        if not self._use_iam_role:
            kwargs["aws_access_key_id"] = self._access_key_id
            kwargs["aws_secret_access_key"] = self._secret_access_key
        return session.client("s3", **kwargs)

    def _to_parquet(self, rows: List[dict]) -> bytes:
        """Serializa as linhas OCSF como Parquet (``pyarrow`` lazy). Testes sobrescrevem.

        Schema inferido do lote (eventos da mesma classe OCSF → shape consistente).
        O schema estrito é o da custom source registrada no Security Lake.
        """
        try:
            import pyarrow as pa  # noqa: PLC0415 — import tardio (mockabilidade)
            import pyarrow.parquet as pq  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(_NO_PYARROW_MSG) from exc
        import io

        table = pa.Table.from_pylist(rows)
        buf = io.BytesIO()
        pq.write_table(table, buf, compression=self._compression)
        return buf.getvalue()

    # ── Formatação / key ─────────────────────────────────────────────────

    def format(self, envelope: Mapping[str, Any]) -> dict:
        """Canônico → linha OCSF (o ``normalized``; fallback ao envelope inteiro)."""
        norm = envelope.get("normalized")
        return dict(norm) if norm not in (None, {}) else dict(envelope)

    @staticmethod
    def _event_day(rows: List[dict]) -> str:
        """``eventDay`` (YYYYMMDD UTC) do campo OCSF ``time`` (epoch ms) do 1º evento."""
        for row in rows:
            raw = row.get("time")
            if isinstance(raw, (int, float)) and raw > 0:
                # OCSF time = epoch ms; heurística: > 1e12 → ms, senão s.
                secs = raw / 1000.0 if raw > 1_000_000_000_000 else float(raw)
                try:
                    return datetime.fromtimestamp(secs, tz=timezone.utc).strftime("%Y%m%d")
                except (ValueError, OSError, OverflowError):
                    continue
        return datetime.now(timezone.utc).strftime("%Y%m%d")

    def _object_key(self, batch: List[Mapping[str, Any]], rows: List[dict]) -> str:
        """Key de partição do Security Lake (determinística via batch_hash)."""
        return (
            f"ext/{self._source}/region={self._region}/accountId={self._account_id}/"
            f"eventDay={self._event_day(rows)}/{_batch_hash(batch)}.parquet"
        )

    def _reject_all(self, batch: List[Mapping[str, Any]], reason: str,
                    kind: str, retryable: bool) -> DeliveryResult:
        return DeliveryResult(
            accepted=0,
            rejected=[RejectedEvent(event_id=_event_id(ev) or "?", reason=reason,
                                    error_kind=kind, retryable=retryable) for ev in batch],
            retryable=retryable,
        )

    # ── Entrega ──────────────────────────────────────────────────────────

    async def send_batch(self, batch: List[Mapping[str, Any]]) -> DeliveryResult:
        if not batch:
            return DeliveryResult.ok(0)
        if not self._has_credentials():
            return self._reject_all(
                batch, "sem credencial AWS (secret_ref ausente e use_iam_role=False)",
                "auth", False)

        rows = [self.format(ev) for ev in batch]
        try:
            body = self._to_parquet(rows)
        except RuntimeError as exc:  # pyarrow ausente — descritivo, não-retryable
            return self._reject_all(batch, str(exc), "unknown", False)
        except Exception as exc:  # noqa: BLE001 — schema heterogêneo → rejeição determinística
            logger.warning("security_lake: falha ao serializar Parquet: %s", exc)
            return self._reject_all(batch, f"parquet: {exc}", "schema_rejected", False)

        key = self._object_key(batch, rows)
        try:
            async with self._client() as s3:
                await s3.put_object(
                    Bucket=self._bucket, Key=key, Body=body,
                    ContentType="application/vnd.apache.parquet",
                )
            return DeliveryResult.ok(len(batch))
        except RuntimeError as exc:  # SDK ausente
            return self._reject_all(batch, str(exc), "unknown", False)
        except Exception as exc:  # noqa: BLE001
            kind, retryable = _classify_error(exc)
            if retryable:
                logger.warning("security_lake.send_batch: transitório: %s — retryable", exc)
                return DeliveryResult(accepted=0, retryable=True)
            logger.warning("security_lake.send_batch: determinístico (%s): %s", kind, exc)
            return self._reject_all(batch, f"{kind}: {exc}", kind, False)

    async def test(self) -> TestResult:
        if not self._has_credentials():
            return TestResult.failed(
                "sem credencial AWS: configure secret_ref ou use_iam_role=True")
        import time

        started = time.monotonic()
        try:
            async with self._client() as s3:
                await s3.head_bucket(Bucket=self._bucket)
            elapsed_ms = (time.monotonic() - started) * 1000.0
            return TestResult.passed(f"bucket acessível: {self._bucket}", latency_ms=elapsed_ms)
        except RuntimeError as exc:
            return TestResult.failed(str(exc))
        except Exception as exc:  # noqa: BLE001
            kind, _ = _classify_error(exc)
            return TestResult.failed(f"head_bucket falhou ({kind}): {exc}")

    async def close(self) -> None:
        """Sem estado persistente (client per-call) — no-op."""
        return None


def _factory(config: DestinationConfig, secrets: Optional[Any] = None) -> SecurityLakeClient:
    cfg = SecurityLakeConfig(**dict(config.config or {}))
    secret: Optional[str] = None
    if secrets is not None and config.secret_ref:
        try:
            secret = secrets.decrypt(config.secret_ref)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "security_lake: falha ao decifrar secret_ref (%s) — dormant",
                type(exc).__name__)
    return SecurityLakeClient(
        bucket=cfg.bucket, account_id=cfg.account_id, region=cfg.region, source=cfg.source,
        access_key_id=cfg.access_key_id, secret_access_key=secret,
        use_iam_role=cfg.use_iam_role, compression=cfg.compression,
    )


register(
    DestinationRegistration(
        kind=KIND,
        factory=_factory,
        config_schema=SecurityLakeConfig,
        default_queue="dispatch.security_lake",
        # batch_hash determinístico = idempotente; lago frio OCSF nativo.
        capabilities=frozenset({"tls", "batch", "test", "idempotent"}),
        required_secrets=("aws_secret_access_key",),
        label="Amazon Security Lake (OCSF Parquet)",
        delivery_defaults={"tier": "cold"},
        # Campos de catálogo self-describing (galeria de destinos).
        category="Object Storage",
        icon_id="amazonsecuritylake",
        tier="beta",
        order=80,
        description="Amazon Security Lake — eventos OCSF em Parquet sobre S3.",
    )
)
