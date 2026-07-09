"""Lake provider — search-in-place no S3/lake.

Consulta os objetos que os sinks JÁ escreveram no S3 — "Search Then
Forward" do Cribl, sem reingerir. Lê via ``run_query`` (ponto canônico) um **FILTRO
ESTRUTURADO** (NÃO SQL livre — evita injeção/scope-creep) e devolve as linhas que
casam, com LIMIT estrito (anti-OOM) e leitura por-arquivo incremental.

**Statement** = JSON ``{"filters": [{"field","op","value"}], "limit": N}`` (mesmas
primitivas do motor de correlação). Janela ``from_ts..to_ts`` recorta as partições
de data (não varre o bucket inteiro).

**Isolamento:**
- ``s3_ndjson``: a partição é ``{prefix}/org={org_id}/YYYY/MM/DD/`` — o provider só
  lista o ``org={integration.organization_id}``, então NÃO há leitura cross-tenant.
- ``security_lake_parquet``: ``ext/{source}/region=/accountId=/eventDay=/`` — a
  integração (org-owned) declara source/account/region da PRÓPRIA org.

Seams mockáveis (testes patcham; nunca tocam o S3 real): ``_s3_client``,
``_iter_objects``, ``_get_bytes``, ``_read_parquet``.
"""

from __future__ import annotations

import gzip
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

from ..base import BaseProvider, HealthResult, QueryResult
from ..errors import ProviderConfigurationError, ProviderError, ProviderQueryError
from ...core.config import settings
from ...services import integration_secrets
from ...services.correlation_engine import matches_where

logger = logging.getLogger(__name__)

_MAX_DAYS = 92  # teto de partições de data varridas por query (anti-scan total)


class LakeProvider(BaseProvider):
    platform = "lake"

    def capabilities(self) -> List[str]:
        return ["health:check"] + self._query_capability_keys()

    # ── config ───────────────────────────────────────────────────────────
    def _config(self) -> dict:
        try:
            return json.loads(self.integration.config_json or "{}")
        except (ValueError, TypeError):
            return {}

    def _conn(self) -> dict:
        cfg = self._config()
        bucket = (self.integration.base_url or "").strip()
        if not bucket:
            raise ProviderConfigurationError("lake sem bucket (base_url)", code="LAKE_NO_BUCKET")
        return {
            "bucket": bucket,
            "region": (self.integration.region or "us-east-1").strip(),
            "access_key_id": (self.integration.client_id or "").strip(),
            "secret_access_key": integration_secrets.read_secret(self.integration, "secret_access_key") or "",
            "account_id": (self.integration.tenant_id or "").strip(),
            "layout": (cfg.get("layout") or "s3_ndjson").strip(),
            "prefix": (cfg.get("prefix") or "centralops").strip("/"),
            "source": (cfg.get("source") or "centralops").strip("/"),
        }

    # ── seams (mockáveis) ─────────────────────────────────────────────────
    def _s3_client(self, conn: dict):
        import boto3  # import tardio — só quem usa lake puxa o SDK

        return boto3.client(
            "s3",
            aws_access_key_id=conn["access_key_id"] or None,
            aws_secret_access_key=conn["secret_access_key"] or None,
            region_name=conn["region"],
        )

    def _iter_objects(self, client, bucket: str, prefix: str):
        """GERADOR de ``(key, size)`` sob o prefixo. Streaming: o caller pode parar
        cedo (ao bater o LIMIT) e a paginação para junto — NÃO materializa milhões de
        keys na memória (anti-OOM do plano de metadados). Cap por prefixo + ``size``
        permite pular objetos gigantes ANTES de baixá-los."""
        token: Optional[str] = None
        yielded = 0
        cap = settings.QUERY_LAKE_MAX_KEYS_PER_PREFIX
        while True:
            kw = {"Bucket": bucket, "Prefix": prefix}
            if token:
                kw["ContinuationToken"] = token
            resp = client.list_objects_v2(**kw)
            for obj in resp.get("Contents", []) or []:
                k = obj.get("Key")
                if not k:
                    continue
                yield k, int(obj.get("Size") or 0)
                yielded += 1
                if yielded >= cap:
                    logger.warning("lake: partição %s excede %d objetos — truncando listagem", prefix, cap)
                    return
            if not resp.get("IsTruncated"):
                return
            token = resp.get("NextContinuationToken")
            if not token:
                return

    def _get_bytes(self, client, bucket: str, key: str) -> bytes:
        return client.get_object(Bucket=bucket, Key=key)["Body"].read()

    def _read_parquet(self, raw: bytes) -> List[dict]:
        import io

        import pyarrow.parquet as pq  # import tardio (dep de requirements-sinks)

        return pq.read_table(io.BytesIO(raw)).to_pylist()

    # ── leitura por layout ────────────────────────────────────────────────
    def _gunzip_capped(self, raw: bytes, key: str) -> bytes:
        """Descomprime com TETO (anti gzip-bomb): um .gz pequeno pode expandir p/ GBs."""
        import io

        cap = settings.QUERY_LAKE_MAX_OBJECT_BYTES
        out = io.BytesIO()
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                if out.tell() > cap:
                    raise ProviderQueryError(
                        f"lake: objeto {key} descomprime acima do teto (gzip-bomb?)",
                        code="LAKE_OBJECT_TOO_LARGE",
                    )
        return out.getvalue()

    def _read_ndjson(self, raw: bytes, key: str) -> List[dict]:
        if key.endswith(".gz"):
            raw = self._gunzip_capped(raw, key)
        out: List[dict] = []
        for line in raw.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
        return out

    def _day_prefixes(self, conn: dict, start: datetime, end: datetime) -> List[str]:
        """Prefixos de partição por dia em [start, end] (UTC). Cap de _MAX_DAYS."""
        d = start.date()
        last = end.date()
        prefixes: List[str] = []
        org_id = self.integration.organization_id
        while d <= last and len(prefixes) < _MAX_DAYS:
            if conn["layout"] == "security_lake_parquet":
                prefixes.append(
                    f"ext/{conn['source']}/region={conn['region']}/"
                    f"accountId={conn['account_id']}/eventDay={d.strftime('%Y%m%d')}/"
                )
            else:  # s3_ndjson — org NA partição (isolamento forte)
                prefixes.append(
                    f"{conn['prefix']}/org={org_id}/{d:%Y}/{d:%m}/{d:%d}/"
                )
            d += timedelta(days=1)
        return prefixes

    # ── contrato ─────────────────────────────────────────────────────────
    def test_connection(self) -> HealthResult:
        try:
            conn = self._conn()
            client = self._s3_client(conn)
            client.list_objects_v2(Bucket=conn["bucket"], MaxKeys=1)
            return HealthResult(status="healthy", details={"bucket": conn["bucket"], "layout": conn["layout"]})
        except Exception as exc:  # pragma: no cover — convertido em status
            return HealthResult(status="error", details={"error": str(exc)})

    def health_check(self) -> HealthResult:
        return self.test_connection()

    def run_query(self, statement: str, from_ts: str, to_ts: str, **kwargs) -> QueryResult:
        # 1. parse do filtro estruturado (NÃO SQL — sem injeção)
        try:
            spec = json.loads(statement) if (statement or "").strip() else {}
        except ValueError as exc:
            raise ProviderQueryError(
                "lake: statement deve ser JSON {filters:[{field,op,value}], limit}",
                code="LAKE_BAD_STATEMENT",
            ) from exc
        if not isinstance(spec, dict):
            raise ProviderQueryError("lake: statement inválido", code="LAKE_BAD_STATEMENT")
        filters = spec.get("filters") or []
        cap = settings.QUERY_LAKE_MAX_ROWS
        limit = min(int(spec.get("limit") or kwargs.get("limit") or cap), cap)

        start = _parse_iso(from_ts)
        end = _parse_iso(to_ts)
        if start is None or end is None:
            raise ProviderQueryError("lake: from_ts/to_ts inválidos", code="LAKE_BAD_WINDOW")

        conn = self._conn()
        max_obj = settings.QUERY_LAKE_MAX_OBJECT_BYTES
        items: List[dict] = []
        try:
            client = self._s3_client(conn)
            for prefix in self._day_prefixes(conn, start, end):
                if len(items) >= limit:
                    break
                for key, size in self._iter_objects(client, conn["bucket"], prefix):
                    if len(items) >= limit:
                        break
                    # Anti-OOM: objeto gigante é PULADO antes de baixar (o LIMIT só
                    # corta linhas DEPOIS — não protege de um objeto multi-GB).
                    if size > max_obj:
                        logger.warning("lake: objeto %s (%d bytes) excede o teto — pulando", key, size)
                        continue
                    # Resiliente por-objeto: um arquivo corrompido/grande não derruba
                    # a query inteira (loga e segue).
                    try:
                        raw = self._get_bytes(client, conn["bucket"], key)
                        rows = (
                            self._read_parquet(raw)
                            if conn["layout"] == "security_lake_parquet"
                            else self._read_ndjson(raw, key)
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("lake: falha lendo %s — pulando: %s", key, exc)
                        continue
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        if filters and not matches_where(row, filters):
                            continue
                        items.append(row)
                        if len(items) >= limit:
                            break
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ProviderQueryError(f"lake: erro lendo S3: {exc}", code="LAKE_READ_ERROR") from exc

        logger.info("lake query bucket=%s layout=%s itens=%d (cap=%d)",
                    conn["bucket"], conn["layout"], len(items), limit)
        return QueryResult(items=items[:limit], total=len(items[:limit]))


def _parse_iso(value: str) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
