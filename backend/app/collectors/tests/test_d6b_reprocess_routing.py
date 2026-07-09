"""O reprocesso de quarentena roteia pelo helper ÚNICO.

Antes, o reprocesso (endpoint single e task bulk) hard-codava uma entrega
direta ao Wazuh → furava o fan-out multi-destino e o roteamento por regra.
Agora ambos chamam ``pipeline._enqueue_dispatch``, de modo que um evento
reprocessado siga EXATAMENTE o mesmo caminho de um evento coletado: roteamento
(sempre ativo), com wazuh-default como um Destination
normal (kind syslog_rfc3164) no catch-all, despachado por
``dispatch_batch_to_destination`` como qualquer outro destino.
"""

from __future__ import annotations

from unittest.mock import patch


def test_single_reprocess_routes_through_enqueue_dispatch() -> None:
    from backend.app.routers.quarantine import _enqueue_reprocess_dispatch

    env = {"_centralops": {"event_id": "x", "organization_id": 1}}
    with patch("backend.app.collectors.pipeline._enqueue_dispatch") as mock_enq:
        _enqueue_reprocess_dispatch(env)
    mock_enq.assert_called_once_with([env])


def test_single_reprocess_propagates_enqueue_failure() -> None:
    """Falha de enqueue (broker offline) PROPAGA → o caller não marca
    ``reprocessed_at`` (invariante preservado pelo helper)."""
    from backend.app.routers.quarantine import _enqueue_reprocess_dispatch

    env = {"_centralops": {"event_id": "x", "organization_id": 1}}
    with patch(
        "backend.app.collectors.pipeline._enqueue_dispatch",
        side_effect=RuntimeError("broker offline"),
    ):
        try:
            _enqueue_reprocess_dispatch(env)
        except RuntimeError as exc:
            assert "broker offline" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("exceção de enqueue deveria propagar")
