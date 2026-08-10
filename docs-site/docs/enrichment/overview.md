---
sidebar_position: 1
title: O que é o enriquecimento
description: Acrescente contexto ao evento dentro do próprio pipeline — geo, reputação, criticidade do ativo — antes de rotear e detectar
---

# O que é o enriquecimento

**Enriquecimento** acrescenta contexto a um evento **dentro do pipeline**, antes de ele ser roteado e antes de ser avaliado pelas regras de detecção. Em vez de o analista abrir uma ferramenta à parte para descobrir "esse IP é de que site?" ou "esse indicador já apareceu em algum feed de ameaça?", o evento já chega ao destino com essa resposta anexada.

O estágio aparece no menu como **Enriquece**, entre **Normaliza** e **Roteia** — essa é também a ordem real em que o evento passa pelo pipeline: primeiro é normalizado para o formato comum (OCSF), depois ganha contexto, só então é roteado para o destino e avaliado pelas regras de detecção.

![Catálogo de fontes de enriquecimento](/img/console/console-enriquecimento-catalogo.png)

## As três peças

| Peça | O que é | Exemplo |
|---|---|---|
| **Enricher** | Uma fonte de contexto já pronta no CentralOps | `table_cidr`, `opencti`, `virustotal` |
| **Tabela** | Um conjunto de dados **seu** que um enricher consulta | Lista de sub-redes da sua empresa, com o site e a criticidade de cada uma |
| **Política** | O conjunto de regras que diz **quando** enriquecer, **com qual** enricher, e **onde** escrever o resultado | "Se o IP de origem bater no plano de endereçamento, marque o site e a criticidade" |

Uma política pode ter várias regras. Cada regra escolhe um enricher, diz de onde tirar a chave de busca (por exemplo, o IP de origem do evento) e para onde escrever cada campo do resultado.

## Dois momentos diferentes

Nem todo enriquecimento acontece na mesma hora. Isso importa porque muda **o que o enriquecimento consegue fazer** com o evento.

| | **Por evento** | **Por lote** |
|---|---|---|
| Quando roda | Evento por evento, no instante em que ele passa pelo pipeline | No fim de um grupo de eventos, de uma vez |
| Alimenta a detecção em pipeline? | **Sim** | Não — chega tarde demais para isso |
| Exemplos | Tabelas do cliente, OpenCTI | VirusTotal |
| Por quê | A fonte já está toda carregada em memória — não precisa esperar rede | A fonte exige uma chamada de rede por indicador, e isso não pode acontecer um a um sem travar o pipeline inteiro |

Na tela de catálogo, cada card mostra o selo **per event** ou **per batch** — é essa a diferença que ele indica.

Na prática: se você quer que uma regra de detecção reaja a "esse IP é reconhecidamente malicioso", o enricher precisa ser dos que rodam **por evento**. Um enricher **por lote** ainda enriquece o evento antes dele chegar ao destino, só não chega a tempo de influenciar a detecção que rodou dentro do próprio pipeline.

## Onde o resultado é escrito

O enriquecimento **nunca** altera os campos originais do evento — nem o dado bruto do fornecedor, nem os campos já normalizados no formato OCSF. Tudo o que ele acrescenta fica numa seção própria do evento, reservada só para isso.

Isso é proposital: os campos normalizados de um evento já passaram por uma validação de conformidade, e sobrescrevê-los depois faria essa validação mentir sobre o que realmente foi entregue. Separar o que veio do fornecedor do que foi acrescentado também deixa claro, no destino, o que é fato relatado e o que é contexto anexado pelo CentralOps.

## Egresso: quando um dado seu sai para um terceiro

Alguns enrichers **consultam um serviço externo** — o que significa que um indicador do seu ambiente (um IP, um hash de arquivo) sai da sua infraestrutura para checar reputação. Outros são **100% internos**: ou consultam algo que já é seu (sua instância do OpenCTI, sua própria tabela), ou não fazem chamada de rede nenhuma.

Cada card do catálogo mostra um selo de egresso:

| Selo | Significado |
|---|---|
| 🔒 **no egress** | Não faz nenhuma chamada externa — tabelas do cliente |
| 🔒 **internal** | Consulta um serviço, mas é **seu** (ex.: sua própria instância OpenCTI) — nada sai para fora da sua infraestrutura |
| 🌐 **sends to third party** | Envia o indicador a um serviço de **terceiro** (ex.: VirusTotal) |

:::caution[Trate o selo de egresso como decisão de privacidade, não como detalhe técnico]
Antes de habilitar uma regra que usa um enricher com egresso a terceiro, confirme que sua organização pode enviar aquele tipo de indicador para fora — em alguns ambientes (dados de cliente sob NDA, setores regulados) isso pode não ser aceitável. O aviso aparece destacado no topo do catálogo sempre que há pelo menos uma fonte assim.
:::

## As telas do console

O console tem três abas em **Enriquece → Enrichment**:

- **Catalog** — todas as fontes de enriquecimento disponíveis, com modo (por evento/por lote), tipos de chave que aceita e selo de egresso.
- **Tables** — as tabelas que sua organização já criou, com quantidade de entradas e tamanho. O botão **New table** cria uma tabela, e cada card abre o histórico de versões — publicar dados novos ou reverter para uma versão anterior é tudo formulário, sem precisar de API.

  ![Tabelas de enriquecimento da organização](/img/console/console-enriquecimento-tabelas.png)

- **Policies** — as políticas já criadas, se estão ativas e quantas regras têm. O botão **New policy** cria uma política, e cada card abre o editor de regras — escrever regras, testar com dry-run e habilitar são passos do mesmo modal.

  ![Políticas de enriquecimento](/img/console/console-enriquecimento-politicas.png)

O guia [Como enriquecer um evento](./how-to-enrich.md) mostra o passo a passo completo pelo console, com a API REST como alternativa para automação e scripts.

## Próximos passos

- **Quer configurar sua primeira tabela e política?** Veja [Como enriquecer um evento](./how-to-enrich.md).
- **Quer saber o que cada fonte pronta oferece?** Veja [Catálogo de fontes](./catalog.md).
