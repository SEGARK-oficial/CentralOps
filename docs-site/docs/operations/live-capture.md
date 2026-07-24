---
sidebar_position: 10
title: Captura ao vivo
description: Gravar uma amostra do tráfego real para ver como cada evento entrou, o que virou e onde terminou
---

# Captura ao vivo

A captura ao vivo grava, por um tempo determinado, uma amostra do tráfego que passa pelo pipeline — e mostra, para cada evento, **como ele chegou**, **no que foi transformado** e **onde terminou**. É a ferramenta para responder "por que esse evento não chegou no meu SIEM?".

Diferente das telas de métrica, que mostram números agregados, aqui você vê o evento em si.

## Quando usar

- Um evento não apareceu no destino e você precisa descobrir em que ponto ele parou.
- Você acabou de criar ou alterar uma regra de mapeamento e quer conferir o resultado com tráfego real.
- Está integrando um fornecedor novo e quer ver o formato exato do que ele manda.
- Precisa mandar um exemplo real para o suporte do fornecedor ou anexar num chamado.

## Como iniciar

Vá em **Configurações → Captura ao vivo**.

1. Escolha a organização (administradores globais precisam nomeá-la).
2. Opcionalmente, filtre por fornecedor para gravar só o tráfego dele.
3. Defina a duração. A sessão **expira sozinha** — não fica gravando esquecida.
4. Inicie. Os eventos aparecem conforme o tráfego passa.

:::note[A janela precisa alcançar uma coleta]
Os eventos só aparecem quando houver coleta na janela. Se o fornecedor é consultado a cada 5 minutos, uma captura de 1 minuto pode terminar vazia sem que nada esteja errado. A tela distingue "sessão ativa e nada aconteceu" de "houve tráfego" para você não confundir os dois casos.
:::

## O que a tela mostra

Cada linha é um evento, com o **desfecho** — o que de fato aconteceu com ele:

| Desfecho | Significado |
|---|---|
| **Entregue** | Saiu para o destino. Uma linha por destino. |
| **Falha na entrega** | Chegou ao envio mas o destino recusou, estava fora do ar ou o disjuntor estava aberto. |
| **Descartado** | Uma regra de roteamento com ação **Descartar** apagou o evento. A linha mostra **qual** regra. |
| **Sem rota** | Nenhuma regra casou e não havia destino padrão — foi para a fila de reenvio. |
| **Amostrado para fora** | A amostragem da regra economizou este evento. |
| **Suprimido** | O rate-limit por assinatura da regra economizou este evento. |
| **Em quarentena** | Foi retido na normalização (mapeamento ausente, campo obrigatório faltando, OCSF inválido). |
| **Bloqueado por residência** | O par evento/destino foi excluído por conflito de residência de dados. |
| **Loop bloqueado** | Evento de fonte Wazuh que voltaria ao próprio manager. |

Esse é o valor central da tela: antes dela, tudo que era coletado mas **não** entregue ficava invisível, e "não capturei nada" era indistinguível de "morreu no meio do caminho".

## Ver a transformação (antes e depois)

Clique em **Inspecionar** numa linha. O painel mostra lado a lado:

- **Como recebemos** — o payload original do fornecedor.
- **Como está sendo mandado** — o evento normalizado em OCSF.

É assim que se confere se uma regra de mapeamento fez o que você esperava, sem precisar ler um JSON gigante de uma vez.

Quando o desfecho tem uma regra de roteamento associada (descarte, amostragem), o identificador dela aparece como etiqueta — respondendo direto "qual regra apagou meu evento?".

:::warning[O "antes" pode já vir podado]
Se o mapeamento tem um bloco `raw_reduction`, o payload que você vê em "como recebemos" já pode estar sem os campos podados. Se um campo que o fornecedor manda não aparece nem aí, verifique a poda do mapeamento antes de concluir que o fornecedor não enviou. Veja [Especificação da DSL](../normalization/dsl-spec.md).
:::

## Exportar o que foi capturado

Os botões **Exportar CSV** e **NDJSON** baixam a sessão inteira.

- **CSV** — abre direto no Excel, com uma linha por evento e colunas de desfecho, rota e destino. É o formato para análise rápida ou para anexar num chamado.
- **NDJSON** — uma linha JSON por evento, para processar com ferramentas de linha de comando ou reprocessar.

Os dados pessoais são **mascarados** no arquivo exportado. O arquivo sai do sistema, então essa proteção é aplicada por padrão.

## Limites e privacidade

- A sessão expira sozinha e há um teto de sessões simultâneas por organização.
- O tráfego capturado fica num armazenamento temporário com expiração automática — a captura não é um repositório de eventos.
- Segredos (tokens, senhas, chaves de API) são removidos antes da gravação, mesmo quando aparecem no meio de um texto.
- A captura mostra tráfego real de cliente: trate a tela e os arquivos exportados com o mesmo cuidado dos dados de produção.

## Próximos passos

- [Campos novos (drift)](../pipelines/drift.md) — descobrir o que o fornecedor manda e você ainda não usa.
- [Roteamento](../outputs/routing.md) — as regras que decidem o desfecho de cada evento.
- [Quarentena](./quarantine.md) — reprocessar o que ficou retido.
