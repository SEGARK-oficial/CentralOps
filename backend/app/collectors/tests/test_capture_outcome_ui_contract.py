"""Guard: o vocabulário de DESFECHO da captura é um contrato backend→UI.

``capture_session.OUTCOMES`` é a fonte da verdade (enum FECHADO). A tela de Captura ao
vivo precisa saber nomear e colorir cada um deles — senão o operador vê a string crua
(``delivery_failed``) num badge NEUTRO, visualmente igual a um desfecho benigno.

Regressão real que motivou este guard: a UI foi escrita com um vocabulário ADIVINHADO
(``sampled``, ``no_route``, ``breaker_open``, ``failed``, ``dlq``…) que o backend nunca
emite, e faltavam 4 dos 9 desfechos reais — inclusive ``delivery_failed``, que é
justamente o "morreu no sink" e o mais importante para troubleshooting.

Este teste vive no BACKEND de propósito: é ele quem define o enum, então é ele quem
cobra a UI. Um desfecho novo quebra o CI até ser mapeado nos 3 locales + no mapa de cor.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..capture_session import OUTCOMES

_REPO = Path(__file__).resolve().parents[4]
_PANEL = _REPO / "frontend" / "src" / "components" / "config" / "CapturePanel.tsx"
_LOCALES = ("pt", "en", "es")
# chaves de UI que não são desfechos (estado "sem outcome" / tooltip explicativo)
_UI_ONLY = {"unknown", "unknownTooltip"}


def _tone_keys() -> set[str]:
    """Chaves do mapa ``OUTCOME_TONES`` do painel (parse textual — sem build do TS)."""
    src = _PANEL.read_text(encoding="utf-8")
    start = src.index("const OUTCOME_TONES")
    end = src.index("\n}", start)
    return set(re.findall(r"^\s*([a-z_]+):\s*\"", src[start:end], re.MULTILINE))


def _locale_keys(loc: str) -> set[str]:
    p = _REPO / "frontend" / "src" / "i18n" / "locales" / loc / "config.json"
    return set(json.loads(p.read_text(encoding="utf-8"))["capture"]["outcomes"])


def test_ui_tone_map_covers_every_backend_outcome() -> None:
    missing = OUTCOMES - _tone_keys()
    assert not missing, (
        f"desfechos sem cor no CapturePanel (cairiam no badge neutro): {sorted(missing)}"
    )


def test_ui_tone_map_has_no_invented_outcomes() -> None:
    """Chave que o backend nunca emite é código morto — e sinaliza que alguém
    adivinhou o vocabulário em vez de ler o enum."""
    extra = _tone_keys() - OUTCOMES
    assert not extra, f"OUTCOME_TONES tem desfechos inexistentes no backend: {sorted(extra)}"


def test_every_locale_translates_every_backend_outcome() -> None:
    for loc in _LOCALES:
        missing = OUTCOMES - _locale_keys(loc)
        assert not missing, (
            f"locale {loc}: desfechos sem tradução (a tela mostraria a string crua): "
            f"{sorted(missing)}"
        )


def test_locales_have_no_invented_outcomes() -> None:
    for loc in _LOCALES:
        extra = _locale_keys(loc) - OUTCOMES - _UI_ONLY
        assert not extra, f"locale {loc}: traduções órfãs (backend não emite): {sorted(extra)}"
