---
sidebar_position: 1
title: Normalização (visão geral)
description: A etapa que converte alertas de qualquer fornecedor em um formato único e padronizado, pronto para suas ferramentas de SOC.
---

# Normalização (visão geral)

A **normalização** transforma os alertas que chegam de diferentes fornecedores de segurança em um **formato único e padronizado**. Cada fornecedor (Sophos, Wazuh, Microsoft Defender, etc.) usa nomes de campo e estruturas próprias; a normalização traduz tudo isso para um modelo comum, para que suas buscas, painéis e regras de resposta funcionem da mesma forma, não importa de onde o alerta veio.

No CentralOps, a normalização é a **primeira etapa** pela qual cada alerta passa antes de ser roteado e entregue aos seus destinos.

```
Coleta  →  Normalização  →  Roteamento  →  Destinos
(alertas    (padroniza      (decide para    (SIEM, data
 de qualquer  o formato)     onde enviar)     lake, etc.)
 fornecedor)
```

## Quando usar

A normalização atua automaticamente em todo alerta que entra na plataforma. Você precisa olhar para ela — e ajustar um **mapeamento** — em situações como estas:

- **Você ativou um novo fornecedor** (por exemplo, passou a coletar alertas do Sophos) e quer que esses alertas apareçam no seu SIEM já no formato padrão, sem precisar reescrever consultas manualmente para cada origem.
- **Um campo importante está vazio ou errado** nos alertas de uma origem — por exemplo, o horário ou o título da detecção não chega como esperado — e você precisa corrigir como esse campo é lido.
- **Surgiram campos novos** no que o fornecedor envia (uma atualização do produto dele adicionou informação) e você quer incorporá-los ao formato padrão para não perder esse dado.

## Como funciona

Cada origem de alertas tem um **mapeamento**: um conjunto de regras que diz "pegue este dado do alerta original e coloque-o neste campo padrão". A plataforma aplica o mapeamento a cada alerta que chega e produz a versão padronizada.

Os mapeamentos têm três características importantes:

| Característica | O que significa para você |
|---------------|---------------------------|
| **Visual** | Você monta as regras pelas telas da plataforma, campo a campo, sem escrever código. |
| **Versionado** | Cada vez que você salva, fica registrada uma nova versão no histórico, com a descrição do que mudou. Você pode comparar versões e voltar a uma anterior. |
| **Testável** | Antes de ativar uma versão, você pode testá-la contra alertas reais de exemplo e ver o resultado, sem afetar o que está em produção. |

## Onde fica na plataforma

Tudo relacionado a normalização está no grupo **Normalização** do menu lateral:

| Tela | Para que serve |
|------|----------------|
| **Mappings** | Lista de mapeamentos. É aqui que você cria, edita e testa as regras de cada origem. |
| **Drift Explorer** | Mostra **campos novos detectados** nos alertas — informação que o fornecedor começou a enviar e que ainda não está no seu mapeamento. |
| **Quarentena** | Alertas que não puderam ser normalizados (por exemplo, um campo obrigatório veio ausente). Você pode investigar e reprocessá-los. |
| **Saúde do Pipeline** | Visão geral de como a normalização está indo: volume processado, quanto foi para quarentena, qualidade dos mapeamentos. |

## Criar ou ajustar um mapeamento

Pelo menu **Normalização → Mappings**:

1. **Abra ou crie o mapeamento.** Clique em um mapeamento existente para editá-lo, ou crie um novo para a origem desejada.
2. **Monte as regras.** Para cada informação que você quer no formato padrão, adicione uma regra apontando de qual campo do alerta original ela vem. A plataforma oferece transformações prontas para os casos comuns (converter formato de data, usar um campo alternativo quando o principal está ausente, definir um valor padrão, extrair listas de itens como IPs ou hashes).
3. **Teste antes de ativar.** Use o teste do editor para rodar o mapeamento contra alertas reais de exemplo. O resultado mostra como ficaria o alerta padronizado e aponta regras que falhariam. Ajuste até ficar limpo.
4. **Salve a nova versão.** Ao salvar, descreva o que mudou. Essa versão passa a valer para os próximos alertas e fica registrada no histórico.

:::tip
Quando o **Drift Explorer** apontar um campo novo, você pode ir direto dele para o mapeamento correspondente, já com o campo pré-preenchido, e decidir se quer incorporá-lo.
:::

## O que acontece depois da normalização

Depois que um alerta é padronizado, ele segue automaticamente pelo restante do pipeline:

1. **Roteamento** — a plataforma decide para qual(is) destino(s) o alerta deve ir, com base em condições que você configura. (Esta etapa fica em **Operação → Roteamento**, disponível para administradores.) Veja [Saídas & Roteamento](../outputs/overview.md).
2. **Mascaramento de dados sensíveis** — quando configurado, informações sensíveis (PII) podem ser mascaradas ou removidas antes da entrega, por rota. Veja [Mascaramento de PII](../outputs/pii-redaction.md).
3. **Destinos** — o alerta é entregue aos seus destinos: SIEMs, data lakes e outros. Veja [Destinos disponíveis](../outputs/destinations.md).

**Nenhum alerta é perdido.** Se um alerta não se encaixar em nenhuma regra de roteamento, ele cai numa rota de segurança que sempre o envia para um destino padrão. Esse destino padrão é definido pela equipe de infraestrutura no momento do deploy. Se precisar alterá-lo, fale com o administrador da plataforma.

## Próximos passos

- **Veja exemplos por fornecedor:** [Casos de uso](use-cases.md)
- **Consulte receitas para problemas comuns:** [Cookbook](cookbook.md)
- **Resolva problemas de normalização:** [Resolução de problemas](troubleshooting.md)
