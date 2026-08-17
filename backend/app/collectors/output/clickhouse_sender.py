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
from .payload_shape import normalizar_row_shape, render_row

logger = logging.getLogger(__name__)

# Status HTTP que indicam erro transitório (retry com backoff).
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Envelope sintético usado por ``chaves_emitidas`` para descobrir, sem tráfego
# real, quais chaves de topo a config atual produz. Só a FORMA importa: os
# valores nunca saem daqui.
_ENVELOPE_SONDA: Mapping[str, Any] = {
    "_centralops": {"event_id": "probe"},
    "normalized": {"class_uid": 0, "time": 0},
    "raw": {},
}


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
        payload: str = "envelope",
        row_shape: str = "flat",
        event_key: str = "event",
        row_fields: Optional[Mapping[str, str]] = None,
        row_fields_from: Optional[Mapping[str, str]] = None,
        verify_tls: bool = True,
        ca_bundle: Optional[str] = None,
    ) -> None:
        self._base = url.rstrip("/")
        self._password = password
        self._database = database
        self._table = table
        self._username = username
        self._skip_unknown_fields = skip_unknown_fields
        self._payload = payload
        self._row_shape = normalizar_row_shape(row_shape)
        self._event_key = event_key
        self._row_fields = dict(row_fields or {})
        self._row_fields_from = dict(row_fields_from or {})
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
        # Sempre EXPLÍCITO, nos dois estados. Antes o parâmetro só era
        # acrescentado quando ligado; desligar o toggle apenas o omitia, e o
        # default do SERVIDOR é 1, então "não ignorar campos desconhecidos"
        # nunca chegava a valer. O controle existia na UI e era decorativo.
        # Mandar 0 devolve o caminho barulhento (erro 117, "Unknown field found
        # while parsing JSONEachRow format"), que é a saída de emergência de
        # quem desconfia que está gravando linha vazia.
        params["input_format_skip_unknown_fields"] = "1" if self._skip_unknown_fields else "0"
        if self._async_insert:
            # async_insert + wait_for_async_insert=1: o servidor agrupa micro-lotes
            # mas a resposta só volta quando persistido (mantém a semântica de
            # confirmação de entrega — sem "aceitei mas perdi").
            params["async_insert"] = "1"
            params["wait_for_async_insert"] = "1"
        return f"{self._base}/?{urllib.parse.urlencode(params)}"

    def format(self, envelope: Mapping[str, Any]) -> Any:
        """Canônico → linha ``JSONEachRow``.

        Dois eixos independentes. ``payload`` decide o CONTEÚDO: com ``ocsf`` a
        linha carrega o evento OCSF 1.8 puro, com ``envelope`` o canônico.
        ``row_shape`` decide a FORMA: com ``flat`` as chaves do conteúdo são as
        colunas, com ``wrapped`` o conteúdo inteiro vai sob ``event_key`` e as
        outras colunas vêm de ``row_fields`` (literal) e ``row_fields_from``
        (derivado do próprio evento — é o que deixa UM destino servir N tenants
        com a linha rotulada corretamente).

        A combinação importa e o erro é silencioso: ``flat`` contra uma tabela
        coluna-wrapper faz o ClickHouse descartar tudo (``skip_unknown_fields``)
        e gravar linhas vazias respondendo 200. Quem barra isso é ``test()``.
        """
        return render_row(
            envelope,
            payload=self._payload,
            row_shape=self._row_shape,
            event_key=self._event_key,
            row_fields=self._row_fields,
            row_fields_from=self._row_fields_from,
        )

    def chaves_emitidas(self) -> set:
        """As chaves de topo que ``format()`` produz para a config atual.

        Descobre a forma pelo próprio caminho de renderização, contra um
        envelope sintético, em vez de reimplementar a lógica: se ``format``
        mudar e isto não acompanhar, o teste de forma passa a mentir. Usado por
        ``test()`` para comparar com as colunas reais da tabela.
        """
        linha = self.format(_ENVELOPE_SONDA)
        return set(linha.keys()) if isinstance(linha, Mapping) else set()

    def _serialize(self, batch: List[Mapping[str, Any]]) -> str:
        return "\n".join(
            json.dumps(self.format(ev), separators=(",", ":"), default=str, ensure_ascii=False)
            for ev in batch
        )

    @staticmethod
    def _event_id(event: Mapping[str, Any]) -> str:
        meta = event.get("_centralops") or {}
        return str(meta.get("event_id") or "?")

    def _resultado_de_sucesso(self, resp: Any, batch: List[Mapping[str, Any]]) -> DeliveryResult:
        """2xx → quantas linhas o servidor diz ter escrito, não quantas mandamos.

        O ClickHouse devolve ``X-ClickHouse-Summary`` com ``written_rows``. Sem
        ler isso, "200" é assumido como "tudo gravado", e existe uma classe de
        falha em que o servidor aceita e escreve menos, ou nada.

        **Fail-open quando o header não vem**, e isso é deliberado: nem toda
        versão nem todo proxy o repassa, e transformar ausência de header em
        falha quebraria entrega que está funcionando. O header é evidência
        quando presente, não requisito.

        Isto NÃO pega o caso "N linhas gravadas, todas vazias" — ali
        ``written_rows == len(batch)``. Quem pega aquele é ``test()``.
        """
        bruto = None
        try:
            bruto = resp.headers.get("X-ClickHouse-Summary")
        except Exception:  # pragma: no cover — headers ilegíveis
            bruto = None
        if not bruto:
            return DeliveryResult.ok(len(batch))
        try:
            escritas = int(json.loads(bruto).get("written_rows", -1))
        except (ValueError, TypeError, AttributeError):
            return DeliveryResult.ok(len(batch))
        if escritas < 0 or escritas == len(batch):
            return DeliveryResult.ok(len(batch))
        if escritas == 0 and batch:
            logger.warning(
                "clickhouse: servidor respondeu 2xx com written_rows=0 para lote de %d "
                "(tabela=%s.%s) — tratando como rejeição",
                len(batch), self._database, self._table,
            )
            return DeliveryResult(
                accepted=0,
                rejected=[
                    RejectedEvent(
                        event_id=self._event_id(ev),
                        reason="ClickHouse respondeu 2xx mas written_rows=0",
                        error_kind="schema_rejected",
                        retryable=False,
                    )
                    for ev in batch
                ],
                retryable=False,
            )
        logger.warning(
            "clickhouse: written_rows=%d difere do lote=%d (tabela=%s.%s)",
            escritas, len(batch), self._database, self._table,
        )
        return DeliveryResult.ok(escritas)

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
                    return self._resultado_de_sucesso(resp, batch)

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

    @staticmethod
    def _literal_sql(valor: str) -> str:
        """Escapa uma string para literal SQL do ClickHouse (aspas simples)."""
        return valor.replace("\\", "\\\\").replace("'", "\\'")

    async def _consulta(self, sql: str) -> tuple:
        """Executa uma consulta de leitura. Devolve ``(status, corpo)``."""
        endpoint = f"{self._base}/?{urllib.parse.urlencode({'query': sql})}"
        session = self._get_session()
        async with session.get(endpoint) as resp:
            try:
                corpo = await resp.text()
            except Exception:  # pragma: no cover — corpo ilegível
                corpo = ""
            return resp.status, corpo

    async def test(self) -> TestResult:
        """Probe em três passos, parando no primeiro que falha. Nunca levanta.

        1. **Conectividade e credencial** (``SELECT 1``). É o único passo que
           funciona com qualquer permissão, por isso vem primeiro. Já pega o
           erro clássico de apontar para a porta 9000: o servidor nativo devolve
           4xx com "You must use port 8123 for HTTP", e o corpo vai na mensagem.

        2. **A tabela existe, e quais colunas tem** (``system.columns``). Se o
           usuário não puder ler o catálogo (um usuário INSERT-only não pode),
           o passo é **inconclusivo, não falha**, e o texto diz qual GRANT
           habilita a verificação. Mentir aqui seria pior que não verificar.

        3. **A forma casa com a tabela.** Compara ``chaves_emitidas()`` com as
           colunas reais. É o passo que pega o modo de falha que motivou tudo
           isto: ``flat`` contra tabela coluna-wrapper, em que o INSERT responde
           200 e grava linhas vazias, sem nenhum sinal de erro em lugar nenhum.
        """
        # ── Passo 1: conectividade e credencial ───────────────────────────
        try:
            status, corpo = await self._consulta("SELECT 1")
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            return TestResult.failed(f"erro de conexão ao ClickHouse: {exc}")
        if status in {401, 403}:
            return TestResult.failed("credencial ClickHouse inválida (401/403)")
        if not (200 <= status < 300):
            return TestResult.failed(
                f"ClickHouse respondeu status={status}: {corpo[:300]!r}"
            )

        alvo = f"{self._database}.{self._table}"

        # ── Passo 2: tabela e colunas ─────────────────────────────────────
        sql = (
            "SELECT name FROM system.columns WHERE database = "
            f"'{self._literal_sql(self._database)}' AND table = "
            f"'{self._literal_sql(self._table)}' FORMAT TSV"
        )
        try:
            status, corpo = await self._consulta(sql)
        except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError) as exc:
            return TestResult.failed(f"erro de conexão ao ClickHouse: {exc}")

        if not (200 <= status < 300):
            if status in {401, 403} or "ACCESS_DENIED" in corpo or "Not enough privileges" in corpo:
                return TestResult.passed(
                    f"Conectou em {self._base}. Não foi possível ler o schema de {alvo} "
                    f"(usuário {self._username} sem permissão no catálogo), então a "
                    "verificação de colunas NÃO rodou. Para habilitá-la: "
                    f"GRANT SHOW COLUMNS ON {self._database}.* TO {self._username}"
                )
            return TestResult.failed(
                f"não foi possível consultar o schema de {alvo}: HTTP {status} {corpo[:200]!r}"
            )

        colunas = {linha.strip() for linha in corpo.splitlines() if linha.strip()}
        if not colunas:
            return TestResult.failed(
                f"banco/tabela não encontrados: {alvo}. Confira os campos Banco e Tabela "
                "separadamente — o nome qualificado inteiro no campo Tabela vira um nome "
                "literal com ponto e não resolve."
            )

        # ── Passo 3: a forma casa com a tabela ────────────────────────────
        emitidas = self.chaves_emitidas()
        if self._row_shape == "wrapped":
            faltando = sorted(k for k in emitidas if k not in colunas)
            if faltando:
                return TestResult.failed(
                    f"a tabela {alvo} não tem a(s) coluna(s) {faltando}. Com row_shape=wrapped "
                    f"o evento vai em '{self._event_key}' e cada chave de row_fields e "
                    f"row_fields_from precisa existir como coluna. "
                    f"Colunas da tabela: {sorted(colunas)}"
                )
            return TestResult.passed(
                f"ClickHouse ok: {alvo}, forma wrapped, colunas {sorted(emitidas)} conferem."
            )

        comuns = emitidas & colunas
        if not comuns:
            return TestResult.failed(
                f"nenhuma chave do formato configurado corresponde a uma coluna de {alvo}. "
                "Com skip_unknown_fields ligado o INSERT responderia 200 e gravaria linhas "
                f"VAZIAS. Colunas da tabela: {sorted(colunas)}. Se a tabela tem uma coluna de "
                "evento mais colunas de rótulo, use row_shape=wrapped."
            )
        descartadas = sorted(emitidas - colunas)
        if descartadas:
            return TestResult.passed(
                f"ClickHouse ok: {alvo}. Aviso: {len(descartadas)} chave(s) não têm coluna e "
                f"serão descartadas: {descartadas[:12]}"
            )
        return TestResult.passed(f"ClickHouse ok: {alvo}, todas as chaves têm coluna.")

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            try:
                await self._session.close()
            except Exception:  # pragma: no cover — best-effort
                logger.exception("clickhouse: erro ao fechar sessão")
            finally:
                self._session = None
