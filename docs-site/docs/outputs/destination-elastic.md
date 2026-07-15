---
sidebar_position: 5
title: "Destino: Elasticsearch / OpenSearch"
description: Configure um destino para enviar seus eventos normalizados a um cluster Elasticsearch ou OpenSearch, com entrega sem duplicação e suporte a exclusão LGPD.
---

# Destino: Elasticsearch / OpenSearch

Um **destino** Elasticsearch / OpenSearch envia os eventos já normalizados pelo CentralOps para o seu cluster de busca. A partir dali, você usa as ferramentas de busca e dashboard do próprio cluster (Kibana / OpenSearch Dashboards) para investigar e visualizar os dados.

A entrega é feita sem duplicação: se o mesmo evento for reenviado (por exemplo, após uma instabilidade de rede), ele não aparece duas vezes no índice.

:::note[Quem configura]
Criar e editar destinos é uma tarefa de **administrador**. As telas de **Destinos**, **Roteamento** e **Fluxo de dados** só aparecem para quem tem perfil de admin. Analistas e operadores conseguem acompanhar a saúde do destino, mas não criá-lo.
:::

## Quando usar

- **Centralizar vários coletores em um só cluster.** Você coleta de Sophos, Wazuh e outras plataformas em regiões diferentes e quer todos os eventos normalizados em um único Elasticsearch, prontos para um dashboard unificado no Kibana.
- **Atender vários clientes (MSSP) no mesmo cluster.** Você presta serviço para múltiplas organizações e quer enviar tudo para um cluster compartilhado, mantendo a separação por organização para consultas e para exclusão de dados sob demanda.
- **Reter eventos para conformidade.** Você precisa guardar um histórico longo de eventos para auditoria e mantém um índice dedicado de arquivamento, com a retenção controlada no próprio cluster.

## Antes de começar

Reúna estas informações sobre o cluster de destino. Se você não as tiver, peça ao administrador do cluster Elasticsearch / OpenSearch.

| O que você precisa | Detalhe |
|--------------------|---------|
| **Endereço do cluster** | A URL HTTPS do cluster (por exemplo, `https://seu-cluster:9200`). |
| **Forma de autenticação** | Uma chave de API (Elasticsearch) **ou** um par usuário e senha (Basic Auth). |
| **Índice de destino** | O nome do índice onde os eventos serão gravados. |
| **Permissões da credencial** | A credencial precisa poder criar/gravar no índice e apagar registros (necessário para atender pedidos de exclusão LGPD). |

### Sobre certificado TLS próprio

Se o cluster usa um certificado emitido por uma autoridade certificadora (CA) privada, o CentralOps precisa reconhecer essa CA para validar a conexão com segurança. **Essa configuração é definida pela equipe de infraestrutura no momento do deploy. Se precisar alterá-la, fale com o administrador da plataforma.**

## Como criar o destino

Vá ao menu **Visão geral → Integrações** para confirmar que seus coletores estão ativos e, em seguida, ao menu **Operação → Destinos** para criar o destino.

### 1. Iniciar um novo destino

Na tela de **Destinos**, inicie a criação de um novo destino e escolha o tipo **Elasticsearch / OpenSearch**.

### 2. Preencher os dados de conexão

| Campo | O que informar |
|-------|----------------|
| **Nome** | Um nome reconhecível para você (ex: "Elasticsearch Produção"). |
| **Endereço do cluster** | A URL HTTPS do cluster. |
| **Índice** | O índice de destino. |
| **Forma de autenticação** | Chave de API, usuário e senha, ou sem autenticação. |
| **Validar certificado** | Deixe **ativado** sempre que possível. Desligar reduz a segurança da conexão e só deve ser usado em ambientes de teste. |

### 3. Informar a credencial

Cole a chave de API (ou a senha) no campo indicado. O CentralOps criptografa essa credencial automaticamente: o valor original nunca é exibido de volta nem aparece em registros.

### 4. Testar a conexão

Use o botão de **testar conexão**. O CentralOps verifica se consegue alcançar o cluster, se a credencial é válida e se o certificado é aceito. Você verá uma confirmação de sucesso ou uma mensagem indicando o que falhou.

### 5. Salvar

Salve o destino. Ele passa a ficar ativo e a receber eventos. Na lista de destinos, um indicador de saúde mostra a situação atual (veja [Acompanhar a saúde](#acompanhar-a-saude-do-destino)).

## Como os eventos são entregues

Você não precisa configurar nada disso — é o comportamento padrão do destino. Vale conhecer para interpretar a tela de saúde:

- **Envio em lotes.** Os eventos são agrupados e enviados em lotes para o cluster, o que mantém a entrega eficiente em alto volume.
- **Sem duplicação.** Cada evento carrega um identificador único. Se um lote for reenviado após uma falha, os eventos que já chegaram são reconhecidos e não são gravados de novo.
- **Tentativas automáticas.** Falhas temporárias (cluster ocupado, indisponível por instantes) geram novas tentativas automáticas, com espera crescente entre elas.
- **Proteção contra destino instável.** Se o cluster começa a falhar de forma persistente, o CentralOps pausa o envio por um curto período para não sobrecarregá-lo, e retoma sozinho quando ele volta a responder.
- **Fila de reenvio.** Eventos que não puderam ser entregues (por exemplo, rejeitados pelo cluster) ficam guardados numa fila de reenvio, onde você pode inspecioná-los e tentar enviá-los de novo.

### Ajustes avançados de entrega

Parâmetros como tamanho do lote, número de tentativas e tempo limite têm valores padrão que atendem à maioria dos casos. Quando o cluster exige um ajuste fino (por exemplo, lotes menores porque ele está sobrecarregado), esses valores podem ser revistos pela equipe de infraestrutura. **Se precisar alterá-los, fale com o administrador da plataforma.**

## Exclusão de dados (LGPD / GDPR)

Quando você precisa remover os dados de uma organização para atender a um pedido de exclusão, o CentralOps remove esses eventos do cluster de destino de forma completa: tanto os que já foram entregues quanto os que ainda estavam na fila de reenvio. A remoção é segura para repetir — apagar algo que já não existe não causa erro.

Esse fluxo é conduzido pelo administrador da plataforma a partir das telas de administração. Como o CentralOps mantém a separação por organização no momento da gravação, a exclusão atinge exatamente os eventos da organização indicada.

## Acompanhar a saúde do destino {#acompanhar-a-saude-do-destino}

### Indicador de saúde

Na tela **Operação → Destinos**, cada destino mostra um indicador de saúde:

| Cor | Significado |
|-----|-------------|
| **Verde** | Eventos sendo entregues normalmente, sem pendências. |
| **Amarelo** | Eventos chegando, mas há itens na fila de reenvio. |
| **Vermelho** | Entrega interrompida — o cluster está indisponível ou recusando os eventos. |
| **Cinza** | Destino desativado. |

### Métricas

Ao abrir o destino, você acompanha indicadores como eventos por segundo, volume de dados, tempo de resposta do cluster e quantidade de eventos na fila de reenvio nas últimas 24 horas. Servem para identificar gargalos e quedas de desempenho.

### Fila de reenvio

Ao abrir o destino, você encontra a fila de reenvio com os eventos que não puderam ser entregues, cada um com o motivo e o horário. Ali você pode:

- **Inspecionar o evento** para entender por que foi rejeitado.
- **Reenviar o evento** para recolocá-lo na fila de entrega após a causa ter sido corrigida.

## Resolução de problemas

Como usuário da interface, você acompanha o destino pela cor do indicador e pelas mensagens de erro mostradas na tela. A maioria das causas envolve o cluster Elasticsearch / OpenSearch em si, que normalmente é administrado por outra equipe.

| O que você vê | O que costuma significar | O que fazer |
|---------------|--------------------------|-------------|
| Falha ao conectar / "não foi possível alcançar o cluster" | O endereço está errado, o cluster está fora do ar, ou há bloqueio de rede entre o CentralOps e o cluster. | Confirme o endereço informado no destino. Se estiver correto, peça ao administrador do cluster para verificar se ele está no ar e acessível. |
| Erro de autenticação / "credencial inválida" | A chave de API ou a senha expirou, foi revogada ou não tem permissão suficiente. | Gere ou peça uma nova credencial com permissão para gravar e apagar no índice, e atualize-a no destino. |
| Erro de certificado / "certificado não confiável" | O cluster usa um certificado de uma autoridade que o CentralOps ainda não reconhece. | Fale com o administrador da plataforma para registrar o certificado do cluster. |
| Indicador vermelho com fila de reenvio crescendo | O cluster está recusando gravações — comum quando ele está sem espaço em disco ou com problemas internos. | Avise o administrador do cluster Elasticsearch / OpenSearch. Quando ele se restabelecer, os eventos na fila de reenvio podem ser reenviados. |

Se o problema persistir mesmo após verificar esses pontos, acione o administrador da plataforma com o nome do destino e o horário em que o erro começou.

## Casos de uso detalhados

### Centralizar vários coletores em um só cluster

1. Em **Visão geral → Integrações**, confirme que os coletores (por exemplo, Sophos e Wazuh) estão ativos e coletando.
2. Crie um único destino Elasticsearch apontando para o cluster central.
3. No Kibana (ou OpenSearch Dashboards), monte um painel unificado. Você consegue separar os eventos por plataforma de origem usando os campos que o CentralOps preenche em cada evento.

### Isolamento por organização (MSSP)

1. Crie um único destino para o cluster compartilhado.
2. O CentralOps já marca cada evento com a organização de origem no momento da gravação, então consultas por cliente ficam diretas no Kibana.
3. Quando chega um pedido de exclusão de um cliente, o administrador da plataforma executa a exclusão LGPD e os eventos daquela organização são removidos do cluster.

### Conformidade e retenção

1. No seu cluster, crie um índice dedicado para arquivamento de longo prazo.
2. Crie um destino apontando para esse índice.
3. Configure a política de retenção desejada (por exemplo, vários anos) no próprio cluster Elasticsearch / OpenSearch.

## Próximos passos

- **Confirmar que os dados estão chegando?** Vá ao menu **Operação → Investigações** e busque pelos eventos.
- **Eventos sem entrega?** Abra o destino em **Operação → Destinos** e revise a fila de reenvio.
- **Adicionar outro destino?** Veja [Destinos: Visão Geral](./overview.md).
- **Entender como os eventos são direcionados?** Veja [Roteamento](./routing.md).
