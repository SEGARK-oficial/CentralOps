"""Regressão: cursor Sophos envenenado por timestamp fora do formato aceito.

Sintoma em produção: integrações Sophos funcionavam e, depois de um tempo,
passavam a responder ``400 Bad Request`` em TODA coleta, só voltando quando um
operador zerava o coletor.

Mecanismo: o cursor de cada ciclo é derivado do ``createdAt``/``updatedAt``/
``time`` do próprio evento. ``_normalize_ts`` só removia microssegundos — um
``createdAt`` com offset não-UTC, sem fuso ou com offset sem dois-pontos passava
intacto e virava o query param ``from``, que a Sophos rejeita com
``validationException: Timestamp ... is not in the right format``. A partir daí
todo ciclo falhava, e o caminho de erro do pipeline regrava ``cursor_before``
byte-a-byte — o valor ruim era imortal até o reset manual.

Estes testes fixam as duas metades da correção: canonicalizar para UTC, e nunca
persistir no cursor um valor que a API rejeitaria.
"""

from __future__ import annotations


import pytest

from ..vendors.sophos import (
    _CANON_TS_RE,
    _default_lookback_iso,
    _normalize_ts,
    _safe_cursor_ts,
)


class TestNormalizeTs:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            # o caso que o fix anterior já cobria
            ("2026-04-23T18:56:10.439851Z", "2026-04-23T18:56:10Z"),
            ("2026-04-23T18:56:10Z", "2026-04-23T18:56:10Z"),
            # os que passavam intactos e envenenavam o cursor
            ("2026-04-23T18:56:10-03:00", "2026-04-23T21:56:10Z"),
            ("2026-04-23T18:56:10.123-03:00", "2026-04-23T21:56:10Z"),
            ("2026-04-23T18:56:10+05:30", "2026-04-23T13:26:10Z"),
            ("2026-04-23T18:56:10+0000", "2026-04-23T18:56:10Z"),
            ("2026-04-23T18:56:10-0300", "2026-04-23T21:56:10Z"),
            # naive: assumido UTC (é o que a Sophos documenta para datas)
            ("2026-04-23T18:56:10", "2026-04-23T18:56:10Z"),
        ],
    )
    def test_canonicaliza_para_utc_com_sufixo_z(self, raw, expected):
        assert _normalize_ts(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "2026-04-23T18:56:10.439851Z",
            "2026-04-23T18:56:10-03:00",
            "2026-04-23T18:56:10+0000",
            "2026-04-23T18:56:10",
        ],
    )
    def test_saida_sempre_no_formato_que_a_api_aceita(self, raw):
        """O contrato que impede o 400: a saída casa com o formato canônico."""
        assert _CANON_TS_RE.match(_normalize_ts(raw)), (
            f"{raw!r} produziu um valor que a Sophos rejeitaria"
        )

    def test_valor_impossivel_de_parsear_volta_como_veio(self):
        assert _normalize_ts("lixo") == "lixo"
        assert _normalize_ts("") == ""
        assert _normalize_ts(None) is None  # type: ignore[arg-type]


class TestSafeCursorTs:
    def test_valor_bom_passa_canonicalizado(self):
        assert (
            _safe_cursor_ts("2026-04-23T18:56:10-03:00", "FALLBACK")
            == "2026-04-23T21:56:10Z"
        )

    @pytest.mark.parametrize("ruim", ["lixo", "", "2026-13-45T99:99:99Z", "não é data"])
    def test_valor_ruim_cai_no_fallback_em_vez_de_envenenar(self, ruim):
        """Preferir recoletar uma janela a travar o feed com 400 permanente."""
        assert _safe_cursor_ts(ruim, "2026-04-23T18:00:00Z") == "2026-04-23T18:00:00Z"

    def test_fallback_padrao_e_sempre_canonico(self):
        """O cold start nunca pode ser a origem de um cursor inválido."""
        assert _CANON_TS_RE.match(_default_lookback_iso())


class TestWatermarkMonotonico:
    """A comparação de watermark é lexicográfica (``created > latest_ts``).

    Isso só é monotônico se os dois lados estiverem no mesmo formato. Com um
    offset não-UTC cru, ``'...T18:56:10-03:00' < '...T18:56:10Z'`` porque
    ``'-'`` (0x2D) vem antes de ``'Z'`` (0x5A) — o watermark andaria para trás
    ou congelaria, que é a outra metade do sintoma relatado.
    """

    def test_offset_negativo_cru_quebraria_a_ordem(self):
        assert not ("2026-04-23T18:56:10-03:00" > "2026-04-23T18:56:10Z")

    def test_apos_canonicalizar_a_ordem_reflete_o_instante_real(self):
        # 18:56 em -03:00 é 21:56 UTC, portanto POSTERIOR a 18:56 UTC.
        depois = _normalize_ts("2026-04-23T18:56:10-03:00")
        antes = _normalize_ts("2026-04-23T18:56:10Z")
        assert depois > antes

    def test_ordem_preservada_entre_formatos_mistos(self):
        """Um tenant pode emitir formatos diferentes no mesmo lote."""
        instantes = [
            "2026-04-23T10:00:00Z",
            "2026-04-23T08:30:00-03:00",  # 11:30 UTC
            "2026-04-23T12:00:00",  # naive -> 12:00 UTC
            "2026-04-23T18:00:00+05:30",  # 12:30 UTC
        ]
        canon = [_normalize_ts(i) for i in instantes]
        assert canon == sorted(canon), "ordem cronológica não sobreviveu à canonicalização"
