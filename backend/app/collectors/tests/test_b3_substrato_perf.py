"""Testes do substrato de performance.

Cobre as 3 frentes:
  1. Isolamento sem deepcopy em payload_reduction (copy-on-write).
  2. _fastjson: orjson disponível + wire bytes idênticos ao stdlib json.
  3. connection_pool por destino: gating por DISPATCH_PERSISTENT_LOOP.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime as _dt
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

os.environ.setdefault("APP_MASTER_KEY", "test-master-key-for-centralops-suite-12345")
os.environ.setdefault("APP_ENV", "test")

from backend.app.collectors.normalize.payload_reduction import (  # noqa: E402
    apply_raw_reduction,
    compile_raw_reduction,
)
from backend.app.collectors.output import connection_pool as cp  # noqa: E402
from backend.app.collectors.output._fastjson import (  # noqa: E402
    _USING_ORJSON,
    dumps_bytes,
    dumps_str,
)
from backend.app.collectors.output.elastic_bulk_sender import (  # noqa: E402
    ElasticBulkClient,
)


# ── 1. payload_reduction: copy-on-write sem deepcopy ─────────────────────────


def _specs(specs_dsl):
    return compile_raw_reduction(specs_dsl)


def test_reduction_does_not_mutate_original_raw() -> None:
    """apply_raw_reduction não muta o raw original — o source permanece intacto."""
    raw = {
        "blob": "x" * 100,
        "items": list(range(50)),
        "nested": {"deep": "abcdefghijklmnop"},
    }
    original_blob = raw["blob"]
    original_items = list(raw["items"])
    original_deep = raw["nested"]["deep"]

    specs = _specs([
        {"path": "blob", "max_bytes": 10},
        {"path": "items", "max_items": 3},
        {"path": "nested.deep", "max_bytes": 5},
    ])
    result = apply_raw_reduction(raw, specs)

    assert result is not None
    # Resultado reduzido.
    assert len(result["blob"].encode("utf-8")) == 10
    assert result["items"] == [0, 1, 2]
    assert len(result["nested"]["deep"].encode("utf-8")) == 5

    # Source intocado.
    assert raw["blob"] == original_blob
    assert raw["items"] == original_items
    assert raw["nested"]["deep"] == original_deep


def test_reduction_nested_dict_in_result_does_not_alias_source() -> None:
    """Mutar o dict nested no resultado NÃO afeta o raw original.

    Com copy-on-write, apenas os dicts no caminho são copiados; garantimos
    que o parent do campo mutado é uma cópia independente.
    """
    raw = {"nested": {"target": "abcdefghij", "sibling": "untouched"}}
    specs = _specs([{"path": "nested.target", "max_bytes": 5}])
    result = apply_raw_reduction(raw, specs)

    assert result is not None
    # Muta o resultado.
    result["nested"]["injected"] = "evil"
    # Raw original permanece limpo.
    assert "injected" not in raw["nested"], "mutação no resultado não deve vazar para raw original"
    assert raw["nested"]["sibling"] == "untouched"


def test_reduction_returns_none_when_nothing_fires() -> None:
    """Nenhuma spec dispara → retorna None (sem cópia alguma)."""
    raw = {"blob": "short", "items": [1, 2]}
    specs = _specs([
        {"path": "blob", "max_bytes": 1000},
        {"path": "items", "max_items": 10},
    ])
    assert apply_raw_reduction(raw, specs) is None


def test_reduction_marks_provenance_in_result() -> None:
    """Resultado inclui ``_centralops_reduced`` com os campos podados."""
    raw = {"big": "x" * 200}
    specs = _specs([{"path": "big", "max_bytes": 10}])
    result = apply_raw_reduction(raw, specs)
    assert result is not None
    assert "_centralops_reduced" in result
    assert any("big" in marker for marker in result["_centralops_reduced"])


def test_reduction_missing_path_does_not_raise() -> None:
    """Path inexistente é silenciosamente ignorado (retorna None)."""
    raw = {"other": "value"}
    specs = _specs([{"path": "nonexistent.deep", "max_bytes": 10}])
    assert apply_raw_reduction(raw, specs) is None


@pytest.mark.parametrize("raw,specs_dsl,expected_none", [
    # Sob o limite: nada dispara.
    ({"s": "abc"}, [{"path": "s", "max_bytes": 100}], True),
    # Igual ao limite: não dispara (só > limit).
    ({"s": "abc"}, [{"path": "s", "max_bytes": 3}], True),
    # Acima do limite: dispara.
    ({"s": "abcd"}, [{"path": "s", "max_bytes": 3}], False),
    # Lista no limite: não dispara.
    ({"l": [1, 2, 3]}, [{"path": "l", "max_items": 3}], True),
    # Lista acima do limite: dispara.
    ({"l": [1, 2, 3, 4]}, [{"path": "l", "max_items": 3}], False),
])
def test_reduction_boundary_conditions(raw, specs_dsl, expected_none) -> None:
    specs = _specs(specs_dsl)
    result = apply_raw_reduction(raw, specs)
    assert (result is None) == expected_none


# ── 2. _fastjson: wire bytes idênticos ao stdlib json ────────────────────────


def test_fastjson_using_orjson() -> None:
    """orjson deve estar instalado no venv (requirements.txt inclui orjson>=3.9)."""
    assert _USING_ORJSON is True, "orjson não instalado — verifique requirements.txt"


@pytest.mark.parametrize("obj", [
    {"key": "value"},
    {"nested": {"a": 1, "b": [1, 2, 3]}},
    {"unicode": "café ☃ 日本語"},
    {"null": None, "empty": ""},
    {"escaped": 'line1\nline2 "quoted" back\\slash'},
    {"number": 42, "float": 3.14},
    {"bool_t": True, "bool_f": False},
])
def test_fastjson_dumps_str_matches_stdlib(obj: dict) -> None:
    """dumps_str deve ser byte-idêntico ao stdlib json.dumps com mesmos args."""
    expected = json.dumps(obj, separators=(",", ":"), default=str, ensure_ascii=False)
    assert dumps_str(obj) == expected


@pytest.mark.parametrize("obj", [
    {"key": "value"},
    {"unicode": "café ☃ 日本語"},
])
def test_fastjson_dumps_bytes_returns_utf8(obj: dict) -> None:
    """dumps_bytes retorna bytes UTF-8."""
    result = dumps_bytes(obj)
    assert isinstance(result, bytes)
    # Decodificável como UTF-8.
    decoded = result.decode("utf-8")
    assert decoded == json.dumps(obj, separators=(",", ":"), default=str, ensure_ascii=False)


def test_fastjson_datetime_uses_str_fallback() -> None:
    """datetime deve serializar como str() (não ISO orjson nativo).

    OPT_PASSTHROUGH_DATETIME força orjson a chamar default=str para
    datetime — resultado idêntico ao stdlib json.dumps(default=str).
    """
    obj = {"when": _dt(2026, 4, 6, 12, 34, 56)}
    expected = json.dumps(obj, separators=(",", ":"), default=str, ensure_ascii=False)
    # stdlib produz "2026-04-06 12:34:56" (str(datetime)); orjson nativo usaria
    # "2026-04-06T12:34:56" — OPT_PASSTHROUGH_DATETIME corrige.
    assert dumps_str(obj) == expected
    assert "2026-04-06 12:34:56" in dumps_str(obj)


def test_fastjson_ensure_ascii_false_no_unicode_escape() -> None:
    """Caracteres multibyte aparecem como UTF-8 bruto (sem \\uXXXX)."""
    obj = {"msg": "café 日本語 ☃"}
    result = dumps_str(obj)
    assert "\\u" not in result
    assert "café" in result
    assert "日本語" in result


def test_fastjson_none_and_booleans_match_stdlib() -> None:
    """null/true/false JSON mapeiam para None/True/False Python."""
    obj = {"n": None, "t": True, "f": False}
    expected = json.dumps(obj, separators=(",", ":"), default=str, ensure_ascii=False)
    assert dumps_str(obj) == expected


# ── 3. connection_pool por destino: gating DISPATCH_PERSISTENT_LOOP ──────────


@pytest.fixture(autouse=True)
def _reset_pool():
    """Zera o pool antes/depois de cada teste de pool."""
    cp.reset()
    yield
    cp.reset()


def _fake_connector():
    """Connector fake (sem socket real) para os testes."""
    c = MagicMock(spec=["close"])
    c.close = AsyncMock()
    return c


def test_pool_returns_none_when_flag_off(monkeypatch) -> None:
    """Com DISPATCH_PERSISTENT_LOOP=0 (default), pool retorna None."""
    monkeypatch.delenv("DISPATCH_PERSISTENT_LOOP", raising=False)

    async def _check():
        return cp.get_pooled_session("dest-1", _fake_connector)

    result = asyncio.run(_check())
    assert result is None, "pool deve retornar None com flag OFF (legado)"


def test_pool_returns_session_when_flag_on(monkeypatch) -> None:
    """Com DISPATCH_PERSISTENT_LOOP=1, pool cria e devolve uma ClientSession."""
    monkeypatch.setenv("DISPATCH_PERSISTENT_LOOP", "1")

    async def _check():
        return cp.get_pooled_session("dest-elastic-01", lambda: aiohttp.TCPConnector())

    result = asyncio.run(_check())
    assert result is not None
    assert isinstance(result, aiohttp.ClientSession)


def test_pool_reuses_session_across_calls(monkeypatch) -> None:
    """Mesmo dest_id + mesmo loop → mesma session (reuso de socket)."""
    monkeypatch.setenv("DISPATCH_PERSISTENT_LOOP", "1")

    calls = {"n": 0}

    def _make():
        calls["n"] += 1
        return aiohttp.TCPConnector()

    async def _check():
        s1 = cp.get_pooled_session("dest-X", _make)
        s2 = cp.get_pooled_session("dest-X", _make)
        return s1, s2

    s1, s2 = asyncio.run(_check())
    assert s1 is s2, "mesmo dest_id + mesmo loop → mesma session"
    assert calls["n"] == 1, "connector_factory chamado UMA vez (reuso)"


def test_pool_isolates_different_destinations(monkeypatch) -> None:
    """Dois dest_id distintos têm sessions distintas (sem aliasing)."""
    monkeypatch.setenv("DISPATCH_PERSISTENT_LOOP", "1")

    async def _check():
        s1 = cp.get_pooled_session("dest-A", lambda: aiohttp.TCPConnector())
        s2 = cp.get_pooled_session("dest-B", lambda: aiohttp.TCPConnector())
        return s1, s2

    s1, s2 = asyncio.run(_check())
    assert s1 is not s2, "dest_id distintos → sessions distintas"


@pytest.mark.asyncio
async def test_pool_close_all_closes_sessions(monkeypatch) -> None:
    """close_all() fecha todas as sessions do pool."""
    monkeypatch.setenv("DISPATCH_PERSISTENT_LOOP", "1")

    s1 = cp.get_pooled_session("dest-A", lambda: aiohttp.TCPConnector())
    s2 = cp.get_pooled_session("dest-B", lambda: aiohttp.TCPConnector())
    assert s1 is not None and s2 is not None

    await cp.close_all()
    assert s1.closed
    assert s2.closed
    assert cp._pool == {}


def test_elastic_bulk_client_uses_pool_when_flag_on(monkeypatch) -> None:
    """ElasticBulkClient com destination_id obtém session do pool (flag ON)."""
    monkeypatch.setenv("DISPATCH_PERSISTENT_LOOP", "1")
    cp.reset()

    client = ElasticBulkClient(
        url="https://es.test.local:9200/",
        secret="k",
        index="centralops",
        verify_tls=False,
        destination_id="dest-elastic-pool-01",
    )

    async def _get():
        return client._get_session()

    # asyncio.run cria loop novo por chamada — o pool detecta loop diferente
    # e recria. O que testamos: ambas retornam ClientSession válida (não None).
    s1 = asyncio.run(_get())
    s2 = asyncio.run(_get())
    assert isinstance(s1, aiohttp.ClientSession)
    assert isinstance(s2, aiohttp.ClientSession)


def test_elastic_bulk_client_without_dest_id_uses_local_session(monkeypatch) -> None:
    """ElasticBulkClient sem destination_id usa singleton local (legado)."""
    monkeypatch.setenv("DISPATCH_PERSISTENT_LOOP", "1")

    client = ElasticBulkClient(
        url="https://es.test.local:9200/",
        secret="k",
        verify_tls=False,
        # destination_id não informado → pool não usado
    )

    async def _get():
        return client._get_session()

    session = asyncio.run(_get())
    assert isinstance(session, aiohttp.ClientSession)
