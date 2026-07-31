"""Admissão de eventos na captura ao vivo: limita CPU sem partir trajetórias.

Duas funções puras, zero I/O, chamadas no TOPO do tap — antes de qualquer
serialização, que é justamente o custo que se quer evitar.

POR QUE EXISTE. Com uma sessão aberta, cada evento paga ``audit_buffer._redact``
(recursivo, reescreve toda string via regex de segredo) mais a serialização.
Medido sobre as fixtures reais do repo: p50 18 µs, média 42 µs, máx 172 µs — e
isso roda SÍNCRONO dentro do event loop da coleta. A 10k EPS com uma sessão
aberta são 180 ms/s no melhor caso e 1,7 s/s no pior vendor: a coleta não
acompanha e o ciclo estoura o soft-timeout.

POR QUE POR HASH, E NÃO POR RELÓGIO. A alternativa óbvia — um token bucket como
mecanismo primário — destrói a feature. ``received`` (worker de coleta) e
``delivered`` (dispatcher, outro processo, minutos depois) seriam admitidos
INDEPENDENTEMENTE, e a chance de um evento ter os dois estágios no ring
tenderia a zero. O jornal só é juntável se a decisão for a MESMA em todo
processo e em todo estágio — e é por isso que a admissão primária é uma função
determinística do ``event_id``, não do relógio.

O bucket continua existindo, mas só como backstop de OOM/CPU, e toda rejeição
dele é CONTADA para a UI poder dizer "capturando N de M ev/s". Sem esse aviso a
amostragem transforma "não houve tráfego" em "não capturei" — exatamente o
sintoma que o tap existe para eliminar.
"""
from __future__ import annotations

import time
import zlib
from typing import Dict, Optional, Tuple

#: Defaults e tetos dos parâmetros de sessão.
DEFAULT_CAPTURE_PERCENT = 100
DEFAULT_MAX_EPS = 500
MAX_EPS_CEILING = 5_000


def admit(event_id: Optional[str], capture_percent: int) -> bool:
    """DETERMINÍSTICA: o mesmo ``event_id`` dá a mesma resposta em todo processo.

    ``event_id`` ausente sempre admite — é o caso dos três sites de quarentena
    PRÉ-envelope, onde não existe id ainda. Inventar um id só para amostrar seria
    pior: produziria uma decisão que ninguém consegue reproduzir.

    Os percentuais são ANINHADOS por construção (``crc32 % 100``): o que é
    admitido em 25% também é admitido em 100%. Isso importa porque o operador
    que sobe a taxa no meio do troubleshooting não perde a trajetória que já
    estava vendo.
    """
    if capture_percent >= 100:
        return True
    if capture_percent <= 0:
        return False
    if not event_id:
        return True
    return zlib.crc32(event_id.encode("utf-8")) % 100 < capture_percent


# ── Token bucket por sessão (backstop) ────────────────────────────────
# Estado de PROCESSO. Cada worker tem o seu, então o teto efetivo é
# ``max_eps × n_workers`` — e isso é intencional: o custo que se está limitando
# é o de CPU de CADA event loop, não uma taxa global.
_buckets: Dict[str, Tuple[float, float]] = {}  # sid -> (tokens, last_monotonic)
_skipped: Dict[str, int] = {}


def throttle(session_id: str, max_eps: int, n: int = 1) -> int:
    """Quantos dos ``n`` eventos passam. Acumula os rejeitados em :func:`skipped`.

    Burst de 2× a taxa: um lote de coleta chega em rajada, e um bucket sem burst
    descartaria a maior parte de todo lote mesmo com folga na média.
    """
    if max_eps <= 0:
        return n
    max_eps = min(int(max_eps), MAX_EPS_CEILING)
    burst = max_eps * 2.0
    now = time.monotonic()
    tokens, last = _buckets.get(session_id, (burst, now))
    tokens = min(burst, tokens + (now - last) * max_eps)
    allowed = int(min(n, tokens))
    tokens -= allowed
    _buckets[session_id] = (tokens, now)
    if allowed < n:
        _skipped[session_id] = _skipped.get(session_id, 0) + (n - allowed)
    return allowed


def skipped(session_id: str) -> int:
    """Quantos eventos o backstop rejeitou nesta sessão, neste processo."""
    return _skipped.get(session_id, 0)


def reset(session_id: Optional[str] = None) -> None:
    """Zera o estado (uma sessão, ou tudo). Estado de módulo — usado nos testes
    e ao encerrar uma sessão."""
    if session_id is None:
        _buckets.clear()
        _skipped.clear()
        return
    _buckets.pop(session_id, None)
    _skipped.pop(session_id, None)


def session_params(meta: Dict[str, str]) -> Tuple[int, int, bool]:
    """``(capture_percent, max_eps, capture_wire)`` de uma meta de sessão.

    Clampa e tolera ausência: sessões criadas antes destes campos existirem caem
    nos defaults, sem migração.
    """
    try:
        pct = int(meta.get("capture_percent") or DEFAULT_CAPTURE_PERCENT)
    except (TypeError, ValueError):
        pct = DEFAULT_CAPTURE_PERCENT
    try:
        eps = int(meta.get("max_eps") or DEFAULT_MAX_EPS)
    except (TypeError, ValueError):
        eps = DEFAULT_MAX_EPS
    return (
        max(1, min(pct, 100)),
        max(1, min(eps, MAX_EPS_CEILING)),
        str(meta.get("capture_wire") or "0") == "1",
    )
