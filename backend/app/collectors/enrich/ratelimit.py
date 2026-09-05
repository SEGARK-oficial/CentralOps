"""Token-bucket LOCAL para enrichers remotos com cota (ADR-LOCAL-0002, W4.2).

**O que resolve.** O VirusTotal público aceita 4 requisições por minuto e 500
por dia. O enricher já deduplica chaves e limita o lote, mas nada contava a
cota: o 4º lote do minuto recebia 429 em cada chave, o lote inteiro saía como
UNKNOWN e o operador descobria pela métrica de erro. Um bucket na frente do
cliente HTTP transforma "429 em produção" em "chaves além da cota ficam para
o próximo ciclo" — e ``Retry-After`` numa espera respeitada em vez de uma
tempestade.

**O que NÃO resolve, e por quê.** O bucket é POR PROCESSO. Dois workers com a
mesma chave somam 8/min contra um limite de 4. Um bucket compartilhado exigiria
Redis no caminho de cada requisição (o L2 existe, mas é cache, não semáforo) e
ainda assim não seria exato — o provedor conta do lado dele. O desenho é em
camadas: o bucket local suaviza; o circuit breaker por fonte (W4.1), que É
compartilhado, para a tempestade quando a cota do provedor de fato acaba. Para
uma cota partilhada por N workers, configure ``requests_per_minute`` = cota / N.

Sem I/O, sem lock além do ``asyncio`` do próprio loop: ``try_acquire`` nunca
espera — ou há token ou a chave fica para depois. Esperar aqui consumiria o
orçamento de 300 ms do lote inteiro em troca de UMA chave.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional


@dataclass
class TokenBucket:
    """Bucket clássico: ``capacity`` tokens, recarga contínua a ``rate`` por segundo.

    ``blocked_until`` é o ``Retry-After`` do provedor: enquanto não passa, nenhum
    token é concedido, independente do saldo — o provedor mandou esperar.
    """

    capacity: float
    rate: float  # tokens por segundo
    clock: Callable[[], float] = time.monotonic
    tokens: float = field(init=False)
    updated_at: float = field(init=False)
    blocked_until: float = field(default=0.0, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.capacity = max(float(self.capacity), 1.0)
        self.rate = max(float(self.rate), 0.0)
        self.tokens = self.capacity
        self.updated_at = self.clock()

    def _refill(self, now: float) -> None:
        elapsed = max(now - self.updated_at, 0.0)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.updated_at = now

    def try_acquire(self, n: int = 1) -> bool:
        """Consome ``n`` tokens se houver. NUNCA espera."""
        with self._lock:
            now = self.clock()
            if now < self.blocked_until:
                return False
            self._refill(now)
            if self.tokens >= n:
                self.tokens -= n
                return True
            return False

    def block_for(self, seconds: float) -> None:
        """``Retry-After``: suspende concessões por ``seconds``."""
        with self._lock:
            until = self.clock() + max(float(seconds), 0.0)
            self.blocked_until = max(self.blocked_until, until)

    def blocked_remaining(self) -> float:
        with self._lock:
            return max(self.blocked_until - self.clock(), 0.0)


@dataclass
class QuotaBuckets:
    """Par (minuto, dia) por identidade de cota — uma chave de API, tipicamente.

    Dois buckets porque os dois limites existem e falham em escalas diferentes:
    o de minuto absorve rajada; o de dia é o que acaba às 15h e não volta até
    a meia-noite do provedor. ``day`` é aproximado (recarga contínua a 1/86400
    por token, não reset à meia-noite) — bom o bastante para não queimar a cota
    diária num ciclo, que é o dano a evitar.
    """

    minute: TokenBucket
    day: Optional[TokenBucket]

    def try_acquire(self) -> bool:
        if self.day is not None and not self.day.try_acquire():
            return False
        if not self.minute.try_acquire():
            # Devolve o token diário: a requisição não vai acontecer.
            if self.day is not None:
                self.day.tokens = min(self.day.capacity, self.day.tokens + 1)
            return False
        return True

    def block_for(self, seconds: float) -> None:
        self.minute.block_for(seconds)


_REGISTRY: Dict[str, QuotaBuckets] = {}
_REGISTRY_LOCK = threading.Lock()


def buckets_for(
    identity: str,
    *,
    requests_per_minute: int,
    requests_per_day: Optional[int] = None,
    clock: Callable[[], float] = time.monotonic,
) -> QuotaBuckets:
    """Bucket por identidade, criado na primeira vez e reaproveitado depois.

    A identidade é um DIGEST da credencial (nunca a credencial), para que dois
    fontes com a mesma chave partilhem a cota — que é o que o provedor faz.
    Mudar os limites da fonte cria um bucket novo (a identidade inclui os
    números), em vez de reconfigurar um que já tem saldo.
    """
    key = f"{identity}:{int(requests_per_minute)}:{int(requests_per_day or 0)}"
    with _REGISTRY_LOCK:
        found = _REGISTRY.get(key)
        if found is None:
            rpm = max(int(requests_per_minute), 1)
            minute = TokenBucket(capacity=rpm, rate=rpm / 60.0, clock=clock)
            day = None
            if requests_per_day:
                rpd = max(int(requests_per_day), 1)
                day = TokenBucket(capacity=rpd, rate=rpd / 86_400.0, clock=clock)
            found = QuotaBuckets(minute=minute, day=day)
            _REGISTRY[key] = found
        return found


def reset_registry() -> None:
    """Só para testes."""
    with _REGISTRY_LOCK:
        _REGISTRY.clear()


def parse_retry_after(value: Optional[str], default: float = 60.0) -> float:
    """``Retry-After`` em segundos. Só a forma numérica; data HTTP cai no default.

    Teto de 1 h: um provedor que mande "volte amanhã" não pode congelar a fonte
    até lá sem que o operador veja — o breaker (W4.1) é quem lida com isso, e
    tem log e métrica.
    """
    if not value:
        return default
    try:
        seconds = float(str(value).strip())
    except ValueError:
        return default
    return min(max(seconds, 0.0), 3600.0)
