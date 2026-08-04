"""Reconciliação periódica das entries dinâmicas do RedBeat.

Uma integração ``kind=tenant`` ativa PRECISA ter entries no RedBeat para ser
coletada. Até aqui essa escrita acontecia em exatamente três momentos — no
``POST /integrations``, na task de sync de tenants (parceiro) e no boot do
Beat (``beat_schedule`` chama ``sync_all_active_integrations()`` como
side-effect de import). Os dois primeiros são fire-and-forget: engolem qualquer
exceção e apenas logam. Logo, um Redis momentaneamente indisponível durante a
criação — ou qualquer caminho de reativação que esquecesse de registrar —
deixava a integração ativa no banco e **invisível para o scheduler**, sem
nenhum sinal na UI. O único conserto era reiniciar o Beat.

Esta task fecha esse buraco: transforma o boot-sync num processo contínuo, de
modo que qualquer divergência entre "ativa no banco" e "agendada no Redis" se
cure sozinha dentro de um período, em vez de esperar o próximo restart.

Por que é seguro rodar de novo periodicamente: ``register_integration_in_beat``
consulta ``_existing_entry_matches`` antes de salvar, e só reescreve a entry
quando ela não existe ou mudou de definição. Isso importa muito aqui —
``entry.save()`` numa entry existente recalcula o score do zset e, para uma
entry que ainda não rodou, reagenda para ``now + intervalo``. Sem esse guard,
uma reconciliação a cada 10 min causaria INANIÇÃO de todo stream cujo intervalo
seja maior que 10 min (foi exatamente o incidente de jul/2026, em que
``cases``/``detections`` nunca disparavam enquanto ``alerts``, de 1 min,
sobrevivia). O teste ``test_scheduler_entry_idempotence`` deixa de ser opcional
e passa a sustentar esta task.

Best-effort: falha aqui nunca afeta coleta nem entrega.
"""

from __future__ import annotations

import logging
from typing import Any

from celery import shared_task

logger = logging.getLogger(__name__)

#: Nome estável — referenciado por ``beat_schedule._static_entries``.
TASK_NAME = "collectors.reconcile_beat_entries"


@shared_task(bind=True, queue="maintenance", name=TASK_NAME)
def reconcile_beat_entries(self: Any) -> dict[str, Any]:
    """Re-sincroniza todas as integrações ativas com o RedBeat.

    Returns: ``{"reconciled": bool}`` — ``False`` quando a reconciliação falhou
    (o erro já foi logado). Nunca levanta: uma indisponibilidade de DB/Redis
    aqui não pode derrubar o worker de maintenance.
    """
    try:
        from .scheduler import sync_all_active_integrations

        sync_all_active_integrations()
        return {"reconciled": True}
    except Exception:
        logger.error(
            "reconcile_beat_entries: reconciliação falhou — integrações ativas "
            "podem seguir sem entry no RedBeat até a próxima tentativa",
            exc_info=True,
        )
        return {"reconciled": False}
