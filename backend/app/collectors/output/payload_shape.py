"""O que sai no corpo: o envelope canônico ou o OCSF puro.

Existem dois consumidores legítimos e incompatíveis do mesmo evento:

* Quem quer **o envelope canônico** do CentralOps, com o namespace
  ``_centralops`` (id do evento, linhagem, org, integração, marcas de redução).
  É o que serve para investigar e correlacionar dentro da plataforma.

* Quem quer **OCSF 1.8 e nada mais**. Um SIEM nativo de OCSF, ou uma tabela
  cujo DDL foi escrito a partir do schema OCSF, recebe o envelope inteiro e
  ou descarta as colunas que não conhece, ou rejeita o lote. As duas saídas são
  ruins: na primeira o operador acha que entregou e o dado está mutilado, na
  segunda a entrega para e ninguém sabe por quê.

Antes disto só o webhook sabia escolher, e ainda assim por um campo de texto
livre chamado ``body``, sem lista de opções. O HEC e o ClickHouse mandavam o
envelope sempre. Este módulo é a definição única, para os três não divergirem.

**Por que "o OCSF puro" é simplesmente ``envelope["normalized"]``.** O pipeline
já produz o evento normalizado nessa chave, validado contra o manifesto OCSF
1.8. Não há conversão aqui, e é de propósito: qualquer transformação neste
ponto seria uma segunda implementação do normalizador, livre para divergir da
primeira.
"""

from __future__ import annotations

from typing import Any, Literal, Mapping

#: Formato do corpo por evento. ``Literal`` (e não ``str``) porque o JSON Schema
#: gerado precisa sair com ``enum``: é o que faz a UI renderizar uma lista em vez
#: de caixa de texto. Com texto livre, um valor errado não dá erro, cai no ramo
#: default e o destino recebe o formato que o operador não pediu.
PayloadShape = Literal["envelope", "ocsf"]

#: Aceito na leitura por compatibilidade: o webhook chamava o OCSF puro de
#: ``normalized``. Config antiga continua funcionando sem migração.
_SINONIMOS = {"normalized": "ocsf"}

DESCRICAO = (
    "O que enviar por evento: 'envelope' (canônico do CentralOps, com o "
    "namespace _centralops) ou 'ocsf' (apenas o evento OCSF 1.8, para "
    "consumidor nativo de OCSF)"
)


def normalizar_shape(valor: Any) -> PayloadShape:
    """Aceita o nome atual, o histórico, e cai no envelope se vier lixo.

    Cair no envelope é a escolha conservadora: é o formato mais completo, e
    perder contexto é reversível enquanto entregar vazio não é.
    """
    if not isinstance(valor, str):
        return "envelope"
    v = valor.strip().lower()
    v = _SINONIMOS.get(v, v)
    return "ocsf" if v == "ocsf" else "envelope"


def render_payload(envelope: Mapping[str, Any], shape: Any = "envelope") -> Any:
    """Envelope canônico → o corpo que vai no fio.

    Em ``ocsf``, devolve o evento normalizado. Se ele estiver ausente (evento
    que não passou pela normalização), devolve dicionário vazio em vez do
    envelope: mandar o envelope aqui entregaria silenciosamente o formato
    errado a um consumidor que declarou esperar OCSF, e um campo a mais numa
    tabela estrita derruba o lote inteiro.

    **Não copia.** Devolve o próprio objeto, e isso é contrato, não detalhe: o
    wrapper do HEC aninha o envelope por REFERÊNCIA, e há teste de contrato de
    fio que compara identidade (``wrapper["event"] is envelope``). Copiar aqui
    quebrava esses testes e ainda pagava uma cópia de dicionário por evento no
    caminho quente, sem ganho: todos os chamadores serializam em seguida e
    nenhum muta o resultado.
    """
    if normalizar_shape(shape) == "ocsf":
        normalizado = envelope.get("normalized")
        return normalizado if isinstance(normalizado, Mapping) else {}
    return envelope
