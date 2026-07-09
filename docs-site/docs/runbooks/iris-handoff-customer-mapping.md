---
sidebar_position: 9
title: "IRIS: caso aberto no cliente errado"
description: "Como confirmar e corrigir quando um alerta abre (ou deixa de abrir) um caso no IRIS para o cliente errado, usando apenas a interface do CentralOps."
---

# IRIS: caso aberto no cliente errado

O CentralOps pode encaminhar alertas para o **DFIR-IRIS**, abrindo um caso de investigação automaticamente. Para isso, cada organização do CentralOps precisa estar associada ao cliente correspondente dentro do IRIS. Quando essa associação está faltando ou aponta para o lugar errado, o caso é criado no cliente errado — ou nenhum caso é aberto. Esta página mostra como confirmar o problema e o que fazer, tudo pela interface web.

:::info Quem faz o quê
A associação entre uma organização do CentralOps e um cliente do IRIS é configurada pela equipe da plataforma. Como operador, seu papel é **identificar** o sintoma, **conferir** se a integração IRIS está ativa e mapeada, e **acionar** o administrador quando a correção for de infraestrutura. Os passos abaixo deixam claro o que você resolve sozinho e o que precisa de escalonamento.
:::

## Quando usar

Use este guia quando, na operação do dia a dia, você perceber um destes cenários:

- **Caso no cliente errado.** Um alerta de uma organização abriu um caso no IRIS, mas o caso aparece sob outro cliente (de outro tenant) ou sob um cliente que nem existe.
- **Nenhum caso foi aberto.** O alerta foi processado normalmente no CentralOps, mas o caso esperado não apareceu no IRIS — sinal de que a organização ainda não está associada a um cliente do IRIS.
- **Onboarding de um novo cliente.** Você acabou de cadastrar uma organização e ativou a integração IRIS, e quer confirmar que os próximos alertas vão abrir casos no cliente certo antes de colocar em produção.

## Como confirmar pela interface

### 1. Identifique a organização do alerta

1. Abra o menu **Operação -> Alertas**.
2. Localize o alerta que abriu (ou deveria ter aberto) o caso e abra os detalhes.
3. Anote a **organização** à qual o alerta pertence. É por essa organização que o caso do IRIS é direcionado.

### 2. Confirme que a integração IRIS está ativa

1. Abra o menu **Visão geral -> Integrações**.
2. Verifique se existe uma integração **IRIS** e se ela está com status ativo/saudável.
3. Se a integração estiver inativa ou com erro de conexão, esse é o motivo de nenhum caso ser criado. Acione o administrador da plataforma para reativá-la.

### 3. Confira o destino IRIS e o mapeamento de cliente (admin)

O encaminhamento para o IRIS é tratado como um **destino**. Quem tem perfil de administrador pode conferir o mapeamento:

1. Abra o menu **Operação -> Destinos**.
2. Localize o destino do tipo **IRIS** e abra seus detalhes.
3. Verifique se a organização identificada no passo 1 está **associada ao cliente IRIS correto**. É essa associação que decide em qual cliente do IRIS o caso é aberto.

O que você pode encontrar:

| Situação | O que significa | O que fazer |
| --- | --- | --- |
| Organização **sem** associação a um cliente IRIS | Por isso nenhum caso foi criado | Acione o administrador para configurar a associação |
| Organização associada ao **cliente errado** | Por isso o caso saiu no cliente errado | Acione o administrador para corrigir a associação |
| Associação **correta**, mas casos continuam errados | A associação está certa; o problema é mais a fundo | Siga para **Escalonamento** |

:::note Configuração feita no deploy
A ligação técnica entre a organização do CentralOps e o cliente do IRIS é definida pela equipe de infraestrutura no momento do deploy. Se precisar alterá-la, fale com o administrador da plataforma. Esta tela serve para você **conferir** o mapeamento, não para reconfigurá-lo manualmente.
:::

## Como reprocessar alertas afetados

Se vários alertas foram para o cliente errado (ou não geraram caso) enquanto o mapeamento estava incorreto, depois que a associação for corrigida você pode reenviar esses eventos:

1. Peça ao administrador IRIS para **fechar/descartar os casos abertos no cliente errado**, evitando duplicidade.
2. No CentralOps, abra o menu **Normalização -> Quarentena** e localize os eventos afetados.
3. **Reprocesse** os eventos a partir da Quarentena. Com a associação já corrigida, eles serão reenviados e abrirão o caso no cliente certo.

Veja [Quarentena](../operations/quarantine.md) para o passo a passo completo de reprocessamento.

:::tip Conferir o reenvio aos destinos
Se a entrega ao IRIS ficou represada durante o problema, o administrador pode acompanhar a fila de reenvio na tela **Operação -> Destinos** e reprocessar o que estiver pendente, sem precisar mexer no alerta original.
:::

## Como prevenir antes de ir para produção

Ao ativar a integração IRIS para uma nova organização, confirme estes pontos pela interface:

- A integração **IRIS** aparece como ativa em **Visão geral -> Integrações**.
- A organização está **associada a um cliente IRIS** no destino IRIS em **Operação -> Destinos** (verificação feita por um administrador).
- Um alerta de teste dessa organização abre o caso **no cliente correto** dentro do IRIS.

Se algum desses pontos não estiver pronto, a configuração de associação é feita pela equipe de infraestrutura. Fale com o administrador da plataforma antes de liberar em produção.

## Escalonamento

Acione o administrador da plataforma (ou o time responsável pelo IRIS) quando:

- A integração IRIS aparece com erro em **Integrações** e você não consegue reativá-la.
- A organização **não tem** cliente IRIS associado, ou está associada ao cliente errado, e você não tem perfil de administrador.
- A associação aparece **correta**, mas os casos continuam saindo no cliente errado — nesse caso o problema pode estar do lado do IRIS (por exemplo, o cliente realmente não existe lá).

Ao escalonar, informe o que ajuda o time a localizar o caso rapidamente:

- Nome da **organização** afetada.
- O **alerta** envolvido (da tela **Operação -> Alertas**).
- Em qual **cliente do IRIS** o caso apareceu (errado) ou que **nenhum caso** foi aberto.

## Links

- [Quarentena](../operations/quarantine.md)
- [Documentação do DFIR-IRIS](https://docs.dfir-iris.org/)
