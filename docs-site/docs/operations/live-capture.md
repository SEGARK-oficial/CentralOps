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

## Ver a transformação (a trajetória do evento)

Clique em **Inspecionar** numa linha. Além do payload daquele registro, o painel
monta a **trajetória**: os registros de todos os estágios do MESMO evento, em
ordem de pipeline.

| Estágio | O que o payload é |
|---|---|
| **Coletado** | O bruto do fornecedor, antes de qualquer normalização. |
| **Roteado** | O envelope depois de normalizar, antes das transformações por destino. |
| **Entregue** | O que de fato saiu, por destino — depois de redação de PII, descarte de bruto e agregação. |

Isso existe porque o mesmo evento aparece com **três normalizações diferentes**
na lista, e antes nada as distinguia. Comparar um registro "em quarentena" com
um "entregue" era comparar coisas incomparáveis.

### O que a tela admite (e por quê)

:::warning[Não é bit a bit]
"Coletado" é o objeto **depois do parse**, não os bytes do fio. Para as fontes
que buscamos por API, o payload é desserializado no coletor: espaçamento, ordem
das chaves e escapes do fornecedor deixam de existir antes de qualquer ponto do
pipeline. Dois coletores ainda pré-processam o payload (o do Wazuh desembrulha o
resultado do índice; o do CloudWatch injeta campos). O único caminho onde os
bytes exatos existem é a ingestão por push.
:::

:::warning[O "coletado" pode não estar mais lá]
Ele é o registro mais **antigo** da trajetória e, portanto, o primeiro a sair
quando o anel de retenção enche. A tela diz isso explicitamente em vez de
mostrar um painel vazio — painel vazio seria lido como "o fornecedor não mandou
nada".
:::

:::warning[Registros pré-entrega não passaram pela redação de PII]
A redação é configurada **por rota**, e ela também alcança o bloco bruto. Um
evento descartado, amostrado para fora ou suprimido aparece aqui **em claro** —
inclusive o que o destino teria recebido redigido. Esses registros levam a
etiqueta "PII não redigida".
:::

:::warning[Com agregação, o evento individual não existe no destino]
Quando um destino tem agregação ligada, o lote é colapsado em métricas
sintéticas antes de sair. O evento original não é entregue individualmente, e
nenhum recurso da captura recupera isso. O registro é marcado como agregado.
:::

### Como saiu no fio

Quando a sessão é iniciada com a opção de wire, o registro de entrega carrega o
payload formatado para aquele destino — com um **nível de fidelidade**, porque
"o que o formatador produz" não é o byte entregue para todos os destinos:

| Nível | Significado |
|---|---|
| **Exato** | É o payload por evento. A nota diz qual é o empacotamento do lote em volta. |
| **Não determinístico** | Syslog: carimbo de tempo, hostname e PID são recalculados a cada envio — a linha exibida nunca será idêntica à entregue. |
| **Parcial** | Falta um pedaço com significado (a linha de ação do `_bulk`, que define a idempotência; o envelope de requisição do OTLP). |
| **Sem representação por evento** | O destino grava o **lote inteiro** comprimido ou colunar (S3, Security Lake). Não existe wire por evento, então **não há prévia** — mostrar um fragmento induziria à comparação errada. |

## Exportar o que foi capturado

Os botões **Exportar CSV** e **NDJSON** baixam a sessão inteira.

- **CSV** — abre direto no Excel. Uma linha por evento, com colunas de desfecho,
  rota, destino, **estágio**, **identificador do evento** e **nível de fidelidade
  do wire**. As colunas originais mantêm a ordem: planilhas e scripts que já
  consomem o arquivo continuam funcionando.
- **NDJSON** — uma linha JSON por evento, com a trajetória completa: estágio,
  tipo de payload, se passou pela redação, versão da configuração do destino no
  momento da entrega e o bloco de wire quando existir.

Use o identificador do evento para juntar as linhas dos vários estágios do mesmo
evento no arquivo exportado.

Os dados pessoais são **mascarados** no arquivo, incluindo campos OCSF por
caminho (linha de comando, nome de dispositivo, endereços). A máscara preserva a
**estrutura**: um identificador de usuário vira `[PII]` mas continua existindo,
para a correlação não se perder. Exportar **sem** máscara exige permissão de
plataforma — ver na tela e extrair um arquivo são coisas diferentes.

:::info[A captura pode ser amostrada]
Uma sessão pode ser iniciada capturando uma fração do tráfego. Quando isso
acontece, **a ausência de um evento não prova que ele não passou**. A tela
sinaliza a taxa efetiva.
:::

## Limites e privacidade

- A sessão expira sozinha e há um teto de sessões simultâneas por organização.
- O tráfego capturado fica num armazenamento temporário com expiração automática — a captura não é um repositório de eventos.
- Segredos (tokens, senhas, chaves de API) são removidos antes da gravação, mesmo quando aparecem no meio de um texto.
- A captura mostra tráfego real de cliente: trate a tela e os arquivos exportados com o mesmo cuidado dos dados de produção.

## Próximos passos

- [Campos novos (drift)](../pipelines/drift.md) — descobrir o que o fornecedor manda e você ainda não usa.
- [Roteamento](../outputs/routing.md) — as regras que decidem o desfecho de cada evento.
- [Quarentena](./quarantine.md) — reprocessar o que ficou retido.
