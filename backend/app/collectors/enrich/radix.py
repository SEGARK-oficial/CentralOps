"""Radix tree binária para lookup de IP por **longest-prefix** (ADR-LOCAL-0002).

É a primitiva que resolve, com uma estrutura só, três problemas que hoje exigiriam
três soluções: plano de endereçamento corporativo (``10.0.0.0/16`` genérico com
``10.0.5.0/24`` específico por andar), listas de bloqueio distribuídas em CIDR
(Spamhaus DROP) e inventário de rede exportado do NetBox.

**Por que importa comercialmente.** Nem o Vector nem o Cribl fazem longest-prefix
nativamente; o ``prefix`` do Edge Delta é prefixo de *string*, que é outra coisa.
Com uma tabela contendo ``10.0.0.0/16`` → *matriz* e ``10.0.5.0/24`` → *filial SP*,
sondar ``10.0.5.7`` tem de devolver **filial SP**. Um dict de igualdade exata
devolveria nada; um match "primeiro que casar" devolveria o que viesse antes.

**Por que uma trie e não ``ipaddress`` + varredura.** A varredura é O(n) por evento
sobre a lista de prefixos, e o aplicador tem orçamento de ~4 µs por regra. A trie é
O(bits) — no máximo 32 passos para IPv4, 128 para IPv6 — independente do número de
prefixos. Uma tabela de 50 mil CIDRs custa o mesmo que uma de 10.

**PURO.** Sem I/O, sem log, sem métrica: este módulo é importado pelo ``applier``,
que está sob guard estrutural de CI.
"""

from __future__ import annotations

import ipaddress
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

__all__ = ["RadixTree", "parse_network", "ip_to_bits"]

#: Sentinela de nó sem valor. ``None`` não serve: uma entrada pode legitimamente
#: mapear para ``None``, e confundir "sem prefixo aqui" com "prefixo mapeado para
#: nada" produziria miss silencioso.
_MISSING = object()


def parse_network(text: str) -> Optional[ipaddress._BaseNetwork]:
    """``"10.0.5.0/24"`` → rede, ou ``None`` se não parsear.

    Aceita IP nu (``"10.0.5.7"``) tratando-o como /32 (ou /128), porque é o que o
    operador escreve quando quer uma exceção pontual dentro de uma faixa.
    ``strict=False`` deixa passar ``10.0.5.7/24`` — que é erro de digitação comum e,
    recusado, obrigaria o cliente a limpar o CSV inteiro antes de subir a tabela.
    """
    try:
        return ipaddress.ip_network(text.strip(), strict=False)
    except (ValueError, AttributeError):
        return None


def ip_to_bits(text: str) -> Optional[Tuple[int, int]]:
    """``"10.0.5.7"`` → ``(inteiro, versão)``, ou ``None``.

    Devolve a versão junto porque IPv4 e IPv6 vivem em árvores SEPARADAS: unificar
    mapeando IPv4 para ``::ffff:a.b.c.d`` faria um prefixo IPv6 amplo capturar
    tráfego IPv4 sem ninguém perceber.
    """
    try:
        addr = ipaddress.ip_address(text.strip())
    except (ValueError, AttributeError):
        return None
    return int(addr), addr.version


class _Node:
    """Nó binário. ``__slots__`` porque uma tabela grande cria centenas de milhares."""

    __slots__ = ("children", "value")

    def __init__(self) -> None:
        # [bit0, bit1] — lista de 2 é mais barata que dict para 2 posições fixas.
        self.children: list = [None, None]
        self.value: Any = _MISSING


class RadixTree:
    """Trie binária de prefixos IP com busca por longest-prefix.

    Duas raízes (v4/v6) pelo motivo em :func:`ip_to_bits`. A profundidade máxima é o
    número de bits do endereço, então o custo por lookup é limitado e previsível —
    é isso que permite prometer orçamento por evento independentemente do tamanho
    da tabela do cliente.
    """

    __slots__ = ("_root4", "_root6", "_count", "_bytes")

    def __init__(self) -> None:
        self._root4 = _Node()
        self._root6 = _Node()
        self._count = 0
        self._bytes = 0

    def insert(self, cidr: str, value: Mapping[str, Any]) -> bool:
        """Insere ``cidr`` → ``value``. ``False`` se o CIDR for inválido.

        Prefixo repetido SOBRESCREVE, e isso é deliberado: um CSV do cliente com a
        mesma rede duas vezes deve terminar com a última linha valendo (semântica de
        planilha), não com a primeira — que é o que um "insere se ausente" faria e
        seria invisível para quem editou o arquivo.
        """
        net = parse_network(cidr)
        if net is None:
            return False
        root = self._root4 if net.version == 4 else self._root6
        bits = net.max_prefixlen
        addr = int(net.network_address)
        node = root
        for i in range(net.prefixlen):
            bit = (addr >> (bits - 1 - i)) & 1
            nxt = node.children[bit]
            if nxt is None:
                nxt = _Node()
                node.children[bit] = nxt
            node = nxt
        if node.value is _MISSING:
            self._count += 1
        node.value = value
        # Estimativa barata para o teto de memória (ver runtime.estimate_bytes):
        # ~120 B por nó criado no pior caso + o payload.
        self._bytes += len(cidr) + 120 * max(net.prefixlen, 1) // 8 + 64
        return True

    def search(self, ip: str) -> Optional[Mapping[str, Any]]:
        """Valor do prefixo MAIS ESPECÍFICO que contém ``ip``, ou ``None``.

        Percorre do bit mais significativo ao menos, guardando o último valor visto:
        o último é, por construção, o prefixo mais longo que casa.
        """
        parsed = ip_to_bits(ip)
        if parsed is None:
            return None
        addr, version = parsed
        node = self._root4 if version == 4 else self._root6
        bits = 32 if version == 4 else 128
        best: Any = _MISSING
        if node.value is not _MISSING:
            best = node.value  # rota default (0.0.0.0/0), se existir
        for i in range(bits):
            bit = (addr >> (bits - 1 - i)) & 1
            node = node.children[bit]
            if node is None:
                break
            if node.value is not _MISSING:
                best = node.value
        return None if best is _MISSING else best

    @classmethod
    def from_rows(cls, rows: Mapping[str, Any]) -> Tuple["RadixTree", int]:
        """Constrói a partir de ``{cidr: {campo: valor}}``.

        Devolve ``(árvore, n_inválidos)``. As linhas inválidas são CONTADAS e não
        levantam: um CSV de 50 mil linhas com três erros de digitação deve subir com
        as 49.997 boas e reportar 3 — recusar o arquivo inteiro é o comportamento que
        faz o operador desistir da feature.
        """
        tree = cls()
        invalid = 0
        for cidr, value in rows.items():
            if not tree.insert(str(cidr), value):
                invalid += 1
        return tree, invalid

    @property
    def entry_count(self) -> int:
        return self._count

    @property
    def approx_bytes(self) -> int:
        return self._bytes

    def iter_prefixes(self) -> Iterable[Tuple[str, Any]]:
        """Para diagnóstico/dry-run. NÃO usar no caminho por evento."""
        def walk(node: _Node, prefix: str, version: int):
            if node.value is not _MISSING:
                yield (prefix or ("0.0.0.0/0" if version == 4 else "::/0"), node.value)
            for bit in (0, 1):
                child = node.children[bit]
                if child is not None:
                    yield from walk(child, prefix + str(bit), version)

        yield from walk(self._root4, "", 4)
        yield from walk(self._root6, "", 6)
