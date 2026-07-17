---
sidebar_position: 9
title: Fortinet FortiGate
description: Receba eventos de tráfego do FortiGate via syslog push e edge-collector Vector
---

# Fortinet FortiGate

Conecte o Fortinet FortiGate ao CentralOps para coletar eventos de tráfego e segurança de rede em tempo real. O FortiGate envia eventos via syslog para um edge-collector (Vector), que os valida e encaminha ao CentralOps de forma segura e resiliente.

## Quando usar

- **Centralizar eventos de tráfego do firewall.** Traga os logs de negociação de pacotes, bloqueios, aceitos e atividades de aplicação do FortiGate para correlacionar com alertas de endpoints.
- **Investigar atividades de rede suspeitas.** Procure por padrões de origem, destino, porta e protocolo sem sair do CentralOps.
- **Encaminhar para SIEM e resposta.** Depois de coletados, os eventos seguem pelas regras de roteamento para os seus destinos (SIEM, data lake, automação de resposta).

## Quem pode fazer o quê

- **Administrador da plataforma:** cria a integração e emite o token (menu **Visão geral -> Integrações**).
- **Administrador de infraestrutura:** configura o Vector near o FortiGate e valida a conectividade.
- **Demais perfis:** visualizam os eventos já coletados (menu **Operação -> Investigações**).

## Pré-requisitos

Antes de começar, garanta que você tem:

- Acesso de **administrador no FortiGate** para configurar syslog.
- Acesso de **administrador no CentralOps**, para criar a integração.
- Um servidor ou container com **Docker** e **Docker Compose** para rodar o edge-collector Vector.
- **Conectividade de rede:** Vector deve alcançar o FortiGate na porta **UDP 5514** (syslog) e o CentralOps na porta **443** (HTTPS para envio).

## Passo 1: Criar a integração no CentralOps

1. No menu lateral, vá em **Visão geral -> Integrações**.
2. Clique para adicionar uma nova integração.
3. Escolha **Fortinet FortiGate** na lista de plataformas e avance.
4. Preencha o campo abaixo:

   | Campo | O que informar |
   |---|---|
   | Nome | Um nome para identificar esta conexão, por exemplo "FortiGate - Matriz". |

5. A integração não precisa de credenciais — FortiGate usa autenticação por token no ingest.
6. Salve a integração.

## Passo 2: Emitir o token de ingestão

Após salvar, a integração exibe um painel **"Ingestão push"** com o token de autenticação.

1. Clique em **"Emitir novo token"** (se não houver um ativo).
2. Copie o token completo e guarde com segurança.

:::warning[Guarde o token com segurança]
O token é uma credencial sensível. Revogue e emita um novo se suspeitar que foi comprometido.
:::

## Passo 3: Rodar o edge-collector Vector

O Vector é um coletor leve que roda perto do FortiGate, recebe syslog e encaminha ao CentralOps.

### Preparar as variáveis de ambiente

Numa máquina com Docker e Docker Compose, crie um arquivo `.env` com:

```env
CENTRALOPS_INGEST_URL=https://seu-dominio/api/ingest/traffic
CENTRALOPS_INGEST_TOKEN=<token copiado no Passo 2>
```

Substitua `seu-dominio` pela URL do seu CentralOps e cole o token exato.

### Iniciar o Vector

Na mesma pasta do `.env`, execute:

```bash
docker compose -f compose/edge/docker-compose.edge.yml up -d
```

Confirme que o container subiu:

```bash
docker compose ps
```

O Vector agora escuta syslog no **UDP 5514** e está pronto.

## Passo 4: Configurar o syslog do FortiGate

Acesse o console de administração do FortiGate e configure o servidor syslog apontando para o Vector.

```
config log syslogd setting
  set status enable
  set server "<IP_DO_VECTOR>"
  set port 5514
  set format default
end
```

Substitua `<IP_DO_VECTOR>` pelo endereço IP ou nome do host da máquina onde Vector está rodando. O formato **deve ser "default"** — assim o FortiGate envia pares chave=valor que o Vector consegue fazer parse.

:::info[Testar conectividade]
Se o FortiGate não conseguir alcançar Vector, confirme:
- IP ou nome de host corretos.
- Firewall permite saída UDP 5514 do FortiGate.
- Vector está rodando (`docker compose ps`).
:::

## Passo 5: Verificar a coleta

### Via painel da integração

1. Volte ao CentralOps, menu **Visão geral -> Integrações**, selecione a integração do FortiGate.
2. No painel **"Ingestão push"**, acompanhe a **profundidade da fila**: deve subir quando eventos chegam, depois descer ~20 segundos depois (enquanto Vector envia ao CentralOps).

### Via tela de Investigações

1. Acesse **Operação -> Investigações**.
2. Procure por eventos com `type: network_activity` ou `source: fortigate`.
3. Se nada aparecer, veja a seção [Solução de problemas](#solução-de-problemas).

## O que é coletado

Cada evento de tráfego do FortiGate normaliza para o padrão **OCSF Network Activity** com campos como:

| Campo | Descrição | Exemplo |
|---|---|---|
| **srcip** | IP de origem | `192.168.1.50` |
| **dstip** | IP de destino | `8.8.8.8` |
| **srcport** | Porta de origem | `54321` |
| **dstport** | Porta de destino | `443` |
| **action** | Ação do firewall | `accept`, `deny` |
| **service** | Nome do serviço | `https`, `dns` |
| **app** | Aplicação / categoria | `web-application` |
| **sentbyte** | Bytes enviados | `1024` |
| **rcvdbyte** | Bytes recebidos | `2048` |
| **devname** | Interface de saída | `port1`, `wan1` |
| **user** | Usuário associado (se autenticado) | `domain\usuario` |
| **timestamp** | Horário do evento | `2026-06-26T10:30:00Z` |

O CentralOps converte automaticamente para OCSF. Se um evento não puder ser normalizado (por exemplo, campo timestamp inválido), vai para a **Quarentena** (menu **Normalização -> Quarentena**) para revisar.

:::note[Timestamp obrigatório]
Vector adiciona um timestamp ISO-8601 automaticamente. Confirme que o FortiGate está sincronizado com NTP.
:::

## Solução de problemas

| Sintoma | Causa provável | O que fazer |
|---|---|---|
| **Erro 401 (Unauthorized) no Vector** | Token inválido ou expirado | Volte ao passo 2, emita um novo token no painel da integração e atualize o `.env`. Reinicie Vector: `docker compose restart`. |
| **Vector não recebe syslog (nenhum evento entrando)** | FortiGate não consegue alcançar Vector, ou firewall bloqueando UDP 5514 | Confirme o IP/nome de host no FortiGate. Teste conectividade: `telnet <IP_DO_VECTOR> 5514` (ou `nc -u` para UDP). Se bloqueado, libere na firewall. |
| **Eventos na quarentena** | Formato ou timestamp inválido | Confirme que o FortiGate está com o formato "default" (pares key=value). Verifique sincronismo NTP do FortiGate. Abra **Normalização -> Quarentena** para ver o conteúdo rejeitado. |
| **Fila de ingestão não drena (cresce continuamente)** | CentralOps indisponível ou URL errada | Confirme a URL no `.env` (sem typos). Teste com `curl`: `curl -X POST -H "Authorization: Bearer <token>" -d '{}' https://seu-dominio/api/ingest/traffic`. Se não responder, fale com o administrador da plataforma. |

## Próximos passos

- **Eventos aparecendo?** Veja o [Dashboard](../operations/dashboard.md).
- **Algo na quarentena?** Veja a [Quarentena](../operations/quarantine.md).
- **Redirecionar para outros destinos?** Veja [Roteamento](../outputs/routing.md).
- **Adicionar outro fornecedor?** Veja a [visão geral de Integrações](./overview.md).
