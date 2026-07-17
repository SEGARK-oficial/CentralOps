---
sidebar_position: 10
title: Windows Event Log (WEC)
description: Colete eventos de segurança via Windows Event Forwarding (WEF) e Windows Event Collector (WEC)
---

# Windows Event Log (WEC)

O Windows Event Log é uma **fonte PUSH** que coleta eventos de segurança nativamente pelo mecanismo de Windows Event Forwarding (WEF) e Windows Event Collector (WEC). Os eventos são normalizados para o formato OCSF Authentication e chegam ao CentralOps através de um endpoint dedicado.

## Quando usar

- **Monitorar logons e falhas de autenticação.** Centralize os eventos de segurança do Windows (logons bem-sucedidos e rejeitados, IDs de evento 4624 e 4625) em um único painel.
- **Investigar acesso a recursos.** Cruze eventos de logon com a timeline de operações suspeitas detectadas em outras fontes (CrowdStrike, Wazuh, Sophos).
- **Atender compliance.** Mantenha um registro imutável de quem acessou qual máquina e quando, para auditoria.

## Quem pode fazer o quê

- **Administrador da plataforma:** cria a integração (menu **Visão geral -> Integrações**) e emite tokens de ingestão.
- **Administrador de infraestrutura Windows:** configura o WEF/WEC no Active Directory e instala o Fluent Bit no servidor coletor.
- **Operador ou superior:** visualiza os eventos (menu **Operação -> Investigações**).

## Arquitetura: WEF → WEC → Fluent Bit → CentralOps

Os endpoints encaminham seus Security logs para um servidor Windows Event Collector (WEC) central por meio de Windows Event Forwarding (WEF), configurado por Group Policy. No servidor WEC, o **Fluent Bit** lê os eventos do canal `ForwardedEvents` e os envia via HTTP POST para o endpoint de ingestão do CentralOps, autenticado com um token Bearer.

**Fluxo:**
1. Endpoint → WEC (via WEF/GPO)
2. WEC local → Fluent Bit (input winevtlog)
3. Fluent Bit → CentralOps (output http com Authorization Bearer)

Você pode usar também **NXLog** como alternativa (input im_msvistalog → output om_http).

## Passo 1: Criar a integração no CentralOps

1. No menu lateral, vá em **Visão geral -> Integrações**.
2. Clique para adicionar uma nova integração.
3. Escolha **Windows Event Log (WEC)** na lista.
4. Preencha o nome (por exemplo, "Windows Security - Datacenter 01") e salve.

:::note[Sem credenciais]
A integração Windows Event Log não pede cliente/senha — é uma ingestão PUSH, então o servidor coletor é que faz o envio. Você apenas emite um token no próximo passo.
:::

## Passo 2: Emitir o token de ingestão

Após salvar a integração:

1. Abra a aba **Ingestão push** na tela da integração.
2. Clique em **Emitir novo token** ou veja o token já gerado.
3. Copie o token e a URI de ingestão — você precisará deles para configurar o Fluent Bit.

| Informação | Onde usar |
|---|---|
| **URI** | Variável `uri` na config do Fluent Bit (ex.: `https://sua-url/api/ingest/security`) |
| **Token Bearer** | Variável `auth_token` na config do Fluent Bit |

## Passo 3: Configurar Windows Event Forwarding (WEF)

Essas etapas são feitas no seu ambiente Windows/Active Directory.

1. **No domínio:** crie uma Group Policy para encaminhar Security logs de todos os endpoints para o seu WEC.
   - Escopo: `Computer Configuration > Policies > Administrative Templates > Windows Components > Event Forwarding`.
   - Configure uma assinatura apontando para o servidor WEC.

2. **No servidor WEC:** confirme que o serviço **Windows Event Collector** está rodando (geralmente já vem habilitado).

3. **Crie uma nova assinatura** na máquina WEC:
   - Execute `wecutil.exe` (ou use o Gerenciador de Eventos graficamente).
   - Nova assinatura → Configure os computadores de origem.
   - Canais de interesse: `Security` e `ForwardedEvents`.

Após a configuração, os eventos de segurança de todos os endpoints começam a chegar no canal `ForwardedEvents` do WEC.

## Passo 4: Instalar e configurar Fluent Bit no WEC

1. **Baixe o Fluent Bit** para Windows desde [fluentbit.io](https://fluentbit.io/download/).
2. **Substitua ou use a config fornecida** em `compose/edge/fluent-bit-windows-wec.conf`:

```
[SERVICE]
    Flush        5
    Log_Level    info
    Parsers_File parsers.conf

[INPUT]
    Name              winevtlog
    Channels          ForwardedEvents,Security
    Read_From_Tail    On
    Tag               wec.*

[OUTPUT]
    Name              http
    Match             wec.*
    Host              sua-url
    Port              443
    URI               /api/ingest/security
    Header            Authorization Bearer seu-token-aqui
    Format            json_lines
    json_date_key     TimeCreated
    compress          gzip
```

3. **Substitua:**
   - `sua-url` pela URL do CentralOps (ex.: `api.centralops.empresa.com`).
   - `seu-token-aqui` pelo token emitido no Passo 2.

4. **Inicie o Fluent Bit:**
   ```powershell
   fluent-bit.exe -c fluent-bit-windows-wec.conf
   ```

5. **Verifique** que os eventos começam a aparecer na tela **Operação -> Investigações** do CentralOps em poucos minutos.

## O que é coletado

Cada evento do Security log chega ao CentralOps com informações como:

| Campo | Exemplo |
|---|---|
| **TimeCreated** | 2026-06-26T10:15:30Z |
| **Computer** | workstation-01.empresa.local |
| **EventID** | 4624 (logon bem-sucedido) ou 4625 (logon falhado) |
| **Channel** | Security, ForwardedEvents |
| **Provider** | Microsoft-Windows-Security-Auditing |
| **TargetUserName** | EMPRESA\usuario |
| **IpAddress** | 192.168.1.100 |
| **LogonType** | 2 (Interactive), 3 (Network), 5 (Service), etc. |

Os eventos são normalizados para **OCSF Authentication** e ficam pesquisáveis em **Operação -> Investigações**.

:::info[Quarentena]
Se um evento chegar sem timestamp ou com campos faltando, ele será colocado em **Normalização -> Quarentena**. Revise e reprocesse se necessário.
:::

## Verificar que tudo está funcionando

- **Na tela de integração:** abra **Visão geral -> Integrações**, selecione a integração Windows Event Log e veja o status da ingestão PUSH.
- **Em Investigações:** vá em **Operação -> Investigações** e filtre por eventos "Windows" ou "Security".
- **Profundidade do buffer:** na aba **Ingestão push**, o gráfico mostra quantos eventos estão na fila aguardando processamento. Deve cair gradualmente para zero.

## Solução de problemas

| Sintoma | Causa provável | O que fazer |
|---|---|---|
| Erro 401 (não autorizado) ao enviar | Token incorreto ou expirado | Verifique no CentralOps se o token foi copiado integralmente. Se precisar, emita um novo na aba **Ingestão push**. |
| Nenhum evento aparece | WEF não está ativo ou Fluent Bit não está rodando | Confirme que a assinatura WEF foi criada no WEC. Execute `wecutil.exe enum-subscription` para listar. Confirme que o Fluent Bit está rodando (verifique `tasklist \| findstr fluent`). |
| Eventos na quarentena | Campo TimeCreated ou outro obrigatório faltando | Abra **Normalização -> Quarentena**, revise o evento rejeitado e verifique se o Fluent Bit está formatando corretamente o JSON. |
| Fluent Bit fecha logo após iniciar | Erro na config | Verifique a sintaxe da config. Rode `fluent-bit.exe -c sua-config.conf -v` para ver logs detalhados. Confira a seção `[OUTPUT]` de forma especial. |

## Próximos passos

- **Eventos aparecendo?** Veja o [Dashboard](../operations/dashboard.md) ou [Investigações](../operations/search.md).
- **Algo na quarentena?** Veja a [Quarentena](../operations/quarantine.md).
- **Quer correlacionar com outras fontes?** Veja [Regras de Correlação](../operations/correlation-rules.md).
- **Adicionar outro fornecedor?** Veja a [visão geral de Integrações](./overview.md).
