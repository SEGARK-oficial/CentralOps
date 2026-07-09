---
sidebar_position: 10
title: Operar destinos (saúde, fila de reenvio, métricas)
description: Acompanhe a saúde dos destinos, investigue falhas, reenvie eventos e gerencie credenciais — tudo pela interface.
---

# Operar destinos

Esta tela mostra a saúde de cada destino para onde o CentralOps envia eventos — conectividade, proteção contra destino instável e fila de reenvio — e permite gerenciar as credenciais de cada um.

Os destinos são gerenciados pelo administrador da plataforma no menu **Operação → Destinos**. Analistas e engenheiros podem acompanhar a saúde e a fila de reenvio; ações que mexem em credenciais (rotacionar, revogar) são restritas ao administrador.

## Quando usar

- **Um destino parou de receber eventos.** O Sentinel, o Splunk ou o data lake do cliente apareceu com aviso vermelho no painel e você precisa descobrir se é credencial vencida, rede ou indisponibilidade do lado do destino.
- **A fila de reenvio está crescendo.** Eventos estão acumulando sem chegar ao destino. Você quer ver o motivo predominante das falhas, corrigir a causa e reenviar o que ficou retido.
- **Rotação de credencial agendada ou após incidente.** O token de um destino vai vencer (ou vazou) e você precisa trocá-lo sem derrubar o fluxo nem perder eventos em trânsito.

## Saúde geral dos destinos

Vá ao menu **Operação → Destinos**. A coluna de status mostra um indicador colorido para cada destino:

| Indicador | Significado | O que fazer |
|-----------|-------------|-------------|
| 🟢 **Saudável** | Conexão OK, nenhuma falha nas últimas 24h | Acompanhar. |
| 🟡 **Degradado** | Conexão OK, mas há eventos na fila de reenvio nas últimas 24h | Abrir a fila de reenvio e investigar os erros. |
| 🔴 **Indisponível** | Proteção contra destino instável **ativada** — o destino não está recebendo | Testar a conexão, conferir a credencial, acionar o responsável pelo destino. |
| ⚪ **Desconhecido** | A plataforma não conseguiu verificar o estado no momento | Aguardar e atualizar a tela; se persistir, falar com o administrador. |
| ⚫ **Desabilitado** | Destino desligado | Reativar pelo administrador, se necessário. |

A tela lista todos os destinos visíveis à sua organização com status, eventos por segundo (na última hora) e volume de dados — útil como visão de painel.

## Testar a conexão de um destino

Antes de colocar um destino em produção, valide a conexão:

1. Vá ao menu **Operação → Destinos** e selecione o destino.
2. Use o botão de **Testar** conexão.
3. A plataforma abre uma conexão temporária só para o teste e verifica:
   - se a rede responde (nome do host resolve, porta aberta);
   - se a credencial é válida (ela é usada apenas neste teste, nunca registrada em log);
   - se o envio de uma amostra funciona no formato esperado pelo destino;
   - a latência da resposta.

Se passar, você vê uma confirmação com o tempo de resposta. Se falhar, a tela mostra a mensagem de erro indicando o motivo (credencial, rede ou formato).

## Saúde detalhada de um destino

Ao abrir um destino, a plataforma mostra os indicadores de saúde dele:

- **Estado da proteção contra destino instável**: normal (recebendo), ativada (parou de enviar e está acumulando na fila de reenvio) ou em recuperação (testando se o destino voltou).
- **Eventos na fila de reenvio nas últimas 24h**: quantos eventos foram recusados recentemente.
- **Eventos por segundo**: ritmo de eventos entregues com sucesso na última hora.
- **Quando ocorreu a última falha**.

### Como funciona a proteção contra destino instável

Para evitar martelar um destino que está caindo, a plataforma protege o fluxo automaticamente:

1. **Falhas seguidas**: a proteção é ativada — os novos eventos vão direto para a fila de reenvio, sem novas tentativas imediatas.
2. **Após um curto período de espera**: a plataforma envia um evento de teste para ver se o destino voltou.
3. **Teste OK**: a proteção é desativada e o tráfego normal volta.
4. **Teste falha**: a proteção continua ativa e a plataforma espera mais um pouco antes de tentar de novo.

Enquanto a proteção está ativada, os eventos retidos aparecem na fila de reenvio com o motivo correspondente.

## Fila de reenvio (eventos recusados)

Quando um destino recusa um evento (formato inválido, credencial vencida, tempo esgotado etc.), o evento não é descartado: ele vai para a **fila de reenvio**, guardado por completo para análise.

### Ver a fila de reenvio

No destino, abra a fila de reenvio. A plataforma mostra:

- o total de eventos retidos;
- um resumo por motivo de falha;
- a lista de eventos, cada um com o motivo, o detalhe do erro, o conteúdo do evento e a data.

Os dados sensíveis do evento (campos com nome contendo "token", "senha" ou "segredo") aparecem mascarados.

### Motivos de falha mais comuns

| Motivo | Causa | O que fazer |
|--------|-------|-------------|
| **Falha de autenticação** | Credencial inválida, vencida ou revogada | Rotacionar a credencial |
| **Proteção ativada** | Destino indisponível por falhas seguidas | Aguardar a recuperação automática e testar a conexão; acionar o responsável pelo destino |
| **Tempo esgotado** | O destino não respondeu a tempo | Verificar a rede; se persistir, falar com o administrador |
| **Evento grande demais** | O evento ultrapassa o tamanho aceito pelo destino | Falar com o administrador (ajuste de configuração do destino) |
| **Erro de formato** | O destino não conseguiu formatar o evento | Revisar o editor de mapeamento do destino |
| **Erro de rede** | Rede inacessível (DNS, conexão recusada) | Testar a conexão e verificar firewall |

## Reenviar eventos da fila

Depois de corrigir a causa (por exemplo, renovar a credencial), reenvie os eventos retidos para o destino. Na tela do destino, na fila de reenvio, você pode:

- **Reenviar tudo**: a plataforma reprocessa todos os eventos retidos daquele destino em segundo plano, sem afetar o fluxo ao vivo.
- **Reenviar eventos selecionados**: escolha eventos específicos da lista para reenviar apenas eles.

Depois de reenviar, atualize a fila:

- se o envio funcionou, o evento sai da fila de reenvio;
- se falhar pelo mesmo motivo, o evento volta para a fila com o detalhe atualizado do erro — sinal de que a causa raiz ainda não foi resolvida.

## Métricas do destino

Na tela do destino, a plataforma mostra a evolução recente em formato de série temporal:

- **Entregues**: eventos entregues com sucesso por minuto.
- **Recusados**: eventos recusados por minuto.
- **Latência média**: tempo médio de entrega por minuto.
- **Eventos na fila local**: eventos aguardando envio (acúmulo momentâneo).
- **Estado de vazão**: normal ou sob sobrecarga. Em sobrecarga, a plataforma pode reduzir o volume automaticamente para proteger o destino.

Acompanhe essas métricas junto com a fila de reenvio para distinguir um pico passageiro de um problema persistente.

### Ver os últimos eventos enviados

Na tela do destino você pode visualizar os eventos mais recentes que passaram por ele (com dados sensíveis mascarados). É a forma mais rápida de confirmar, na hora, que o destino está recebendo o conteúdo no formato esperado — sem precisar consultar o destino final.

## Credenciais do destino

Cada destino tem uma credencial (token ou senha) usada para se autenticar no sistema de destino. Na tela do destino, a plataforma indica:

- se há uma credencial cadastrada;
- quando ela foi rotacionada pela última vez;
- quando ela vence (se houver data de validade).

Um destino **sem credencial** fica desabilitado automaticamente, pois não tem como se autenticar.

### Rotacionar uma credencial

Use a rotação para trocar o token ou a senha **sem apagar** o destino:

1. Vá ao menu **Operação → Destinos** e abra o destino.
2. Acesse a área de credencial do destino.
3. Escolha **rotacionar** a credencial.
4. Cole o novo token ou senha.
5. Defina uma data de validade (opcional).
6. Confirme.

A troca não causa interrupção: a credencial anterior continua válida por um curto período, então os eventos que já estavam em trânsito não falham. Se o destino estava desabilitado por uma revogação anterior, a rotação o reativa.

### Revogar uma credencial

Use a revogação em um incidente de segurança, quando precisa cortar o acesso imediatamente:

1. Abra o destino e acesse a área de credencial.
2. Escolha **revogar** a credencial.

A plataforma apaga a credencial e desabilita o destino. Nenhum evento é enviado até você cadastrar uma nova credencial pela rotação.

### Histórico de credenciais

Cada destino mantém um histórico de quem acessou, rotacionou ou revogou a credencial, com o usuário responsável, a ação e a data. Use-o para auditar mudanças de credencial, por exemplo após um incidente. As ações registradas incluem:

- **teste**: a credencial foi usada em um teste de conexão;
- **rotação**: a credencial foi renovada;
- **revogação**: a credencial foi revogada.

### Histórico de alterações do destino

Separado do histórico de credenciais, há também um registro das alterações de configuração do destino (criação, edição, remoção), com o usuário e a data de cada mudança. Esse registro nunca expõe o valor da credencial — apenas indica se há uma credencial cadastrada.

## Rastrear um evento até o destino

Você pode verificar se um evento específico chegou a um destino consultando o rastreamento de entregas na tela do destino. Ele mostra os eventos entregues com sucesso e quando isso ocorreu, dentro da janela de retenção recente.

Se um evento não aparecer no rastreamento de entregas, procure-o na fila de reenvio do destino — é provável que ele tenha sido recusado.

> O rastreamento de entregas é uma ferramenta operacional de curto prazo, não um arquivo de conformidade. Para retenção de longo prazo, use um destino de armazenamento (por exemplo, um data lake) configurado para esse fim.

## Comportamento de entrega

O comportamento de entrega de cada destino — agrupamento de eventos em lotes, tentativas automáticas, proteção contra destino instável, tempo limite de resposta, prioridade e retenção — é definido na configuração do destino pelo administrador da plataforma. Se precisar ajustar qualquer um desses parâmetros, fale com o administrador.

## Solução de problemas

### Destino aparece como indisponível

1. Use o botão de **Testar** conexão para validar rede e credencial.
2. Se falhar:
   - **Credencial vencida?** Rotacione a credencial.
   - **Host não responde?** Verifique rede, DNS, firewall e certificado.
   - **Destino fora do ar?** Acione o responsável pelo sistema de destino.
3. Se passar, a proteção contra destino instável se recupera sozinha em pouco tempo.

### Fila de reenvio crescendo

1. Abra a fila de reenvio e veja o motivo de falha predominante.
2. Se for **falha de autenticação**: a credencial venceu — rotacione-a.
3. Se for **tempo esgotado**: verifique a rede; se for necessário ajustar o tempo limite, fale com o administrador.
4. Se for **proteção ativada**: aguarde a recuperação automática e teste a conexão.
5. Depois de resolver a causa, reenvie os eventos retidos.

### Credencial revogada, destino desabilitado

Rotacione a credencial (cole o novo token e confirme). O destino volta a operar imediatamente.

### Eventos por segundo em zero — nada sendo entregue

1. Confirme que o destino está **habilitado** e **saudável**.
2. Confirme que existe uma regra de roteamento ativa enviando eventos para esse destino. O roteamento é gerenciado pelo administrador no menu **Operação → Roteamento**.
3. Verifique se os eventos não estão sendo reduzidos por sobrecarga (estado de vazão nas métricas do destino).

### Erro ao usar a credencial ("não foi possível descriptografar")

A chave de criptografia da plataforma é definida pela equipe de infraestrutura no momento do deploy. Se um teste falhar com erro de descriptografia da credencial, isso indica um problema de configuração da plataforma. Fale com o administrador da plataforma — não há ação no painel para resolver isso.

## Próximos passos

- **Configurar um novo destino**: veja [Visão geral de destinos](./destinations.md).
- **Eventos na fila de reenvio, mas o destino parece OK?** Investigue a normalização em [Visão geral de normalização](../normalization/overview.md).
- **Auditar todas as ações?** Use os históricos de credencial e de alterações na própria tela do destino.
