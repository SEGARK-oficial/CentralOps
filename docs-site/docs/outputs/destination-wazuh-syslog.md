---
sidebar_position: 3
title: "Destino: Syslog e JSONL"
description: Envie eventos como Syslog (RFC 3164/5424) para o Wazuh e SIEMs, ou grave em arquivo JSONL local como buffer durável.
---

# Syslog e JSONL

O CentralOps pode entregar os eventos normalizados a um destino externo em formato **Syslog** (padrões RFC 3164 e RFC 5424) ou gravá-los em **arquivo JSONL local**. É assim que você integra a plataforma ao Wazuh, ao Graylog, ao QRadar, ao Splunk e a qualquer SIEM que receba syslog — ou mantém uma cópia local de tudo que passou pela plataforma.

> **Quem configura:** apenas o administrador cria e edita destinos e rotas (menu **Operação → Destinos** e **Operação → Roteamento**). Qualquer analista ou engenheiro pode acompanhar o status da entrega em **Normalização → Saúde do Pipeline**.

## Quando usar

| Cenário de SOC | Modo recomendado |
|---|---|
| Você já usa o **Wazuh** como SIEM central e quer que os alertas de Sophos, Defender, AWS, etc. cheguem lá normalizados. | **Syslog RFC 3164** — o Wazuh reconhece o JSON automaticamente. |
| Seu time investiga no **Graylog** (ou Splunk Heavy Forwarder / QRadar) e quer aproveitar os campos estruturados do syslog moderno. | **Syslog RFC 5424** — usa o bloco de dados estruturados. |
| Você precisa de uma **cópia durável** de todos os eventos para auditoria, ou de um *fallback* caso o SIEM fique indisponível. | **Arquivo JSONL local** — grava cada evento em disco, independente da rede. |

## Os três modos de entrega

| Modo | Para onde vai | Quando escolher |
|------|---------------|-----------------|
| **Syslog RFC 3164** | Wazuh, QRadar, Splunk e SIEMs que aceitam syslog clássico. | É o caminho recomendado para **Wazuh**: o evento vai como JSON e é reconhecido automaticamente. |
| **Syslog RFC 5424** | Graylog, Splunk Heavy Forwarder, QRadar. | Quando o destino sabe ler os **dados estruturados** do syslog moderno. Não é o ideal para Wazuh padrão. |
| **Arquivo JSONL local** | Um arquivo no próprio servidor do CentralOps. | Buffer durável, auditoria, ou cópia paralela do que sai para o SIEM. |

Em todos os modos, cada evento já sai **normalizado** e acompanhado dos metadados internos que a plataforma adiciona (origem, integração e horário de coleta), para que o SIEM consiga filtrar e correlacionar.

## Syslog RFC 3164 (recomendado para Wazuh)

É o formato clássico de syslog. O evento viaja como JSON completo, e o Wazuh, na configuração padrão, reconhece esse JSON sozinho.

### Como configurar

1. Abra o menu **Operação → Destinos**.
2. Adicione um novo destino e escolha o tipo **Syslog RFC 3164**.
3. Preencha os campos de conexão:

   | Campo | O que informar |
   |-------|----------------|
   | **Host** | O endereço do seu receptor syslog (ex.: o servidor do Wazuh). |
   | **Porta** | A porta em que o receptor escuta (514 é o padrão de syslog). |
   | **Usar TLS** | Ative para criptografar a conexão. Recomendado sempre que o destino suportar. |

4. Use a ação de **testar a conexão** na própria tela. Se o teste passar, salve o destino.
5. Vá em **Operação → Roteamento** e crie uma rota que envie os eventos desejados para esse destino.

> **Sobre o certificado (TLS):** se o seu SIEM usa um certificado público (Let's Encrypt, DigiCert, etc.), não é preciso fazer nada além de ativar o TLS. Se ele usa um certificado interno/próprio, o CentralOps precisa confiar na autoridade certificadora (CA) dele. **Essa CA é instalada na plataforma pela equipe de infraestrutura no momento do deploy.** Se o teste de conexão acusar erro de certificado, fale com o administrador da plataforma para que a CA do seu SIEM seja adicionada.

### O que o Wazuh recebe

O Wazuh recebe o alerta como JSON e enxerga, além dos campos do evento, um bloco de **metadados internos** que a plataforma adiciona — com a integração de origem, a plataforma e o horário de coleta. Use esses campos no Wazuh para filtrar por origem (ex.: separar o que veio do Sophos do que veio do Defender).

> **Os eventos não aparecem no Wazuh?** Na configuração padrão, o Wazuh reconhece automaticamente os eventos em formato RFC 3164 enviados pelo CentralOps. Se mesmo assim eles não surgirem, peça ao administrador do Wazuh para confirmar que o reconhecimento de eventos em JSON está ativo no receptor.

:::info Anti-loop: Wazuh como fonte
Se você coletou eventos **de um Wazuh** e está tentando roteá-los de volta **para o mesmo Wazuh** via destino syslog, eles serão **suprimidos** para evitar um loop infinito (evento coletado → reenviado → recoletado). Essa supressão é registrada em log como `loop_blocked`. Se você quiser reenviá-los a outro Wazuh, use um endereço de network diferente para cada um. Para mais detalhes, veja a documentação de [Wazuh](../integrations/wazuh.md).
:::

## Syslog RFC 5424 (Graylog, Splunk, QRadar)

É o formato de syslog moderno, com um bloco de **dados estruturados** que destinos como o Graylog sabem extrair em campos próprios.

### Como configurar

O passo a passo é o mesmo do RFC 3164 — em **Operação → Destinos**, escolha o tipo **Syslog RFC 5424** e informe **Host**, **Porta** e **Usar TLS**. Depois crie a rota em **Operação → Roteamento**.

> **Não use RFC 5424 para Wazuh padrão.** O Wazuh, na configuração comum, não lê o JSON quando ele vem nesse formato. Para Wazuh, prefira o **RFC 3164**.

## Arquivo JSONL local

Neste modo, cada evento é gravado como uma linha de JSON em um arquivo no próprio servidor do CentralOps, com um arquivo novo a cada dia. Serve como buffer durável e cópia de auditoria.

### Como configurar

1. Em **Operação → Destinos**, adicione um destino do tipo **Arquivo JSONL (local)**.
2. Crie a rota correspondente em **Operação → Roteamento**.

> **Onde os arquivos ficam e por quanto tempo:** a pasta de gravação e a política de retenção/limpeza dos arquivos JSONL são definidas pela equipe de infraestrutura no momento do deploy. Se precisar mudar o local, o período de retenção ou a compactação dos arquivos, fale com o administrador da plataforma.

### Para que serve

- **Buffer durável:** os eventos são gravados em disco, independente de o SIEM estar acessível.
- **Fallback:** se o syslog cair, os eventos se acumulam localmente em vez de se perderem.
- **Auditoria:** você mantém uma cópia local de tudo que passou pela plataforma.

## Acompanhar a entrega

Qualquer usuário pode verificar se os eventos estão saindo, sem precisar de acesso de administrador.

1. Abra o menu **Normalização → Saúde do Pipeline**.
2. Localize o destino ou a rota que você quer acompanhar.
3. Observe o indicador de status:
   - **Verde / saudável** — os eventos estão sendo entregues normalmente.
   - **Vermelho / com erro** — passe o mouse sobre o indicador para ver a mensagem do erro (por exemplo, falha de conexão ou de certificado).

Nessa tela você também acompanha o volume de eventos, a latência e a quantidade de eventos ainda em espera para envio.

## Resolução de problemas

| Sintoma | O que verificar |
|---------|-----------------|
| **O teste de conexão falha.** | Confirme **Host** e **Porta** na tela do destino (**Operação → Destinos**). Se o destino estiver em outra rede, o acesso de rede entre a plataforma e o SIEM é responsabilidade da equipe de infraestrutura — fale com o administrador. |
| **Erro de certificado no teste de conexão.** | O CentralOps não confia na autoridade certificadora do seu SIEM. Peça ao administrador da plataforma para adicionar a CA do destino. Veja a nota sobre certificado na seção do RFC 3164. |
| **O teste passa, mas os eventos não chegam ao SIEM.** | Em **Operação → Roteamento**, confira se a rota não está com um filtro restritivo demais (por exemplo, filtrando só severidade "crítica" e descartando o resto). Em **Normalização → Saúde do Pipeline**, confirme que o destino está saudável e que há eventos saindo. |
| **Os eventos chegam ao Wazuh, mas em formato inesperado.** | Confirme que a rota está usando **Syslog RFC 3164** (e não 5424). Se ainda assim o Wazuh não reconhecer o conteúdo, peça ao administrador do Wazuh para verificar o reconhecimento de eventos em JSON no receptor. |
| **Espera de envio acumulando.** | Em **Normalização → Saúde do Pipeline**, um número alto de eventos em espera costuma indicar que o destino está lento ou indisponível. Verifique o status do destino e a mensagem de erro no indicador. |

## Casos de uso

### Centralizar várias plataformas no Wazuh

Crie uma rota por plataforma de origem, todas apontando para o destino **Syslog RFC 3164** do Wazuh:

- Eventos do Sophos → Syslog RFC 3164 (Wazuh)
- Eventos do Defender → Syslog RFC 3164 (Wazuh)
- Eventos da AWS → Syslog RFC 3164 (Wazuh)

Todos chegam ao Wazuh já normalizados e com os metadados de origem, o que permite filtrar por plataforma diretamente lá.

### Enviar em paralelo para Wazuh e Graylog

Use o **envio simultâneo a vários destinos** com filtros diferentes por rota:

- Severidade alta/crítica → Syslog RFC 3164 (Wazuh)
- Severidade média ou acima → Syslog RFC 5424 (Graylog)

Assim o Wazuh recebe o que é prioritário e o Graylog guarda uma visão mais granular para investigação e auditoria.

### Buffer local com fallback de syslog

Crie duas rotas para os mesmos eventos:

- Todos os eventos → Arquivo JSONL local
- Todos os eventos → Syslog RFC 3164 (Wazuh)

O arquivo JSONL funciona como cache durável: se o syslog ficar indisponível, os eventos continuam preservados localmente.

## Próximos passos

- **Criar a rota de entrega:** menu **Operação → Roteamento**.
- **Ajustar como os campos são mapeados:** menu **Normalização → Mappings**.
- **Acompanhar a saúde da entrega:** menu **Normalização → Saúde do Pipeline**.
