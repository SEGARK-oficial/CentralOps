---
sidebar_position: 2
title: CML — A linguagem de mapeamento
description: O que é CML, quando criar ou editar um mapeamento, como ele transforma eventos brutos em OCSF e como o CentralOps versiona, testa e reverte cada mudança
---

# CML — CentralOps Mapping Language

**CML é a linguagem do CentralOps para transformar os eventos brutos de cada vendor de segurança no formato padronizado OCSF.** Cada integração tem um mapeamento (mapping) escrito em CML que diz, campo por campo, como o evento original do vendor vira um evento normalizado que o resto da plataforma entende.

Você trabalha com mapeamentos na tela **Normalização → Mappings**. Para a referência detalhada de cada campo da linguagem, veja a [Especificação DSL](./dsl-spec.md).

## Quando usar

Você cria ou edita um mapeamento quando precisa decidir como os eventos brutos de uma fonte viram eventos OCSF. Cenários reais:

- **Nova integração conectada.** Você acabou de conectar uma integração Sophos em **Integrações** e precisa definir como os alertas brutos viram eventos OCSF (severidade, host, usuário, observables) antes que eles fluam para os destinos.
- **O vendor mudou o schema.** O fornecedor atualizou o produto e novos campos começaram a aparecer em **Normalização → Drift Explorer**. Você precisa decidir o que mapear, o que ignorar e atualizar o mapeamento.
- **Eventos caindo na quarentena.** A tela **Normalização → Saúde do Pipeline** ou **Normalização → Quarentena** mostra eventos que falharam na normalização (por exemplo, um campo obrigatório veio vazio). Você ajusta o mapeamento para corrigir a regra e reprocessa.

## O que é CML

Um mapeamento CML é uma lista de **regras**, e cada regra descreve uma transformação simples:

| Você define | Significa |
|---|---|
| De onde vem o valor | Qual campo do evento bruto do vendor (ou um valor fixo, ou uma lista de campos a tentar em ordem) |
| Para onde ele vai | Qual campo do evento normalizado OCSF recebe esse valor |
| Como é transformado | Tabelas de tradução de valores e conversões de tipo (texto → data, texto → número etc.) |
| Quando a regra roda | Condições para a regra só executar em certos eventos |
| O que fazer se faltar | Um valor padrão, ou marcar o campo como obrigatório (se vier vazio, o evento vai para a quarentena) |

CML é **declarativa**: você descreve o resultado desejado, não escreve um programa com laços e condicionais. Isso é intencional. Em troca de menos liberdade, você ganha:

- **Previsibilidade** — o mesmo mapeamento sempre produz o mesmo resultado.
- **Auditoria** — dá para comparar duas versões e ver exatamente o que mudou de comportamento.
- **Testabilidade** — dá para testar o mapeamento contra eventos reais antes de publicar.

## Como um evento é transformado

Cada evento bruto passa por uma sequência fixa, sem ramificações:

1. **Preparação** — extrações reutilizáveis rodam uma vez (por exemplo, abrir um JSON que veio embutido como texto dentro do evento).
2. **Regras, uma a uma, em ordem.** Para cada regra:
   - Se a condição da regra não bate, a regra é pulada e o campo não é escrito.
   - O valor é pego da origem (campo do vendor, valor fixo, ou a primeira origem que existir na lista de alternativas).
   - Se vier vazio, aplica-se o valor padrão.
   - Aplicam-se as conversões e tabelas de tradução, sempre na mesma ordem.
   - Se a regra é obrigatória e mesmo assim o valor ficou vazio, o evento vai para a **quarentena** (nunca é descartado em silêncio).
   - O valor final é escrito no campo de destino OCSF.
3. **Evento normalizado pronto.** O resultado tem três partes: o evento OCSF normalizado, os metadados internos do evento (origem, versão do mapeamento, horário de coleta) e o payload original preservado para auditoria.

Depois disso, o evento normalizado segue para o motor de roteamento, que decide quais destinos recebem o evento. Veja [Roteamento](../outputs/routing.md). Antes da entrega, cada destino pode aplicar redação de PII — veja [Redação de PII](../outputs/pii-redaction.md).

Pontos importantes:

- **Ordem garantida.** As transformações de cada regra rodam sempre na mesma ordem. O resultado é determinístico.
- **Falha vira quarentena, nunca perda.** Um campo obrigatório vazio manda o evento para a quarentena, onde você pode inspecionar e reprocessar.
- **Campos novos viram drift.** Qualquer campo presente no evento bruto que nenhuma regra consome é detectado automaticamente e aparece no Drift Explorer para você decidir o que fazer.
- **Evento bruto preservado.** O payload original é guardado junto com o evento normalizado, para auditoria completa.

## Como é um mapeamento (exemplo)

Abaixo, um mapeamento mínimo. Você não precisa decorar a sintaxe — o editor de mapeamento na tela **Normalização → Mappings** ajuda a montar e validar cada regra. O exemplo serve para você reconhecer a estrutura:

```jsonc
{
  "preprocess": [],
  "rules": [
    // metadados OCSF fixos
    { "target": "normalized.class_uid",   "const": 2004 },
    { "target": "normalized.class_name",  "const": "Detection Finding" },

    // campo simples com conversão de tipo (data ISO → epoch)
    {
      "target": "normalized.time",
      "source": "eventTime",
      "type_cast": "iso_to_epoch",
      "required": true
    },

    // severidade traduzida por uma tabela de valores
    {
      "target": "normalized.severity_id",
      "source": "severity",
      "value_map": { "critical": 5, "high": 4, "medium": 3, "low": 2 },
      "default": 0
    },

    // título, tentando campos alternativos em ordem
    {
      "target": "normalized.finding_info.title",
      "source": "ruleTitle",
      "fallback_source": ["attackType", "description"],
      "default": "Unknown Detection"
    },

    // regra que só roda quando o campo existe no evento
    {
      "target": "normalized.actor.user.email_addr",
      "source": "mailFrom",
      "when": { "exists": "mailFrom" }
    }
  ]
}
```

Para a referência completa de cada campo, condição, conversão e tratamento de erro, veja:

- **[Especificação DSL](./dsl-spec.md)** — referência formal de cada campo
- **[Cookbook](./cookbook.md)** — receitas para problemas comuns
- **[Referência de operadores](./operators-reference.md)** — todas as conversões, condições e operações de preparação
- **[Casos de uso](./use-cases.md)** — passo a passo por vendor (Sophos, Defender, Wazuh)

## Governança: testar, versionar e reverter

No CentralOps, todo o ciclo de vida de um mapeamento acontece dentro do produto, na tela **Normalização → Mappings**. Você não precisa de ferramentas externas para versionar ou aprovar mudanças.

### Teste antes de publicar (dry-run)

Antes de publicar uma versão nova, você testa o mapeamento contra eventos reais já coletados, sem afetar a produção:

1. Abra o mapeamento em **Normalização → Mappings**.
2. Edite as regras no editor de mapeamento.
3. Rode o teste do mapeamento (dry-run) contra a amostra de eventos coletados nos últimos dias.
4. O CentralOps mostra um resumo: percentual de regras que aplicaram, percentual de eventos que iriam para quarentena, campos que ficaram sempre vazios e campos novos (drift) que a mudança introduziria.
5. Se o resultado estiver bom, publique a nova versão. O CentralOps só permite publicar depois que você testou.

Resultado: todo mapeamento em produção passou por um teste contra eventos reais antes de entrar no ar.

### Histórico de versões

Cada mudança publicada cria uma nova versão do mapeamento, e as versões anteriores **nunca são apagadas**. Na tela do mapeamento você vê o histórico completo, com:

- Quem fez a mudança e quando.
- A mensagem de descrição da mudança.
- A diferença em relação à versão anterior (regras adicionadas, removidas ou alteradas).
- O resumo do teste que validou aquela versão.

Esse histórico serve como trilha de auditoria para conformidade.

### Reverter para uma versão anterior

Se uma versão nova causou problema, você reverte para uma versão anterior a partir do histórico do mapeamento. A troca é imediata e nenhum evento em andamento é perdido — os próximos eventos passam a usar a versão escolhida em poucos segundos.

### Campos novos do vendor (drift)

A cada coleta, o CentralOps compara os campos que suas regras consomem com os campos realmente presentes no evento bruto. Campos que apareceram mas ninguém mapeou são listados em **Normalização → Drift Explorer**, onde você pode:

- Filtrar por vendor e tipo de evento.
- Marcar campos como **ignorar** (param de alertar).
- Marcar campos como **mapear** (entram na sua lista de pendências para atualizar o mapeamento).
- Ignorar vários de uma vez quando o vendor adiciona telemetria interna irrelevante.

Em muitas plataformas a evolução do schema do vendor passa despercebida. No CentralOps ela é exibida ativamente para você decidir o que fazer.

## Casos de uso passo a passo

Para ver mapeamentos reais sendo construídos para vendors específicos (Sophos, Microsoft Defender, Wazuh), veja [Casos de uso](./use-cases.md).

## No roadmap (ainda não disponível)

Os itens abaixo estão planejados e **ainda não estão disponíveis** na plataforma. Não conte com eles hoje:

- **Sugestão de mapeamento assistida por IA** — geração automática de regras a partir de uma amostra de evento bruto e do schema OCSF alvo.
- **Verificação de qualidade no teste** — avisos automáticos durante o dry-run (por exemplo, campos OCSF obrigatórios faltando, tabelas de tradução grandes demais).

## Próximos passos

- **[Especificação DSL](./dsl-spec.md)** — referência formal e detalhada de cada campo
- **[Cookbook](./cookbook.md)** — receitas de problemas reais
- **[Casos de uso](./use-cases.md)** — Sophos, Defender, Wazuh passo a passo
- **[Operadores](./operators-reference.md)** — todas as conversões e condições
- **[Troubleshooting](./troubleshooting.md)** — como diagnosticar mapeamentos em produção
- **[Roteamento](../outputs/routing.md)** — como o evento normalizado é enviado aos destinos
- **[Destinos](../outputs/destinations.md)** — catálogo de destinos de saída
