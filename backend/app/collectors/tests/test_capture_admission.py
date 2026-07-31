"""Admissão da captura: limita CPU sem partir a trajetória de um evento.

A alternativa óbvia — amostrar por relógio (token bucket como mecanismo
primário) — destrói a feature: ``received`` é gravado pelo worker de coleta e
``delivered`` pelo dispatcher, em OUTRO processo e minutos depois. Admitidos
independentemente, a chance de um evento ter os dois estágios no ring tenderia a
zero, e o jornal deixaria de ser juntável.

Por isso a admissão primária é uma função determinística do ``event_id``, e o
bucket fica só como backstop de CPU.
"""
from __future__ import annotations

import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import pytest

from backend.app.collectors import capture_admission as adm


@pytest.fixture(autouse=True)
def _clean():
    adm.reset()
    yield
    adm.reset()


# ── admit(): determinismo ─────────────────────────────────────────────


def test_admit_is_deterministic_across_calls() -> None:
    first = adm.admit("evento-abc", 25)
    for _ in range(10_000):
        assert adm.admit("evento-abc", 25) is first


def test_admit_is_nested_across_percentages() -> None:
    """Admitido em 25% ⇒ admitido em 100%.

    Importa porque o operador que SOBE a taxa no meio do troubleshooting não
    pode perder a trajetória que já estava acompanhando.
    """
    for i in range(500):
        eid = f"e{i}"
        if adm.admit(eid, 25):
            assert adm.admit(eid, 50) is True
            assert adm.admit(eid, 100) is True


def test_admit_full_rate_never_rejects() -> None:
    assert all(adm.admit(f"e{i}", 100) for i in range(1000))


def test_admit_zero_rejects_everything_with_an_id() -> None:
    assert adm.admit("e1", 0) is False


def test_admit_without_event_id_always_passes() -> None:
    """Os três sites de quarentena PRÉ-envelope não têm id. Inventar um só para
    amostrar produziria uma decisão que ninguém consegue reproduzir."""
    assert adm.admit(None, 1) is True
    assert adm.admit("", 1) is True


def test_admit_rate_is_roughly_the_requested_percentage() -> None:
    admitted = sum(1 for i in range(10_000) if adm.admit(f"evt-{i}", 30))
    assert 2_500 <= admitted <= 3_500, f"taxa fora do esperado: {admitted}/10000"


def test_admit_decision_is_stable_across_processes() -> None:
    """crc32 é estável entre processos e versões — ao contrário de ``hash()``,
    que é randomizado por PYTHONHASHSEED e daria decisões diferentes no worker
    de coleta e no dispatcher."""
    import zlib

    assert adm.admit("chave-fixa", 50) is (
        zlib.crc32(b"chave-fixa") % 100 < 50
    )


# ── throttle(): backstop ──────────────────────────────────────────────


def test_throttle_allows_within_rate(monkeypatch) -> None:
    clock = {"t": 1000.0}
    monkeypatch.setattr(adm.time, "monotonic", lambda: clock["t"])
    assert adm.throttle("s1", 100, 50) == 50


def test_throttle_caps_and_counts_the_rejected(monkeypatch) -> None:
    clock = {"t": 1000.0}
    monkeypatch.setattr(adm.time, "monotonic", lambda: clock["t"])
    # burst = 2x a taxa = 200
    allowed = adm.throttle("s1", 100, 500)
    assert allowed == 200
    assert adm.skipped("s1") == 300


def test_throttle_refills_over_time(monkeypatch) -> None:
    clock = {"t": 1000.0}
    monkeypatch.setattr(adm.time, "monotonic", lambda: clock["t"])
    adm.throttle("s1", 100, 200)  # esvazia o burst
    assert adm.throttle("s1", 100, 10) == 0

    clock["t"] += 1.0  # 1 s ⇒ +100 tokens
    assert adm.throttle("s1", 100, 10) == 10


def test_throttle_has_burst_for_batched_arrival(monkeypatch) -> None:
    """Um lote de coleta chega em rajada. Sem burst, o bucket descartaria a
    maior parte de todo lote mesmo com folga na média."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(adm.time, "monotonic", lambda: clock["t"])
    assert adm.throttle("s1", 200, 400) == 400


def test_throttle_is_per_session() -> None:
    adm.throttle("s1", 1, 100)
    assert adm.skipped("s1") > 0
    assert adm.skipped("s2") == 0


def test_reset_clears_only_the_named_session() -> None:
    adm.throttle("s1", 1, 100)
    adm.throttle("s2", 1, 100)
    adm.reset("s1")
    assert adm.skipped("s1") == 0
    assert adm.skipped("s2") > 0


# ── session_params ────────────────────────────────────────────────────


def test_session_params_defaults_when_absent() -> None:
    """Sessões criadas antes destes campos existirem caem nos defaults, sem
    migração."""
    pct, eps, wire = adm.session_params({})
    assert pct == 100
    assert eps == adm.DEFAULT_MAX_EPS
    assert wire is False


def test_session_params_clamps() -> None:
    pct, eps, _ = adm.session_params({"capture_percent": "999", "max_eps": "999999"})
    assert pct == 100
    assert eps == adm.MAX_EPS_CEILING


def test_session_params_tolerates_garbage() -> None:
    pct, eps, wire = adm.session_params(
        {"capture_percent": "abc", "max_eps": "", "capture_wire": "sim"}
    )
    assert pct == 100
    assert eps == adm.DEFAULT_MAX_EPS
    assert wire is False  # só "1" liga


def test_capture_wire_is_off_by_default() -> None:
    """O custo de ``format()`` no dispatcher é opt-in: uma sessão criada pelo
    fluxo de hoje mantém EXATAMENTE o custo de hoje."""
    assert adm.session_params({})[2] is False
    assert adm.session_params({"capture_wire": "1"})[2] is True


# ── jornal: chave de junção e retrocompatibilidade ────────────────────


def test_event_id_is_copied_never_recomputed() -> None:
    """O TESTE MAIS IMPORTANTE DA FASE.

    Com ``raw_reduction`` ativo, ``build_envelope`` recebe o raw REDUZIDO, então
    recomputar o id sobre o raw ORIGINAL produziria um valor diferente — e a
    junção do jornal quebraria em SILÊNCIO, que é o pior modo de falha para uma
    chave de correlação.
    """
    from backend.app.collectors import capture_session as cs
    from backend.app.collectors.normalize.envelope import compute_event_id

    raw_original = {"id": "vendor-1", "campo_grande": "x" * 5000}
    raw_reduzido = {"id": "vendor-1"}

    # Sem vendor_msg_id nativo, o id é derivado do raw QUE O ENVELOPE RECEBEU.
    id_reduzido = compute_event_id(raw_reduzido, None)
    id_original = compute_event_id(raw_original, None)
    assert id_reduzido != id_original, (
        "fixture inválida: os dois raws precisam gerar ids diferentes para o "
        "teste provar alguma coisa"
    )

    envelope = {"_centralops": {"event_id": id_reduzido, "vendor": "x"}}
    # O extractor COPIA do envelope — nunca recomputa.
    assert cs._event_id_of(envelope) == id_reduzido


def test_event_id_of_tolerates_garbage() -> None:
    from backend.app.collectors import capture_session as cs

    assert cs._event_id_of(None) is None
    assert cs._event_id_of({}) is None
    assert cs._event_id_of({"_centralops": "nao-e-dict"}) is None
    assert cs._event_id_of({"_centralops": {}}) is None


def test_normalize_entry_fills_v1_defaults() -> None:
    """Registros v1 continuam no ring por até 3.900 s após o deploy. É a única
    coisa que NÃO dá para testar depois de subir."""
    from backend.app.collectors import capture_session as cs

    v1 = {
        "event": {"_centralops": {"event_id": "e-42", "vendor": "sophos"}},
        "vendor": "sophos",
        "captured_at": 1.0,
        "outcome": "delivered",
    }
    out = cs.normalize_entry(dict(v1))

    assert out["v"] == 1
    assert out["stage"] == cs.STAGE_ROUTED
    assert out["payload_kind"] == cs.PAYLOAD_ENVELOPE
    assert out["pii_redacted"] is False
    # O id é RESGATADO do envelope, não inventado.
    assert out["event_id"] == "e-42"


def test_normalize_entry_leaves_v2_untouched() -> None:
    from backend.app.collectors import capture_session as cs

    v2 = {
        "v": 2,
        "stage": cs.STAGE_COLLECTED,
        "payload_kind": cs.PAYLOAD_VENDOR_RAW,
        "event_id": "e-1",
        "pii_redacted": True,
        "event": {},
    }
    assert cs.normalize_entry(dict(v2)) == v2


def test_new_outcomes_are_in_the_closed_vocabulary() -> None:
    from backend.app.collectors import capture_session as cs

    assert cs.OUTCOME_RECEIVED in cs.OUTCOMES
    assert cs.OUTCOME_DEDUPED in cs.OUTCOMES
    assert len(cs.OUTCOMES) == 11


def test_stage_vocabulary_is_closed() -> None:
    from backend.app.collectors import capture_session as cs

    assert cs.STAGES == {"collected", "routed", "delivered"}
