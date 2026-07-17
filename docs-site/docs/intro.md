---
sidebar_position: 0
slug: /
title: CentralOps
description: "Plataforma que coleta a telemetria dos seus vendors de segurança, padroniza tudo em um formato único e entrega a múltiplos destinos — operada 100% pela interface web."
---

# CentralOps

**Plataforma de dados de segurança para SOCs e MSSPs que operam vários vendors ao mesmo tempo.**

O CentralOps reúne os eventos de segurança dos seus vendors (como Sophos Central/XDR e Wazuh), padroniza todos em um formato único e consistente, decide para onde cada evento vai por regra, e entrega simultaneamente a vários destinos (Wazuh, Splunk, Elastic, S3, Microsoft Sentinel, Kafka, syslog e outros). Tudo é feito pela interface web — você não precisa de terminal, código nem arquivos de configuração.

A plataforma foi pensada para o dia a dia de quem opera muitos clientes com produtos diferentes: cada cliente fica isolado, os alertas chegam já padronizados, e há ferramentas para investigar, responder e auditar — tanto para analistas quanto para administradores.

## Quando usar

- **Receber alertas de vendors diferentes em um só lugar** — você opera Sophos e Wazuh (e outros), mas não quer abrir vários consoles. O CentralOps coleta e padroniza tudo num formato único, entrega aos seus destinos e deixa os eventos pesquisáveis em **Operação -> Investigações**.
- **Enviar o mesmo evento para mais de um destino** — o time de detecção precisa dos eventos no SIEM, mas o time de compliance quer uma cópia arquivada em um data lake. O administrador configura as duas entregas e cada destino recebe sua versão (por exemplo, a do SIEM com dados pessoais mascarados).
- **Investigar um incidente com contexto** — durante um caso, o analista usa **Operação -> Investigações** para buscar eventos relacionados, todos no mesmo schema, sem precisar traduzir campos entre fabricantes.
- **Operar vários clientes com isolamento** — em um MSSP, cada organização vê apenas seus próprios dados, e o administrador gerencia clientes, integrações e políticas pela mesma interface.

## Para quem é

| Perfil | O que faz no CentralOps |
| --- | --- |
| **MSSPs e MDRs** | Operam dezenas a centenas de clientes com vendors variados, cada um isolado. |
| **Analistas de SOC** | Priorizam alertas críticos, investigam com contexto e trabalham num formato de evento consistente. |
| **Engenheiros de segurança** | Ajustam como os campos de cada vendor são padronizados, com versionamento e testes antes de publicar. |
| **Administradores** | Configuram destinos, regras de roteamento, retenção e o mascaramento de dados pessoais por organização. |

## Primeiros passos

O CentralOps é um serviço acessado pelo navegador — não há nada para instalar na sua máquina. Peça o endereço de acesso e o login ao administrador da plataforma.

1. **Faça login** no endereço da sua plataforma.
2. **Conheça o painel** em **Visão geral -> Dashboard** — é a sua visão geral do que está entrando e de como o pipeline está se comportando.
3. **Veja as integrações** em **Visão geral -> Integrações** — aqui aparecem os vendors conectados e a saúde de cada coleta.
4. **Investigue os eventos** em **Operação -> Investigações** — busca pelos eventos padronizados já entregues aos destinos; para triagem de detecções geradas por regras e buscas, use **Operação -> Detecções**.

## O que a plataforma faz

- **Vários vendors numa só plataforma** — os eventos de cada fabricante chegam ao mesmo lugar. Para conectar um novo vendor, o administrador usa o catálogo de integrações na própria interface — não é preciso programar nada.
- **Eventos padronizados** — cada vendor descreve os mesmos dados de um jeito diferente; o CentralOps converte tudo para um formato único, para que um campo (por exemplo, "endereço de origem") signifique sempre a mesma coisa, venha de onde vier. Esse mapeamento é configurado pelo editor de mapeamento na tela **Normalização -> Mappings**, com versões, comparação entre versões, teste prévio e possibilidade de voltar atrás.
- **Isolamento por cliente** — em ambientes multi-tenant, cada organização vê apenas seus próprios eventos. Novos clientes podem ser detectados automaticamente e só entram após aprovação de uma pessoa.
- **Quarentena de eventos com problema** — eventos que chegam fora do esperado não são descartados em silêncio: ficam na tela **Normalização -> Quarentena**, com o motivo do problema, e podem ser reprocessados depois de corrigir o mapeamento.
- **Campos novos detectados (drift)** — quando um vendor passa a enviar campos que ainda não estão mapeados, eles aparecem em **Normalização -> Drift Explorer** para você revisar e incorporar, sem perder informação.
- **Entrega a vários destinos** — o administrador define, em **Operação -> Roteamento**, regras que decidem para onde cada evento vai, e em **Operação -> Destinos** quais são esses destinos (Wazuh, Splunk, Elastic, S3, Microsoft Sentinel, Kafka, syslog e outros). Um mesmo evento pode ser enviado a vários destinos ao mesmo tempo. Sempre há um destino padrão de segurança, para que nenhum evento se perca.
- **Mascaramento de dados pessoais por destino** — dados sensíveis (como e-mail, IP e telefone) podem ser mascarados antes da entrega, e cada destino pode receber uma versão diferente (por exemplo, o SIEM recebe a versão mascarada e o data lake a versão completa).
- **Acompanhamento da saúde do pipeline** — em **Normalização -> Saúde do Pipeline** você vê se a coleta, a padronização e a entrega estão fluindo bem. Quando um destino fica instável, a plataforma se protege e segura os envios até ele se recuperar; os eventos pendentes entram numa fila de reenvio e são entregues automaticamente depois.
- **Investigação e resposta** — busque eventos em **Operação -> Investigações**, aplique bloqueios em **Operação -> Resposta** e consulte o que já passou em **Operação -> Histórico**.
- **Auditoria completa** — quem fez o quê e quando, com trilha exportável para fins de conformidade.

## Como os dados fluem

Em alto nível, todo evento percorre quatro etapas dentro do CentralOps:

**Coleta** → **Padronização** → **Roteamento** → **Entrega**

1. **Coleta** — a plataforma busca os eventos nas integrações dos seus vendors.
2. **Padronização** — cada evento é convertido para o formato único; o que não encaixa vai para a quarentena.
3. **Roteamento** — as regras decidem para quais destinos o evento deve ir.
4. **Entrega** — o evento é enviado simultaneamente aos destinos escolhidos, com o mascaramento de dados pessoais aplicado conforme cada rota.

> As regras de roteamento, os destinos e o fluxo de dados em tempo real só aparecem para administradores, nos respectivos itens do grupo **Operação**.

## Saiba mais

- **[Padronização de eventos](./normalization/overview.md)** — como configurar e versionar os mapeamentos.
- **[Roteamento e Destinos](./outputs/routing.md)** — como definir para onde os eventos vão e o mascaramento de dados pessoais.

Algumas configurações de infraestrutura são definidas pela equipe de infraestrutura no momento do deploy. Se precisar alterá-las, fale com o administrador da plataforma.
