"""Cliente HTTP do ClickHouse — destino ``clickhouse`` (ingestão analítica).

Envia eventos em lote para o ClickHouse via interface HTTP usando
``INSERT INTO <db>.<table> FORMAT JSONEachRow`` com corpo NDJSON (um objeto
JSON por linha). É o caminho de ingestão padrão e mais portável do ClickHouse
(funciona em ClickHouse OSS, ClickHouse Cloud e forks compatíveis).

Referência: https://clickhouse.com/docs/en/interfaces/http

**Autenticação.** Header ``X-ClickHouse-User`` + ``X-ClickHouse-Key`` (senha do
usuário) — preferidos sobre basic-auth porque não vazam credencial em logs de
proxy que registram a linha de ``Authorization``. A senha fica em ``secret_ref``
(cofre), nunca na config.

**Robustez de schema.** Por padrão liga ``input_format_skip_unknown_fields=1``
(campos do envelope que não existem na tabela são ignorados em vez de derrubar o
INSERT inteiro) — comportamento *enterprise* esperado quando o envelope canônico
evolui mais rápido que o DDL da tabela. Configurável via ``skip_unknown_fields``.

**Modelo de entrega.** O HTTP do ClickHouse responde por LOTE (all-or-nothing):
não há resultado por item como no ``_bulk`` do Elastic. Classificação:
``200`` → aceito; ``401/403`` → ``auth``; demais ``4xx`` (parse/DDL) →
``schema_rejected`` não-retryable (→ DLQ); ``429/5xx``/timeout/conexão →
``retryable`` (retry com backoff). Nunca levanta — sempre devolve ``DeliveryResult``.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from typing import Any, List, Mapping, Optional

import aiohttp

from .base import DeliveryResult, RejectedEvent, TestResult

logger = logging.getLogger(__name__)

# Status HTTP que indicam erro transitório (retry com backoff).
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ClickHouseClient:
    """Cliente de ingestão ClickHouse via HTTP ``JSONEachRow``.

    Satisfaz o protocolo ``Destination`` diretamente (``kind``/``format``/
    ``send_batch``/``test``/``close``). Sessão ``aiohttp`` persistente, criada
    lazily no primeiro envio.
    """

    kind: str = "clickhouse"

    def __init__(
        self,
        url: str,
        password: Optional[str],
        *,
        database: str = "default",
        table: str = "centralops_events",
        username: str = "default",
        skip_unknown_fields: bool = True,
        async_insert: bool = False,
        verify_tls: bool = True,
        ca_bundle: Optional[str] = None,
    ) -> None:
        self._base = url.rstrip("/")
        self._password = password
        self._database = database
        self._table = table
        self._username = username
        self._skip_unknown_fields = skip_unknown_fields
        self._async_insert = async_insert
        self._verify_tls = verify_tls
        self._ca_bundle = ca_bundle
        self._session: Optional[aiohttp.ClientSession] = None

    # ── Wire ──────────────────────────────────────────────────────────────
    def _insert_query(self) -> str:
        """``INSERT INTO <db>.<table> FORMAT JSONEachRow`` (identificadores citados)."""
        # Citação com crase protege nomes reservados/maiúsculos; o backtick é
        # escapado por duplicação (defesa contra um nome de tabela hostil que
        # venha da config — embora a config seja de admin, é cinto+suspensório).
        db = self._database.replace("`", "``")
        tbl = self._table.replace("`", "``")
        return f"INSERT INTO `{db}`.`{tbl}` FORMAT JSONEachRow"

    def _endpoint(self) -> str:
        params = {"query": self._insert_query()}
        if self._skip_unknown_fields:
            params["input_format_skip_unknown_fields"] = "1"
        if self._async_insert:
            # async_insert + wait_for_async_insert=1: o servidor agrupa micro-lotes
            # mas a resposta só volta quando persistido (mantém a semântica de
            # confirmação de entrega — sem "aceitei mas perdi").
            params["async_insert"] = "1"
            params["wait_for_async_insert"] = "1"
        return f"{self._base}/?{urllib.parse.urlencode(params)}"

    def format(self, envelope: Mapping[str, Any]) -> dict:
        """Canônico → linha ``JSONEachRow``. O envelope canônico é a própria linha;
        a tabela alvo deve ter colunas correspondentes (ou ``skip_unknown_fields``)."""
        return dict(envelope)

    def _serialize(self, batch: List[Mapping[str, Any]]) -> str:
        return "\n".join(
            json.dumps(self.format(ev), separators=(",", ":"), default=str, ensure_ascii=False)
            for ev in batch
        )

    @staticmethod
    def _event_id(event: Mapping[str, Any]) -> str:
        meta = event.get("_centralops") or {}
        return str(meta.get("event_id") or "?")

    # ── Sessão ────────────────────────────────────────────────────────────
    def _build_ssl(self) -> Any:
        if not self._verify_tls:
            return False
        if self._ca_bundle:
            import ssl

            ctx = ssl.create_default_context(cafile=self._ca_bundle)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            return ctx
        return True

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {"Content-Type": "application/x-ndjson", "X-ClickHouse-User": self._username}
            if self._password:
                headers["X-ClickHouse-Key"] = self._password
            connector = aiohttp.TCPConnector(ssl=self._build_ssl())
            self._session = aiohttp.ClientSession(headers=headers, connector=connector)
        return self._session

    # ── Entrega ───────────────────────────────────────────────────────────
    async def send_batch(self, batch: List[Mapping[str, Any]]) -> DeliveryResult:
        if not batch:
            return DeliveryResult.ok(0)

        # Serialização DENTRO do try: um envelope não-serializável (ciclo, tipo
        # exótico) vira rejeição estruturada — o contrato é "nunca levanta".
        session = self._get_session()
        try:
            payload = self._serialize(batch)
            async with session.post(self._endpoint(), data=payload.encode("utf-8")) as resp:
                status = resp.status
                if status in _RETRYABLE_STATUS:
                    logger.warning("clickhouse: status transitório %s — retryable", status)
                    return DeliveryResult(accepted=0, retryable=True)
                if 200 <= status < 300:
                    return DeliveryResult.ok(len(batch))

                # 4xx determinístico: parse/DDL (schema_rejected) ou auth.
                body = ""
                try:
                    body = (await resp.text())[:500]
                except Exception:  # pragma: no cover — corpo ilegível
                    body = f"HTTP {status}"
                error_kind = "auth" if status in {401, 403} else "schema_rejected"
                logger.warning(
                    "clickhouse: rejeição de lote status=%s kind=%s detail=%r",
                    status, error_kind, body,
                )
                return DeliveryResult(
                    accepted=0,
                    rejected=[
                        RejectedEvent(
                            event_id=self._event_id(ev),
                            reason=body or f"HTTP {status}",
                            error_kind=error_kind,
                            retryable=False,
                        )
                        for ev in batch
                    ],
                    retryable=False,
                )
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            logger.warning("clickhouse: erro de conexão transitório: %s", exc)
            return DeliveryResult(accepted=0, retryable=True)
        except (TypeError, ValueError) as exc:
            # Falha de serialização (envelope não-JSON): rejeição determinística.
            logger.warning("clickhouse: evento não-serializável (%s) — schema_rejected", type(exc).__name__)
            return DeliveryResult(
                accepted=0,
                rejected=[
                    RejectedEvent(event_id=self._event_id(ev), reason="serialização falhou", error_kind="schema_rejected", retryable=False)
                    for ev in batch
                ],
                retryable=False,
            )

    async def test(self) -> TestResult:
        """Probe: ``SELECT 1`` via HTTP. ``200`` → ok; ``401/403`` → credencial
        inválida; conexão/timeout → falha de rede. Nunca levanta."""
        endpoint = f"{self._base}/?{urllib.parse.urlencode({'query': 'SELECT 1'})}"
        session = self._get_session()
        try:
            async with session.get(endpoint) as resp:
                if resp.status in {401, 403}:
                    return TestResult.failed("credencial ClickHouse inválida (401/403)")
                if 200 <= resp.status < 300:
                    return TestResult.passed(f"ClickHouse ok: {self._base}")
                detail = ""
                try:
                    detail = (await resp.text())[:300]
                except Exception:  # pragma: no cover
                    detail = f"HTTP {resp.status}"
                return TestResult.failed(f"ClickHouse respondeu status={resp.status}: {detail!r}")
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            return TestResult.failed(f"erro de conexão ao ClickHouse: {exc}")

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            try:
                await self._session.close()
            except Exception:  # pragma: no cover — best-effort
                logger.exception("clickhouse: erro ao fechar sessão")
            finally:
                self._session = None
