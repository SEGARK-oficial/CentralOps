---
sidebar_position: 5
title: "Casos de Uso — Guias Passo a Passo"
description: "Como criar, testar e ativar um mapeamento na tela de Mappings, com três exemplos reais de vendor"
---

# Casos de Uso — Guias Passo a Passo

Esta página mostra, passo a passo na interface, como criar e ativar um mapeamento que transforma os eventos de um vendor no formato padronizado do CentralOps. Use exemplos reais (Microsoft Defender, Sophos e um vendor novo do zero) para guiar você do primeiro evento recebido até o evento normalizado pronto para ser entregue aos destinos.

Um mapeamento diz ao CentralOps **quais campos do evento original correspondem a quais campos do formato padrão** (severidade, dispositivo, usuário, indicadores). Você cria e edita tudo isso por formulários na tela de Mappings — não é preciso escrever nenhum código.

## Quando usar

- **Onboarding de um novo vendor:** você acabou de conectar uma integração (por exemplo, um EDR novo) e os eventos chegam, mas com campos do vendor. Você precisa criar um mapeamento para que severidade, host e indicadores apareçam corretamente nas Investigações.
- **Campos faltando nos alertas:** os analistas reclamam que o nome do host ou o usuário não aparece num alerta de phishing. Você abre o mapeamento daquele vendor, identifica o campo de origem que está sem ligação e corrige.
- **Vendor mudou o formato:** o fornecedor passou a enviar a severidade como texto ("high") em vez de número, e os eventos começaram a cair na Quarentena. Você ajusta a conversão de severidade no mapeamento e reprocessa.

---

## Antes de começar: o ciclo de um mapeamento

Todo mapeamento segue o mesmo ciclo na tela **Normalização -> Mappings**:

1. **Criar / abrir** o mapeamento do vendor.
2. **Ligar os campos** de origem (do vendor) aos campos de destino (formato padrão).
3. **Testar** com eventos de amostra usando a simulação (Dry Run), sem afetar produção.
4. **Revisar a cobertura** — quais campos do vendor ficaram de fora.
5. **Ativar** — a partir daí os eventos passam a ser normalizados e seguem para entrega.

Os exemplos abaixo aplicam esse mesmo ciclo a vendors de complexidade crescente.

---

## O que todo mapeamento precisa preencher

Independente do vendor, o resultado padronizado sempre tem um conjunto de campos básicos. Ao montar o mapeamento, garanta que estes estejam ligados a alguma origem:

| Campo padronizado | O que representa | Obrigatório? |
|-------------------|------------------|--------------|
| Tipo de evento | A categoria do evento (ex.: detecção de segurança) | Sim |
| Horário | Quando o evento ocorreu | Sim |
| Identificador | ID único do evento no vendor | Sim |
| Severidade | A gravidade, padronizada numa escala comum | Sim |
| Título / mensagem | O resumo legível do que aconteceu | Recomendado |
| Dispositivo | Host, máquina ou equipamento envolvido | Recomendado |
| Usuário / ator | Quem estava envolvido | Recomendado |
| Indicadores | IPs, e-mails, hashes, arquivos, processos (os IOCs) | Recomendado |
| Identificação do produto | Nome e fabricante da ferramenta de origem | Recomendado |

Os campos obrigatórios são os que, se não forem preenchidos, fazem o evento ir para a **Quarentena**. Sempre os mapeie primeiro.

---

## Caso 1: Microsoft Defender for Endpoint (detecção)

**Vendor:** Microsoft Defender XDR
**Tipo de evento:** alerta de detecção em endpoint
**Complexidade:** alta (vários indicadores, evento com estrutura aninhada)

O Defender é um vendor de primeira classe no CentralOps, integrado tanto como fonte quanto como destino. Este caso reforça que a plataforma é vendor-neutra: a mesma normalização vale para Sophos, Microsoft, Wazuh ou qualquer outra ferramenta.

### O desafio deste evento

Um alerta de detecção do Defender traz uma lista de evidências, e cada evidência pode ser um arquivo, um processo ou um usuário. Você quer que **cada uma dessas evidências vire um indicador separado** no evento padronizado, para que os analistas consigam pivotar por hash, por nome de processo ou por usuário.

### Passos na interface

1. Vá em **Normalização -> Mappings** e clique para criar um novo mapeamento.
2. Selecione o vendor (Microsoft Defender) e o tipo de evento de detecção.
3. Preencha os campos básicos:
   - **Severidade:** o Defender envia texto ("informational", "low", "medium", "high", "critical"). Configure a conversão para a escala padrão de severidade do CentralOps.
   - **Status:** o Defender classifica como verdadeiro positivo / falso positivo. Ligue esse campo ao status padronizado.
   - **Horário, identificador e título** ligados aos campos correspondentes do alerta.
   - **Dispositivo e usuário** ligados ao nome/ID da máquina e ao responsável atribuído.
4. Configure os **indicadores** a partir da lista de evidências. Como a lista mistura tipos diferentes, separe um indicador para cada tipo:
   - caminho e hash de arquivo,
   - nome de processo e linha de comando,
   - usuário.

   Quando a evidência traz uma lista, marque a opção que gera **um indicador por item** da lista, para não perder evidências.
5. Habilite a **remoção de duplicados** dos indicadores, para que valores repetidos (ex.: o mesmo destinatário citado duas vezes) apareçam só uma vez.
6. Clique em **Dry Run** com eventos de amostra e confira o resultado.

### Resultado esperado

Depois de ativado, um alerta de "credential dumping" do Defender chega às Investigações já com:

- severidade padronizada,
- status (verdadeiro/falso positivo) traduzido,
- host, sistema operacional e analista responsável preenchidos,
- e uma lista de indicadores com o arquivo `lsass.dmp`, seu hash, o processo `ntdsutil.exe`, a linha de comando e o usuário — cada um pesquisável nas Investigações.

> **Sobre a escala de severidade:** cada vendor usa uma escala própria. O mapeamento traduz a escala do vendor para a escala padronizada do CentralOps. Confira sempre na simulação se "medium", "high" etc. caíram no nível esperado.

---

## Caso 2: Detecção do Sophos XDR (e-mail)

**Vendor:** Sophos Central
**Tipo de evento:** detecção de segurança de e-mail (phishing)
**Complexidade:** alta (campo aninhado com detalhes de e-mail, vários indicadores)

### O desafio deste evento

O Sophos entrega a detecção como um evento "plano", mas os detalhes mais úteis (IP do cliente, remetente, destinatários e anexos) vêm empacotados dentro de um campo aninhado. Você quer **abrir esse campo aninhado** e extrair de lá os indicadores.

### Passos na interface

1. Em **Normalização -> Mappings**, crie um novo mapeamento para o Sophos (tipo detecção).
2. Habilite o **pré-processamento do campo aninhado**: indique qual campo do evento contém os detalhes de e-mail empacotados, para que o CentralOps o expanda e disponibilize os campos internos (IP, remetente, destinatários, anexos) para o mapeamento.
3. Preencha os campos básicos (tipo, horário, identificador, título, dispositivo).
   - **Severidade:** o Sophos envia um número. Ligue diretamente ao campo de severidade — confira na simulação se a faixa numérica do Sophos faz sentido na escala padrão (veja a nota abaixo).
   - **Título:** use a regra de detecção como título principal e configure um **valor alternativo** (o tipo de ataque) para o caso de o título vir vazio.
4. Mapeie as **táticas MITRE ATT&CK** vindas do evento para o formato padronizado, para enriquecer a investigação.
5. Configure os **indicadores** a partir dos campos já expandidos do e-mail:
   - IP de origem,
   - e-mail do remetente,
   - e-mails dos destinatários (gere **um indicador por destinatário**),
   - hash e nome de cada anexo (gere **um indicador por anexo**).
6. Habilite a **remoção de duplicados** dos indicadores.
7. Rode o **Dry Run** e confira a cobertura.

### Resultado esperado

Uma detecção de phishing do Sophos chega normalizada com o IP do remetente, o e-mail do atacante, os dois destinatários como indicadores separados, e os dois anexos (com nome e hash) — tudo pronto para correlação e bloqueio.

> **Sobre a severidade do Sophos:** o Sophos usa uma escala numérica própria. Por padrão, o valor é armazenado como veio, sem reescala. Se a sua equipe precisar alinhar essa escala à severidade padronizada do CentralOps, configure uma conversão de valores no mapeamento. Decida isso de forma consciente — não deixe acontecer por acidente.

---

## Caso 3: Alerta do Sophos (firewall / EDR)

**Vendor:** Sophos Central
**Tipo de evento:** alerta de firewall ou EDR
**Complexidade:** média (sem campo aninhado, indicadores simples)

### O desafio deste evento

Os alertas do Sophos são mais simples que as detecções: todos os dados já vêm planos. Aqui o foco é mostrar as **diferenças** em relação ao Caso 2.

### Diferenças em relação ao mapeamento de detecção

| Aspecto | Detecção (Caso 2) | Alerta (Caso 3) |
|---------|-------------------|-----------------|
| Campo aninhado | Precisa expandir o campo de e-mail | Não há — tudo já é plano |
| Horário | Um único campo de horário | Tente o horário de criação e, se faltar, use o de registro como alternativa |
| Severidade | Número direto | Texto ("high", "medium"…) — configure a conversão para a escala padrão |
| Indicadores | IPs, e-mails, hashes (listas) | Apenas host e identificador do tenant — sem listas |
| Táticas MITRE | Presentes | Ausentes — não mapeie |

### Passos na interface

1. Crie o mapeamento de alerta do Sophos em **Normalização -> Mappings**.
2. Configure o **horário** com um valor alternativo: use o horário de criação como principal e o de registro como reserva.
3. Configure a **severidade por texto**, traduzindo "critical/high/medium/low/info" para a escala padronizada.
4. Ligue **dispositivo** (nome, host, tipo) e **usuário** (nome, ID) aos campos do agente e da pessoa.
5. Configure indicadores simples: identificador e nome do dispositivo e identificador do tenant.
6. Rode o **Dry Run** e ative.

---

## Caso 4: Vendor novo do zero (template)

**Vendor:** Acme EDR (fictício)
**Tipo de evento:** detecção / ameaça
**Complexidade:** ponto de partida para um vendor ainda não suportado

### Cenário

Você está integrando o "Acme EDR", uma plataforma de detecção de endpoint nova. Você já recebeu alguns eventos de exemplo e quer ter um mapeamento funcional em cerca de 30 minutos.

### Passo 1: Veja quais campos o padrão espera

Antes de mapear, lembre-se dos campos básicos que todo evento padronizado precisa (veja a tabela no início desta página): tipo, horário, identificador, severidade, dispositivo, usuário, indicadores e identificação do produto. Esses são o seu "esqueleto".

### Passo 2: Compare os campos do vendor com o padrão

Olhe um evento de amostra do Acme e anote a correspondência. Por exemplo, para um evento como este:

- `event_id` -> identificador do evento
- `timestamp` -> horário
- `alert_name` -> título
- `alert_severity` -> severidade
- `machine.hostname` -> nome do dispositivo
- `user.username` -> usuário
- `process.name`, `file.path`, `file.hash` -> indicadores (IOCs)

Essa tabela de correspondência é o que você vai reproduzir nos formulários do mapeamento.

### Passo 3: Monte o mapeamento inicial

1. Em **Normalização -> Mappings**, crie um novo mapeamento e selecione o vendor Acme.
2. Preencha primeiro os **campos obrigatórios** (tipo, horário, identificador, severidade) — assim você falha cedo se algo estiver faltando.
3. Em seguida, ligue **dispositivo** e **usuário**.
4. Por fim, configure os **indicadores** (nome do processo, caminho do arquivo, hash).
5. Preencha a identificação do produto (nome "Acme EDR", fabricante "Acme").

### Passo 4: Teste com a simulação (Dry Run)

1. Salve o mapeamento.
2. Clique em **Dry Run** com seus eventos de amostra.
3. Revise o resultado:
   - **Todos os eventos passaram?** Procure campos marcados como "100% valor padrão" — sinal de que a ligação não pegou nada e você precisa ajustar a origem.
   - **Algum evento foi para a Quarentena?** Veja o motivo — normalmente é um campo obrigatório ausente ou uma conversão (ex.: de horário) que falhou.
   - **Algum campo do vendor ficou de fora?** O **Drift Explorer** (em Normalização -> Drift Explorer) lista os campos novos/não usados. Decida quais valem a pena mapear.

### Passo 5: Itere com base nos achados

Exemplos típicos de ajustes depois da simulação:

| Achado na simulação | Ação |
|---------------------|------|
| Um campo de IP de rede aparece em metade dos eventos, mas não está mapeado | Adicione-o como indicador, gerando um por item da lista |
| A severidade às vezes vem vazia e cai no valor padrão | Marque essa regra como intencional, para o painel não acusar como problema |
| Um campo (ex.: processo pai) não é acionável | Ignore — não precisa mapear tudo |

### Passo 6: Ative — e o evento segue para a entrega

Quando a simulação mostrar cobertura razoável (a maioria dos campos mapeada ou conscientemente ignorada):

1. **Salve** o mapeamento — o CentralOps registra uma nova versão.
2. **Ative** o mapeamento — os eventos passam a ser normalizados.
3. A partir daí, cada evento normalizado **segue automaticamente para a entrega**:
   - Se houver **regras de roteamento ativas**, o evento é enviado aos destinos que correspondem às condições (severidade, vendor etc.).
   - Se nenhuma regra se aplicar, o evento segue para o **destino padrão**, garantindo que nada se perca.
   - Um mesmo evento pode ser enviado a **vários destinos ao mesmo tempo** (por exemplo, um caminho para o SIEM principal e outro para armazenamento de baixo custo).

> A criação e a ativação de regras de roteamento e a configuração de destinos são feitas pelo administrador, nas telas **Operação -> Roteamento** e **Operação -> Destinos** (visíveis apenas para administradores). Consulte [Roteamento e Destinos](../outputs/routing.md) e [Destinos](../outputs/destinations.md).

---

## Resumo do fluxo

| Fase | Ação | Onde, na interface | Esforço |
|------|------|--------------------|---------|
| Descoberta | Revisar eventos de amostra e os campos do vendor | Normalização -> Mappings / Drift Explorer | ~5 min |
| Esqueleto | Preencher os campos obrigatórios (tipo, horário, ID, severidade) | Normalização -> Mappings | ~5 min |
| Mapeamento | Ligar dispositivo, usuário e indicadores | Normalização -> Mappings | ~10 min |
| Testes | Rodar Dry Run, revisar drift e iterar | Mappings + Drift Explorer | ~10 min |
| Ativação | Salvar e ativar o mapeamento | Normalização -> Mappings | ~5 min |
| **Total** | | | **~35 min** |

**Dicas:**

- Comece sempre pelos campos obrigatórios — assim o evento falha cedo e visivelmente se algo crítico faltar.
- Para campos que podem vir em mais de um lugar do evento, configure um **valor alternativo**.
- Para listas (vários destinatários, vários anexos), gere **um indicador por item**.
- Rode o Dry Run cedo e com frequência — ele pega a maior parte dos problemas antes da produção.
- Depois de ativado, o resultado é **vendor-neutro**: o mesmo evento normalizado pode ir para qualquer destino configurado.
- Se algum comportamento de roteamento ou destino não estiver disponível para você, ele provavelmente é exclusivo do administrador — fale com o administrador da plataforma.
