---
sidebar_position: 1
title: O que é o enriquecimento
description: Acrescente contexto ao evento dentro do próprio pipeline, geo, reputação, criticidade do ativo, antes de rotear e detectar
---

# O que é o enriquecimento

**Enriquecimento** acrescenta contexto a um evento **dentro do pipeline**, antes de ele ser roteado e antes de ser avaliado pelas regras de detecção. Em vez de o analista abrir uma ferramenta à parte para descobrir "esse IP é de que site?" ou "esse indicador já apareceu em algum feed de ameaça?", o evento já chega ao destino com essa resposta anexada.

O estágio aparece no menu como **Enriquece**, entre **Normaliza** e **Roteia**, essa é também a ordem real em que o evento passa pelo pipeline: primeiro é normalizado para o formato comum (OCSF), depois ganha contexto, só então é roteado para o destino e avaliado pelas regras de detecção.

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
| Alimenta a detecção em pipeline? | **Sim** | Não, chega tarde demais para isso |
| Exemplos | Tabelas do cliente, OpenCTI | VirusTotal |
| Por quê | A fonte já está toda carregada em memória, não precisa esperar rede | A fonte exige uma chamada de rede por indicador, e isso não pode acontecer um a um sem travar o pipeline inteiro |

Na tela de catálogo, cada card diz **por evento** ou **por lote** no começo da descrição, é essa a diferença que isso indica.

Na prática: se você quer que uma regra de detecção reaja a "esse IP é reconhecidamente malicioso", o enricher precisa ser dos que rodam **por evento**. Um enricher **por lote** ainda enriquece o evento antes dele chegar ao destino, só não chega a tempo de influenciar a detecção que rodou dentro do próprio pipeline.

## Onde o resultado é escrito

O enriquecimento **nunca** altera os campos originais do evento, nem o dado bruto do fornecedor, nem os campos já normalizados no formato OCSF. Tudo o que ele acrescenta fica numa seção própria do evento, reservada só para isso.

Isso é proposital: os campos normalizados de um evento já passaram por uma validação de conformidade, e sobrescrevê-los depois faria essa validação mentir sobre o que realmente foi entregue. Separar o que veio do fornecedor do que foi acrescentado também deixa claro, no destino, o que é fato relatado e o que é contexto anexado pelo CentralOps.

## Egresso: quando um dado seu sai para um terceiro

Alguns enrichers **consultam um serviço externo**, o que significa que um indicador do seu ambiente (um IP, um hash de arquivo) sai da sua infraestrutura para checar reputação. Outros são **100% internos**: ou consultam algo que já é seu (sua instância do OpenCTI, sua própria tabela), ou não fazem chamada de rede nenhuma.

Cada card do catálogo mostra um selo de egresso:

| Selo | Significado |
|---|---|
| **sem egresso** | Não faz nenhuma chamada externa, tabelas do cliente |
| **interno** | Consulta um serviço, mas é **seu** (ex.: sua própria instância OpenCTI), nada sai para fora da sua infraestrutura |
| **envia a terceiro** | Envia o indicador a um serviço de **terceiro** (ex.: VirusTotal) |

:::caution[Trate o selo de egresso como decisão de privacidade, não como detalhe técnico]
Antes de habilitar uma regra que usa um enricher com egresso a terceiro, confirme que sua organização pode enviar aquele tipo de indicador para fora, em alguns ambientes (dados de cliente sob NDA, setores regulados) isso pode não ser aceitável. O aviso aparece destacado no topo do catálogo sempre que há pelo menos uma fonte assim.

O gate `when` da regra é avaliado **antes** da chamada externa, então o que ele barra não sai da sua infraestrutura nem consome cota do provedor.
:::

## Uma organização por vez, salvo no Enterprise

Um token com escopo de organização enxerga **apenas a própria**. Se você opera um SOC que acompanha vários clientes e vê só um deles, isso não é falha: é como a edição Community funciona. Ver a subárvore de organizações filhas é recurso Enterprise.

Duas saídas: usar um token de **escopo global**, ou a edição Enterprise, que habilita a visão de subárvore e o compartilhamento de uma fonte entre a matriz e as filhas escolhidas. A própria tela avisa quando você está vendo uma organização só.

## As telas do console

O console tem cinco abas em **Enriquece → Enriquecimento**:

- **Catálogo**, todas as fontes de enriquecimento disponíveis, com modo (por evento/por lote), tipos de chave que aceita e selo de egresso. Clicar num card já abre o cadastro de fonte com o enricher escolhido.
- **Fontes**, as fontes configuradas: a instância de um enricher nesta organização, com o endereço e a credencial. Enrichers que exigem credencial (OpenCTI, VirusTotal) precisam de uma fonte antes de aparecerem numa regra. O botão **Testar** consulta o serviço de verdade e devolve o erro real do provedor, sem publicar nada.
- **Tabelas**, as tabelas que sua organização já criou, com quantidade de entradas e tamanho. O botão **Nova tabela** cria uma tabela, e cada card abre o histórico de versões, publicar dados novos ou reverter para uma versão anterior é tudo formulário, sem precisar de API.

  ![Tabelas de enriquecimento da organização](/img/console/console-enriquecimento-tabelas.png)

- **Políticas**, as políticas já criadas, se estão ativas e quantas regras têm. O botão **Nova política** cria uma política, e cada card abre o editor de regras, escrever regras, testar com dry-run e habilitar são passos do mesmo modal. O editor abre já com as regras da versão vigente, porque publicar substitui a lista inteira, não faz merge.

  ![Políticas de enriquecimento](/img/console/console-enriquecimento-politicas.png)

- **Execução**, para responder "isso está funcionando?". Mostra qual política está de fato valendo (o worker aplica uma por organização, a mais antiga habilitada). Lista cada tentativa de consulta com a mensagem do provedor quando falha, e o aproveitamento de cada regra na janela. As duas leituras juntas distinguem problema de credencial (a consulta falha) de problema de dado (a consulta funciona e o acerto cai), que pedem ações opostas.

O guia [Como enriquecer um evento](./how-to-enrich.md) mostra o passo a passo completo pelo console, com a API REST como alternativa para automação e scripts.

## Próximos passos

- **Quer configurar sua primeira tabela e política?** Veja [Como enriquecer um evento](./how-to-enrich.md).
- **Quer saber o que cada fonte pronta oferece?** Veja [Catálogo de fontes](./catalog.md).
