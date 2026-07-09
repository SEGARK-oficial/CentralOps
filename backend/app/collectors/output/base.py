"""Contrato público de **destino de saída**.

Promove o ``_Target`` privado de ``wazuh_target.py`` a um protocolo
público ``Destination``, separando **formatação** (canônico → wire) de
**envio** (entrega do lote). É a fundação para "destino = integração de
primeira classe" (multi-destino + roteamento).

**Multi-destino é GA.** O Wazuh (``wazuh-default``) em
produção continua via ``wazuh_target.get_target`` — byte-a-byte idêntico,
em lane dedicada. As peças aqui (protocolo, ``DeliveryResult``, adapter,
registry, cache multi-singleton) alimentam o fan-out aditivo por destino.

Os 3 senders atuais (``Rfc3164JsonClient``, ``SyslogTCPClient``,
``JSONLWriter``) já satisfazem ``send_batch``/``close`` do ``_Target``.
O ``LegacyTargetDestination`` os embrulha como ``Destination`` sem
alterar uma linha do wire — ``send_batch`` delega ao target legado e
sintetiza um ``DeliveryResult``.

**Formatação desacoplada do envio.** As
funções de formatação dos kinds legados são FONTE ÚNICA do wire em
``output/formatters.py``: tanto o caminho de ENVIO (``*_sender.send_batch``
/ ``JSONLWriter.send_batch``) quanto o ``Destination.format()`` consomem a
MESMA função — uma definição, dois consumidores, byte-idêntico. Provado em
``tests/test_format_decoupling.py`` (o que ``send_batch`` enfileira, menos o
framing, == ``format()``).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, List, Mapping, Optional, Protocol, Sequence, runtime_checkable


# ── Resultado de entrega (at-least-once + falha parcial) ──


@dataclass(frozen=True)
class RejectedEvent:
    """Um evento rejeitado por um destino, com motivo estruturado.

    ``error_kind`` segue o vocabulário: ``payload_too_large``
    (> OS_MAXSTR do Wazuh / limite do HEC), ``schema_rejected`` (inválido
    p/ DCR/_bulk), ``auth`` (credencial), ``unknown``. ``retryable``
    distingue 4xx determinístico (→ DLQ) de transitório (→ retry).
    """

    event_id: str
    reason: str
    error_kind: str = "unknown"
    retryable: bool = False


@dataclass(frozen=True)
class DeliveryResult:
    """Resultado da entrega de um lote a UM destino.

    - ``accepted``  — nº de eventos aceitos pelo sink.
    - ``rejected``  — subconjunto rejeitado (vai p/ DLQ/quarentena do
      destino, **não** re-tentado cegamente).
    - ``retryable`` — erro transitório de lote (429/5xx) → retry com
      backoff; ``False`` para 4xx determinístico.
    """

    accepted: int
    rejected: List[RejectedEvent] = field(default_factory=list)
    retryable: bool = False

    @property
    def all_accepted(self) -> bool:
        return not self.rejected

    @classmethod
    def ok(cls, accepted: int) -> "DeliveryResult":
        """Atalho para o caminho feliz (lote inteiro aceito)."""
        return cls(accepted=accepted)


@dataclass(frozen=True)
class TestResult:
    """Resultado de um probe de conexão (``Destination.test()``)."""

    ok: bool
    detail: str = ""
    latency_ms: Optional[float] = None

    @classmethod
    def passed(cls, detail: str = "", latency_ms: Optional[float] = None) -> "TestResult":
        return cls(ok=True, detail=detail, latency_ms=latency_ms)

    @classmethod
    def failed(cls, detail: str) -> "TestResult":
        return cls(ok=False, detail=detail)


@dataclass(frozen=True)
class ErasureResult:
    """Resultado de uma operação de right-to-erasure.

    ``erased``  — IDs efetivamente deletados no destino.
    ``failed``  — IDs que deveriam ter sido deletados mas falharam.
    ``detail``  — mensagem legível (motivo de falha ou confirmação).

    Um resultado com ``failed`` não-vazio ainda é parcialmente aceito
    (best-effort, sem re-throw): o job Celery marca o job como "partial"
    mas continua executando os demais destinos.
    """

    erased: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    detail: str = ""

    @property
    def ok(self) -> bool:
        return not self.failed

    @classmethod
    def success(cls, erased: Sequence[str], detail: str = "") -> "ErasureResult":
        return cls(erased=list(erased), detail=detail)

    @classmethod
    def error(cls, failed: Sequence[str], detail: str) -> "ErasureResult":
        return cls(failed=list(failed), detail=detail)


# ── Protocolos ─────────────────────────────────────────────────────────


@runtime_checkable
class LegacyTarget(Protocol):
    """O contrato ``_Target`` histórico (``send_batch`` retorna ``None``).

    Mantido como protocolo nomeado para o adapter tipar os 3 senders
    atuais sem importá-los (evita ciclo)."""

    async def send_batch(self, batch: List[Mapping[str, Any]]) -> None: ...
    async def close(self) -> None: ...


@runtime_checkable
class Destination(Protocol):
    """Destino de saída de primeira classe.

    Um destino **possui**: identidade de tipo (``kind``), formatação
    própria (canônico → wire), entrega (``send_batch`` → ``DeliveryResult``),
    probe de conexão (``test``) e fechamento limpo (``close``).
    """

    #: chave do ``DestinationRegistry`` — "wazuh_syslog" | "jsonl" | "splunk_hec" | ...
    kind: str

    def format(self, envelope: Mapping[str, Any]) -> "bytes | dict": ...

    async def send_batch(self, batch: List[Mapping[str, Any]]) -> DeliveryResult: ...

    async def test(self) -> TestResult: ...

    async def close(self) -> None: ...


# ── Adapter: target legado → Destination (preserva wire byte-a-byte) ───


Formatter = Callable[[Mapping[str, Any]], Any]
Probe = Callable[[], Awaitable[TestResult]]


class LegacyTargetDestination:
    """Embrulha um ``_Target`` legado como ``Destination``.

    **Invariante de byte-identidade:** ``send_batch`` delega ao target
    legado **sem tocar no payload nem no framing** — o wire é exatamente
    o de hoje. O ``DeliveryResult`` é sintetizado (lote all-or-nothing,
    como o comportamento atual).

    ``format`` delega ao formatter de módulo (``format_rfc3164`` etc.)
    quando fornecido — útil para shadow/preview sem entregar.
    ``test`` usa o probe injetado, ou um default no-op que reporta
    "não suportado".
    """

    def __init__(
        self,
        kind: str,
        target: LegacyTarget,
        *,
        formatter: Optional[Formatter] = None,
        probe: Optional[Probe] = None,
    ) -> None:
        self.kind = kind
        self._target = target
        self._formatter = formatter
        self._probe = probe

    def format(self, envelope: Mapping[str, Any]) -> Any:
        if self._formatter is None:
            raise NotImplementedError(
                f"destino kind={self.kind!r} não expõe formatter desacoplado (Fase 1)"
            )
        return self._formatter(envelope)

    async def send_batch(self, batch: List[Mapping[str, Any]]) -> DeliveryResult:
        # Caminho byte-a-byte idêntico ao atual: o target legado faz o
        # format+framing+envio inline. Aceito = lote inteiro (all-or-nothing).
        await self._target.send_batch(batch)
        return DeliveryResult.ok(len(batch))

    async def test(self) -> TestResult:
        if self._probe is not None:
            started = time.monotonic()
            result = await self._probe()
            if result.latency_ms is None and result.ok:
                elapsed_ms = (time.monotonic() - started) * 1000.0
                return TestResult(ok=True, detail=result.detail, latency_ms=elapsed_ms)
            return result
        return TestResult.failed(f"probe de conexão não implementado para kind={self.kind!r}")

    async def close(self) -> None:
        await self._target.close()

    # Acesso ao target embrulhado — usado por testes de paridade.
    @property
    def legacy_target(self) -> LegacyTarget:
        return self._target
