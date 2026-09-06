"""Classificação por conteúdo (W3.3): JSON parseado → ``stream``.

Para fontes genéricas (syslog e ``custom_json``) o transporte não diz o que o
evento É. O classificador diz: uma lista ordenada de ``{"when": <JMESPath>,
"stream": <nome>}``; a primeira que casa vence; nada casa ⇒ ``default_stream``.

``when`` pode citar um **detector de fábrica** por nome (``"@fortigate"``):
são expressões prontas para o que chega em porta 514 com mais frequência,
escritas contra a saída do :mod:`app.syslog.parser` (``msg``, ``app``, ``sd``,
``facility``…). O operador pode ver e copiar a expressão real pela API.

Puro: compila uma vez, avalia N vezes, nunca levanta na avaliação (JMESPath
com tipo inesperado devolve ``None`` ⇒ não casa).
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import jmespath
from jmespath.exceptions import JMESPathError

#: Detectores de fábrica. A chave é o nome que o operador cita (``@nome``); o
#: valor é JMESPath sobre o evento parseado. Fixtures anonimizadas dos seis
#: formatos vivem em ``tests/test_syslog_classifier.py``.
FACTORY_DETECTORS: Mapping[str, Dict[str, str]] = {
    "fortigate": {
        "label": "Fortinet FortiGate (syslog key=value)",
        "when": "contains(msg, 'devname=') && contains(msg, 'logid=')",
    },
    "paloalto": {
        "label": "Palo Alto PAN-OS (CSV: TRAFFIC/THREAT/SYSTEM)",
        "when": "contains(msg, ',TRAFFIC,') || contains(msg, ',THREAT,') || contains(msg, ',SYSTEM,') || contains(msg, ',CONFIG,')",
    },
    "cisco_asa": {
        "label": "Cisco ASA / FTD (%ASA-n-nnnnnn)",
        "when": "contains(msg, '%ASA-') || contains(msg, '%FTD-')",
    },
    "pfsense": {
        "label": "pfSense / OPNsense (filterlog)",
        "when": "app == 'filterlog' || contains(msg, 'filterlog')",
    },
    "linux_auth": {
        "label": "Linux auth (sshd, sudo, su, PAM — facility authpriv)",
        "when": "app == 'sshd' || app == 'sudo' || app == 'su' || app == 'login' || facility == `10` || facility == `4`",
    },
    "windows_vector": {
        "label": "Windows Event Log via Vector/NXLog (EventID no msg ou SD)",
        "when": "app == 'Microsoft-Windows-Security-Auditing' || contains(msg, 'EventID=') || contains(msg, '\"EventID\"') || sd.win != null",
    },
}


class ClassifierError(ValueError):
    pass


def resolve_when(when: str) -> str:
    """``@nome`` → expressão do detector; qualquer outra string é JMESPath cru."""
    w = str(when or "").strip()
    if w.startswith("@"):
        det = FACTORY_DETECTORS.get(w[1:])
        if det is None:
            raise ClassifierError(f"detector de fábrica desconhecido: {w!r}. Válidos: {sorted(FACTORY_DETECTORS)}")
        return det["when"]
    if not w:
        raise ClassifierError("'when' vazio")
    return w


class Classifier:
    __slots__ = ("_rules", "default_stream")

    def __init__(self, rules: Sequence[Tuple[Any, str, str]], default_stream: Optional[str]) -> None:
        self._rules = tuple(rules)
        self.default_stream = default_stream

    @property
    def rules(self) -> Tuple[Tuple[str, str], ...]:
        """(when como escrito, stream) — para a UI mostrar."""
        return tuple((w, s) for _, w, s in self._rules)

    def classify(self, event: Mapping[str, Any]) -> Optional[str]:
        for compiled, _w, stream in self._rules:
            try:
                if compiled.search(event):
                    return stream
            except Exception:  # noqa: BLE001 — tipo inesperado = não casa
                continue
        return self.default_stream

    def explain(self, event: Mapping[str, Any]) -> Dict[str, Any]:
        """Dry-run: qual regra casou e o que cada uma devolveu."""
        trace: List[Dict[str, Any]] = []
        chosen: Optional[str] = None
        for compiled, w, stream in self._rules:
            try:
                r = compiled.search(event)
            except Exception as exc:  # noqa: BLE001
                r = f"erro: {exc}"
            hit = bool(r) and not isinstance(r, str)
            trace.append({"when": w, "stream": stream, "result": r, "matched": hit})
            if hit and chosen is None:
                chosen = stream
        return {"stream": chosen or self.default_stream, "matched_rule": chosen is not None, "trace": trace}


def compile_classifier(config: Optional[Mapping[str, Any]], *, default_stream: Optional[str] = None) -> Classifier:
    """``{"rules": [{"when", "stream"}], "default_stream"}`` → :class:`Classifier`.
    Erro de sintaxe é levantado AQUI (na escrita da config), não no servidor."""
    cfg = dict(config or {})
    rules_raw = cfg.get("rules") or []
    if not isinstance(rules_raw, list):
        raise ClassifierError("'rules' deve ser uma lista")
    if len(rules_raw) > 50:
        raise ClassifierError("no máximo 50 regras de classificação por fonte")
    compiled: List[Tuple[Any, str, str]] = []
    for i, r in enumerate(rules_raw):
        if not isinstance(r, Mapping):
            raise ClassifierError(f"regra #{i}: deve ser objeto {{when, stream}}")
        stream = str(r.get("stream") or "").strip()
        if not stream:
            raise ClassifierError(f"regra #{i}: 'stream' obrigatório")
        expr = resolve_when(str(r.get("when") or ""))
        try:
            c = jmespath.compile(expr)
        except JMESPathError as exc:
            raise ClassifierError(f"regra #{i}: JMESPath inválido em 'when': {exc}") from exc
        compiled.append((c, str(r.get("when")), stream))
    ds = cfg.get("default_stream") or default_stream
    return Classifier(compiled, str(ds).strip() if ds else None)


def streams_referenced(config: Optional[Mapping[str, Any]]) -> List[str]:
    cfg = dict(config or {})
    out = [str(r.get("stream")) for r in (cfg.get("rules") or []) if isinstance(r, Mapping) and r.get("stream")]
    if cfg.get("default_stream"):
        out.append(str(cfg["default_stream"]))
    return sorted(set(out))
