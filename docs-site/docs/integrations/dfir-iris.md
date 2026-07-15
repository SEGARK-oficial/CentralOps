---
sidebar_position: 7
title: DFIR-IRIS
description: "Integração opcional para enviar casos de incident response ao DFIR-IRIS, com cada organização associada ao seu cliente no IRIS"
---

# Integração DFIR-IRIS

O DFIR-IRIS é uma plataforma de resposta a incidentes (incident response). O CentralOps pode enviar casos ao IRIS como um **destino opcional** — ele nunca é obrigatório para a coleta e a entrega de eventos funcionarem. Se o IRIS estiver fora do ar ou nem chegar a ser configurado, o restante da plataforma continua operando normalmente.

A ideia central: o CentralOps mantém cada **organização** ligada ao **cliente correspondente no IRIS** de forma automática. Assim, quando um caso é aberto, ele cai no cliente certo dentro do IRIS, sem ninguém precisar digitar identificadores à mão.

## Quando usar

- **SOC que abre casos formais de IR.** Sua equipe trabalha alertas no CentralOps, mas registra e conduz a investigação formal (timeline, evidências, tarefas) dentro do DFIR-IRIS. A integração garante que cada caso nasça já vinculado ao cliente certo.
- **MSSP / operação multi-cliente.** Você atende várias organizações e quer que os casos de cada uma fiquem isolados no IRIS, sem risco de misturar dados entre clientes.
- **Onboarding de um novo cliente.** Ao cadastrar uma nova organização no CentralOps, você quer que ela já apareça como cliente no IRIS, pronta para receber casos, sem cadastro manual em dois lugares.

## Como funciona

| O que você vê | O que o CentralOps faz por baixo |
|---------------|----------------------------------|
| Você cadastra uma **organização** no CentralOps | A organização recebe um identificador próprio, que acompanha todos os eventos dela |
| Você liga a integração com o IRIS | O CentralOps **associa cada organização a um cliente no IRIS** — cria o cliente se ele ainda não existir |
| Um caso é aberto para uma organização | O caso vai para o cliente correto no IRIS, usando essa associação |

Pontos importantes:

- A associação organização → cliente no IRIS é **automática e auditável**. Você não precisa decorar ou colar nenhum identificador.
- A criação do cliente no IRIS é **best-effort**: se o IRIS estiver indisponível no momento, a entrega dos eventos **não** é bloqueada. Você pode tentar a associação de novo mais tarde.
- O IRIS é apenas mais um **destino**. Você pode usar o CentralOps sem ele e ligá-lo quando quiser.

## Pré-requisito: o IRIS precisa estar conectado

A conexão técnica com o IRIS (endereço do servidor e credencial de acesso) é definida pela equipe de infraestrutura no momento do deploy. Essa configuração não fica numa tela do CentralOps.

:::info[Quem configura o quê]
A URL do servidor IRIS e a credencial de acesso **são definidas pela equipe de infraestrutura no momento do deploy. Se precisar alterá-las, fale com o administrador da plataforma.**

Já a associação das organizações aos clientes do IRIS (descrita abaixo) é feita por você, pela interface do CentralOps.
:::

Por boa prática de segurança, peça à infraestrutura que o CentralOps acesse o IRIS por uma **conta de serviço dedicada** com o mínimo de privilégio — apenas o necessário para ler e criar clientes, nunca uma conta de administrador. Isso reduz o impacto caso a credencial seja comprometida.

## Associar organizações ao IRIS

Depois que a conexão com o IRIS está ativa, você associa as organizações aos seus clientes no IRIS.

### Clientes Sophos (Partner)

Para organizações que chegam via integração Sophos Central Partner, a associação é **tentada automaticamente** quando você aprova o cliente (tenant). Se a tentativa falhar (por exemplo, IRIS momentaneamente fora do ar), a aprovação do cliente **não** é bloqueada.

Para tentar de novo as organizações que já estão aprovadas, use a opção de **sincronizar/atualizar a integração** na tela da integração Sophos:

1. Vá em **Visão geral → Integrações**.
2. Abra a integração Sophos Central correspondente.
3. Acione a opção de **atualizar/sincronizar** agora.

### Organizações cadastradas manualmente

Para organizações que você cadastra à mão (Sophos fora do modelo Partner, Wazuh, ou qualquer outro vendor), faça a associação a partir da própria organização:

1. Vá em **Visão geral → Organizações** *(disponível apenas para administradores)*.
2. Abra a organização desejada.
3. Acione a opção de **associar ao IRIS** (sincronizar o cliente no IRIS).

Se a organização já tiver uma associação e você precisar refazê-la — por exemplo, trocou de servidor IRIS ou quer corrigir um vínculo errado — use a opção de **reassociar/forçar** na mesma tela.

:::tip[Ligou o IRIS com a base já cheia?]
Se você habilitou o IRIS quando já tinha muitas organizações cadastradas, repita o passo de associação para cada uma. Se forem muitas, peça ao administrador da plataforma para executar a associação em lote.
:::

## Conferir se a associação funcionou

Para confirmar que uma organização ficou corretamente vinculada ao IRIS:

1. Vá em **Visão geral → Organizações**.
2. Abra a organização.
3. Verifique, na tela da organização, se ela aparece **associada ao IRIS** (com o cliente correspondente).

Se a associação não aparecer ou estiver com erro, veja a seção **Resolução de problemas** abaixo.

## Resolução de problemas

| Sintoma | O que verificar |
|---------|-----------------|
| **"IRIS não configurado"** ao tentar associar | A conexão com o IRIS ainda não foi configurada no deploy. Fale com o administrador da plataforma. |
| **A associação falha** mesmo com o IRIS conectado | Pode ser um problema de rede entre o CentralOps e o IRIS, credencial inválida, ou permissões insuficientes da conta de serviço. Isso é resolvido pela equipe de infraestrutura — abra um chamado com o administrador da plataforma. |
| **Sincronizar não associa minha organização** | Confirme se a organização é Partner ou manual. Se for **manual**, use o passo de associação na tela da organização (não a sincronização da integração Sophos). Se for **Partner**, confirme que o cliente está **aprovado** — clientes pendentes ou excluídos não são associados. |
| **Preciso remover uma associação com o IRIS** | A remoção de uma associação pela interface ainda **não está disponível** (em construção). Por enquanto, fale com o administrador da plataforma para removê-la. |

## Integração de borda com Wazuh

Se o seu ambiente Wazuh já cria casos no IRIS a partir dos alertas, essa rotina continua funcionando ao lado do CentralOps. Cada evento que o CentralOps roteia ao Wazuh carrega o identificador da organização, e o Wazuh usa esse identificador para abrir o caso no cliente certo do IRIS.

A configuração dessa rotina de borda fica no lado do Wazuh e é mantida pela equipe de infraestrutura. **Se a sua rotina Wazuh → IRIS já existe e precisa ser ajustada para o novo modelo, fale com o administrador da plataforma.**

## Próximos passos

- **Organizações e multi-cliente** → [Organizações](../administration/organizations.md)
- **Outros destinos de entrega** → [Roteamento e destinos](../outputs/routing.md)
- **Gerenciar clientes do Partner** → [Sophos Central](./sophos.md)
- **Visão geral das integrações** → [Integrações](../integrations/overview.md)
