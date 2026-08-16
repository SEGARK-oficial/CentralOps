"""Como a linha sai no fio: o que vai DENTRO, e a forma AO REDOR.

São duas perguntas independentes, e o módulo responde as duas separadamente
porque colapsá-las numa só já custou caro (ver "A topologia que faltava").

**Pergunta 1, o conteúdo (``payload``).** Existem dois consumidores legítimos e
incompatíveis do mesmo evento:

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

**Pergunta 2, a forma da linha (``row_shape``).** Responder só a primeira
pergunta assume uma topologia de consumidor: a tabela tem **uma coluna por
campo do evento**. Existe uma segunda topologia, igualmente comum, em que a
tabela tem **uma coluna que recebe o evento inteiro** mais colunas de rótulo::

    flat     {"class_uid": 1006, "time": 178…, "metadata": {…}}
    wrapped  {"event": {"class_uid": 1006, …}, "source_type": "sophos"}

Para a primeira, ``payload="ocsf"`` já bastava. Para a segunda, **nenhum** valor
de ``payload`` servia, e o modo de falha era o pior possível: as chaves emitidas
não intersectam as colunas, o ClickHouse com
``input_format_skip_unknown_fields=1`` (o default do servidor) descarta tudo em
silêncio, responde **HTTP 200**, e grava a quantidade certa de linhas todas
vazias. O produtor conta como entregue, o consumidor conta como recebido, e o
dado não existe. Foi medido, não deduzido.

``row_shape`` existe para tornar essa segunda topologia declarável em vez de
impossível. O default é ``flat``, byte-idêntico ao comportamento anterior.
"""

from __future__ import annotations

from typing import Any, Dict, Literal, Mapping, Optional

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

#: Forma da linha ao redor do payload. ``Literal`` pelo mesmo motivo de
#: ``PayloadShape``: enum no JSON Schema, Select na UI.
RowShape = Literal["flat", "wrapped"]

DESCRICAO_ROW_SHAPE = (
    "Forma da linha: 'flat' (as chaves do evento são as colunas da tabela) ou "
    "'wrapped' (o evento inteiro vai dentro de UMA coluna, e as outras colunas "
    "vêm de 'row_fields'). Use 'wrapped' quando a tabela tem uma coluna de "
    "evento mais colunas de rótulo."
)

DESCRICAO_EVENT_KEY = (
    "Só com row_shape='wrapped': nome da coluna que recebe o evento inteiro "
    "(ex: event)"
)

DESCRICAO_ROW_FIELDS = (
    "Só com row_shape='wrapped': colunas fixas ao lado do evento, chave=coluna "
    "e valor=texto literal (ex: source_type = meu_feed). Sem template e sem "
    "substituição."
)


def normalizar_row_shape(valor: Any) -> RowShape:
    """Aceita o nome atual e cai em ``flat`` se vier lixo.

    ``flat`` é o conservador aqui pelo motivo oposto ao de ``normalizar_shape``:
    é o comportamento que já existia, então lixo na config nunca muda o fio de
    quem já estava entregando.
    """
    if not isinstance(valor, str):
        return "flat"
    return "wrapped" if valor.strip().lower() == "wrapped" else "flat"


def wrap_payload(
    corpo: Any,
    *,
    event_key: str,
    row_fields: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Payload → linha com o payload aninhado sob uma chave, mais chaves irmãs.

    É a primitiva que o HEC já implementava embutida (``{"event": …,
    "sourcetype": …}``), extraída para servir qualquer consumidor de topologia
    coluna-wrapper mais rótulo.

    ``event_key`` vem primeiro e ``row_fields`` depois, na ordem de declaração,
    porque há teste de contrato de fio que compara bytes serializados. Uma chave
    de ``row_fields`` igual a ``event_key`` é **ignorada**, nunca sobrescreve o
    evento: o guard que impede essa config existe no schema, e aqui é a segunda
    linha de defesa para o caso de uma config antiga chegar pelo banco.

    **Não copia** o ``corpo``, pela mesma razão documentada em
    ``render_payload``: o teste de fio do HEC compara identidade.
    """
    linha: Dict[str, Any] = {event_key: corpo}
    for chave, valor in (row_fields or {}).items():
        if chave != event_key:
            linha[chave] = valor
    return linha


def render_row(
    envelope: Mapping[str, Any],
    *,
    payload: Any = "envelope",
    row_shape: Any = "flat",
    event_key: str = "event",
    row_fields: Optional[Mapping[str, str]] = None,
) -> Any:
    """Envelope canônico → a linha que vai no fio. Ponto de entrada único.

    Existe para que nenhum sink reimplemente o ramo ``wrapped``: quando cada um
    monta o próprio wrapper, eles divergem, e foi exatamente assim que o HEC
    acabou sendo o único que sabia envelopar.
    """
    corpo = render_payload(envelope, payload)
    if normalizar_row_shape(row_shape) == "wrapped":
        return wrap_payload(corpo, event_key=event_key, row_fields=row_fields)
    return corpo


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
