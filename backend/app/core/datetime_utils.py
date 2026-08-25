"""Utilitários de datetime para o CentralOps.

Padrão do projeto: datetimes são armazenados como NAIVE-UTC no banco e comparados
com datetime.utcnow() (também naive). Qualquer datetime que vem de origem externa
(request JSON, API de terceiros) pode chegar timezone-AWARE (ex: "2030-01-01T00:00:00Z"
parseado pelo Pydantic). Esta função normaliza para NAIVE-UTC antes de qualquer
comparação ou persistência, mantendo consistência com o restante do codebase.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from pydantic import PlainSerializer


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


def to_utc_iso(dt: datetime) -> str:
    """Serializa um datetime do projeto como ISO-8601 **com o fuso explícito**.

    Contraparte de SAÍDA de :func:`ensure_naive_utc`. Aquela normaliza o que
    ENTRA; esta rotula o que SAI.

    Naive é interpretado como UTC, que é a convenção do banco. O sufixo é ``Z``
    e não ``+00:00`` por ser o que a maior parte dos clientes espera ler.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


#: Use no lugar de ``datetime`` em campo de schema que carrega um instante vindo
#: do banco. Sem isto o Pydantic emite o naive **sem fuso nenhum**
#: (``"2026-08-25T06:27:57.457811"``) e o consumidor é obrigado a adivinhar —
#: o ``new Date()` do JavaScript adivinha **hora local**, por especificação.
#:
#: O estrago não é cosmético. Num browser em UTC−3 a tela de Coletores mostrava
#: TODO atraso 180 min menor que o real, e qualquer coleta das últimas 3h caía no
#: ``Math.max(0, …)`` do badge e aparecia como **"Ativo" verde** — um timestamp
#: "no futuro" virava zero. Durante o apagão de ago/2026 a tela dizia 156 min
#: enquanto ``stale_minutes_max``, calculado no servidor (naive − naive, correto),
#: dizia 358. As duas telas do mesmo produto discordavam pelo offset do operador.
#:
#: ``when_used="json"`` mantém ``model_dump()`` em modo Python devolvendo
#: ``datetime`` — só o que vai para a rede é rotulado.
UtcDateTime = Annotated[
    datetime, PlainSerializer(to_utc_iso, return_type=str, when_used="json")
]
