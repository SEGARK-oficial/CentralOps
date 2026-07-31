"""Fidelidade do wire: o produto não pode prometer o que não entrega.

``format()`` é o mais próximo do payload entregue, mas NÃO é o wire para todos
os kinds — e o cliente vai comparar o que a tela mostra com o log do SIEM dele.
Sem rótulo por kind, o produto mente em 4 dos 16 e é irreproduzível em 2.

O teste mais importante é o de EXAUSTIVIDADE: um kind novo sem entrada na tabela
cairia num default, e um default ``exact`` seria a pior mentira possível.
"""
from __future__ import annotations

import copy
import os

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SESSION_SECURE_COOKIE", "false")

import pytest

from backend.app.collectors.output import wire_preview as wp
from backend.app.collectors.output.destinations import registry


def _registry_kinds() -> list:
    for attr in ("kinds", "available", "list_kinds", "registered"):
        fn = getattr(registry, attr, None)
        if callable(fn):
            try:
                return sorted(fn())
            except Exception:
                continue
    for attr in ("_REGISTRY", "_BUILDERS", "REGISTRY"):
        reg = getattr(registry, attr, None)
        if isinstance(reg, dict) and reg:
            return sorted(reg)
    pytest.skip("não consegui enumerar os kinds do registry")


# ── exaustividade ─────────────────────────────────────────────────────


def test_every_registered_kind_is_classified() -> None:
    """Um 17º kind sem entrada FALHA aqui, e não em produção.

    O repo tem o precedente do risco: o shadow do pipeline conta
    ``NotImplementedError`` como sucesso, então um kind sem formatter passa
    despercebido no caminho normal.
    """
    kinds = _registry_kinds()
    assert kinds, "registry vazio — o import dos built-ins não rodou"
    faltando = [k for k in kinds if k not in wp.WIRE_FIDELITY]
    assert not faltando, (
        f"kinds sem classificação de fidelidade: {faltando}. Adicione em "
        "WIRE_FIDELITY — o default é not_representable justamente para não "
        "prometer exatidão que ninguém verificou."
    )


def test_table_has_no_entries_for_kinds_that_do_not_exist() -> None:
    kinds = set(_registry_kinds())
    sobrando = [k for k in wp.WIRE_FIDELITY if k not in kinds]
    assert not sobrando, f"classificação órfã (kind removido?): {sobrando}"


def test_unknown_kind_fails_closed() -> None:
    """Kind desconhecido NÃO recebe o benefício da dúvida num campo cujo
    propósito é dizer o quanto confiar."""
    spec = wp.spec_for("kind_que_nao_existe")
    assert spec.fidelity == wp.FIDELITY_NOT_REPRESENTABLE


@pytest.mark.parametrize("kind", sorted(wp.WIRE_FIDELITY))
def test_every_spec_has_an_explanatory_note(kind: str) -> None:
    """A nota é o que o operador lê para saber o que FALTA. Um nível sem
    explicação é um selo sem valor."""
    spec = wp.WIRE_FIDELITY[kind]
    assert spec.note and len(spec.note) > 20, f"{kind} sem nota útil"
    assert spec.encoding in ("json", "ndjson", "text", "binary")
    assert spec.fidelity in (
        wp.FIDELITY_EXACT,
        wp.FIDELITY_NONDETERMINISTIC,
        wp.FIDELITY_PARTIAL,
        wp.FIDELITY_NOT_REPRESENTABLE,
    )


def test_batch_oriented_sinks_are_not_marked_exact() -> None:
    """s3 e security_lake gravam o LOTE (gzip/Parquet). Marcar ``exact`` neles
    faria o operador diffar um fragmento contra um objeto binário."""
    for kind in ("s3", "security_lake"):
        assert wp.WIRE_FIDELITY[kind].fidelity == wp.FIDELITY_NOT_REPRESENTABLE


def test_syslog_kinds_are_nondeterministic() -> None:
    """Recalculam relógio/hostname/PID a cada chamada — a linha exibida nunca
    será byte-idêntica à entregue."""
    for kind in ("syslog_rfc3164", "syslog_rfc5424"):
        assert wp.WIRE_FIDELITY[kind].fidelity == wp.FIDELITY_NONDETERMINISTIC


# ── render() ──────────────────────────────────────────────────────────


class _FakeDest:
    def __init__(self, kind, payload=None, exc=None):
        self.kind = kind
        self._payload = payload
        self._exc = exc

    def format(self, envelope):
        if self._exc is not None:
            raise self._exc
        return self._payload


_ENV = {"_centralops": {"vendor": "sophos", "event_id": "e1"}, "normalized": {"a": 1}}


def test_render_not_representable_has_no_text() -> None:
    out = wp.render(_FakeDest("s3", {"x": 1}), _ENV)
    assert out["fidelity"] == wp.FIDELITY_NOT_REPRESENTABLE
    assert "text" not in out, "preview de fragmento induz a comparação errada"


def test_render_exact_returns_text() -> None:
    out = wp.render(_FakeDest("webhook", {"event": {"a": 1}}), _ENV)
    assert out["fidelity"] == wp.FIDELITY_EXACT
    assert '"a"' in out["text"]
    assert out["bytes"] > 0
    assert out["truncated"] is False


def test_render_accepts_str_payload() -> None:
    """Dois kinds devolvem ``str`` — o contrato do Protocol foi alargado para
    admitir isso em vez de forçar ``.encode()`` no framing legado."""
    out = wp.render(_FakeDest("syslog_rfc3164", "<134>Jul 31 ..."), _ENV)
    assert out["text"].startswith("<134>")
    assert out["fidelity"] == wp.FIDELITY_NONDETERMINISTIC


def test_render_accepts_bytes_payload() -> None:
    out = wp.render(_FakeDest("kafka", b'{"a":1}'), _ENV)
    assert out["text"] == '{"a":1}'


def test_render_never_raises_on_formatter_error() -> None:
    """Alguns senders recusam envelope degenerado. Isso é diagnóstico legítimo,
    não um bug do preview."""
    out = wp.render(_FakeDest("webhook", exc=ValueError("envelope sem host")), _ENV)
    assert out["fidelity"] == wp.FIDELITY_ERROR
    assert "ValueError" in out["note"]
    assert "text" not in out


def test_render_handles_not_implemented() -> None:
    out = wp.render(_FakeDest("jsonl", exc=NotImplementedError()), _ENV)
    assert out["fidelity"] == wp.FIDELITY_NOT_REPRESENTABLE
    assert "text" not in out


def test_render_never_mutates_the_envelope() -> None:
    """OBRIGATÓRIO: ``splunk_hec`` devolve o PRÓPRIO envelope aninhado, e a
    maioria dos kinds devolve cópia RASA — guardar o objeto criaria aliasing
    com o payload que ainda vai ser entregue."""
    env = {"_centralops": {"vendor": "x"}, "normalized": {"lista": [1, 2, 3]}}
    antes = copy.deepcopy(env)

    class _Aliasing:
        kind = "splunk_hec"

        def format(self, envelope):
            return {"event": envelope}  # aliasing proposital

    out = wp.render(_Aliasing(), env)
    assert env == antes, "render() mutou o envelope"
    assert isinstance(out["text"], str), "o payload tem de ser serializado na hora"


def test_render_truncates_and_marks() -> None:
    out = wp.render(_FakeDest("webhook", "x" * 100_000), _ENV, limit_bytes=1024)
    assert out["truncated"] is True
    assert len(out["text"].encode("utf-8")) <= 1024
    assert out["bytes"] == 100_000, "``bytes`` é o tamanho ANTES do teto"


def test_render_truncation_keeps_valid_utf8() -> None:
    out = wp.render(_FakeDest("webhook", "ção" * 20_000), _ENV, limit_bytes=512)
    out["text"].encode("utf-8").decode("utf-8")  # não pode levantar


def test_render_tolerates_missing_kind() -> None:
    class _NoKind:
        def format(self, envelope):
            return {"a": 1}

    out = wp.render(_NoKind(), _ENV)
    assert out["fidelity"] == wp.FIDELITY_NOT_REPRESENTABLE
