"""On-demand, time-bounded traffic capture for troubleshooting.

A capture SESSION records the LIFECYCLE of every event of an organization — optionally
filtered by vendor — into a dedicated, short-lived Redis ring, for a bounded window.
Unlike the always-on audit ring (:mod:`audit_buffer`: last 500, 24h, all vendors), a
session is EXPLICIT (the operator starts/stops it), SCOPED (org + optional vendor) and
TIME-BOXED — so the operator captures exactly the traffic of a specific client/vendor
while troubleshooting ("press listening, watch what flows, filtered").

TAP DE CICLO DE VIDA (não só de entrega). Cada registro carrega um ``outcome`` — o
DESFECHO daquele evento (ver :data:`OUTCOMES`) — para o operador responder "como entrou
e como saiu aquele log". Antes deste tap, o único ponto de gravação ficava atrás da
guarda ``accepted_total > 0`` do dispatch: tudo que era coletado mas NÃO entregue
(drop, sem rota, quarentena, sink fora do ar, breaker, suppress, sample) era INVISÍVEL —
o operador via "capturei nada" sem distinguir "não houve tráfego" de "morreu antes do
tap". Um evento entregue a N destinos gera N registros ``delivered`` (desfecho POR
destino, desejável); o ring (``MAX_RING_SIZE``) e o TTL continuam limitando o volume.

Reuses :func:`audit_buffer._redact` so PII/secrets never hit the ring. Best-effort on
the hot path (:func:`record` / :func:`record_sync`): a failure to record NEVER affects
dispatch nor collection. Keys carry a Redis TTL (window + grace) so abandoned sessions
self-expire.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from typing import Any, Dict, List, Mapping, Optional, Sequence

import redis.asyncio as redis_async

from . import capture_admission
from .audit_buffer import _redact

logger = logging.getLogger(__name__)

DEFAULT_DURATION_SECONDS = 300
MAX_DURATION_SECONDS = 3600
DEFAULT_RING_SIZE = 5000
MAX_RING_SIZE = 20000
# Mantém os eventos legíveis um tempo após a janela fechar (operador revisa depois).
GRACE_SECONDS = 300
# Teto de sessões simultâneas POR ORG — anti-abuso (cada sessão tem um ring; sem teto,
# um admin poderia exaurir a memória do Redis e o loop de record() ficaria O(N)).
MAX_SESSIONS_PER_ORG = 5
# Teto do texto livre de ``detail`` (motivo curto) — o ring não é lugar de stacktrace.
MAX_DETAIL_CHARS = 200
# Teto por CAMPO (em chars, via ``quarantine._reduce_structure``) e por ENTRADA
# serializada (em bytes). O de entrada é o guard DURO; o de campo é otimização —
# ``_reduce_structure`` corta por caractere, então uma string toda acentuada pode
# passar de 8 KiB reais. Nunca usar ``decode(..., "ignore")``: corta codepoint no meio.
MAX_FIELD_BYTES = 8192
MAX_ENTRY_BYTES = 65_536
# Teto de bytes RESIDENTES por sessão. O ring de captura divide o Redis com o
# dedupe, que roda sob ``volatile-lru`` — e as chaves de dedupe TAMBÉM têm TTL,
# logo são candidatas à mesma evicção. Evictar dedupe significa REENTREGA
# silenciosa de log de segurança (há incidente documentado de 310k chaves).
# Um diagnóstico não pode ser a causa de entrega duplicada.
# 24 MiB acomoda o ring DEFAULT cheio (5.000 × ~3,4 KB = 17 MB) sem nunca morder:
# o teto só age em quem sobe o ring, e aí ENCURTA o ring — nunca encerra a sessão.
CAPTURE_SESSION_MAX_BYTES = 25_165_824
# Teto GLOBAL de sessões simultâneas. Sem ele, N orgs × 5 sessões crescem
# linearmente contra um Redis compartilhado de 512 MB. 8 × 24 MiB = 192 MiB =
# 37,5% do maxmemory, determinístico.
MAX_ACTIVE_SESSIONS_GLOBAL = 8


# ── Desfechos (outcome) ────────────────────────────────────────────────
#: Entregue a UM destino (um registro por destino que aceitou).
OUTCOME_DELIVERED = "delivered"
#: O lote chegou ao dispatch mas NÃO foi entregue (destino ausente/desabilitado,
#: cross-tenant, rejeição 4xx do sink, breaker aberto, sink fora do ar).
OUTCOME_DELIVERY_FAILED = "delivery_failed"
#: Descartado por uma rota ``action=drop`` (filtro de ruído / controle de custo).
OUTCOME_DROPPED = "dropped"
#: Nenhuma rota casou e não há destino default → DLQ (``error_kind=unrouted``).
OUTCOME_UNROUTED = "unrouted"
#: Fonte Wazuh suprimida de um destino que voltaria ao próprio manager (anti-loop).
OUTCOME_LOOP_BLOCKED = "loop_blocked"
#: Par (evento, destino) excluído por conflito de residência de dados.
OUTCOME_RESIDENCY_BLOCKED = "residency_blocked"
#: Amostrado PARA FORA de um destino pela alavanca de redução (sample_percent).
OUTCOME_SAMPLED_OUT = "sampled_out"
#: Suprimido pelo rate-limit por assinatura (Number-to-Allow) antes do roteamento.
OUTCOME_SUPPRESSED = "suppressed"
#: Quarentenado na normalização/validação (mapping ausente, OCSF inválido, ...).
OUTCOME_QUARANTINED = "quarantined"
#: COLETADO do vendor, antes de qualquer normalização. É o "como era antes".
OUTCOME_RECEIVED = "received"
#: Rejeitado pelo dedupe (``claim()``) — já visto dentro da janela de TTL.
#: Antes deste desfecho o evento sumia SEM registro: o ``continue`` do dedupe
#: acontece antes de qualquer tap, e essa é a causa nº1 de "meu evento não
#: apareceu". Era estruturalmente invisível.
OUTCOME_DEDUPED = "deduped"

#: Vocabulário fechado dos desfechos (a UI pode filtrar/agrupar por ele).
OUTCOMES = frozenset(
    {
        OUTCOME_DELIVERED,
        OUTCOME_DELIVERY_FAILED,
        OUTCOME_DROPPED,
        OUTCOME_UNROUTED,
        OUTCOME_LOOP_BLOCKED,
        OUTCOME_RESIDENCY_BLOCKED,
        OUTCOME_SAMPLED_OUT,
        OUTCOME_SUPPRESSED,
        OUTCOME_QUARANTINED,
        OUTCOME_RECEIVED,
        OUTCOME_DEDUPED,
    }
)

# ── Estágio (stage) ────────────────────────────────────────────────────
# ORTOGONAL a ``outcome``: o desfecho diz O QUE ACONTECEU, o estágio diz QUAL
# TRANSFORMAÇÃO o payload gravado já sofreu. Sem ele a tela mente por omissão —
# hoje o MESMO evento aparece com TRÊS normalizações diferentes na mesma lista,
# sem rótulo: ``quarantined`` traz o raw íntegro, ``dropped``/``sampled_out``
# trazem o envelope PRÉ-transformação-por-destino, e ``delivered`` traz PÓS
# redação de PII, PÓS drop_raw e PÓS aggregate.
STAGE_COLLECTED = "collected"
STAGE_ROUTED = "routed"
STAGE_DELIVERED = "delivered"

STAGES = frozenset({STAGE_COLLECTED, STAGE_ROUTED, STAGE_DELIVERED})

#: Forma do payload gravado, para a UI não prometer o que não tem.
PAYLOAD_VENDOR_WIRE = "vendor_wire"
PAYLOAD_VENDOR_RAW = "vendor_raw"
PAYLOAD_ENVELOPE = "envelope"
PAYLOAD_AGGREGATE_METRIC = "aggregate_metric"

#: Versão do formato de entrada do ring. v2 é ADITIVO — nenhuma chave do v1 foi
#: renomeada nem removida, e o payload continua em ``event``. Registros v1 vivos
#: (TTL ≤ 3.900 s) seguem legíveis via :func:`normalize_entry`, sem migração.
ENTRY_VERSION = 2


class CaptureLimitReached(RuntimeError):
    """O org atingiu ``MAX_SESSIONS_PER_ORG`` sessões simultâneas."""


def _meta_key(session_id: str) -> str:
    return f"capture:session:{session_id}:meta"


def _events_key(session_id: str) -> str:
    return f"capture:session:{session_id}:events"


def _org_index_key(org_id: int) -> str:
    return f"capture:sessions:org:{org_id}"


def _global_index_key() -> str:
    """Índice de TODAS as sessões ativas, para o teto global de memória."""
    return "capture:sessions:global"


async def _prune_global_index(redis: redis_async.Redis) -> int:
    """Poda ids cujo meta já sumiu e devolve quantas sessões AINDA vivem.

    Sem a poda o índice acumularia ids mortos e, com um teto de 8, bloquearia
    novas sessões PARA SEMPRE depois das 8 primeiras expirarem — um teto de
    memória viraria uma negação de serviço da própria feature. O índice tem TTL
    próprio, mas ele só cobre o caso de abandono total; dentro da janela é esta
    poda que mantém a contagem honesta.
    """
    ids = await redis.smembers(_global_index_key())
    alive = 0
    for raw_id in ids:
        sid = _s(raw_id)
        if await redis.exists(_meta_key(sid)):
            alive += 1
        else:
            await redis.srem(_global_index_key(), sid)
    return alive


def _s(value: Any) -> str:
    return value.decode() if isinstance(value, (bytes, bytearray)) else str(value)


# ── Short-circuit barato (cache NEGATIVO) ──────────────────────────────
# O tap agora é chamado em vários pontos do hot path (por lote no roteamento, por
# evento suprimido/quarentenado). O caso ESMAGADORAMENTE comum é "org sem nenhuma
# sessão ativa" — e descobrir isso custava um round-trip Redis por chamada.
# Memoizamos APENAS a ausência (nunca a presença) por uma janela curta: um org sem
# sessão pula o Redis inteiro; um org COM sessão relê sempre (nada de evento perdido
# por cache velho). ``start_session`` invalida o próprio processo; outros processos
# (API inicia a sessão, worker grava) convergem em ≤ ``_NO_SESSION_TTL_SECONDS``.
_NO_SESSION_TTL_SECONDS = 2.0
_NO_SESSION_CACHE_MAX = 10_000
_no_session_until: Dict[int, float] = {}


def _absent_cached(org_id: Any, now: float) -> bool:
    try:
        return _no_session_until.get(int(org_id), 0.0) > now
    except (TypeError, ValueError):
        return False


def _mark_absent(org_id: Any, now: float) -> None:
    try:
        key = int(org_id)
    except (TypeError, ValueError):
        return
    if len(_no_session_until) >= _NO_SESSION_CACHE_MAX:
        # Evicta o decil MAIS ANTIGO, não o cache inteiro. Um ``clear()`` manda
        # 10.000 orgs de volta ao Redis no MESMO instante (thundering herd) —
        # justo no momento em que o cache está sob pressão.
        victims = sorted(_no_session_until, key=_no_session_until.__getitem__)
        for victim in victims[: max(1, _NO_SESSION_CACHE_MAX // 10)]:
            _no_session_until.pop(victim, None)
    _no_session_until[key] = now + _NO_SESSION_TTL_SECONDS


# ── Breaker do tap ─────────────────────────────────────────────────────
# O tap grava com um cliente Redis SÍNCRONO, e o tap de roteamento roda DENTRO
# do event loop da coleta. O ``try/except`` best-effort protege contra EXCEÇÃO,
# não contra LATÊNCIA: sem ``socket_timeout`` (o estado anterior), um Redis lento
# — não caído — pendura a coleta inteira, e o ciclo estoura o soft-timeout do
# Celery, cuja consequência documentada é reversão de cursor (perda de janela).
#
# Timeout sozinho não basta: ele converte um hang indeterminado numa parada
# DETERMINÍSTICA e repetida. Com 5 sessões e 3 falhas até abrir, o pior caso de
# loop bloqueado é 3 × 0,25 s × 5 = 3,75 s por janela de 30 s — ~200× melhor que
# o infinito anterior, mas ainda alto o bastante para exigir o breaker.
_TAP_SOCKET_TIMEOUT_SECONDS = 0.25
_TAP_FAIL_THRESHOLD = 3
_TAP_COOLDOWN_SECONDS = 30.0
_tap_fails = 0
_tap_blind_until = 0.0


def _tap_blind(now: Optional[float] = None) -> bool:
    """True enquanto o breaker está aberto (captura cega, coleta protegida)."""
    return _tap_blind_until > (now if now is not None else time.monotonic())


def _tap_ok() -> None:
    """Sucesso no Redis: zera o contador de falhas consecutivas."""
    global _tap_fails
    _tap_fails = 0


def _tap_failed() -> None:
    """Falha (exceção OU timeout). Abre o breaker no limiar."""
    global _tap_fails, _tap_blind_until
    _tap_fails += 1
    if _tap_fails >= _TAP_FAIL_THRESHOLD and not _tap_blind():
        _tap_blind_until = time.monotonic() + _TAP_COOLDOWN_SECONDS
        try:
            from .metrics import CAPTURE_TAP_DISABLED

            CAPTURE_TAP_DISABLED.inc()
        except Exception:  # noqa: BLE001 — métrica nunca quebra o hot path
            pass
        # UM warning por cooldown, não por falha: sob Redis morto isto seria
        # ruído de milhares de linhas/s no caminho de coleta.
        logger.warning(
            "capture: tap DESABILITADO por %.0fs após %d falhas consecutivas de Redis "
            "(a captura fica cega; a coleta segue normal)",
            _TAP_COOLDOWN_SECONDS,
            _tap_fails,
        )


def reset_tap_breaker() -> None:
    """Zera o breaker. Estado de módulo — usado pelos testes."""
    global _tap_fails, _tap_blind_until
    _tap_fails = 0
    _tap_blind_until = 0.0


def reset_session_cache(org_id: Optional[int] = None) -> None:
    """Invalida o cache negativo (um org, ou tudo). Chamado por ``start_session`` e
    pelos testes — o cache é estado de módulo."""
    if org_id is None:
        _no_session_until.clear()
        return
    try:
        _no_session_until.pop(int(org_id), None)
    except (TypeError, ValueError):
        pass


# ── Cliente SYNC (produtor/roteamento) ─────────────────────────────────
# O tap de roteamento (``_enqueue_routed``) e o de quarentena rodam em contexto
# SÍNCRONO. Espelha o cliente fork-safe cacheado de ``observability_store`` (mesma
# ``REDIS_URL``, mesmo ``decode_responses``) para que produtor (sync), worker (async)
# e API compartilhem o MESMO ring.
_sync_client = None
_sync_client_pid: Optional[int] = None
_sync_lock = threading.Lock()


def _sync_redis():
    global _sync_client, _sync_client_pid
    pid = os.getpid()
    if _sync_client is not None and _sync_client_pid == pid:
        return _sync_client
    with _sync_lock:
        if _sync_client is None or _sync_client_pid != pid:
            import redis as redis_sync

            from ..core.config import settings

            # ``socket_timeout`` é OBRIGATÓRIO aqui, e é o valor mais agressivo
            # do repo (o cliente geral usa 5 s, o Celery 10 s) porque este é o
            # ÚNICO cliente chamado de dentro do event loop da coleta. Captura é
            # diagnóstico: perder um registro é infinitamente melhor que atrasar
            # a coleta. ``retry_on_timeout=False`` pelo mesmo motivo — um retry
            # dobraria a janela de bloqueio.
            _sync_client = redis_sync.from_url(
                settings.REDIS_URL or "redis://localhost:6379/0",
                decode_responses=True,
                socket_timeout=_TAP_SOCKET_TIMEOUT_SECONDS,
                socket_connect_timeout=_TAP_SOCKET_TIMEOUT_SECONDS,
                retry_on_timeout=False,
                health_check_interval=30,
            )
            _sync_client_pid = pid
    return _sync_client


# ── Partes PURAS compartilhadas pelos taps async e sync ────────────────


def _event_vendor(ev: Any) -> Optional[str]:
    """``_centralops.vendor`` do envelope, tolerante a payloads não-dict."""
    if not isinstance(ev, Mapping):
        return None
    labels = ev.get("_centralops")
    if not isinstance(labels, Mapping):
        return None
    vendor = labels.get("vendor")
    return None if vendor is None else str(vendor)


def _vendor_matches(vfilter: str, vendor: Optional[str]) -> bool:
    """Filtro de vendor da sessão, CASE-INSENSITIVE: uma sessão criada como "Sophos"
    casa eventos rotulados "sophos" (o operador digita o nome, o coletor emite o slug).
    Filtro vazio = casa tudo."""
    if not vfilter:
        return True
    if vendor is None:
        return False
    return vendor.strip().casefold() == vfilter.casefold()


def _session_is_active(m: Mapping[str, str], now: float) -> bool:
    return m.get("status") == "active" and float(m.get("expires_at") or 0) >= now


def _ring_params(m: Mapping[str, str], now: float) -> tuple:
    """(ring_size clampado, TTL do ring de eventos, teto de bytes) da sessão."""
    try:
        ring_size = int(m.get("ring_size") or DEFAULT_RING_SIZE)
    except (TypeError, ValueError):
        ring_size = DEFAULT_RING_SIZE
    ring_size = max(1, min(ring_size, MAX_RING_SIZE))
    # TTL próprio no ring de eventos (janela restante + graça) — senão o ring vira
    # órfão quando o meta expira. Renovado a cada gravação.
    evt_ttl = max(
        GRACE_SECONDS,
        int(float(m.get("expires_at") or now) - now) + GRACE_SECONDS,
    )
    try:
        ring_bytes = int(m.get("ring_bytes") or CAPTURE_SESSION_MAX_BYTES)
    except (TypeError, ValueError):
        ring_bytes = CAPTURE_SESSION_MAX_BYTES
    ring_bytes = max(64 * 1024, min(ring_bytes, CAPTURE_SESSION_MAX_BYTES))
    return ring_size, evt_ttl, ring_bytes


# ── Append atômico com orçamento de bytes RESIDENTES ───────────────────
# ``LTRIM`` descarta entradas SEM devolver nem contar o que descartou — logo
# qualquer ``HINCRBY bytes_escritos`` mede bytes CUMULATIVOS, não residência, e
# um teto sobre esse número dispararia com o ring pela metade, matando a sessão
# no meio do troubleshooting. A única forma de a contabilidade ser EXATA é
# evictar com ``RPOP`` e DECREMENTAR pelo tamanho do que saiu — o que exige
# atomicidade, e portanto um script.
#
# De quebra colapsa 5 round-trips em 1. O fallback (abaixo) preserva o
# comportamento anterior e MARCA a degradação no meta, nunca em silêncio.
_APPEND_LUA = """
local events_key = KEYS[1]
local meta_key = KEYS[2]
local ring_size = tonumber(ARGV[1])
local ring_bytes = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
local outcome = ARGV[4]
local n = 0
local added = 0
for i = 5, #ARGV do
  redis.call('LPUSH', events_key, ARGV[i])
  added = added + string.len(ARGV[i])
  n = n + 1
end
redis.call('HINCRBY', meta_key, 'event_count', n)
redis.call('HINCRBY', meta_key, 'outcome:' .. outcome, n)
local used = redis.call('HINCRBY', meta_key, 'ring_bytes_used', added)
while redis.call('LLEN', events_key) > ring_size do
  local popped = redis.call('RPOP', events_key)
  if not popped then break end
  used = redis.call('HINCRBY', meta_key, 'ring_bytes_used', -string.len(popped))
end
while used > ring_bytes do
  local popped = redis.call('RPOP', events_key)
  if not popped then break end
  used = redis.call('HINCRBY', meta_key, 'ring_bytes_used', -string.len(popped))
end
redis.call('EXPIRE', events_key, ttl)
redis.call('EXPIRE', meta_key, ttl)
return {n, used}
"""
_append_sha: Optional[str] = None


def _append_entries(r: Any, sid: str, entries: List[str], *, ring_size: int,
                    evt_ttl: int, ring_bytes: int, outcome: str) -> None:
    """Append + poda por CONTAGEM e por BYTES RESIDENTES, atômico.

    Cai no pipeline de comandos anterior se o EVAL falhar por qualquer motivo
    (script desabilitado, CROSSSLOT em cluster, fake de teste sem suporte),
    gravando ``budget_enforcement=unavailable`` no meta — a UI exibe. Degradação
    ANUNCIADA; o que não pode acontecer é o operador achar que o teto está valendo.
    """
    global _append_sha
    args = [ring_size, ring_bytes, evt_ttl, outcome, *entries]
    try:
        if _append_sha is None:
            _append_sha = r.script_load(_APPEND_LUA)
        r.evalsha(_append_sha, 2, _events_key(sid), _meta_key(sid), *args)
        return
    except Exception as exc:  # noqa: BLE001 — qualquer falha cai no fallback
        msg = str(exc).upper()
        if "NOSCRIPT" in msg:
            try:
                _append_sha = r.script_load(_APPEND_LUA)
                r.evalsha(_append_sha, 2, _events_key(sid), _meta_key(sid), *args)
                return
            except Exception:  # noqa: BLE001
                pass
        logger.debug("capture: EVAL indisponível, usando fallback (%s)", exc)

    pipe = r.pipeline()
    pipe.lpush(_events_key(sid), *entries)
    pipe.ltrim(_events_key(sid), 0, ring_size - 1)
    pipe.expire(_events_key(sid), evt_ttl)
    pipe.hincrby(_meta_key(sid), "event_count", len(entries))
    # Contador POR DESFECHO no meta: sobrevive à poda do ring (o ltrim
    # descarta eventos antigos, o contador não), então a UI distingue
    # "sessão ativa e nada aconteceu" de "houve tráfego, mas rolou".
    pipe.hincrby(_meta_key(sid), f"outcome:{outcome}", len(entries))
    # O teto de BYTES não vale neste caminho — e o operador precisa saber.
    pipe.hset(_meta_key(sid), "budget_enforcement", "unavailable")
    pipe.execute()


def _dumps(payload: Mapping[str, Any]) -> str:
    """Serializa uma entrada do ring com o MESMO codec do resto do pipeline.

    O ``json`` do stdlib escapa não-ASCII (``\\uXXXX``), inflando payload pt-BR/es
    em ~1,29× — enquanto ``_envelope_bytes`` (routing/engine.py) e a entrega usam
    orjson com UTF-8 bruto. Sem isto o orçamento de bytes do ring mediria uma
    unidade diferente da contabilidade de custo do sistema.
    """
    from .output._fastjson import dumps_bytes

    return dumps_bytes(payload).decode("utf-8")


def _clip_for_ring(payload: Dict[str, Any]) -> tuple[str, Optional[Dict[str, Any]]]:
    """Serializa respeitando ``MAX_ENTRY_BYTES``, em CASCATA e sempre JSON válido.

    Antes disto não havia teto nenhum no caminho de captura — só ``detail`` era
    truncado (200 chars) e o evento entrava inteiro. Um CloudWatch/Defender de
    centenas de KB tornava a estimativa "ring_size × tamanho típico" inválida, e o
    pior caso era ilimitado por design.

    Cascata: (1) reduz a ESTRUTURA reusando ``quarantine._reduce_structure``, que
    já clipa string e limita lista preservando o shape; (2) se ainda estourar,
    substitui o bloco ``raw`` inteiro por um marcador; (3) depois ``normalized``.
    ``_centralops`` NUNCA é descartado — sem ele o registro perde a identidade e
    deixa de ser juntável.

    NUNCA cortar a string JÁ SERIALIZADA: isso produz JSON inválido, e o repo tem
    o incidente documentado (``quarantine.py``: quebrou o reprocesso).

    Devolve ``(json, meta_de_corte_ou_None)``. O meta vai para o namespace
    ``_capture`` da ENTRADA, jamais dentro de ``event`` — o export mascara
    ``entry["event"]``, e metadado do tap não pode ser confundido com dado do
    vendor.
    """
    text = _dumps(payload)
    if len(text.encode("utf-8")) <= MAX_ENTRY_BYTES:
        return text, None

    from .quarantine import _reduce_structure

    notes: List[Dict[str, Any]] = []
    dropped: List[str] = []
    original_bytes = len(text.encode("utf-8"))
    event = payload.get("event")

    if isinstance(event, dict):
        payload = dict(payload)
        payload["event"] = _reduce_structure(
            event, str_cap=MAX_FIELD_BYTES, array_cap=200
        )
        notes.append({"path": "event", "how": "reduce_structure"})
        text = _dumps(payload)

        for block in ("raw", "normalized"):
            if len(text.encode("utf-8")) <= MAX_ENTRY_BYTES:
                break
            blk = payload["event"].get(block)
            if blk is None:
                continue
            payload["event"] = dict(payload["event"])
            payload["event"][block] = {
                "__truncated__": True,
                "reason": "entry_cap",
            }
            dropped.append(block)
            text = _dumps(payload)

    meta = {
        "truncated": notes,
        "dropped_blocks": dropped,
        "original_bytes": original_bytes,
        "kept_bytes": len(text.encode("utf-8")),
    }
    return text, meta


def _event_id_of(ev: Any) -> Optional[str]:
    """``_centralops.event_id`` do envelope, quando houver.

    NUNCA recomputa via ``compute_event_id``: com ``raw_reduction`` ativo, o
    envelope foi construído sobre o raw REDUZIDO, então recomputar sobre o raw
    original produziria um id diferente e quebraria a junção do jornal — em
    silêncio, que é o pior modo de falha possível para uma chave de correlação.
    """
    if not isinstance(ev, Mapping):
        return None
    labels = ev.get("_centralops")
    if not isinstance(labels, Mapping):
        return None
    eid = labels.get("event_id")
    return str(eid) if eid else None


def normalize_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Preenche os campos do v2 numa entrada lida do ring, seja ela v1 ou v2.

    Centraliza a retrocompatibilidade num ponto só, em vez de espalhar ``.get()``
    com default por todo leitor. Registros v1 continuam no ring por até 3.900 s
    após o deploy — e não há migração nem dual-write, porque o payload nunca saiu
    da chave ``event``.
    """
    if entry.get("v"):
        return entry
    entry["v"] = 1
    # v1 só existia no tap de roteamento e no de entrega; ambos gravavam o
    # envelope pós-roteamento. ``routed`` é o rótulo honesto para os dois.
    entry.setdefault("stage", STAGE_ROUTED)
    entry.setdefault("payload_kind", PAYLOAD_ENVELOPE)
    entry.setdefault("pii_redacted", False)
    if "event_id" not in entry:
        entry["event_id"] = _event_id_of(entry.get("event"))
    return entry


def _entries_for(
    m: Mapping[str, str],
    batch: Sequence[Any],
    now: float,
    outcome: str,
    destination_id: Optional[str],
    detail: Optional[str],
    route_id: Optional[str] = None,
    *,
    stage: str = STAGE_ROUTED,
    payload_kind: str = PAYLOAD_ENVELOPE,
    pii_redacted: bool = False,
    event_ids: Optional[Sequence[Optional[str]]] = None,
    destination_kind: Optional[str] = None,
    dest_config_version: Optional[str] = None,
    wires: Optional[Sequence[Optional[Dict[str, Any]]]] = None,
) -> List[str]:
    """Serializa os eventos do lote que passam pelo filtro de vendor DESTA sessão.

    Formato COMPATÍVEL com o que a UI já lê (``event``/``vendor``/``captured_at``);
    ``outcome`` é sempre adicionado e ``destination_id``/``detail``/``route_id`` só
    quando aplicáveis (mantém o ring enxuto).

    ``route_id`` é ESTRUTURADO (campo próprio), não texto dentro de ``detail``: é a
    rota responsável pelo desfecho — para o operador responder "em qual rota bateu"
    e "por que foi dropado" sem parsear string livre."""
    vfilter = (m.get("vendor") or "").strip()
    out: List[str] = []
    for idx, ev in enumerate(batch):
        vendor = _event_vendor(ev)
        if not _vendor_matches(vfilter, vendor):
            continue
        # ``event_ids``/``wires`` são POR EVENTO (indexados pelo lote), ao
        # contrário de route_id/destination_id, que são escalares do grupo. O
        # id vem do caller quando ele o conhece antes do envelope existir (tap
        # de coleta); senão é extraído do próprio envelope.
        eid = (
            event_ids[idx]
            if event_ids is not None and idx < len(event_ids)
            else _event_id_of(ev)
        )
        payload: Dict[str, Any] = {
            "v": ENTRY_VERSION,
            "event": _redact(ev),
            "vendor": vendor,
            "captured_at": now,
            "outcome": outcome,
            "stage": stage,
            "payload_kind": payload_kind,
            "pii_redacted": pii_redacted,
            "event_id": eid,
        }
        if destination_kind:
            payload["destination_kind"] = str(destination_kind)
        if dest_config_version:
            payload["dest_config_version"] = str(dest_config_version)
        if wires is not None and idx < len(wires) and wires[idx] is not None:
            payload["wire"] = wires[idx]
        if destination_id is not None:
            payload["destination_id"] = str(destination_id)
        if route_id:
            payload["route_id"] = str(route_id)
        if detail:
            payload["detail"] = str(detail)[:MAX_DETAIL_CHARS]
        text, clip_meta = _clip_for_ring(payload)
        if clip_meta is not None:
            # Re-serializa com o marcador. O ``_capture`` fica FORA de ``event``
            # de propósito: o export mascara ``entry["event"]``, e um metadado do
            # tap não pode ser lido como dado do vendor.
            payload["_capture"] = clip_meta
            text, _ = _clip_for_ring(payload)
        out.append(text)
    return out


def _decode_meta(meta: Mapping[Any, Any]) -> Dict[str, Any]:
    """Normaliza o hash do Redis (bytes|str) para um dict tipado + status derivado."""
    m = {_s(k): _s(v) for k, v in meta.items()}
    now = time.time()
    expires_at = float(m.get("expires_at") or 0)
    raw_status = m.get("status") or "active"
    # 'active' só enquanto não expirou nem foi parado explicitamente.
    status = "expired" if (raw_status == "active" and expires_at < now) else raw_status
    return {
        "id": m.get("id", ""),
        "organization_id": int(m["org_id"]) if m.get("org_id") else None,
        "vendor": m.get("vendor") or None,
        "created_at": float(m["created_at"]) if m.get("created_at") else None,
        "expires_at": expires_at or None,
        "status": status,
        "event_count": int(m.get("event_count") or 0),
        "capture_percent": int(m.get("capture_percent") or 100),
        "max_eps": int(m.get("max_eps") or capture_admission.DEFAULT_MAX_EPS),
        "capture_wire": str(m.get("capture_wire") or "0") == "1",
        "ring_bytes": int(m.get("ring_bytes") or CAPTURE_SESSION_MAX_BYTES),
        "ring_bytes_used": int(m.get("ring_bytes_used") or 0),
        # "unavailable" quando o append caiu no fallback sem EVAL: o teto de
        # bytes NÃO está sendo aplicado, e a UI precisa dizer isso.
        "budget_enforcement": m.get("budget_enforcement") or "active",
    }


async def start_session(
    redis: redis_async.Redis,
    org_id: int,
    *,
    vendor: Optional[str] = None,
    duration_seconds: int = DEFAULT_DURATION_SECONDS,
    ring_size: int = DEFAULT_RING_SIZE,
    capture_percent: int = capture_admission.DEFAULT_CAPTURE_PERCENT,
    max_eps: int = capture_admission.DEFAULT_MAX_EPS,
    capture_wire: bool = False,
) -> Dict[str, Any]:
    """Inicia uma sessão de captura escopada a ``org_id`` (e opcionalmente ``vendor``)."""
    # Anti-abuso: teto de sessões simultâneas por org.
    active = await redis.scard(_org_index_key(org_id))
    if active >= MAX_SESSIONS_PER_ORG:
        raise CaptureLimitReached(
            f"limite de {MAX_SESSIONS_PER_ORG} sessões de captura simultâneas atingido"
        )
    # Teto GLOBAL. O por-org sozinho não limita nada: N orgs × 5 sessões crescem
    # linearmente contra um Redis COMPARTILHADO com o dedupe. A mensagem diz
    # "global" explicitamente — dizer "da sua org" mandaria o operador fechar
    # sessões que não são a causa.
    active_global = await _prune_global_index(redis)
    if active_global >= MAX_ACTIVE_SESSIONS_GLOBAL:
        raise CaptureLimitReached(
            f"limite GLOBAL de {MAX_ACTIVE_SESSIONS_GLOBAL} sessões de captura "
            "simultâneas atingido (todas as organizações somadas) — aguarde uma "
            "sessão encerrar"
        )
    duration = max(1, min(int(duration_seconds), MAX_DURATION_SECONDS))
    size = max(1, min(int(ring_size), MAX_RING_SIZE))
    now = time.time()
    session_id = uuid.uuid4().hex
    meta = {
        "id": session_id,
        "org_id": str(org_id),
        "vendor": (vendor or "").strip(),
        # str() (não repr()) garante string decimal parseável (contrato documentado).
        "created_at": str(now),
        "expires_at": str(now + duration),
        "ring_size": str(size),
        "status": "active",
        "event_count": "0",
        # Parâmetros lidos pelo WORKER a cada lote. Ficam no meta (que já viaja
        # inteiro em ``active_sessions_sync``) para a decisão "admito? gravo
        # wire?" não custar round-trip extra.
        "capture_percent": str(max(1, min(int(capture_percent), 100))),
        "max_eps": str(max(1, min(int(max_eps), capture_admission.MAX_EPS_CEILING))),
        # OFF por default e deliberadamente: o custo de ``format()`` no
        # dispatcher é opt-in, e uma sessão criada pelo fluxo de hoje mantém
        # EXATAMENTE o custo de hoje.
        "capture_wire": "1" if capture_wire else "0",
        "ring_bytes": str(CAPTURE_SESSION_MAX_BYTES),
        "ring_bytes_used": "0",
    }
    ttl = duration + GRACE_SECONDS
    # TTL do índice é FIXO (não regride p/ a janela da última sessão) — senão uma
    # sessão curta encurtaria o índice e sumiria sessões longas dele.
    index_ttl = MAX_DURATION_SECONDS + GRACE_SECONDS
    pipe = redis.pipeline()
    pipe.hset(_meta_key(session_id), mapping=meta)
    pipe.expire(_meta_key(session_id), ttl)
    pipe.sadd(_org_index_key(org_id), session_id)
    pipe.expire(_org_index_key(org_id), index_ttl)
    pipe.sadd(_global_index_key(), session_id)
    pipe.expire(_global_index_key(), index_ttl)
    await pipe.execute()
    # invalida o cache negativo DESTE processo (os demais convergem pelo TTL curto).
    reset_session_cache(org_id)
    logger.info(
        "capture: sessão iniciada id=%s org=%s vendor=%s duração=%ss",
        session_id, org_id, vendor or "*", duration,
    )
    return _decode_meta(meta)


async def get_session(redis: redis_async.Redis, session_id: str) -> Optional[Dict[str, Any]]:
    meta = await redis.hgetall(_meta_key(session_id))
    if not meta:
        return None
    return _decode_meta(meta)


async def list_sessions(redis: redis_async.Redis, org_id: int) -> List[Dict[str, Any]]:
    """Sessões (ativas/expiradas/paradas) do tenant, mais recentes primeiro. Poda ids
    cujo meta já expirou no Redis (TTL)."""
    ids = await redis.smembers(_org_index_key(org_id))
    out: List[Dict[str, Any]] = []
    for raw_id in ids:
        sid = _s(raw_id)
        meta = await redis.hgetall(_meta_key(sid))
        if not meta:
            await redis.srem(_org_index_key(org_id), sid)  # TTL expirou o meta
            continue
        out.append(_decode_meta(meta))
    out.sort(key=lambda m: m.get("created_at") or 0, reverse=True)
    return out


async def stop_session(
    redis: redis_async.Redis, session_id: str, org_id: int
) -> bool:
    """Marca a sessão como parada (mantém os eventos legíveis até o TTL). Verifica o
    org DONO no próprio engine (defense-in-depth: não confia só no gate HTTP) — uma
    sessão de outro tenant nunca é alterada."""
    meta = await redis.hgetall(_meta_key(session_id))
    if not meta:
        return False
    m = {_s(k): _s(v) for k, v in meta.items()}
    if int(m.get("org_id") or -1) != int(org_id):
        return False
    await redis.hset(_meta_key(session_id), "status", "stopped")
    # Libera o slot do teto GLOBAL na hora. O meta continua vivo até o TTL (os
    # eventos seguem legíveis para revisão), então sem este SREM uma sessão
    # parada seguraria o slot por até 1h — o operador pararia a sessão e mesmo
    # assim não conseguiria abrir outra.
    await redis.srem(_global_index_key(), session_id)
    return True


async def delete_session(redis: redis_async.Redis, session_id: str, org_id: int) -> None:
    """Remove a sessão + seus eventos imediatamente (dado sensível)."""
    pipe = redis.pipeline()
    pipe.delete(_meta_key(session_id))
    pipe.delete(_events_key(session_id))
    pipe.srem(_org_index_key(org_id), session_id)
    pipe.srem(_global_index_key(), session_id)
    await pipe.execute()


async def read_events(
    redis: redis_async.Redis, session_id: str, *, limit: int = 200
) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit), MAX_RING_SIZE))
    raw = await redis.lrange(_events_key(session_id), 0, limit - 1)
    events: List[Dict[str, Any]] = []
    for item in raw:
        try:
            events.append(normalize_entry(json.loads(_s(item))))
        except Exception:  # pragma: no cover — entrada corrompida é ignorada
            continue
    return events


EXPORT_PAGE_SIZE = 500


async def iter_events(
    redis: redis_async.Redis,
    session_id: str,
    *,
    page_size: int = EXPORT_PAGE_SIZE,
    max_events: int = MAX_RING_SIZE,
):
    """Itera os eventos do ring em PÁGINAS (``LRANGE`` por bloco), para o export
    streamar sem materializar o ring inteiro na RAM da API — o ``read_events``
    carrega tudo de uma vez, e o export pode percorrer até 20k eventos.

    ``max_events`` é o teto duro de linhas percorridas (anti-exfiltração/OOM). O
    pico de memória é uma página, não o dataset."""
    page_size = max(1, min(int(page_size), MAX_RING_SIZE))
    max_events = max(1, min(int(max_events), MAX_RING_SIZE))
    key = _events_key(session_id)
    start = 0
    while start < max_events:
        stop = min(start + page_size, max_events) - 1
        raw = await redis.lrange(key, start, stop)
        if not raw:
            return
        for item in raw:
            try:
                yield normalize_entry(json.loads(_s(item)))
            except Exception:  # pragma: no cover — entrada corrompida é ignorada
                continue
        if len(raw) <= stop - start:  # última página (ring menor que o teto)
            return
        start = stop + 1


async def active_sessions(
    redis: redis_async.Redis, org_id: Any
) -> List[Dict[str, str]]:
    """Metas CRUAS das sessões ATIVAS do ``org_id`` (``[]`` = nada a capturar).

    Resolva UMA vez por lote e passe em ``sessions=`` quando for gravar vários
    desfechos do mesmo lote — evita reabrir o índice a cada desfecho. Best-effort:
    NUNCA levanta (devolve ``[]``). Usa o cache negativo (ver ``_no_session_until``)."""
    try:
        now = time.time()
        if _absent_cached(org_id, now):
            return []
        ids = await redis.smembers(_org_index_key(org_id))
        if not ids:
            _mark_absent(org_id, now)
            return []
        out: List[Dict[str, str]] = []
        for raw_id in ids:
            sid = _s(raw_id)
            meta = await redis.hgetall(_meta_key(sid))
            if not meta:
                await redis.srem(_org_index_key(org_id), sid)  # TTL expirou o meta
                continue
            m = {_s(k): _s(v) for k, v in meta.items()}
            if not _session_is_active(m, now):
                continue
            m.setdefault("id", sid)
            out.append(m)
        if not out:
            _mark_absent(org_id, now)
        return out
    except Exception as exc:  # pragma: no cover — nunca quebra o hot path
        # Redis fora do ar: memoiza a ausência para não tentar reconectar a CADA
        # lote/evento do hot path (a captura é diagnóstico, não pode virar custo).
        _mark_absent(org_id, time.time())
        logger.debug("capture_session.active_sessions falhou (não-fatal): %s", exc)
        return []


def likely_no_session(org_id: Any) -> bool:
    """Sonda EM MEMÓRIA (zero I/O) do cache negativo: True quando aprendemos há pouco
    que este org NÃO tem sessão de captura ativa.

    Existe para o caller pular o hop de thread-pool no hot path. Sem ela, um caminho
    de alto volume que grava desfecho POR EVENTO (supressão, no laço de coleta) paga
    ~50µs/evento só em troca de contexto para descobrir que não há nada a gravar —
    medido 130× mais caro que a chamada síncrona direta.

    CONSERVADORA: devolve False quando não sabemos (cache frio/expirado ou erro), aí o
    caminho normal decide. Nunca levanta.

    EXCEÇÃO à conservadoria: com o breaker ABERTO devolve True incondicionalmente.
    Aí não é mais "não sei" — sabemos que o Redis está degradado, e insistir custa
    ``socket_timeout`` por chamada dentro do event loop da coleta."""
    try:
        if _tap_blind():
            return True
        return _absent_cached(org_id, time.time())
    except Exception:  # noqa: BLE001 — sonda best-effort; na dúvida, não pula
        return False


def active_sessions_sync(org_id: Any, *, redis: Any = None) -> List[Dict[str, str]]:
    """Versão SÍNCRONA de :func:`active_sessions` (produtor/roteamento). Best-effort."""
    try:
        now = time.time()
        if _tap_blind():
            return []
        if _absent_cached(org_id, now):
            return []
        r = redis if redis is not None else _sync_redis()
        ids = r.smembers(_org_index_key(org_id))
        if not ids:
            _mark_absent(org_id, now)
            return []
        out: List[Dict[str, str]] = []
        for raw_id in ids:
            sid = _s(raw_id)
            meta = r.hgetall(_meta_key(sid))
            if not meta:
                r.srem(_org_index_key(org_id), sid)
                continue
            m = {_s(k): _s(v) for k, v in meta.items()}
            if not _session_is_active(m, now):
                continue
            m.setdefault("id", sid)
            out.append(m)
        if not out:
            _mark_absent(org_id, now)
        _tap_ok()
        return out
    except Exception as exc:  # pragma: no cover — nunca quebra a coleta/roteamento
        _tap_failed()
        _mark_absent(org_id, time.time())  # ver :func:`active_sessions`
        logger.debug("capture_session.active_sessions_sync falhou (não-fatal): %s", exc)
        return []


async def record(
    redis: redis_async.Redis,
    batch: Sequence[Any],
    org_id: Any,
    *,
    outcome: str = OUTCOME_DELIVERED,
    destination_id: Optional[str] = None,
    detail: Optional[str] = None,
    route_id: Optional[str] = None,
    sessions: Optional[Sequence[Mapping[str, str]]] = None,
) -> None:
    """Anexa o lote às sessões de captura ATIVAS do ``org_id`` com o DESFECHO
    ``outcome``, filtrando cada evento pelo vendor da sessão (case-insensitive).

    ``outcome`` default ``delivered`` (compatível com o call-site histórico do
    dispatch). ``destination_id`` identifica o destino quando o desfecho é por-destino
    (delivered / delivery_failed / residency_blocked / sampled_out); ``route_id`` é a
    rota responsável (estruturado); ``detail`` é um motivo CURTO (truncado em
    ``MAX_DETAIL_CHARS``) — juntos respondem "como entrou, por qual rota e como saiu".
    ``sessions`` reusa uma resolução prévia de :func:`active_sessions`.

    Best-effort: NUNCA levanta (chamado do hot path de dispatch/coleta)."""
    if not batch:
        return
    try:
        metas = (
            list(sessions)
            if sessions is not None
            else await active_sessions(redis, org_id)
        )
        if not metas:
            return
        now = time.time()
        for m in metas:
            sid = m.get("id") or ""
            if not sid:
                continue
            entries = _entries_for(m, batch, now, outcome, destination_id, detail, route_id)
            if not entries:
                continue
            ring_size, evt_ttl, _ring_bytes = _ring_params(m, now)
            pipe = redis.pipeline()
            pipe.lpush(_events_key(sid), *entries)
            pipe.ltrim(_events_key(sid), 0, ring_size - 1)
            pipe.expire(_events_key(sid), evt_ttl)
            pipe.hincrby(_meta_key(sid), "event_count", len(entries))
            # Contador POR DESFECHO no meta: sobrevive à poda do ring (o ltrim
            # descarta eventos antigos, o contador não), então a UI distingue
            # "sessão ativa e nada aconteceu" de "houve tráfego, mas rolou".
            pipe.hincrby(_meta_key(sid), f"outcome:{outcome}", len(entries))
            await pipe.execute()
    except Exception as exc:  # pragma: no cover — captura nunca quebra o dispatch
        logger.debug("capture_session.record falhou (não-fatal): %s", exc)


def record_sync(
    batch: Sequence[Any],
    org_id: Any,
    *,
    outcome: str = OUTCOME_DELIVERED,
    destination_id: Optional[str] = None,
    detail: Optional[str] = None,
    route_id: Optional[str] = None,
    sessions: Optional[Sequence[Mapping[str, str]]] = None,
    redis: Any = None,
    stage: str = STAGE_ROUTED,
    payload_kind: str = PAYLOAD_ENVELOPE,
    pii_redacted: bool = False,
    event_ids: Optional[Sequence[Optional[str]]] = None,
    destination_kind: Optional[str] = None,
    dest_config_version: Optional[str] = None,
    wires: Optional[Sequence[Optional[Dict[str, Any]]]] = None,
) -> None:
    """Versão SÍNCRONA de :func:`record` para os taps que rodam fora do event loop
    (roteamento no produtor, quarentena via ``asyncio.to_thread``). Mesmo ring, mesmo
    contrato best-effort — NUNCA levanta."""
    if not batch:
        return
    # Breaker: com o Redis degradado, cada chamada custaria ``socket_timeout``
    # DENTRO do event loop da coleta. ``sessions=`` pré-resolvido não protege —
    # o LPUSH acontece de qualquer forma.
    if _tap_blind():
        return
    try:
        r = redis if redis is not None else _sync_redis()
        metas = (
            list(sessions)
            if sessions is not None
            else active_sessions_sync(org_id, redis=r)
        )
        if not metas:
            return
        now = time.time()
        for m in metas:
            sid = m.get("id") or ""
            if not sid:
                continue
            entries = _entries_for(
                m, batch, now, outcome, destination_id, detail, route_id,
                stage=stage, payload_kind=payload_kind, pii_redacted=pii_redacted,
                event_ids=event_ids, destination_kind=destination_kind,
                dest_config_version=dest_config_version, wires=wires,
            )
            if not entries:
                continue
            ring_size, evt_ttl, ring_bytes = _ring_params(m, now)
            _append_entries(
                r, sid, entries,
                ring_size=ring_size, evt_ttl=evt_ttl,
                ring_bytes=ring_bytes, outcome=outcome,
            )
        _tap_ok()
    except Exception as exc:  # pragma: no cover — captura nunca quebra a coleta
        _tap_failed()
        logger.debug("capture_session.record_sync falhou (não-fatal): %s", exc)
