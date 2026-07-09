"""Cliente Elasticsearch/OpenSearch ``_bulk``.

Envia eventos em lote via ``POST {url}/_bulk`` usando o protocolo NDJSON do
``_bulk`` (uma linha de ação + uma linha de documento, alternadas, com newline
final obrigatório). Funciona com **Elasticsearch e OpenSearch** (a API ``_bulk``
é compatível entre os dois).

É o destino "lago/SIEM" mais ubíquo do mercado e o
``_bulk`` devolve status **por item** (``items[].status``/``error``) — encaixe
NATIVO na falha-parcial-por-item: só o subconjunto rejeitado vira
``DeliveryResult.rejected``, o resto é aceito sem retry do lote inteiro.

**Idempotência:** usamos a ação ``create`` com ``_id = event_id`` — uma
reentrega do mesmo evento colide (HTTP 409 ``version_conflict_engine_exception``)
e é contada como ACEITA (já estava lá), não como erro. Sem ``event_id``, cai para
``index`` (upsert) sem ``_id`` — o cluster gera o id.

**Ativo quando há um destino kind=elastic_bulk configurado** (multi-destino é
GA). Sem credencial (``secret_ref`` ausente),
``send_batch``/``test`` falham de forma descritiva sem levantar — destino dormant.

Design de sessão: uma ``aiohttp.ClientSession`` persistente reutilizada entre
chamadas (espelha ``SplunkHecClient``), criada lazily e fechada em ``close``.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, List, Mapping, Optional

import aiohttp

from ._fastjson import dumps_str as _json_dumps
from .base import DeliveryResult, ErasureResult, RejectedEvent, TestResult
from .connection_pool import get_pooled_session

# Campo no documento indexado que armazena o organization_id do envelope canônico.
# O documento gravado é o envelope inteiro (format() = dict(envelope)), portanto
# _centralops.organization_id é o caminho de ponto no índice Elastic/OpenSearch.
_ORG_FIELD = "_centralops.organization_id"

logger = logging.getLogger(__name__)

# Status HTTP de erro transitório no nível do REQUEST inteiro (retry do lote).
_RETRYABLE_STATUS = {429, 502, 503, 504}
# Status de item que indicam erro transitório no nível do ITEM (→ retryable).
_RETRYABLE_ITEM_STATUS = {429, 503}
# version_conflict (create com _id já existente) = idempotência: já entregue.
_IDEMPOTENT_CONFLICT = 409


def _event_id(event: Mapping[str, Any]) -> Optional[str]:
    """event_id do namespace ``_centralops`` (chave de idempotência), ou None."""
    meta = event.get("_centralops") or {}
    ev = meta.get("event_id")
    return str(ev) if ev else None


def format_bulk_action(envelope: Mapping[str, Any], *, index: str) -> dict:
    """Linha de AÇÃO do ``_bulk`` para um envelope (canônico → wire dict).

    ``create`` + ``_id=event_id`` dá dedup idempotente; sem event_id, ``index``
    (o cluster gera o id). É a primeira das duas linhas NDJSON por evento.
    """
    ev = _event_id(envelope)
    if ev is not None:
        return {"create": {"_index": index, "_id": ev}}
    return {"index": {"_index": index}}


class ElasticBulkClient:
    """Cliente Elasticsearch/OpenSearch ``_bulk`` com sessão aiohttp persistente.

    Satisfaz o protocolo ``Destination`` diretamente: define ``kind``,
    ``format``, ``send_batch``, ``test`` e ``close``.

    ``auth_scheme``:
      - ``api_key``: header ``Authorization: ApiKey <secret>`` (Elastic).
      - ``basic``:   header ``Authorization: Basic <b64(secret)>`` — ``secret`` já
        é ``user:pass`` (OpenSearch / ES com basic).
      - ``none``:    sem auth (cluster aberto / atrás de proxy mTLS).
    """

    kind: str = "elastic_bulk"

    def __init__(
        self,
        url: str,
        secret: Optional[str],
        *,
        index: str = "centralops",
        auth_scheme: str = "api_key",
        verify_tls: bool = True,
        ca_bundle: Optional[str] = None,
        destination_id: Optional[str] = None,
    ) -> None:
        self._base_url = url.rstrip("/")
        self._url = self._base_url + "/_bulk"
        self._health_url = self._base_url + "/_cluster/health"
        self._secret = secret
        self._index = index
        self._auth_scheme = auth_scheme
        self._verify_tls = verify_tls
        self._ca_bundle = ca_bundle
        # ID do destino para chaveamento do pool de conexão por destino.
        # Opcional: sem id, pool não é usado.
        self._destination_id = destination_id
        self._session: Optional[aiohttp.ClientSession] = None

    def format(self, envelope: Mapping[str, Any]) -> dict:
        """Documento como será INDEXADO (canônico → wire dict).

        Usado por shadow/preview e pelo data-tap. A linha de ação
        (``format_bulk_action``) é framing — aqui devolvemos o doc-fonte.
        """
        return dict(envelope)

    def _auth_header(self) -> Optional[str]:
        if not self._secret:
            return None
        if self._auth_scheme == "api_key":
            return f"ApiKey {self._secret}"
        if self._auth_scheme == "basic":
            token = base64.b64encode(self._secret.encode("utf-8")).decode("ascii")
            return f"Basic {token}"
        return None

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
        """Retorna a ClientSession ativa para este destino.

        Quando ``DISPATCH_PERSISTENT_LOOP=1`` E ``destination_id`` foi
        informado, delega ao ``connection_pool`` — que reutiliza a session
        entre lotes do mesmo destino no loop persistente (reuso de socket
        TCP/TLS, sem reconexão por lote). Caso contrário usa o singleton
        local por instância (comportamento legado, byte-idêntico).
        """
        # Tenta obter do pool por destino (ativado somente com loop persistente).
        if self._destination_id is not None:
            headers: dict = {"Content-Type": "application/x-ndjson"}
            auth = self._auth_header()
            if auth:
                headers["Authorization"] = auth
            ssl_ctx = self._build_ssl()

            def _make_connector() -> aiohttp.TCPConnector:
                return aiohttp.TCPConnector(ssl=ssl_ctx)

            pooled = get_pooled_session(self._destination_id, _make_connector, headers)
            if pooled is not None:
                return pooled

        # Fallback: singleton local por instância (padrão / loop efêmero).
        if self._session is None or self._session.closed:
            headers = {"Content-Type": "application/x-ndjson"}
            auth = self._auth_header()
            if auth:
                headers["Authorization"] = auth
            connector = aiohttp.TCPConnector(ssl=self._build_ssl())
            self._session = aiohttp.ClientSession(headers=headers, connector=connector)
        return self._session

    def _ndjson(self, batch: List[Mapping[str, Any]]) -> str:
        """Serializa o lote no corpo NDJSON do ``_bulk`` (ação\\ndoc\\n…\\n).

        Usa ``_fastjson.dumps_str`` (orjson quando disponível) em vez de
        stdlib json — ~2-4× mais rápido para payloads típicos de eventos.
        Wire bytes idênticos (compact separators, ensure_ascii=False, default=str).
        """
        parts: list[str] = []
        for ev in batch:
            action = format_bulk_action(ev, index=self._index)
            parts.append(_json_dumps(action))
            parts.append(_json_dumps(ev))
        # Newline final é OBRIGATÓRIO no _bulk.
        return "\n".join(parts) + "\n"

    @staticmethod
    def _item_result(item: Mapping[str, Any]) -> Mapping[str, Any]:
        """Extrai o sub-objeto de resultado do item (``create``/``index``/…)."""
        if not isinstance(item, Mapping) or not item:
            return {}
        # Cada item é {"create": {...}} ou {"index": {...}}.
        return next(iter(item.values()), {}) or {}

    async def send_batch(self, batch: List[Mapping[str, Any]]) -> DeliveryResult:
        """Envia o lote ao ``_bulk``; mapeia status por item para DeliveryResult.

        - request 429/5xx → ``retryable=True`` (lote inteiro).
        - ``errors=false`` → tudo aceito.
        - ``errors=true`` → caminha ``items``: 2xx/409(conflict idempotente) =
          aceito; 429/503 de item = retryable (lote re-tentado); 4xx restante =
          rejeitado (schema_rejected/auth, não-retryable) → DLQ por item.
        Nunca levanta exceção.
        """
        if not batch:
            return DeliveryResult.ok(0)

        payload = self._ndjson(batch)
        session = self._get_session()
        rejected: list[RejectedEvent]
        try:
            async with session.post(self._url, data=payload) as resp:
                status = resp.status
                if status in _RETRYABLE_STATUS:
                    logger.warning("elastic_bulk: status transitório %s — retryable", status)
                    return DeliveryResult(accepted=0, retryable=True)
                if status in {401, 403}:
                    rejected = [
                        RejectedEvent(
                            event_id=_event_id(ev) or "?",
                            reason=f"auth HTTP {status}",
                            error_kind="auth",
                            retryable=False,
                        )
                        for ev in batch
                    ]
                    return DeliveryResult(accepted=0, rejected=rejected, retryable=False)

                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    body = {}

                if not isinstance(body, dict):
                    return DeliveryResult(accepted=0, retryable=True)

                # Caminho feliz: sem erros por item.
                if not body.get("errors"):
                    return DeliveryResult.ok(len(batch))

                items = body.get("items") or []
                # Guard: um ``items`` mais CURTO que o lote (resposta
                # truncada/malformada) faria o ``zip`` abaixo DESCARTAR eventos da
                # contabilidade silenciosamente (nem accepted nem rejected) com o lote
                # ACKado → perda. Reconcilia: mismatch → retry do lote inteiro (os já
                # indexados reentram como 409 idempotente via create+_id).
                if len(items) != len(batch):
                    logger.error(
                        "elastic_bulk: items=%d != batch=%d (resposta truncada/malformada)"
                        " — retry do lote",
                        len(items),
                        len(batch),
                    )
                    return DeliveryResult(accepted=0, retryable=True)
                accepted = 0
                rejected = []
                any_item_retryable = False
                for ev, item in zip(batch, items):
                    res = self._item_result(item)
                    st = res.get("status", 0)
                    if (isinstance(st, int) and 200 <= st < 300) or st == _IDEMPOTENT_CONFLICT:
                        accepted += 1
                        continue
                    if st in _RETRYABLE_ITEM_STATUS:
                        any_item_retryable = True
                        continue
                    err = res.get("error") or {}
                    reason = (
                        err.get("reason") if isinstance(err, Mapping) else str(err)
                    ) or f"status {st}"
                    kind = "auth" if st in {401, 403} else "schema_rejected"
                    rejected.append(
                        RejectedEvent(
                            event_id=_event_id(ev) or "?",
                            reason=str(reason),
                            error_kind=kind,
                            retryable=False,
                        )
                    )
                # Se algum item é transitório, peça retry do lote (os já-aceitos
                # reentram como 409-idempotente — sem duplicar).
                if any_item_retryable:
                    return DeliveryResult(accepted=accepted, retryable=True)
                return DeliveryResult(accepted=accepted, rejected=rejected, retryable=False)

        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            logger.warning("elastic_bulk: erro de conexão transitório: %s", exc)
            return DeliveryResult(accepted=0, retryable=True)

    async def test(self) -> TestResult:
        """Probe: ``GET {url}/_cluster/health``. 200 → ok; 401/403 → credencial."""
        session = self._get_session()
        try:
            async with session.get(self._health_url) as resp:
                status = resp.status
                if status in {401, 403}:
                    return TestResult.failed(f"credencial inválida ({status})")
                if status == 200:
                    try:
                        body = await resp.json(content_type=None)
                    except Exception:
                        body = {}
                    cluster_status = (
                        body.get("status") if isinstance(body, dict) else None
                    )
                    return TestResult.passed(
                        f"cluster ok (status={cluster_status or 'desconhecido'})"
                    )
                return TestResult.failed(f"_cluster/health respondeu HTTP {status}")
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            return TestResult.failed(f"erro de conexão ao cluster: {exc}")

    async def erase(
        self,
        event_ids: List[str],
        *,
        filter: Optional[Mapping[str, Any]] = None,
    ) -> ErasureResult:
        """Right-to-erasure: apaga eventos por ``_id`` e/ou por filtro de org.

        Dois modos de operação, compostos quando ambos fornecidos:

        **Modo 1 — delete by _id via ``_bulk``** (``event_ids`` não-vazio):
          Usa a ação ``delete`` no ``_bulk`` — uma linha de ação por id, sem linha de
          documento. Idempotente: 404 (já apagado) é contado como ``erased``.
          Cobre eventos que caíram na DLQ (rastreados pelo dispatcher).

        **Modo 2 — delete_by_query** (``filter`` com ``organization_id`` presente):
          Emite ``POST {index}/_delete_by_query`` com uma query ``term`` no campo
          ``_centralops.organization_id``. Cobre dados ENTREGUES com sucesso que não
          aparecem na DLQ — garante purge LGPD completo de toda a org no cluster.
          ``deleted`` da resposta é informado em ``detail``; sem ``_id``s explícitos,
          ``erased`` é uma lista de marcadores sintéticos (sem UUIDs individuais).

        Quando ambos fornecidos, executa os dois e combina resultados (best-effort:
        falha em um não cancela o outro). Nunca levanta exceção — erros vão para
        ``ErasureResult.failed`` / ``detail``.
        """
        results: list[ErasureResult] = []

        # --- Modo 1: delete por _id (DLQ) ---
        if event_ids:
            results.append(await self._erase_by_ids(event_ids))

        # --- Modo 2: delete_by_query (dados entregues) ---
        if filter is not None:
            org_id = filter.get("organization_id")
            if org_id is not None:
                results.append(await self._erase_by_query_org(org_id))

        if not results:
            return ErasureResult.success([], detail="nothing to erase")

        # Combina resultados: concatena erased/failed, une details.
        all_erased: list[str] = []
        all_failed: list[str] = []
        details: list[str] = []
        for r in results:
            all_erased.extend(r.erased)
            all_failed.extend(r.failed)
            if r.detail:
                details.append(r.detail)
        combined_detail = "; ".join(details) if details else ""
        return ErasureResult(erased=all_erased, failed=all_failed, detail=combined_detail)

    async def _erase_by_ids(self, event_ids: List[str]) -> ErasureResult:
        """Implementação interna: apaga por _id via ``_bulk`` delete."""
        # Build _bulk delete body: one {"delete": {"_index":..., "_id":...}} per id.
        parts: list[str] = []
        for eid in event_ids:
            parts.append(_json_dumps({"delete": {"_index": self._index, "_id": eid}}))
        payload = "\n".join(parts) + "\n"

        session = self._get_session()
        try:
            async with session.post(self._url, data=payload) as resp:
                status = resp.status
                if status in _RETRYABLE_STATUS:
                    logger.warning(
                        "elastic_bulk.erase: status transitório %s — todos marcados como falhos",
                        status,
                    )
                    return ErasureResult.error(event_ids, detail=f"HTTP {status} (retryable error)")
                if status in {401, 403}:
                    return ErasureResult.error(event_ids, detail=f"auth error HTTP {status}")

                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    body = {}

                if not isinstance(body, dict):
                    return ErasureResult.error(event_ids, detail="resposta inesperada (não-JSON)")

                # ``errors=false`` → all deleted (or already absent — idempotent).
                if not body.get("errors"):
                    return ErasureResult.success(event_ids, detail=f"{len(event_ids)} eventos apagados")

                items = body.get("items") or []
                if len(items) != len(event_ids):
                    logger.error(
                        "elastic_bulk.erase: items=%d != ids=%d (resposta truncada) — "
                        "marcando todos como falhos",
                        len(items),
                        len(event_ids),
                    )
                    return ErasureResult.error(event_ids, detail="resposta truncada do _bulk")

                erased: list[str] = []
                failed: list[str] = []
                for eid, item in zip(event_ids, items):
                    res = self._item_result(item)
                    st = res.get("status", 0)
                    # 200 (deleted) or 404 (not found = already gone) → erased.
                    if st in {200, 404}:
                        erased.append(eid)
                    else:
                        failed.append(eid)
                        logger.warning(
                            "elastic_bulk.erase: falha ao apagar id=%r status=%s",
                            eid,
                            st,
                        )
                detail = f"{len(erased)} apagados, {len(failed)} falhos"
                if failed:
                    return ErasureResult(erased=erased, failed=failed, detail=detail)
                return ErasureResult.success(erased, detail=detail)

        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            logger.warning("elastic_bulk.erase: erro de conexão: %s", exc)
            return ErasureResult.error(event_ids, detail=f"conexão falhou: {exc}")

    async def _erase_by_query_org(self, organization_id: Any) -> ErasureResult:
        """Implementação interna: apaga por query de org via ``_delete_by_query``.

        Emite ``POST {base_url}/{index}/_delete_by_query`` com:
          {"query": {"term": {"_centralops.organization_id": <org_id>}}}

        A resposta contém ``deleted`` (documentos removidos) e ``failures`` (lista
        de falhas de shard). Sem ``_id``s explícitos, usa marcadores sintéticos no
        campo ``erased`` para indicar que a operação ocorreu.
        """
        dbq_url = f"{self._base_url}/{self._index}/_delete_by_query"
        query_body = {
            "query": {
                "term": {
                    _ORG_FIELD: organization_id,
                }
            }
        }
        payload = _json_dumps(query_body)

        session = self._get_session()
        try:
            async with session.post(
                dbq_url,
                data=payload,
                headers={"Content-Type": "application/json"},
            ) as resp:
                status = resp.status
                if status in _RETRYABLE_STATUS:
                    logger.warning(
                        "elastic_bulk.erase_by_query: status transitório %s org=%s",
                        status,
                        organization_id,
                    )
                    return ErasureResult.error(
                        [f"org:{organization_id}"],
                        detail=f"delete_by_query HTTP {status} (retryable error)",
                    )
                if status in {401, 403}:
                    return ErasureResult.error(
                        [f"org:{organization_id}"],
                        detail=f"delete_by_query auth error HTTP {status}",
                    )

                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    body = {}

                if not isinstance(body, dict):
                    return ErasureResult.error(
                        [f"org:{organization_id}"],
                        detail="delete_by_query resposta inesperada (não-JSON)",
                    )

                deleted_count: int = int(body.get("deleted") or 0)
                failures: list = body.get("failures") or []

                if failures:
                    logger.warning(
                        "elastic_bulk.erase_by_query: %d falhas de shard org=%s: %s",
                        len(failures),
                        organization_id,
                        failures[:3],  # log primeiras 3 para não explodir o log
                    )
                    return ErasureResult(
                        erased=[f"org:{organization_id}:deleted:{deleted_count}"],
                        failed=[f"org:{organization_id}:shard_failures:{len(failures)}"],
                        detail=(
                            f"delete_by_query: {deleted_count} docs apagados, "
                            f"{len(failures)} falhas de shard"
                        ),
                    )

                logger.info(
                    "elastic_bulk.erase_by_query: %d docs apagados org=%s",
                    deleted_count,
                    organization_id,
                )
                return ErasureResult.success(
                    [f"org:{organization_id}:deleted:{deleted_count}"],
                    detail=f"delete_by_query: {deleted_count} docs apagados para org={organization_id}",
                )

        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            logger.warning(
                "elastic_bulk.erase_by_query: erro de conexão org=%s: %s",
                organization_id,
                exc,
            )
            return ErasureResult.error(
                [f"org:{organization_id}"],
                detail=f"delete_by_query conexão falhou: {exc}",
            )

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            try:
                await self._session.close()
            except Exception:  # pragma: no cover — best-effort
                logger.exception("elastic_bulk: erro ao fechar sessão")
            finally:
                self._session = None
