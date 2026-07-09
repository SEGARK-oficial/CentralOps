---
sidebar_position: 11
title: "Destino: ClickHouse"
description: Envie eventos normalizados ao ClickHouse pela tela de Destinos para análise e armazenamento columnar em tempo real.
---

# Destino: ClickHouse

O destino **ClickHouse** entrega os eventos já normalizados pelo CentralOps diretamente para o seu ClickHouse, um banco de dados columnar otimizado para análise de alto volume e baixa latência. Você cria e gerencia tudo pela interface, na tela de Destinos.

Esta tela só aparece para administradores da plataforma.

## Quando usar

- **Armazenar grande volume de eventos para análise em tempo real.** O ClickHouse é otimizado para ingestão e consulta rápida de bilhões de registros; ideal para SIEM e observabilidade com retenção longa.
- **Alimentar dashboards e investigação analítica.** Os eventos saem do CentralOps já normalizados e ficam prontos para você montar agregações, séries temporais e correlações no ClickHouse.
- **Integrar com ferramentas de BI e análise.** Os dados no ClickHouse podem ser consumidos por Grafana, Metabase, ou qualquer ferramenta BI que fale SQL, sem precisar replicar em outro lugar.

## O que você precisa antes de começar

Para criar o destino, tenha em mãos:

- **Endereço do ClickHouse** — a URL da sua instância, incluindo a porta HTTPS (por exemplo, `https://clickhouse.exemplo.com:8443`). Quem administra o ClickHouse fornece esse endereço.
- **Usuário** — geralmente `default` ou um usuário dedicado criado no ClickHouse.
- **Senha do usuário** — o CentralOps guarda essa senha de forma criptografada; ela nunca aparece em tela depois de salvo.
- **Banco (database) e tabela** — o banco de dados padrão é `default` e a tabela padrão é `centralops_events`. A tabela deve ter colunas que correspondam aos campos dos eventos normalizados (ou deixe "Ignorar campos desconhecidos" ligado para flexibilidade).

> A tabela no ClickHouse é criada pela equipe que administra aquele ambiente, ou você a cria antes via SQL. No CentralOps você apenas informa qual banco e tabela usar.

## Criar o destino

1. No menu lateral, abra **Visão geral -> Integrações** para confirmar que suas fontes já estão coletando, depois vá em **Operação -> Destinos**.
2. Use a opção de criar um novo destino.
3. Escolha o tipo **ClickHouse**.
4. Preencha os campos abaixo.

| Campo | O que informar |
|-------|----------------|
| **Nome** | Um nome que ajude a identificar este destino (ex.: "ClickHouse Análise"). |
| **URL** | O endereço completo da instância ClickHouse, com `https://` e a porta (ex.: `https://clickhouse.exemplo.com:8443`). |
| **Banco** | O banco de dados no ClickHouse (padrão: `default`). |
| **Tabela** | O nome da tabela (padrão: `centralops_events`). |
| **Usuário** | O usuário ClickHouse (padrão: `default`). |
| **Senha** | A senha do usuário. Fica criptografada após salvar. |
| **Ignorar campos desconhecidos** | Recomendado ligado — o CentralOps não falha se há colunas na tabela que não existem nos eventos (valores padrão são ignorados na INSERT). |
| **async_insert** | Por padrão desligado. Quando ligado, usa o modo de INSERT assíncrono do ClickHouse (ainda aguarda persistência). |
| **Verificar TLS** | Mantenha ativado para garantir uma conexão segura. |
| **CA bundle** | Caminho para certificado customizado (opcional). Normalmente definido pela equipe de infraestrutura no momento do deploy. |

### Sobre a verificação TLS e certificados próprios

Mantenha **Verificar TLS** ativado sempre que possível — ele garante que o CentralOps está mesmo falando com o seu ClickHouse.

Se o seu ClickHouse usa um certificado próprio (autoassinado ou de uma autoridade interna), a confiança nesse certificado é definida pela equipe de infraestrutura no momento do deploy. Se a conexão segura falhar por causa do certificado, fale com o administrador da plataforma para que ele registre o certificado correto. Você não precisa lidar com arquivos de certificado pela interface.

### Testar e salvar

Antes de salvar, use o botão **Testar conexão**. O CentralOps verifica, de uma só vez:

- se consegue alcançar a instância do ClickHouse;
- se o usuário e a senha são aceitos;
- se o banco e a tabela existem e têm as colunas esperadas.

Se o teste passar, salve. O destino já fica **ativo** (badge verde) e começa a receber os eventos roteados para ele.

> O teste de conexão substitui qualquer verificação manual: não é preciso checar conectividade por fora da plataforma.

## Como os eventos são entregues

Você não precisa configurar nada do funcionamento interno — ele já vem ajustado para entrega eficiente ao ClickHouse. Vale apenas entender o comportamento:

- **Envio em lotes.** Os eventos são agrupados e enviados em blocos, o que é mais eficiente do que enviar um a um. Cada linha do INSERT é um evento em formato JSON.
- **Nova tentativa automática.** Se o ClickHouse recusar ou ficar indisponível por um instante, o CentralOps tenta reenviar sozinho, esperando um pouco mais entre cada tentativa.
- **Proteção contra destino instável.** Se o ClickHouse começar a falhar de forma persistente, o CentralOps pausa o envio por um curto período e volta a tentar automaticamente.
- **Entrega ao menos uma vez.** Em uma queda no momento errado, um evento pode chegar duplicado. Cada evento carrega um identificador único (`event_id`), então você pode fazer dedup no ClickHouse usando esse campo.

Os parâmetros finos desse comportamento (tamanho de lote, número de tentativas, limites da proteção contra destino instável) são definidos pela equipe de infraestrutura no momento do deploy. Se precisar ajustá-los, fale com o administrador da plataforma.

## Acompanhar a saúde do destino

Abra **Operação -> Destinos** e selecione o destino ClickHouse.

O badge de saúde mostra a situação atual:

| Cor | Significado |
|-----|-------------|
| Verde | Eventos sendo entregues normalmente, sem itens na fila de reenvio. |
| Amarelo | Eventos chegando, mas há itens parados na fila de reenvio. |
| Vermelho | Envio pausado pela proteção contra destino instável ou ClickHouse indisponível. |
| Cinza | Destino desativado. |

Na visão do destino você acompanha as métricas em tempo real:

- **Eventos por segundo** — ritmo de entrega na última hora.
- **Volume** — quanto dado está saindo na última hora.
- **Latência média** — quanto o ClickHouse leva para responder.
- **Itens na fila de reenvio (24h)** — quantos eventos foram recusados no último dia.

Para ver os eventos que não puderam ser entregues, abra a **fila de reenvio** na visão do destino. Cada item mostra o identificador do evento, o motivo da recusa informado pelo ClickHouse, o horário e o conteúdo exato que foi rejeitado — útil para entender e corrigir a causa.

Para uma visão mais ampla de como os dados percorrem a plataforma até os destinos, use **Operação -> Fluxo de dados** e **Normalização -> Saúde do Pipeline**.

## Resolver problemas comuns

| Sintoma | O que verificar |
|---------|-----------------|
| **Não conecta ao ClickHouse** | A URL está completa, com `https://` e a porta correta? O ClickHouse está no ar? Se houver firewall entre as redes, o time de infraestrutura precisa liberar o acesso à porta. |
| **Usuário/senha recusados (erro 401 ou 403)** | O usuário existe no ClickHouse? A senha foi digitada corretamente? O usuário tem permissão de INSERT na tabela? Verifique no ClickHouse e atualize o destino. |
| **Tabela não encontrada (erro 60)** | O banco e a tabela existem? Verifique com `SELECT * FROM database.table LIMIT 1` no ClickHouse. Se não existem, crie-os primeiro. |
| **Eventos recusados por coluna faltante** | A tabela tem todas as colunas que os eventos normalizados usam, ou "Ignorar campos desconhecidos" está ligado? Se há campos novos que você quer guardar, adicione as colunas na tabela no ClickHouse. |
| **Envio pausado / "muitas requisições"** | O ClickHouse pode estar sobrecarregado. A proteção contra destino instável volta a tentar sozinha após um curto intervalo; se persistir, acione a equipe do ClickHouse para revisar carga ou limites. |
| **Falha de certificado seguro (TLS)** | Acontece quando o ClickHouse usa um certificado próprio que a plataforma ainda não reconhece. Essa confiança é configurada no deploy — fale com o administrador da plataforma. |

## Próximos passos

- **Confirmar que os dados estão chegando:** abra **Operação -> Destinos**, selecione o ClickHouse e veja as métricas de eventos por segundo.
- **Investigar eventos recusados:** abra a fila de reenvio na visão do destino.
- **Adicionar outros destinos:** veja a [visão geral de destinos](./destinations.md).
- **Decidir quais eventos vão para cada destino:** use a tela de [Roteamento](./routing.md).
