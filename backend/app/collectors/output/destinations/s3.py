"""Kind ``s3`` — destino object-store / data-lake NDJSON.

O sink "lago frio": grava o lote como UM objeto NDJSON (opcionalmente gzip) em
um bucket S3 (ou S3-compatível: MinIO, Ceph, Wasabi, R2…) sob uma key
DETERMINÍSTICA derivada do hash dos ``event_id``s do lote.

**Idempotência (honesta):** a key é determinística para um lote *idêntico* —
uma reentrega do MESMO lote (mesmos event_ids, mesma ordem) recalcula a MESMA
key e SOBRESCREVE o mesmo objeto, sem duplicar. PORÉM, uma reentrega
PARCIAL (ex.: drain por-evento da DLQ) forma um lote diferente → batch_hash
diferente → key diferente → objeto NOVO. Isso DUPLICA objetos (não há perda; a
deduplicação é feita na camada de leitura/query por ``event_id``).

Layout de partição (Hive-style, amigável a Athena/Trino/Spark):

    {prefix}/org={oid}/{YYYY}/{MM}/{DD}/{batch_hash}.ndjson[.gz]

A credencial **não** está na config: ``secret_access_key`` vem de ``secret_ref``
(cofre). Com ``use_iam_role=True`` nenhuma credencial explícita é usada — o SDK
resolve o IAM role do host (instance profile / IRSA / task role). Sem credencial
nem IAM role, ``send_batch``/``test`` falham de forma descritiva (dormant).

**Mockabilidade (crítico):** ``aioboto3`` é importado TARDIAMENTE, isolado em
``_client()``. O módulo importa SEM ``aioboto3`` no venv; ausente em runtime →
falha descritiva ("instale aioboto3"). Os testes monkeypatcham ``_client`` com um
fake async client — nunca importam ``aioboto3``.

Capacidades adicionais (tiering / LGPD):
  - ``prune_expired(retention_days)`` — apaga objetos mais antigos que a retenção
    do tier "cold" (enforcement de retenção).
  - ``erase_by_org(organization_id)`` — apaga todos os objetos da org
    (right-to-erasure).
"""

from __future__ import annotations

import gzip
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, List, Literal, Mapping, Optional

from pydantic import BaseModel, Field

from ..base import DeliveryResult, ErasureResult, RejectedEvent, TestResult
from .registry import DestinationConfig, DestinationRegistration, register

logger = logging.getLogger(__name__)

KIND = "s3"

# Mensagem única quando o SDK não está instalado no host.
_NO_SDK_MSG = "aioboto3 não instalado — instale: pip install -r requirements-sinks.txt"

# Códigos de erro S3 (4xx determinístico) que indicam credencial/permissão.
_AUTH_ERROR_CODES = frozenset(
    {
        "AccessDenied",
        "InvalidAccessKeyId",
        "SignatureDoesNotMatch",
        "InvalidToken",
        "ExpiredToken",
        "AccountProblem",
        "AllAccessDisabled",
    }
)


def _event_id(envelope: Mapping[str, Any]) -> Optional[str]:
    """``event_id`` do namespace ``_centralops`` (componente do batch_hash)."""
    meta = envelope.get("_centralops") or {}
    ev = meta.get("event_id")
    return str(ev) if ev else None


def _org_id(envelope: Mapping[str, Any]) -> str:
    """``organization_id`` do envelope, ou ``"global"`` quando ausente."""
    meta = envelope.get("_centralops") or {}
    oid = meta.get("organization_id")
    return str(oid) if oid not in (None, "") else "global"


def _batch_hash(batch: List[Mapping[str, Any]]) -> str:
    """sha1 dos ``event_id``s do lote (na ordem) — chave de idempotência.

    Eventos sem ``event_id`` contribuem com o índice posicional para preservar
    determinismo sem colidir lotes distintos do mesmo tamanho.
    """
    h = hashlib.sha1()
    for i, ev in enumerate(batch):
        eid = _event_id(ev) or f"#{i}"
        h.update(eid.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


class S3Config(BaseModel):
    """Schema de config do destino S3 / object store.

    A credencial **não** está aqui: ``secret_access_key`` vem de ``secret_ref``
    (cofre). ``access_key_id`` é público (par da secret). Com ``use_iam_role`` o
    SDK resolve o role do host e nenhuma credencial explícita é passada.
    """

    bucket: str = Field(description="Nome do bucket de destino")
    region: str = Field(default="us-east-1", description="Região AWS do bucket")
    prefix: str = Field(
        default="centralops",
        description="Prefixo de key (raiz do layout de partição)",
    )
    endpoint_url: Optional[str] = Field(
        default=None,
        description="Endpoint S3-compatível (MinIO/Ceph/R2…); None = AWS S3",
    )
    access_key_id: Optional[str] = Field(
        default=None,
        description="AWS access key id (par público da secret); None com use_iam_role",
    )
    compression: Literal["none", "gzip"] = Field(
        default="gzip",
        description="Compressão do objeto NDJSON (gzip recomendado p/ lago frio)",
    )
    use_iam_role: bool = Field(
        default=False,
        description="Usar o IAM role do host (sem credencial explícita)",
    )


class S3Client:
    """Cliente S3 / object store NDJSON.

    Satisfaz o protocolo ``Destination`` diretamente: define ``kind``,
    ``format``, ``send_batch``, ``test`` e ``close``; mais ``prune_expired`` e
    ``erase_by_org`` (tiering / LGPD).

    O ``aioboto3`` é importado lazy em ``_client()`` — um async context manager
    que devolve o client S3. Os testes sobrescrevem ``_client`` com um fake.
    """

    kind: str = "s3"

    def __init__(
        self,
        bucket: str,
        *,
        region: str = "us-east-1",
        prefix: str = "centralops",
        endpoint_url: Optional[str] = None,
        access_key_id: Optional[str] = None,
        secret_access_key: Optional[str] = None,
        compression: str = "gzip",
        use_iam_role: bool = False,
    ) -> None:
        self._bucket = bucket
        self._region = region
        # Normaliza o prefixo: sem "/" nas pontas para uma key previsível.
        self._prefix = (prefix or "centralops").strip("/")
        self._endpoint_url = endpoint_url
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._compression = compression if compression in ("none", "gzip") else "gzip"
        self._use_iam_role = use_iam_role

    # ── Credencial / SDK ─────────────────────────────────────────────────

    def _has_credentials(self) -> bool:
        """True quando há como autenticar: par explícito OU IAM role."""
        if self._use_iam_role:
            return True
        return bool(self._access_key_id and self._secret_access_key)

    def _client(self) -> Any:
        """Async context manager do client S3 (``aioboto3`` importado lazy).

        Ponto de override dos testes: um fake retorna aqui um async CM que
        captura as chamadas (``put_object``/``head_bucket``/``list_objects_v2``/
        ``delete_objects``). Em runtime, ausência de ``aioboto3`` levanta
        ``RuntimeError`` descritivo — capturado pelos callers.
        """
        try:
            import aioboto3  # noqa: PLC0415 — import tardio proposital (mockabilidade)
        except ImportError as exc:  # pragma: no cover — depende do ambiente
            raise RuntimeError(_NO_SDK_MSG) from exc

        session = aioboto3.Session()
        kwargs: dict[str, Any] = {"region_name": self._region}
        if self._endpoint_url:
            kwargs["endpoint_url"] = self._endpoint_url
        if not self._use_iam_role:
            kwargs["aws_access_key_id"] = self._access_key_id
            kwargs["aws_secret_access_key"] = self._secret_access_key
        return session.client("s3", **kwargs)

    # ── Formatação / serialização ────────────────────────────────────────

    def format(self, envelope: Mapping[str, Any]) -> dict:
        """Documento como será gravado (uma linha NDJSON) — o próprio envelope."""
        return dict(envelope)

    def _object_key(self, batch: List[Mapping[str, Any]]) -> str:
        """Key determinística do objeto: partição Hive + batch_hash.

        ``{prefix}/org={oid}/{YYYY}/{MM}/{DD}/{batch_hash}.ndjson[.gz]``

        A data de partição vem do primeiro evento (``_centralops`` UTC) — não do
        relógio de envio — para que uma reentrega caia na MESMA partição (a
        idempotência depende de key estável). Sem tempo no envelope, usa a data
        UTC corrente. ``oid`` vem do primeiro evento.
        """
        oid = _org_id(batch[0]) if batch else "global"
        when = self._partition_dt(batch[0]) if batch else datetime.now(timezone.utc)
        bh = _batch_hash(batch)
        ext = "ndjson.gz" if self._compression == "gzip" else "ndjson"
        return (
            f"{self._prefix}/org={oid}/"
            f"{when.year:04d}/{when.month:02d}/{when.day:02d}/"
            f"{bh}.{ext}"
        )

    @staticmethod
    def _partition_dt(envelope: Mapping[str, Any]) -> datetime:
        """Data de partição (UTC) — ``_centralops.received_at``/``timestamp`` se
        presente e parseável (ISO-8601), senão o relógio corrente."""
        meta = envelope.get("_centralops") or {}
        raw = meta.get("received_at") or meta.get("timestamp")
        if isinstance(raw, str) and raw:
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except ValueError:
                pass
        return datetime.now(timezone.utc)

    def _serialize(self, batch: List[Mapping[str, Any]]) -> bytes:
        """Serializa o lote como NDJSON (uma linha JSON por evento), gzip opcional."""
        from .._fastjson import dumps_bytes

        lines = b"\n".join(dumps_bytes(ev) for ev in batch)
        body = lines + b"\n" if lines else b""
        if self._compression == "gzip":
            # mtime=0 → bytes gzip determinísticos (idempotência byte-a-byte).
            return gzip.compress(body, mtime=0)
        return body

    # ── Entrega ──────────────────────────────────────────────────────────

    async def send_batch(self, batch: List[Mapping[str, Any]]) -> DeliveryResult:
        """Grava o lote como UM objeto NDJSON sob a key determinística.

        Sucesso → ``DeliveryResult.ok(len(batch))``. Erro de credencial/permissão
        (4xx) → rejected (``error_kind="auth"``, ``retryable=False``). 5xx/timeout/
        conexão → ``retryable=True`` (lote inteiro). Nunca levanta exceção.

        Idempotência: para o MESMO lote (mesmos event_ids/ordem), a key é estável
        e o objeto é sobrescrito (sem duplicar). Uma reentrega PARCIAL (lote
        diferente, ex.: drain por-evento da DLQ) gera key diferente → objeto novo
        → DUPLICAÇÃO (sem perda; dedupe por ``event_id`` na leitura).
        """
        if not batch:
            return DeliveryResult.ok(0)

        if not self._has_credentials():
            rejected = [
                RejectedEvent(
                    event_id=_event_id(ev) or "?",
                    reason="sem credencial S3 (secret_ref ausente e use_iam_role=False)",
                    error_kind="auth",
                    retryable=False,
                )
                for ev in batch
            ]
            return DeliveryResult(accepted=0, rejected=rejected, retryable=False)

        key = self._object_key(batch)
        body = self._serialize(batch)
        content_type = "application/x-ndjson"
        content_encoding = "gzip" if self._compression == "gzip" else None
        put_kwargs: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": key,
            "Body": body,
            "ContentType": content_type,
        }
        if content_encoding:
            put_kwargs["ContentEncoding"] = content_encoding

        try:
            async with self._client() as s3:
                await s3.put_object(**put_kwargs)
            return DeliveryResult.ok(len(batch))
        except RuntimeError as exc:
            # SDK ausente — não-retryable, falha descritiva por item.
            rejected = [
                RejectedEvent(
                    event_id=_event_id(ev) or "?",
                    reason=str(exc),
                    error_kind="unknown",
                    retryable=False,
                )
                for ev in batch
            ]
            return DeliveryResult(accepted=0, rejected=rejected, retryable=False)
        except Exception as exc:
            kind, retryable = _classify_error(exc)
            if retryable:
                logger.warning("s3.send_batch: erro transitório: %s — retryable", exc)
                return DeliveryResult(accepted=0, retryable=True)
            logger.warning("s3.send_batch: erro determinístico (%s): %s", kind, exc)
            rejected = [
                RejectedEvent(
                    event_id=_event_id(ev) or "?",
                    reason=f"{kind}: {exc}",
                    error_kind=kind,
                    retryable=False,
                )
                for ev in batch
            ]
            return DeliveryResult(accepted=0, rejected=rejected, retryable=False)

    async def test(self) -> TestResult:
        """Probe de conexão: ``head_bucket`` — mede latency_ms.

        Sem credencial nem IAM role → ``TestResult.failed`` descritivo. SDK
        ausente → idem. 4xx de auth/404 → failed; sucesso → passed.
        """
        if not self._has_credentials():
            return TestResult.failed(
                "sem credencial S3: configure secret_ref ou use_iam_role=True"
            )
        import time

        started = time.monotonic()
        try:
            async with self._client() as s3:
                await s3.head_bucket(Bucket=self._bucket)
            elapsed_ms = (time.monotonic() - started) * 1000.0
            return TestResult.passed(
                f"bucket acessível: {self._bucket}", latency_ms=elapsed_ms
            )
        except RuntimeError as exc:
            return TestResult.failed(str(exc))
        except Exception as exc:
            kind, _ = _classify_error(exc)
            return TestResult.failed(f"head_bucket falhou ({kind}): {exc}")

    # ── Tiering / LGPD ───────────────────────────────────────────────────

    async def prune_expired(self, retention_days: int) -> int:
        """Apaga objetos sob ``{prefix}/org=*`` mais antigos que ``now-retention``.

        Usado pelo tiering (enforcement de retenção do tier "cold"). Lista
        paginando ``list_objects_v2`` sob ``{prefix}/`` e deleta em lote
        (``delete_objects``) os objetos vencidos. Retorna a contagem de objetos
        deletados. ``retention_days<=0`` = sem retenção (no-op).

        **Fonte da verdade = data da KEY**, não ``LastModified``. A key codifica
        a data de partição (``.../YYYY/MM/DD/...``, derivada do ``received_at`` do
        evento). Usar ``LastModified`` (tempo do PUT) abriria buraco de compliance
        (LGPD): numa reentrega/sobrescrita o ``LastModified`` reseta p/ agora e o
        dado antigo escaparia da retenção. Por isso o corte usa a data extraída da
        key; ``LastModified`` é só FALLBACK quando a key não casa o padrão de data.

        **Propaga exceção** em falha real (SDK ausente, bucket inacessível,
        erro de list/delete) — NÃO engole como "0 deletados". O caller
        (``enforce_destination_retention``) isola por destino (marca -1 em
        exceção), então um sucesso(0) significa de fato "nada a podar".
        """
        if retention_days <= 0:
            return 0

        from datetime import timedelta

        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        list_prefix = f"{self._prefix}/org="
        deleted = 0
        async with self._client() as s3:
            token: Optional[str] = None
            while True:
                list_kwargs: dict[str, Any] = {
                    "Bucket": self._bucket,
                    "Prefix": list_prefix,
                }
                if token:
                    list_kwargs["ContinuationToken"] = token
                resp = await s3.list_objects_v2(**list_kwargs)
                contents = resp.get("Contents") or []
                stale = [
                    obj["Key"]
                    for obj in contents
                    if self._is_expired(obj.get("Key"), obj.get("LastModified"), cutoff)
                ]
                if stale:
                    await s3.delete_objects(
                        Bucket=self._bucket,
                        Delete={"Objects": [{"Key": k} for k in stale]},
                    )
                    deleted += len(stale)
                if resp.get("IsTruncated"):
                    token = resp.get("NextContinuationToken")
                    if not token:
                        break
                else:
                    break
        return deleted

    @staticmethod
    def _extract_partition_date(key: Any) -> Optional[datetime]:
        """Extrai a data de partição (``.../YYYY/MM/DD/...``) da key, UTC.

        A key segue ``{prefix}/org={oid}/{YYYY}/{MM}/{DD}/{hash}.ndjson[.gz]``.
        Procura, da direita p/ esquerda, a primeira tripla de segmentos
        numéricos YYYY/MM/DD com faixa válida (ano 1970–9999, mês 1–12, dia
        1–31). Retorna ``None`` quando a key não casa o padrão (não-derivável).
        """
        if not isinstance(key, str) or not key:
            return None
        parts = key.split("/")
        # Precisa de pelo menos .../YYYY/MM/DD/file → varre janelas de 3.
        for i in range(len(parts) - 3, -1, -1):
            y, m, d = parts[i], parts[i + 1], parts[i + 2]
            if not (len(y) == 4 and len(m) == 2 and len(d) == 2):
                continue
            if not (y.isdigit() and m.isdigit() and d.isdigit()):
                continue
            year, month, day = int(y), int(m), int(d)
            if not (1970 <= year <= 9999 and 1 <= month <= 12 and 1 <= day <= 31):
                continue
            try:
                return datetime(year, month, day, tzinfo=timezone.utc)
            except ValueError:
                continue
        return None

    @classmethod
    def _is_expired(cls, key: Any, last_modified: Any, cutoff: datetime) -> bool:
        """True quando o objeto está vencido em relação a ``cutoff``.

        Fonte da verdade = data da KEY (``received_at`` do evento). ``LastModified``
        (tempo do PUT) só vale como FALLBACK quando a key não codifica data —
        senão uma sobrescrita/reentrega resetaria o relógio e o dado antigo
        escaparia da retenção (LGPD).
        """
        partition_dt = cls._extract_partition_date(key)
        if partition_dt is not None:
            return partition_dt < cutoff
        return _is_before(last_modified, cutoff)

    async def erase_by_org(self, organization_id: Any) -> ErasureResult:
        """Right-to-erasure: apaga todos os objetos da org.

        Deleta tudo sob ``{prefix}/org={oid}/``. Best-effort: nunca levanta —
        erros vão para ``ErasureResult.failed``/``detail``. Sem objetos = sucesso
        vazio (idempotente).
        """
        oid = str(organization_id) if organization_id not in (None, "") else "global"
        org_prefix = f"{self._prefix}/org={oid}/"
        erased: List[str] = []
        try:
            async with self._client() as s3:
                token: Optional[str] = None
                while True:
                    list_kwargs: dict[str, Any] = {
                        "Bucket": self._bucket,
                        "Prefix": org_prefix,
                    }
                    if token:
                        list_kwargs["ContinuationToken"] = token
                    resp = await s3.list_objects_v2(**list_kwargs)
                    contents = resp.get("Contents") or []
                    keys = [obj["Key"] for obj in contents if obj.get("Key")]
                    if keys:
                        await s3.delete_objects(
                            Bucket=self._bucket,
                            Delete={"Objects": [{"Key": k} for k in keys]},
                        )
                        erased.extend(keys)
                    if resp.get("IsTruncated"):
                        token = resp.get("NextContinuationToken")
                        if not token:
                            break
                    else:
                        break
        except Exception as exc:
            logger.warning("s3.erase_by_org: falha org=%s: %s", oid, exc)
            return ErasureResult.error(
                [f"org:{oid}"], detail=f"erase_by_org falhou: {exc}"
            )
        return ErasureResult.success(
            erased, detail=f"{len(erased)} objetos apagados para org={oid}"
        )

    async def close(self) -> None:
        """Sem estado persistente (client é per-call CM) — no-op."""
        return None


def _classify_error(exc: Exception) -> tuple[str, bool]:
    """Mapeia uma exceção do SDK para (error_kind, retryable).

    botocore.ClientError carrega ``response['Error']['Code']`` e
    ``ResponseMetadata.HTTPStatusCode``. 4xx de credencial → ("auth", False);
    outros 4xx → ("schema_rejected", False); 5xx/timeout/conexão → (kind, True).
    Sem botocore importável (mock), inspeciona atributos best-effort.
    """
    code: Optional[str] = None
    status: Optional[int] = None
    resp = getattr(exc, "response", None)
    if isinstance(resp, Mapping):
        err = resp.get("Error") or {}
        if isinstance(err, Mapping):
            code = err.get("Code")
        meta = resp.get("ResponseMetadata") or {}
        if isinstance(meta, Mapping):
            status = meta.get("HTTPStatusCode")

    if code in _AUTH_ERROR_CODES:
        return "auth", False
    if isinstance(status, int):
        if status in (401, 403):
            return "auth", False
        if 400 <= status < 500 and status not in (408, 429):
            return "schema_rejected", False
        # 5xx, 408 (timeout), 429 (throttle) → transitório.
        return "unknown", True

    # Erro de conexão / timeout sem status estruturado → transitório.
    name = type(exc).__name__.lower()
    if "timeout" in name or "connection" in name or "endpoint" in name:
        return "unknown", True
    # Default conservador: trata como transitório (retry, não DLQ silenciosa).
    return "unknown", True


def _is_before(last_modified: Any, cutoff: datetime) -> bool:
    """True quando ``last_modified`` (datetime tz-aware ou naive) < ``cutoff``."""
    if not isinstance(last_modified, datetime):
        return False
    lm = last_modified
    if lm.tzinfo is None:
        lm = lm.replace(tzinfo=timezone.utc)
    return lm < cutoff


def _factory(config: DestinationConfig, secrets: Optional[Any] = None) -> S3Client:
    """Constrói um ``S3Client`` a partir da config resolvida.

    A ``secret_access_key`` é decifrada via ``secrets.decrypt(config.secret_ref)``
    quando ambos presentes. Ausente (dormant) e ``use_iam_role=False`` →
    ``secret_access_key=None``; ``send_batch``/``test`` falham descritivamente sem
    levantar aqui (fail-closed controlado).
    """
    cfg = S3Config(**dict(config.config or {}))

    secret: Optional[str] = None
    if secrets is not None and config.secret_ref:
        try:
            secret = secrets.decrypt(config.secret_ref)
        except Exception as exc:
            # NÃO logar secret_ref nem a exceção: a mensagem do decrypt pode
            # vazar path da master key KMS / material sensível. Só o tipo.
            logger.warning(
                "s3: falha ao decifrar secret_ref (%s) — secret=None (dormant)",
                type(exc).__name__,
            )

    return S3Client(
        bucket=cfg.bucket,
        region=cfg.region,
        prefix=cfg.prefix,
        endpoint_url=cfg.endpoint_url,
        access_key_id=cfg.access_key_id,
        secret_access_key=secret,
        compression=cfg.compression,
        use_iam_role=cfg.use_iam_role,
    )


register(
    DestinationRegistration(
        kind=KIND,
        factory=_factory,
        config_schema=S3Config,
        default_queue="dispatch.s3",
        # Key determinística (batch_hash) = idempotente; object store suporta
        # erasure por prefixo de org (erasure_by_query) e enforcement de retenção.
        capabilities=frozenset(
            {"tls", "batch", "test", "idempotent", "erasure_by_query", "retention"}
        ),
        required_secrets=("aws_secret_access_key",),
        label="S3 / Object Store (NDJSON)",
        # Sink "lago frio" — tier cold por default (metadado de tiering).
        delivery_defaults={"tier": "cold"},
        # Campos de catálogo self-describing (galeria de destinos).
        category="Object Storage",
        icon_id="amazons3",
        tier="stable",
        order=70,
        description="Amazon S3 ou compatível — objetos NDJSON particionados (data lake / arquivo frio).",
    )
)
