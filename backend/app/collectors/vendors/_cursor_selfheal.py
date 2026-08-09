"""Auto-cura de cursor opaco rejeitado pelo vendor.

**O modo de falha.** Vários coletores persistem no cursor um token opaco que o
próprio vendor emitiu para continuar a paginação — ``pageFromKey`` (Sophos
alerts), ``page_cursor`` (Sophos SIEM v1), ``run_id`` (Sophos XDR detections),
``after`` (CrowdStrike), ``@odata.nextLink`` (Entra), ``next_url`` (Okta). Esses
tokens **expiram ou são invalidados** do lado do vendor, e quando isso acontece a
resposta é um 4xx.

Sozinho, um 4xx transitório não seria problema. O que o torna permanente é o
caminho de erro do pipeline: quando o coletor levanta, ``run_collection_once``
regrava ``cursor_before`` — o cursor lido no INÍCIO do ciclo — byte a byte. O
token morto volta ao banco, o ciclo seguinte o reenvia, toma 4xx de novo, e o
feed fica travado **até alguém zerar o coletor à mão**. Foi exatamente esse o
sintoma observado em produção: "funciona e depois de um tempo só volta com
reset".

**A cura.** Em 4xx, não levantar: descartar o token e encerrar o ciclo por um
caminho NORMAL, para que a escrita final do coletor persista o cursor já saneado
(token nulo, janela temporal preservada). O próximo ciclo recomeça pela janela —
custa reler o que já foi lido, e o dedupe do pipeline absorve a duplicata.

Trocar dado repetido por feed travado é sempre o negócio certo aqui: a
re-leitura é barata e idempotente, a parada é silenciosa e indefinida.

**O que NÃO é coberto.** 401 fica de fora de propósito: é recuperação de token
de autenticação, tratada pelo pipeline (que renova a credencial e repete). 5xx
também fica fora — erro do servidor merece retry com o cursor intacto, porque o
token provavelmente continua válido.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def is_stale_cursor_response(status: int, *, has_opaque_token: bool) -> bool:
    """``True`` se este 4xx deve ser lido como "o token de paginação morreu".

    Só vale quando havia um token opaco em jogo: um 4xx no PRIMEIRO request de um
    ciclo (sem token) é um erro real — parâmetro inválido, permissão, tenant
    errado — e precisa continuar subindo, senão o coletor engole uma falha de
    configuração e reporta sucesso sem coletar nada.

    401 e 5xx ficam fora (ver o docstring do módulo).
    """
    if not has_opaque_token:
        return False
    return 400 <= status < 500 and status != 401


def log_stale_cursor(
    vendor_stream: str,
    *,
    status: int,
    integration_id: Optional[int],
    token_field: str,
    resume_from: Any,
    body_preview: str = "",
) -> None:
    """Registra o descarte de forma diagnosticável.

    Precisa dizer o que foi jogado fora e de onde o próximo ciclo vai retomar —
    sem isso, o operador vê "coletou zero" e não tem como distinguir de um tenant
    sem eventos.
    """
    logger.warning(
        "%s: HTTP %s com %s — token de paginação descartado, próximo ciclo "
        "retoma de %r (integration=%s)%s",
        vendor_stream,
        status,
        token_field,
        resume_from,
        integration_id,
        f" body={body_preview}" if body_preview else "",
    )
