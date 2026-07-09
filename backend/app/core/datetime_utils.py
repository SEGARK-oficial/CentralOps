"""Utilitários de datetime para o CentralOps.

Padrão do projeto: datetimes são armazenados como NAIVE-UTC no banco e comparados
com datetime.utcnow() (também naive). Qualquer datetime que vem de origem externa
(request JSON, API de terceiros) pode chegar timezone-AWARE (ex: "2030-01-01T00:00:00Z"
parseado pelo Pydantic). Esta função normaliza para NAIVE-UTC antes de qualquer
comparação ou persistência, mantendo consistência com o restante do codebase.
"""

from __future__ import annotations

from datetime import datetime, timezone


def ensure_naive_utc(dt: datetime | None) -> datetime | None:
    """Converte datetime aware para naive-UTC; retorna naive inalterado; None -> None.

    Exemplos:
        datetime(2030, 1, 1, tzinfo=timezone.utc) -> datetime(2030, 1, 1)  # naive
        datetime(2030, 1, 1)                       -> datetime(2030, 1, 1)  # sem mudança
        None                                        -> None
    """
    if dt is None:
        return None
    if dt.tzinfo is not None:
        # Converte para UTC e remove tzinfo — mantém consistência com utcnow() naive.
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt
