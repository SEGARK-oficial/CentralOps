"""Memória da FRONTEIRA do cursor: quais eventos do instante onde a janela
recomeça já foram emitidos.

**O defeito que isto fecha (incidente out/2026, Sophos Cases).** Muitos
coletores fazem ``próxima janela = maior timestamp visto`` e pedem ao vendor
"tudo a partir daí". Se o limite inferior do vendor é INCLUSIVO (``ge``/``gte``)
ou só aceita segundos inteiros, o ÚLTIMO evento visto volta em todo ciclo: um
case criado às ``17:06:39.978`` faz o próximo pedido começar em ``17:06:39`` e
trazer o mesmo case de novo. Num tenant com pouco volume nenhum evento mais
novo empurra a janela, e ela fica parada nele para sempre. O dedupe (Redis, TTL
de 1 dia por padrão) esconde a repetição até o TTL vencer — aí o evento é
reenviado, uma vez por TTL.

Somar um epsilon ao cursor não serve: um evento criado no MESMO segundo mas
indexado depois pelo vendor seria perdido. A solução é lembrar, no próprio
cursor, as chaves dos eventos que estão no instante onde a próxima janela
começa, e pular esses no ciclo seguinte. A janela continua começando no mesmo
lugar (nada se perde) e o evento sai uma vez só, com ou sem dedupe.

**Por que pular é seguro.** O pipeline só grava o cursor quando o ciclo termina
com sucesso; em erro ele regrava o anterior. Então uma chave presente no cursor
gravado é de um evento que um ciclo bem-sucedido já processou.

**A chave é a do dedupe** (``extract_message_id``). Coletor cujo id carrega a
versão do evento (``id::updatedAt``) continua emitindo uma ATUALIZAÇÃO de evento
da fronteira — a chave nova não está na memória.

**Degradação.** A memória guarda no máximo :data:`MAX_KEYS` chaves. Se mais
eventos que isso caírem na fronteira (centenas no mesmo instante), os que
sobrarem voltam uma vez no ciclo seguinte — e o dedupe os segura, como antes.
Não há caminho em que este módulo perca evento.

Uso, num coletor com janela ``from``/``createdAfter``::

    boundary = BoundaryKeys.load(cursor, window_start=created_after)
    for ev in items:
        if boundary.seen(ev.get("createdAt"), self.extract_message_id(ev)):
            continue
        yield ev
    # cursor RESUMÍVEL (teto por ciclo, mesma janela): preserve a memória atual
    self.ctx.cursor = {"created_after": created_after, "page": page, **boundary.carry()}
    # cursor FINAL (próxima janela começa em ``latest``)
    self.ctx.cursor = {"created_after": latest, **boundary.dump(latest)}
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Tuple, Union

from .base import _parse_iso_utc_naive

logger = logging.getLogger(__name__)

#: Chave da memória dentro do dict de cursor. Fica ao lado das chaves do coletor;
#: ``watermark_at`` lê só a dele, então isto não mexe no atraso de watermark.
CURSOR_KEY = "boundary"

#: Teto de chaves lembradas. A fronteira normal tem 1 a 5 eventos; 500 cobre
#: rajada no mesmo segundo sem deixar o cursor (Redis + Postgres, gravado todo
#: ciclo) crescer sem limite — ~40 KB no pior caso com ids de 80 caracteres.
MAX_KEYS = 500

When = Union[str, datetime, None]


def _instant(when: When) -> Optional[datetime]:
    """Instante naive-UTC de ``when``, ou ``None`` se não dá para posicioná-lo.

    Compara por instante, NUNCA por string: ``"…:39Z" > "…:39.5Z"`` em ordem
    lexicográfica (``Z`` vem depois de ``.``), e vendors devolvem número variável
    de casas decimais no mesmo campo.
    """
    if isinstance(when, datetime):
        if when.tzinfo is None:
            return when
        return when.astimezone(timezone.utc).replace(tzinfo=None)
    if isinstance(when, str) and when.strip():
        return _parse_iso_utc_naive(when)
    return None


class BoundaryKeys:
    """Chaves já emitidas na fronteira da janela, carregadas do cursor e
    recalculadas para o próximo ciclo."""

    def __init__(self, window_start: Optional[str], previous: FrozenSet[str]) -> None:
        self._window_start = window_start
        self._previous = previous
        # (instante, chave) dos eventos vistos neste ciclo, podado para os mais
        # recentes — só eles podem estar na fronteira da PRÓXIMA janela.
        self._recent: List[Tuple[datetime, str]] = []
        #: Eventos pulados neste ciclo por já terem saído (para log/teste).
        self.skipped = 0

    @classmethod
    def load(
        cls, cursor: Optional[Mapping[str, Any]], *, window_start: Optional[str]
    ) -> "BoundaryKeys":
        """Lê a memória do cursor — válida só para a MESMA janela.

        ``window_start`` é o limite inferior que o coletor vai mandar ao vendor
        neste ciclo, exatamente como foi gravado. Se a memória é de outra janela
        (cursor resetado, backfill, formato antigo), ela não se aplica e nada é
        pulado: na dúvida, emitir de novo e deixar o dedupe decidir.
        """
        state = (cursor or {}).get(CURSOR_KEY)
        previous: FrozenSet[str] = frozenset()
        if (
            window_start
            and isinstance(state, Mapping)
            and state.get("at") == window_start
            and isinstance(state.get("keys"), list)
        ):
            previous = frozenset(k for k in state["keys"] if isinstance(k, str) and k)
        return cls(window_start, previous)

    def seen(self, when: When, key: str) -> bool:
        """Registra o evento e diz se ele JÁ saiu num ciclo anterior (pular).

        O evento pulado também é registrado: ele continua na fronteira, e se a
        janela não andar ele volta no próximo ciclo e precisa continuar lembrado.
        """
        self.observe(when, key)
        if key and key in self._previous:
            self.skipped += 1
            return True
        return False

    def observe(self, when: When, key: str) -> None:
        """Registra um evento emitido sem perguntar se ele já saiu."""
        instant = _instant(when)
        if instant is None or not key:
            return
        self._recent.append((instant, key))
        if len(self._recent) > 2 * MAX_KEYS:
            # Poda amortizada: nenhum evento fora dos MAX_KEYS mais recentes pode
            # entrar na memória gravada (que é capada em MAX_KEYS).
            self._recent.sort()
            del self._recent[:-MAX_KEYS]

    def carry(self) -> Dict[str, Any]:
        """Memória da janela ATUAL, para um cursor que retoma a mesma janela.

        Use no cursor resumível (teto de páginas por ciclo): a janela não mudou,
        então a memória carregada continua valendo. Dict vazio quando não há o
        que lembrar — o cursor sai byte-idêntico ao de antes desta mudança.
        """
        if not self._previous or not self._window_start:
            return {}
        return {CURSOR_KEY: {"at": self._window_start, "keys": sorted(self._previous)}}

    def dump(self, next_window_start: Optional[str]) -> Dict[str, Any]:
        """Memória para a PRÓXIMA janela, que vai começar em ``next_window_start``.

        Guarda as chaves de todo evento deste ciclo com instante ``>=`` o início
        da próxima janela — exatamente os que o vendor pode devolver de novo. Vale
        para cursor = máximo visto (fronteira de 1 instante), para máximo truncado
        em segundos (o segundo inteiro) e para cursor com sobreposição proposital
        (``max − 2s``: os 2 segundos inteiros).

        Ciclo que não viu NADA na fronteira e não moveu a janela (resposta vazia
        do vendor, falha transitória de indexação) mantém a memória carregada:
        apagá-la faria o evento da fronteira sair de novo assim que o vendor o
        devolvesse.
        """
        bound = _instant(next_window_start)
        if bound is None:
            return {}
        at_or_after = sorted(
            (item for item in self._recent if item[0] >= bound), reverse=True
        )
        keys: List[str] = []
        chosen = set()
        for _instant_, key in at_or_after:
            if key in chosen:
                continue
            if len(keys) >= MAX_KEYS:
                logger.info(
                    "cursor_boundary: mais de %d eventos na fronteira %s — os "
                    "excedentes voltam uma vez e o dedupe os segura",
                    MAX_KEYS, next_window_start,
                )
                break
            chosen.add(key)
            keys.append(key)
        if not keys:
            return self.carry() if next_window_start == self._window_start else {}
        return {CURSOR_KEY: {"at": next_window_start, "keys": sorted(keys)}}


def is_later(candidate: When, current: When) -> bool:
    """``candidate`` é um instante estritamente posterior a ``current``?

    Para o "maior timestamp visto" que vira a próxima janela. Compara por
    INSTANTE: em string, ``"…39Z" > "…39.5Z"`` (``Z`` vem depois de ``.``) e o
    cursor ficaria no instante MAIS VELHO; ``+0000`` (Wazuh) contra ``Z`` (o
    lookback default) também desalinha. Valor que não é data não avança o cursor
    — em string, ``"abc" > "2026"`` avançaria. ``current`` ilegível perde para
    qualquer ``candidate`` legível.
    """
    cand = _instant(candidate)
    if cand is None:
        return False
    cur = _instant(current)
    return cur is None or cand > cur
