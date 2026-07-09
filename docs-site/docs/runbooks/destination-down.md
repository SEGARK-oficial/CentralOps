---
sidebar_position: 6
title: "Destino externo fora do ar"
description: "Como identificar e recuperar um destino (Splunk, Elasticsearch, S3, Sentinel, Kafka, OTLP) que parou de receber eventos, usando a interface do CentralOps"
---

# Destino externo fora do ar

Quando um destino externo para de receber eventos, o CentralOps deixa de entregar os dados para ele, ativa a **proteção contra destino instável** e guarda os eventos não entregues na **fila de reenvio**. Esta página explica como reconhecer essa situação na interface, descobrir a causa e recolocar o destino no ar.

> A configuração de destinos só aparece para administradores da plataforma, no menu **Operação → Destinos**.

## Quando usar

Use esta página quando notar qualquer um destes sinais na interface:

- Um destino aparece com **badge vermelho** ("indisponível") ou amarelo ("degradado") na tela **Operação → Destinos**.
- A **fila de reenvio** de um destino (Splunk, Elasticsearch, S3, Sentinel, Kafka ou OTLP) está crescendo e não drena.
- O botão de **testar conexão** do destino falha.

Cenários reais de SOC:

| Cenário | O que você observa |
| --- | --- |
| O Splunk para de aceitar eventos de madrugada porque o token expirou | Badge vermelho no destino Splunk, fila de reenvio subindo, rejeições do tipo "autenticação" |
| O cluster Elasticsearch fica sobrecarregado em um pico de alertas | Destino degradado, latência alta, rejeições por excesso de requisições |
| A regra de coleta (DCR) do Sentinel é renomeada pela equipe de nuvem | Destino indisponível, rejeições do tipo "regra de coleta inválida" |

## Passo 1 — Confirme a falha na interface

1. Abra **Operação → Destinos**.
2. Clique no destino com problema.
3. Veja o **status de saúde** do destino. Os estados possíveis são:

| Estado | Significado |
| --- | --- |
| Saudável | O destino responde e os eventos fluem normalmente. |
| Degradado | Há eventos acumulando na fila de reenvio nas últimas 24h, mas o destino ainda responde em parte. |
| Indisponível | A proteção contra destino instável foi ativada — o CentralOps parou de tentar entregar e está guardando os eventos na fila de reenvio. |

4. Use o botão de **testar conexão** do destino. Se o teste falhar, anote o tipo de erro mostrado — ele indica a causa e direciona a correção nas seções abaixo.

## Passo 2 — Identifique o tipo de erro

Na tela do destino, abra a **fila de reenvio** e veja qual tipo de rejeição predomina. Os tipos mais comuns e a ação correspondente:

| Tipo de rejeição | Causa provável | Ação |
| --- | --- | --- |
| Autenticação | Credencial (token, chave, usuário/senha) expirada ou inválida | Gire a credencial do destino (Passo 3). |
| Excesso de requisições | O destino está recebendo mais do que aguenta | Reduza a velocidade de envio do destino (Passo 3). |
| Tempo de resposta esgotado | O destino ou a rede está lento/indisponível | Verifique a disponibilidade do destino com a equipe responsável. |
| Carga muito grande | Os lotes de eventos estão acima do limite aceito pelo destino | Reduza a velocidade de envio ou ajuste o limite do destino. |
| Formato/esquema rejeitado | O formato dos eventos não bate com o esperado pelo destino | Ajuste o mapeamento (menu **Normalização → Mappings**) e confira a saúde do pipeline. |

## Passo 3 — Corrija conforme o tipo de destino

Todas as correções abaixo são feitas na tela do destino, em **Operação → Destinos**. Selecione o destino e edite a configuração indicada.

### Credencial expirada (qualquer destino)

Esse é o caso mais comum quando a rejeição é de **autenticação**.

1. Abra o destino em **Operação → Destinos**.
2. Vá até a área de **credenciais** do destino.
3. Use a opção de **girar/atualizar credencial** e cole o novo valor (token do Splunk, chave de acesso da AWS, usuário e senha do Kafka, etc.).
4. Salve e use o botão de **testar conexão** para confirmar.

### Excesso de requisições ou tempo esgotado

Quando o destino está sobrecarregado (rejeições por **excesso de requisições**, **tempo esgotado** ou **carga muito grande**):

1. Abra o destino em **Operação → Destinos** e edite-o.
2. Reduza a **velocidade de envio** (número de envios simultâneos) do destino para um valor menor.
3. Salve e teste novamente. Aumente a velocidade aos poucos quando o destino estabilizar.

### Splunk

- Erros comuns: autenticação, tempo de resposta esgotado, falha em confirmar o recebimento.
- Se a rejeição for de **autenticação**, gere um novo token no Splunk e atualize a credencial do destino (passos acima).
- Confira no campo de endereço do destino se a URL do Splunk está correta (sem barra no final).
- Se as rejeições forem por sobrecarga, reduza a velocidade de envio.

### Elasticsearch / OpenSearch

- Erros comuns: autenticação, excesso de requisições, formato/esquema rejeitado.
- Se a credencial expirou, gire-a no destino.
- Em caso de **excesso de requisições**, reduza a velocidade de envio.
- Em caso de **formato/esquema rejeitado**, verifique o mapeamento em **Normalização → Mappings** e a saúde do pipeline em **Normalização → Saúde do Pipeline**. Se o cluster estiver com problema de índice, acione a equipe responsável pelo Elasticsearch.

### S3

- Erros comuns: autenticação (credenciais de acesso), tempo esgotado, carga muito grande.
- Se a credencial expirou, gire a chave de acesso no destino.
- Se houver bloqueio de permissão (o destino não consegue gravar no bucket), as permissões do bucket são definidas no provedor de nuvem. Acione a equipe responsável pela conta de nuvem.

### Azure Sentinel

- Erros comuns: autenticação, regra de coleta (DCR) inválida, tempo esgotado.
- O CentralOps renova o acesso automaticamente. Se a falha de autenticação persistir, confira no destino o endereço e a regra de coleta configurados.
- Se a rejeição for de **regra de coleta inválida**, a regra pode ter sido renomeada ou removida no portal da Azure:
  1. Peça à equipe de nuvem para confirmar, no portal da Azure, que a regra de coleta existe e está habilitada, e para fornecer o identificador atualizado.
  2. Em **Operação → Destinos**, edite o destino Sentinel, atualize o identificador da regra de coleta e salve.

### Kafka

- Erros comuns: autenticação (SASL), broker indisponível, esquema incompatível.
- Se a rejeição for de **autenticação**, edite o destino Kafka e confira se o mecanismo de autenticação bate com o do broker; gire usuário e senha se necessário.
- Se o tópico não existir no Kafka ou o esquema estiver incompatível, a criação do tópico e o registro de esquema são feitos do lado do Kafka. Acione a equipe responsável pelo Kafka.

### OTLP (OpenTelemetry)

- Erros comuns: tempo esgotado, formato inválido, certificado/TLS.
- Em caso de **tempo esgotado**, reduza a velocidade de envio do destino.
- Problemas de certificado (TLS) com certificados próprios ou cadeias customizadas dependem de configuração feita no servidor. **Esta configuração é definida pela equipe de infraestrutura no momento do deploy. Se precisar alterá-la, fale com o administrador da plataforma.**

## Passo 4 — Teste e acompanhe a recuperação

1. Use o botão de **testar conexão** do destino. Se passar, o destino está respondendo novamente.
2. Acompanhe o destino por alguns minutos em **Operação → Destinos**:
   - O status deve voltar para **saudável**.
   - A **fila de reenvio** deve começar a drenar (a contagem cai conforme os eventos guardados são reenviados).

> **Como a proteção contra destino instável funciona:** após algumas falhas seguidas, o CentralOps para de tentar entregar e guarda os eventos na fila de reenvio, em vez de insistir e sobrecarregar o destino. Passado um curto período de espera, ele faz uma tentativa de prova. Se passar, volta a entregar normalmente; se falhar, espera mais um pouco antes de tentar de novo. Por isso, depois de corrigir a causa, a recuperação pode levar alguns minutos até a próxima tentativa de prova.

## Prevenção

Para reduzir a chance de um destino cair sem aviso:

- **Teste a conexão periodicamente.** Use o botão de **testar conexão** em **Operação → Destinos** de tempos em tempos, especialmente em destinos cujas credenciais expiram (tokens de curta duração, chaves rotacionadas pela equipe de nuvem).
- **Monitore a fila de reenvio.** Acompanhe em **Operação → Destinos** se a fila de algum destino está crescendo. Uma fila que sobe de forma constante é o primeiro sinal de um destino com problema.
- **Acompanhe a saúde do pipeline.** A tela **Normalização → Saúde do Pipeline** mostra a saúde geral da entrega e ajuda a identificar destinos degradados antes de uma queda completa.
- **Mantenha as credenciais em dia.** Combine com as equipes donas dos destinos (Splunk, nuvem, Kafka) um aviso antes de rotacionar credenciais, para girar a credencial no CentralOps na mesma janela.

> O envio automático de notificações (Slack, e-mail) quando a fila de reenvio passa de um limite está no roadmap e ainda não está disponível na interface. Por enquanto, use o acompanhamento manual descrito acima.

## Quando escalar

Escale se, depois de corrigir a causa e testar, o destino continuar indisponível ou a fila de reenvio não drenar:

1. **Reúna evidências pela interface:**
   - O status do destino e o resultado do **teste de conexão**.
   - O tipo de rejeição predominante na **fila de reenvio** (uma captura de tela ajuda).
   - Identificação do destino (nome e tipo).
2. **Acione a equipe dona do destino:**
   - Splunk: equipe responsável pelo Splunk.
   - Elasticsearch / OpenSearch: equipe de banco de dados ou infraestrutura.
   - S3 / Sentinel / Kafka: equipe de nuvem ou de operações.
3. **Acione o administrador da plataforma** se a causa parecer estar na própria entrega do CentralOps (por exemplo, todos os destinos do mesmo tipo caíram ao mesmo tempo) ou se a correção exigir mudança de configuração feita no deploy.
