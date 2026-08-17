---
sidebar_position: 12
title: "Destino: nano SIEM"
description: Entregue eventos já normalizados ao nano SIEM, escolhendo entre os dois caminhos de entrega e sabendo o que cada um cobra em troca.
---

# Destino: nano SIEM

O nano é um SIEM leve que guarda os eventos em ClickHouse e aceita OCSF pronto. Como o CentralOps já coleta do fabricante, normaliza para OCSF 1.8, enriquece e reduz, o encaixe é direto: você entrega o evento normalizado e o nano cuida da busca, da detecção e da investigação.

Esta tela só aparece para administradores da plataforma.

![Tela de Destinos do console](/img/console/console-destinos.png)

## Dois caminhos, e eles não são equivalentes

O nano abre duas portas de entrada e a escolha entre elas tem consequência. Leia esta seção antes de criar o destino, porque trocar de caminho depois significa refazer configuração dos dois lados.

| | **Entrega direta** | **Entrega por coletor (HEC)** |
|---|---|---|
| Como funciona | O CentralOps grava direto no banco do nano | O CentralOps entrega para o serviço do nano, que grava |
| O que você configura no nano | Um usuário de banco com permissão de escrita | Um token de ingestão e um arquivo de tradução |
| Quem grava | O CentralOps, com credencial de banco | O próprio nano, com as credenciais dele |
| Latência | Menor: um salto a menos | Um pouco maior |
| Se o nano mudar por dentro | A entrega pode parar | Segue funcionando |
| Precisa de permissão de banco | Sim | Não |

### Quando escolher a entrega direta

É o caminho mais curto e o de menor latência. O evento sai do CentralOps e entra no banco do nano sem intermediário.

O que você ganha:

- **Nada é reprocessado.** O nano recebe exatamente o OCSF que o CentralOps produziu.
- **Menos partes móveis.** Não há arquivo de tradução para manter.
- **Um salto a menos**, o que aparece na latência quando o volume é alto.

O que você paga:

- **Depende de permissões de banco.** O usuário de ingestão precisa poder escrever na tabela de destino **e** ter acesso a algumas tabelas internas que o nano usa para montar as estatísticas dele. Se faltar qualquer uma, a gravação é recusada. Veja [Quando a gravação é recusada por permissão](#quando-a-gravacao-e-recusada-por-permissao).
- **Acompanha as mudanças internas do nano.** Uma atualização que mexa na estrutura interna pode exigir ajuste de permissão de novo.
- **A conexão pode estar perfeita e a gravação falhar mesmo assim.** É o ponto que mais confunde, e está explicado em [O que o teste de conexão não diz](#o-que-o-teste-de-conexao-nao-diz).

### Quando escolher a entrega por coletor

Aqui o CentralOps conversa com o serviço de ingestão do nano, e quem grava no banco é o nano.

O que você ganha:

- **Nenhuma permissão de banco.** Você usa um token de ingestão. O problema de permissão simplesmente não existe nesse caminho.
- **Resistente a mudanças internas.** Se o nano reorganizar as tabelas dele, a sua entrega continua de pé.
- **Erros mais legíveis.** O serviço responde com mensagens de aplicação, não com erro de banco.

O que você paga:

- **Um arquivo de tradução do lado do nano.** Ele não reprocessa nada, só converte o formato de entrega para o formato que o nano guarda. Está descrito no [passo a passo](#caminho-b-entrega-por-coletor-hec).
- **Um salto a mais**, e portanto um pouco mais de latência.

**Na dúvida, escolha a entrega por coletor.** Ela custa um arquivo a mais e devolve independência: sua ingestão deixa de depender de como o nano está organizado por dentro.

## O que ter em mãos

Para os dois caminhos:

- **O endereço do nano na sua rede.**
- **Um rótulo para esta origem**, em letras minúsculas, sem espaço (por exemplo `centralops_sophos`). É por ele que você separa e filtra os dados dentro do nano.

Só para a entrega direta:

- **Usuário e senha de ingestão do nano.** O instalador cria esse usuário e gera a senha.

Só para a entrega por coletor:

- **O token de ingestão do nano.**

> A senha e o token ficam guardados de forma criptografada. Depois de salvar, eles não aparecem mais em tela.

## Caminho A: entrega direta

1. No menu lateral, abra **Roteia → Destinos**.
2. Crie um destino novo e escolha o tipo **nano SIEM**.
3. Preencha:

| Campo | O que informar |
|---|---|
| **Nome** | Algo que ajude a reconhecer o destino depois, como "nano SIEM Produção". |
| **URL** | O endereço do nano com a porta da interface web do banco: `http://seu-nano:8123`, ou `https://seu-nano:8443` se for com TLS. Confira qual delas o seu deploy publicou. |
| **Rótulo da origem** | Minúsculo, sem espaço. Use um por feed. |
| **Rótulo derivado de** | Deixe vazio por enquanto. Volte aqui depois de ler [Um destino para vários clientes](#um-destino-para-varios-clientes). |
| **Usuário** | O usuário de ingestão do nano. |
| **Senha** | A senha desse usuário. |
| **Banco** e **Tabela** | Os valores já vêm preenchidos com o padrão do nano. Só mude se a sua instalação usar outros. |
| **Verificar TLS** | Deixe ligado. Desligue apenas se o nano usar certificado próprio e a conexão falhar por causa dele. |

4. Salve. O destino nasce ativo e passa a receber o que for roteado para ele.

:::caution[Uma porta que parece certa e não é]
Se você tentar apontar para a porta 9000, o salvamento é recusado com uma explicação. Aquela porta usa um protocolo diferente e não serve para este caminho. Use 8123, ou 8443 com TLS.
:::

## Caminho B: entrega por coletor (HEC) {#caminho-b-entrega-por-coletor-hec}

Este caminho usa o tipo de destino **Splunk HEC**, porque o nano fala o mesmo protocolo de coleta.

1. No menu lateral, abra **Roteia → Destinos**.
2. Crie um destino novo e escolha o tipo **Splunk HEC**.
3. Preencha:

| Campo | O que informar |
|---|---|
| **Nome** | Algo como "nano SIEM (coletor)". |
| **URL** | O endereço do nano com a porta do coletor: `https://seu-nano:8088`. |
| **Token** | O token de ingestão do nano. |
| **Sourcetype** | O rótulo desta origem, minúsculo e sem espaço. É o mesmo papel do rótulo da origem no outro caminho. |
| **Payload** | **OCSF.** Esse campo é o que mais importa aqui: escolher a outra opção faz o nano receber um formato que ele não sabe guardar, e a tradução recusa o evento. |
| **Canal** | Deixe vazio. O CentralOps preenche sozinho com um identificador estável. |
| **Verificar TLS** | Deixe ligado, com a mesma ressalva de certificado próprio. |

4. Salve.

5. **Instale o arquivo de tradução no nano.** Sem ele, os eventos chegam mas o nano não sabe onde guardá-los. O arquivo pega o que o CentralOps entrega e converte para a forma que o nano armazena. Ele não reinterpreta o conteúdo: o evento já vem normalizado, e reprocessar seria refazer um trabalho que já foi feito, com risco de o resultado divergir.

O arquivo faz quatro coisas:

- Reaproveita o evento como está, sem tocar no conteúdo.
- Copia o rótulo da origem para o campo que o nano usa para separar as fontes.
- Converte o horário do evento para o formato que o nano espera.
- Reaproveita o identificador do evento, para que um mesmo evento entregue duas vezes não vire duas linhas.

Ele também recusa o evento, em vez de guardar torto, se perceber que o **Payload** ficou na opção errada. É melhor ver um erro contado do que descobrir semanas depois que uma parte dos dados entrou sem classificação.

[Baixe o arquivo de tradução](/files/nano-centralops.vrl) e coloque-o na pasta de tradutores do nano. Se a sua instalação tiver o caminho diferente do padrão, quem administra o nano sabe onde é.

## Um destino para vários clientes {#um-destino-para-varios-clientes}

Se você atende mais de um cliente, o rótulo fixo vira um problema de escala: um destino, uma rota e uma credencial por cliente. Em dez ainda dá; em cinquenta, não.

O campo **Rótulo derivado de** resolve isso. Em vez de escrever o rótulo, você escolhe de onde ele sai, e cada evento entregue leva o rótulo do cliente que o originou. Um destino só atende todos.

A opção mais usada é **organização + fabricante**, que produz rótulos como `acme_sophos` e `beta_wazuh`. Ela separa por cliente, que é o que importa para o escopo, e mantém o fabricante visível, que é o que costuma aparecer nos filtros.

O **Rótulo da origem** continua útil junto: ele vira o valor de reserva, usado quando o evento não trouxer a informação de origem.

:::tip[Isso muda como escrever as regras no nano]
Com rótulo derivado, cada cliente passa a ter o seu. Uma regra escrita para um rótulo específico deixa de enxergar todos os outros, em silêncio.

Escreva as regras filtrando pelo **conteúdo** do evento (tipo, severidade, categoria) e use o rótulo apenas como coluna de resultado, para saber de quem é. Assim uma regra atende todos os clientes, que é justamente o motivo de normalizar antes de entregar.
:::

## Testar a conexão

Na página do destino, use o botão **Testar**. Ele verifica se o nano responde, se a credencial é aceita e se o formato combina com o que está do outro lado.

### O que o teste não diz {#o-que-o-teste-de-conexao-nao-diz}

O teste faz perguntas ao nano; ele não grava nada. Isso significa que **passar no teste não é prova de que os eventos estão sendo guardados**. Existe uma situação real em que o teste passa e a gravação falha: quando o endereço, a credencial e o formato estão todos corretos, mas o nano recusa a escrita por permissão.

Por isso o resultado do teste diz o que ele não cobre e aponta onde está a resposta de verdade: a **fila de reenvio** do destino. Se ela está vazia e o contador de eventos sobe, está entregando. Se ela cresce, não está — independentemente do que o teste disse.

Confira sempre os dois.

## Confirmar que os dados chegaram

Abra **Roteia → Destinos**, selecione o destino e veja o contador de eventos e a fila de reenvio.

Do lado do nano, procure pelo rótulo que você configurou. Os eventos ficam na tabela de logs consolidada.

:::warning[Não procure na tabela de entrada]
A tabela onde o CentralOps escreve é apenas uma porta de entrada: por desenho ela **não guarda nada**, só repassa. Consultá-la vai devolver zero mesmo quando tudo estiver funcionando, e é um jeito fácil de concluir errado que a integração está parada.

Procure na tabela de logs, filtrando pelo seu rótulo.
:::

## Quando a gravação é recusada por permissão {#quando-a-gravacao-e-recusada-por-permissao}

Isso vale apenas para a **entrega direta**.

Ao receber um evento, o nano não só guarda a linha: ele também atualiza algumas tabelas internas de estatística. Essa atualização acontece com as permissões de quem está gravando, ou seja, o seu usuário de ingestão. Se faltar acesso a qualquer uma dessas tabelas internas, o nano recusa o evento inteiro.

Como isso aparece na prática:

- o teste de conexão passa;
- a fila de reenvio cresce sem parar;
- os eventos que chegam a entrar são só uma fração do que foi enviado.

O que fazer: peça a quem administra o nano para conceder ao usuário de ingestão o acesso de leitura às tabelas de agregação. Em algumas instalações o usuário é criado por arquivo de configuração, e nesse caso o ajuste precisa ser feito nesse arquivo, no servidor, e não por comando.

Nenhum evento se perde enquanto isso: eles ficam na fila de reenvio e são entregues quando a permissão for corrigida.

**Se este problema aparecer, considere migrar para a entrega por coletor.** Ela não depende de permissão de banco, então esse modo de falha deixa de existir.

## Como os eventos são entregues

Você não precisa configurar nada disso, mas ajuda saber o que esperar:

- **Vão em lotes**, o que é mais eficiente do que um a um.
- **Se falhar, tenta de novo sozinho**, esperando um pouco mais a cada tentativa.
- **Se o nano ficar instável**, o envio pausa por um curto período e volta automaticamente, para não piorar a situação do outro lado.
- **Um evento pode chegar duplicado** numa queda no momento errado. Cada evento carrega um identificador próprio, o que permite identificar repetições.

## Acompanhar a saúde

| Cor | O que significa |
|---|---|
| Verde | Entregando normalmente, sem nada parado. |
| Amarelo | Entregando, mas há itens na fila de reenvio. |
| Vermelho | Envio pausado, ou o nano indisponível. |
| Cinza | Destino desativado. |

A fila de reenvio mostra cada evento recusado com o motivo, o horário e o conteúdo exato. É por onde se começa qualquer investigação de entrega.

## Problemas comuns

| O que você vê | O que verificar |
|---|---|
| **Consultei a tabela e está vazia** | Confira se você não está consultando a tabela de entrada, que por desenho não guarda nada. Procure na tabela de logs, filtrando pelo seu rótulo. |
| **O teste passa mas a fila de reenvio cresce** | Quase sempre é permissão no nano. Veja [Quando a gravação é recusada por permissão](#quando-a-gravacao-e-recusada-por-permissao). |
| **Não conecta** | O endereço está completo, com `http://` ou `https://` e a porta? A porta está publicada para fora do host? É comum o deploy publicar só uma delas. |
| **Usuário ou senha recusados** | O usuário existe no nano e a senha confere? Ela pode ter sido regerada em uma reinstalação. |
| **Reenviei o mesmo lote e nada apareceu** | Não é erro. O nano ignora um lote idêntico reenviado, o que torna a repetição segura. Para testar de novo, use um evento diferente. |
| **Os dados aparecem sem classificação de origem** | O rótulo foi escrito igual dos dois lados? Ele diferencia maiúscula de minúscula. |
| **Depois de ligar o rótulo derivado, as regras pararam** | Esperado: o rótulo mudou. Reescreva as regras filtrando por conteúdo em vez de por rótulo. |
| **Falha de certificado** | Acontece quando o nano usa certificado próprio. Fale com quem cuida do deploy, ou desligue a verificação de TLS se for um ambiente interno controlado. |

## Próximos passos

- **Escolher o que vai para cada destino:** [Roteamento](./routing.md).
- **Ver outros destinos disponíveis:** [visão geral](./overview.md).
- **Entender a fila de reenvio:** [operação de destinos](./destination-operations.md).
