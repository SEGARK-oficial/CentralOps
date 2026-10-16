"""Memória da fronteira do cursor (``cursor_boundary``) — contrato genérico.

Os testes de cada coletor provam o efeito ponta a ponta (o último evento não
volta a cada ciclo). Aqui fica o que vale para QUALQUER coletor: ordenação por
instante, os três formatos de fronteira, o teto e a recusa em aplicar memória de
outra janela.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .. import cursor_boundary as cb
from ..cursor_boundary import BoundaryKeys


def _roundtrip(boundary: BoundaryKeys, next_start: str) -> BoundaryKeys:
    """Grava a memória num cursor e a recarrega como o próximo ciclo faria."""
    return BoundaryKeys.load(dict(boundary.dump(next_start)), window_start=next_start)


def test_dump_keeps_every_event_at_or_after_the_next_window_start():
    """Cursor truncado em segundos: o segundo INTEIRO da fronteira é lembrado —
    são esses que o vendor devolve no próximo pedido."""
    b = BoundaryKeys.load(None, window_start="2026-10-15T17:00:00Z")
    b.observe("2026-10-15T17:06:38.999Z", "antes")
    b.observe("2026-10-15T17:06:39.000Z", "inicio-do-segundo")
    b.observe("2026-10-15T17:06:39.978Z", "fim-do-segundo")

    assert b.dump("2026-10-15T17:06:39Z") == {
        "boundary": {
            "at": "2026-10-15T17:06:39Z",
            "keys": ["fim-do-segundo", "inicio-do-segundo"],
        }
    }


def test_overlapping_window_remembers_the_whole_overlap():
    """Cursor com sobreposição proposital (``max − 2s``, como o Veeam): todos os
    eventos dos 2 segundos voltam, então todos são lembrados."""
    b = BoundaryKeys.load(None, window_start="2026-10-15T17:00:00Z")
    for second, key in ((36, "fora"), (37, "a"), (38, "b"), (39, "c")):
        b.observe(f"2026-10-15T17:06:{second}.500Z", key)

    assert b.dump("2026-10-15T17:06:37Z")["boundary"]["keys"] == ["a", "b", "c"]


def test_ordering_is_by_instant_not_by_string():
    """``"…:39Z" > "…:39.5Z"`` como string (``Z`` vem depois de ``.``). Ordenar
    por string jogaria o evento de .5s para fora da fronteira de :39.5."""
    b = BoundaryKeys.load(None, window_start="2026-10-15T17:00:00Z")
    b.observe("2026-10-15T17:06:39Z", "inteiro")
    b.observe("2026-10-15T17:06:39.5Z", "meio")

    assert b.dump("2026-10-15T17:06:39.5Z")["boundary"]["keys"] == ["meio"]


def test_seen_skips_only_what_the_previous_cycle_emitted():
    first = BoundaryKeys.load(None, window_start="2026-10-15T17:00:00Z")
    first.observe("2026-10-15T17:06:39.978Z", "velho")

    second = _roundtrip(first, "2026-10-15T17:06:39Z")
    assert second.seen("2026-10-15T17:06:39.978Z", "velho") is True
    assert second.seen("2026-10-15T17:06:39.990Z", "novo") is False
    assert second.skipped == 1


def test_a_skipped_event_stays_remembered_while_the_window_does_not_move():
    """Pulado também é registrado: se a janela fica parada, ele volta no
    ciclo seguinte e tem de continuar sendo pulado (senão sai a cada 2 ciclos)."""
    b = BoundaryKeys.load(None, window_start="2026-10-15T17:00:00Z")
    b.observe("2026-10-15T17:06:39.978Z", "k")
    start = "2026-10-15T17:06:39Z"

    for _ in range(5):
        b = _roundtrip(b, start)
        assert b.seen("2026-10-15T17:06:39.978Z", "k") is True


def test_memory_of_another_window_is_ignored():
    """Memória vale só para a janela em que foi gravada; cursor resetado, backfill
    ou formato antigo não pulam nada."""
    state = {"boundary": {"at": "2026-10-15T17:06:39Z", "keys": ["k"]}}

    assert BoundaryKeys.load(state, window_start="2026-10-15T16:00:00Z").seen(None, "k") is False
    assert BoundaryKeys.load(state, window_start=None).seen(None, "k") is False
    assert BoundaryKeys.load(state, window_start="2026-10-15T17:06:39Z").seen(None, "k") is True


def test_malformed_state_is_ignored_not_raised():
    """O cursor é JSON livre no Redis/Postgres: lixo nele não pode derrubar a coleta."""
    for state in (
        {"boundary": "x"},
        {"boundary": {"at": "2026-10-15T17:06:39Z", "keys": "k"}},
        {"boundary": {"at": "2026-10-15T17:06:39Z", "keys": [1, None, ""]}},
        {"boundary": None},
        None,
    ):
        b = BoundaryKeys.load(state, window_start="2026-10-15T17:06:39Z")
        assert b.seen("2026-10-15T17:06:39.1Z", "1") is False


def test_unplaceable_events_are_emitted_but_not_remembered():
    b = BoundaryKeys.load(None, window_start="2026-10-15T17:00:00Z")
    b.observe(None, "sem-hora")
    b.observe("nao-e-data", "lixo")
    b.observe("2026-10-15T17:06:39Z", "")
    assert b.dump("2026-10-15T17:00:00Z") == {}


def test_empty_boundary_leaves_the_cursor_byte_identical():
    """Sem nada a lembrar, nenhuma chave nova no cursor — cursor de stream vazio
    (e de cap sem memória) fica igual ao de antes desta mudança."""
    b = BoundaryKeys.load(None, window_start="2026-10-15T17:00:00Z")
    assert b.dump("2026-10-15T17:00:00Z") == {}
    assert b.carry() == {}


def test_carry_preserves_the_current_window_memory():
    state = {"boundary": {"at": "2026-10-15T17:06:39Z", "keys": ["b", "a"]}}
    b = BoundaryKeys.load(state, window_start="2026-10-15T17:06:39Z")
    assert b.carry() == {"boundary": {"at": "2026-10-15T17:06:39Z", "keys": ["a", "b"]}}


def test_accepts_aware_and_naive_datetimes():
    b = BoundaryKeys.load(None, window_start="2026-10-15T17:00:00Z")
    b.observe(datetime(2026, 10, 15, 14, 6, 39, tzinfo=timezone(timedelta(hours=-3))), "aware")
    b.observe(datetime(2026, 10, 15, 17, 6, 39), "naive")
    assert b.dump("2026-10-15T17:06:39Z")["boundary"]["keys"] == ["aware", "naive"]


def test_cap_keeps_the_most_recent_and_never_grows_unbounded(monkeypatch):
    """Rajada maior que o teto: a memória fica capada (o cursor não cresce sem
    limite) e guarda os MAIS RECENTES; os excedentes voltam uma vez e o dedupe os
    segura. A poda interna também é exercitada (mais de 2× o teto observado)."""
    monkeypatch.setattr(cb, "MAX_KEYS", 10)
    b = BoundaryKeys.load(None, window_start="2026-10-15T17:00:00Z")
    base = datetime(2026, 10, 15, 17, 6, 39)
    for i in range(35):
        b.observe(base + timedelta(milliseconds=i), f"k{i:02d}")

    keys = b.dump("2026-10-15T17:06:39Z")["boundary"]["keys"]
    assert keys == [f"k{i:02d}" for i in range(25, 35)]
    assert len(b._recent) <= 2 * cb.MAX_KEYS


def test_duplicate_key_in_the_same_cycle_is_stored_once():
    """Paginação por offset pode devolver o mesmo item em duas páginas."""
    b = BoundaryKeys.load(None, window_start="2026-10-15T17:00:00Z")
    b.observe("2026-10-15T17:06:39.1Z", "k")
    b.observe("2026-10-15T17:06:39.1Z", "k")
    assert b.dump("2026-10-15T17:06:39Z")["boundary"]["keys"] == ["k"]
