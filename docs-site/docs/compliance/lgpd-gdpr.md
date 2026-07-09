---
sidebar_position: 2
title: LGPD / GDPR — Direito ao esquecimento
description: Como apagar todos os dados de uma organização e mascarar dados pessoais antes de enviá-los a destinos externos
---

# LGPD / GDPR — Direito ao esquecimento e minimização de dados

O CentralOps oferece duas ferramentas para atender LGPD e GDPR pela própria interface: **apagar todos os dados de uma organização** (direito ao esquecimento) e **mascarar campos pessoais antes de entregá-los a um destino externo** (minimização de dados).

**Quem pode usar**: apenas administradores da plataforma.

## Quando usar

| Cenário | Ferramenta |
|---------|------------|
| Um cliente cancelou o contrato e pediu para apagar tudo o que existe sobre ele. | Direito ao esquecimento (apagar a organização). |
| Você recebeu um pedido formal de exclusão (GDPR art. 17) de um titular de dados. | Direito ao esquecimento (apagar a organização). |
| Você precisa enviar eventos completos para o seu lago de dados, mas o time do SIEM não deve ver e-mails, IPs ou documentos pessoais. | Mascaramento de dados pessoais por rota. |

---

## Parte 1 — Mascarar dados pessoais por rota (minimização)

A LGPD (art. 6) e a GDPR pedem que dados pessoais sejam "adequados, relevantes e limitados ao necessário" para cada finalidade. Nem todo destino precisa receber e-mail, IP ou documento em texto aberto. Com o mascaramento por rota, o **mesmo evento** sai completo para um destino e mascarado para outro.

Isso é configurado por **regra de mascaramento** dentro de cada rota. O evento original guardado no CentralOps nunca é alterado — o mascaramento acontece apenas na cópia entregue ao destino daquela rota.

### Como configurar

1. Vá em **Operação → Roteamento**.
2. Abra a rota que entrega ao destino que deve receber dados mascarados (por exemplo, a rota do SIEM).
3. Na configuração da rota, adicione as regras de mascaramento. Para cada campo pessoal, escolha:

| Tipo de mascaramento | O que faz | Quando usar |
|----------------------|-----------|-------------|
| **Ocultar** | Substitui o valor inteiro por um marcador fixo. O destino não vê nada do original. | E-mails, documentos, qualquer campo que o destino não precisa correlacionar. |
| **Pseudonimizar** | Transforma o valor em um código fixo. O destino consegue correlacionar eventos do mesmo titular sem ver o valor real. | Quando o analista precisa agrupar eventos por "mesma pessoa", mas não pode ver quem é. |
| **Parcial** | Revela só um pedaço do valor (por exemplo, os dois primeiros grupos de um IP: `192.168.x.x`). | IPs e identificadores onde um prefixo já basta para a investigação. |
| **Remover campo** | Apaga o campo por completo da entrega. | Campos sensíveis que o destino não deve nem saber que existiram. |

4. Salve a rota. A partir daí, todo evento que sair por essa rota é mascarado antes da entrega.

### Boas práticas

- **Deixe um destino sem mascaramento** (por exemplo, a rota do lago de dados) e aplique o mascaramento **apenas** na rota do SIEM. Assim você preserva o dado bruto onde precisa e protege o destino que não deve vê-lo.
- O mascaramento nunca altera os metadados internos do evento (os campos que o CentralOps usa para roteamento e auditoria). Você só mascara os campos do conteúdo do evento.
- Se uma regra não puder ser aplicada com segurança a um valor, o CentralOps oculta o campo por inteiro em vez de arriscar vazar o original.

> **Exemplo prático**: na rota do SIEM, configure o e-mail do usuário como **Pseudonimizar** e o IP de origem como **Parcial** (manter 2 grupos). O analista do SIEM continua conseguindo agrupar tentativas de login pela mesma pessoa e ver a sub-rede de origem, sem nunca enxergar o e-mail real nem o IP completo. Na rota do lago de dados, não configure nenhuma regra — lá o evento chega completo.

---

## Parte 2 — Direito ao esquecimento (apagar uma organização)

Quando um cliente pede a exclusão dos seus dados, você apaga **toda a organização** dele no CentralOps. A exclusão remove os dados internos da plataforma e, quando o destino externo permite, dispara a remoção também nos destinos.

> **Atenção**: a exclusão é **irreversível**. Os dados apagados não podem ser recuperados sem um backup externo (que é responsabilidade do time de infraestrutura, fora do CentralOps).

### Antes de começar

- [ ] Tenha o **pedido de exclusão por escrito** do cliente (e-mail ou formulário).
- [ ] Tenha um **motivo documentado** ("contrato cancelado", "pedido de exclusão GDPR", etc.) — ele é registrado na auditoria.
- [ ] Confirme com o time de infraestrutura se um **backup** foi feito, caso ainda precise dos dados.

### Passo a passo

1. **Abra a organização.** Vá em **Visão geral → Organizações** e localize a organização a apagar.
2. **Inicie o pedido de exclusão.** Dentro da organização, acione a opção de apagar/expurgar os dados. Um aviso de confirmação aparece informando que a ação é irreversível e listando o que será removido (eventos, rotas, destinos, usuários e mapeamentos).
3. **Confirme as condições.** Marque que você tem backup (se aplicável) e que notificou o cliente.
4. **Digite a confirmação exata.** Para evitar exclusão acidental, o sistema pede que você digite uma frase de confirmação exatamente como mostrada (incluindo o identificador da organização). Se digitar errado, a operação é cancelada por segurança.
5. **Informe o motivo.** Preencha o motivo da exclusão — ele fica registrado no log de auditoria.
6. **Confirme o expurgo.** Ao confirmar, o CentralOps cria um **trabalho de exclusão** que roda em segundo plano.

### O que acontece durante a exclusão

O trabalho de exclusão passa pelos estados **pendente → em execução → concluído** (ou **parcial**, se algum destino externo não pôde ser limpo). Cada trabalho recebe um **identificador próprio** para você acompanhar.

Durante a execução, o CentralOps remove **todos** os dados da organização dentro da plataforma, incluindo:

- Integrações e collectors (Sophos, Wazuh, etc.).
- Mapeamentos de normalização e seu histórico de versões.
- Rotas de roteamento e suas regras de mascaramento.
- Destinos configurados e os vínculos com identificadores externos.
- Eventos de entrada, eventos normalizados e eventos em quarentena.
- A fila de reenvio (eventos que falharam na entrega).
- Registros de auditoria de roteamento e de envio, e o histórico geral.
- Usuários da organização e a própria organização.
- Dados temporários em memória (cursores, cache, agendamentos).

### Remoção nos destinos externos

Quando o destino permite, o trabalho de exclusão também **dispara a remoção dos dados já entregues** ao destino. O comportamento depende do tipo de destino:

| Destino | Remoção automática? | O que você precisa fazer |
|---------|---------------------|--------------------------|
| **Elastic** | Sim. O CentralOps remove os dados da organização. | Nada — feito automaticamente. |
| **S3 / lago de dados** | Sim. O CentralOps remove os objetos da organização. | Nada — feito automaticamente. |
| **Splunk** | Não. O Splunk não tem remoção em massa por organização. | Apague manualmente no Splunk pelas políticas de retenção. |
| **Microsoft Sentinel** | Não. A retenção é controlada pelo workspace. | Apague manualmente no Sentinel pelas políticas de retenção. |
| **Kafka** | Não. A retenção é por tópico. | Ajuste a retenção (TTL) do tópico junto ao time responsável pelo Kafka. |
| **OTLP / tracing** | Não. A retenção é do backend de tracing (Jaeger, Tempo, etc.). | Ajuste a retenção no backend de tracing. |

Quando todos os destinos puderam ser limpos, o trabalho termina como **concluído**. Se algum destino não suporta remoção automática (Splunk, Sentinel, Kafka, OTLP) ou estava indisponível, o trabalho termina como **parcial** — e você precisa fazer a limpeza manual nesses destinos.

### O que NÃO é apagado — e por quê

- **Log de auditoria da exclusão**: cada exclusão gera um comprovante que **sobrevive** à remoção dos dados. Ele é a prova legal de que a exclusão foi executada (quem pediu, quando, motivo e o que foi removido). Você consegue baixá-lo na tela de detalhes do trabalho de exclusão.
- **Backups externos**: backups feitos pelo time de infraestrutura ficam fora do controle do CentralOps. Se o pedido exige apagar também os backups, trate isso com a equipe de infraestrutura.

---

## Acompanhar e confirmar a exclusão

Acompanhe o andamento pela tela de detalhes do trabalho de exclusão, dentro de **Visão geral → Organizações**. Lá você vê:

- O **status** atual (pendente, em execução, concluído ou parcial).
- O **progresso** das etapas (quais já terminaram).
- O **resultado por destino** (quantos registros foram removidos em cada destino e quais exigem limpeza manual).

> A visualização de progresso em tempo real e a lista consolidada de trabalhos de exclusão estão em evolução. O comportamento exato da tela pode variar conforme a versão da plataforma.

### Baixar o comprovante de exclusão

Na tela de detalhes do trabalho, baixe o **log de auditoria da exclusão**. Ele resume quem solicitou, quando, o motivo, quantos registros foram apagados e o resultado em cada destino. Guarde esse arquivo para fins de compliance e como prova de execução.

### SLA de exclusão

- O trabalho começa assim que houver capacidade de processamento em segundo plano — normalmente em menos de 1 hora.
- Os dados apagados são **irrecuperáveis** sem um backup externo.

---

## Casos de uso detalhados

### Cliente cancela o contrato

1. Documente o pedido (guarde o e-mail do cliente).
2. Vá em **Visão geral → Organizações** e abra a organização.
3. Inicie o pedido de exclusão e siga o passo a passo de confirmação.
4. Aguarde a conclusão (normalmente menos de 1 hora).
5. Confira o resultado por destino na tela do trabalho.
6. Baixe o log de auditoria da exclusão.
7. Responda ao cliente confirmando a exclusão e anexe o comprovante.

### Pedido de exclusão GDPR (art. 17)

1. Confirme se o titular corresponde a uma organização inteira.
2. Se sim, apague a organização pelo passo a passo acima.
3. Se o titular for apenas uma pessoa dentro de uma organização com várias pessoas, fale com o administrador da plataforma antes de prosseguir — a separação de um único titular dentro de uma organização ainda está em evolução.
4. Confira o resultado por destino. Se ficar **parcial**, faça a limpeza manual nos destinos que não suportam remoção automática (Splunk, Sentinel, Kafka, OTLP).
5. Informe o time de compliance sobre eventuais ações manuais pendentes.

### Minimização — SIEM mascarado, lago de dados completo

1. Em **Operação → Roteamento**, crie ou abra a rota do SIEM e configure as regras de mascaramento (por exemplo, pseudonimizar o e-mail e mascarar parcialmente o IP).
2. Mantenha a rota do lago de dados **sem** regras de mascaramento.
3. Resultado: o lago recebe e-mail e IP completos; o SIEM recebe o e-mail pseudonimizado e o IP parcial.

---

## Solução de problemas

### O trabalho de exclusão ficou parado por muito tempo

- Espere alguns minutos: exclusões com muitos eventos podem demorar.
- Se continuar parado, fale com o administrador da plataforma para verificar o processamento em segundo plano.

### A organização não foi totalmente apagada

- Verifique se o trabalho terminou como **parcial**.
- Veja no resultado por destino se algum destino ficou com falhas (por exemplo, Elastic ou S3 indisponível no momento).
- Reexecute o trabalho de exclusão depois que o destino voltar, ou faça a limpeza manual no destino.

### Os dados ainda aparecem no Splunk ou no Sentinel

Isso é esperado: Splunk e Sentinel não oferecem remoção automática por organização.

1. No log de auditoria da exclusão, confirme que o destino aparece marcado como sem remoção automática.
2. Apague manualmente pela política de retenção do próprio destino (Splunk ou Sentinel).
3. Informe o time de compliance de que a limpeza do destino externo foi feita manualmente.

---

## Checklist de compliance

- [ ] Pedido de exclusão documentado (e-mail, formulário, etc.).
- [ ] Backup feito antes, se necessário (com o time de infraestrutura).
- [ ] Trabalho de exclusão concluído (status concluído ou parcial).
- [ ] Log de auditoria da exclusão baixado e guardado.
- [ ] Resultado por destino conferido.
- [ ] Destinos sem remoção automática (Splunk, Sentinel, Kafka, OTLP) limpos manualmente, se aplicável.
- [ ] Cliente notificado da conclusão.
- [ ] Time de compliance informado.

## Próximos passos

- **Configurar retenção de dados?** Veja [Retenção](./retention.md).
- **Gerenciar rotas e mascaramento?** Veja [Roteamento](../outputs/routing.md).
- **Gerenciar destinos?** Veja [Destinos](../outputs/destinations.md).
- **Gerenciar organizações?** Veja [Organizações](../administration/organizations.md).
- **Consultar auditoria?** Veja [Histórico](../operations/history-audit.md).
