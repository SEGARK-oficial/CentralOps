---
sidebar_position: 1
title: Visão Geral
description: O que é o CentralOps, para quem serve e como os dados fluem na plataforma
---

# Visão Geral

**O CentralOps reúne os alertas de várias ferramentas de segurança em um único lugar, padroniza os campos e entrega cada evento aos destinos que você escolher.**

Se você opera vários clientes ou tem Sophos, Defender, NinjaOne e Wazuh em consoles separados, cada um chega em um formato diferente, com sua própria nomenclatura. O CentralOps coleta tudo, normaliza os campos para um formato comum e encaminha os eventos para os destinos certos (por exemplo Wazuh, Splunk, Elastic, S3, Microsoft Sentinel ou Kafka), sem você precisar tratar cada fonte na mão.

A operação é feita 100% pela interface web. O onboarding de novos clientes passa por aprovação humana, os dados de cada organização ficam isolados, as regras de normalização ficam versionadas (com comparação, teste e volta para uma versão anterior) e cada evento pode ser enviado a vários destinos ao mesmo tempo.

## Quando usar

- **Consolidar alertas de fornecedores diferentes** — você recebe alertas de Sophos, Defender e Wazuh em formatos distintos e quer todos no mesmo painel, com campos comparáveis, para triagem unificada.
- **Atender vários clientes (MSSP)** — cada cliente fica em uma organização separada, com seus próprios alertas, integrações e usuários, sem misturar dados de um cliente com outro.
- **Entregar os eventos a mais de um destino** — você precisa que o mesmo alerta chegue ao SIEM do cliente e, ao mesmo tempo, a um data lake para retenção longa, sem montar pipelines paralelos.

## Como os dados fluem

1. **Coleta** — os Collectors buscam os eventos nas ferramentas conectadas em intervalos curtos, respeitando os limites de cada fornecedor e descartando eventos duplicados automaticamente.
2. **Normalização** — o sistema converte os campos de cada fornecedor para um formato comum. Por exemplo, um nível de severidade numérico de um fornecedor passa a aparecer como `high` no formato padronizado.
3. **Quarentena** — eventos que não passam na validação ficam retidos na fila de Quarentena. Você revisa o motivo, ajusta a regra de normalização e reprocessa o evento pela tela de Quarentena.
4. **Campos novos detectados** — quando um fornecedor passa a enviar um campo que ainda não estava previsto, a plataforma sinaliza para você não perder informação sem perceber.
5. **Roteamento e entrega** — cada evento é avaliado contra suas regras e enviado, ao mesmo tempo, aos destinos definidos. A ocultação de dados sensíveis (PII) pode ser aplicada por rota.
6. **Auditoria** — toda mudança fica registrada: quem alterou qual regra, qual rota ou qual destino, e quando.

## Para quem é

| Papel | O que faz |
|------|-----------|
| **Analista de SOC** | Acompanha alertas no Dashboard, investiga eventos em Quarentena e faz buscas por ativos e criticidade. Sem permissão de edição. |
| **Operador** | Faz triagem: descarta eventos falsos, reprocessa a Quarentena, pausa integrações e ajusta os destinos ativos. Sem permissão para editar regras de normalização. |
| **Engenheiro** | Edita as regras de normalização (Mappings), marca campos como tratados, executa recoletas históricas, volta a versões anteriores e monta as rotas de entrega. |
| **Administrador** | Cria usuários, gerencia organizações, configura destinos e o roteamento. As demais opções de plataforma ficam na Administração. |

Consulte a página de [RBAC](../concepts/rbac.md) para a matriz completa de permissões por papel.

> Algumas telas só aparecem para administradores: **Organizações**, **Destinos**, **Roteamento** e **Fluxo de dados**.

## Conceitos-chave

**Integração** — uma ferramenta conectada (por exemplo Sophos Central de uma empresa), com suas credenciais e o estado da coleta. Fica na tela **Visão geral -> Integrações**.

**Mapping** — a regra que converte os eventos brutos para o formato padronizado. As versões ficam guardadas, e você pode voltar para uma versão anterior em um clique. Fica na tela **Normalização -> Mappings**.

**Quarentena** — eventos que falharam na validação (por exemplo, um campo obrigatório ausente). Você revisa, descarta ou reprocessa depois de corrigir o Mapping. Fica na tela **Normalização -> Quarentena**.

**Campos novos detectados (drift)** — campos que um fornecedor passou a enviar e que ainda não estavam previstos. Você pode ignorá-los ou marcá-los como já tratados para deixarem de aparecer. Fica na tela **Normalização -> Drift Explorer**.

**Organização** — a separação de dados entre clientes. Cada organização tem suas próprias integrações, regras e usuários, sem cruzar dados com outras. Ideal para quem atende vários clientes. Fica na tela **Visão geral -> Organizações** (só admin).

**Destino** — para onde os eventos são enviados (por exemplo Wazuh, Splunk ou S3), com suas credenciais, formatação e estado de saúde próprios. É independente da coleta. Fica na tela **Operação -> Destinos** (só admin).

**Rota** — a regra que decide quais eventos vão para quais destinos. Avalia condições, envia para vários destinos ao mesmo tempo e permite ocultar dados sensíveis por rota. Fica na tela **Operação -> Roteamento** (só admin).

## Próximos passos

- **[Primeiro login](./first-login.md)** — acesse o painel e familiarize-se com as telas.
- **[Quickstart](./quickstart.md)** — conecte uma integração Sophos e configure seu primeiro destino.
