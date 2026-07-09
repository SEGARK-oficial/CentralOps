"""Wrapper de serialização JSON de alta performance.

Tenta usar ``orjson`` (C-extension, ~2-4× mais rápido que stdlib json para
payloads típicos de eventos). Faz fallback para ``json`` stdlib se ``orjson``
não estiver instalado — garantindo que o código funcione em ambientes onde o
pacote não foi instalado sem falha em import.

``orjson.dumps`` retorna ``bytes``; este módulo expõe:
  - ``dumps_str(obj) -> str``  — para senders que precisam de string (syslog)
  - ``dumps_bytes(obj) -> bytes`` — para senders que precisam de bytes (elastic)

Ambos aplicam os mesmos defaults:
  - ``separators=(",",":")`` compacto (sem espaços)
  - ``ensure_ascii=False`` — preserva UTF-8 bruto no wire
  - ``default=str`` — converte tipos não-serializáveis via str()

Não use este módulo em caminhos frios (admin, migrations) — stdlib json é
suficiente fora do hot path.
"""

from __future__ import annotations

from typing import Any

try:
    import orjson as _orjson

    # orjson usa flags em vez de keyword args.
    # OPT_NON_STR_KEYS: permite chaves não-string (robustez).
    # OPT_PASSTHROUGH_DATETIME: não serializa datetime nativamente — passa
    #   para default=str, que gera "YYYY-MM-DD HH:MM:SS", idêntico ao
    #   str(datetime) do stdlib. SEM esta flag, orjson usaria ISO-8601
    #   ("YYYY-MM-DDTHH:MM:SS"), quebrando o wire contract dos testes.
    _ORJSON_OPTS = _orjson.OPT_NON_STR_KEYS | _orjson.OPT_PASSTHROUGH_DATETIME

    def dumps_bytes(obj: Any) -> bytes:
        """Serializa ``obj`` para bytes JSON compactos (UTF-8).

        orjson emite JSON compacto por default (sem espaços), ensure_ascii=False
        (UTF-8 bruto). ``default=str`` converte tipos não-serializáveis via
        str() — mesma semântica do stdlib ``json.dumps(default=str)``.
        """
        try:
            return _orjson.dumps(obj, default=str, option=_ORJSON_OPTS)
        except Exception:
            # Fallback pontual para objetos que orjson recusa (raro)
            import json as _json_fallback
            return _json_fallback.dumps(
                obj, separators=(",", ":"), default=str, ensure_ascii=False
            ).encode("utf-8")

    def dumps_str(obj: Any) -> str:
        """Serializa ``obj`` para string JSON compacta (UTF-8 → decodifica)."""
        return dumps_bytes(obj).decode("utf-8")

    _USING_ORJSON = True

except ImportError:
    # orjson não instalado — usa stdlib json com os mesmos defaults.
    import json as _json

    def dumps_bytes(obj: Any) -> bytes:  # type: ignore[misc]
        """Fallback stdlib: serializa para bytes JSON compactos."""
        return _json.dumps(
            obj, separators=(",", ":"), default=str, ensure_ascii=False
        ).encode("utf-8")

    def dumps_str(obj: Any) -> str:  # type: ignore[misc]
        """Fallback stdlib: serializa para string JSON compacta."""
        return _json.dumps(
            obj, separators=(",", ":"), default=str, ensure_ascii=False
        )

    _USING_ORJSON = False
