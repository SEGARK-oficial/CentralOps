---
sidebar_position: 9
title: "Destino: OTLP"
description: Envie eventos normalizados (OCSF) para backends de observabilidade compatíveis com OpenTelemetry, como Grafana, Datadog, Honeycomb ou SigNoz.
---

# Destino: OTLP

O destino **OTLP** envia os eventos já normalizados do CentralOps para qualquer backend de observabilidade compatível com **OpenTelemetry** (OTLP/HTTP). É assim que você integra a plataforma a ferramentas como Grafana, Datadog, Honeycomb, SigNoz ou Jaeger sem precisar de coletores intermediários.

A configuração de destinos é feita pelo administrador da plataforma, no menu **Operação → Destinos**. Quem não tem perfil de admin não enxerga essa tela, mas qualquer analista pode acompanhar a saúde dos destinos depois de criados.

---

## Quando usar

- **Centralizar eventos de segurança no painel de observabilidade do SOC.** Sua equipe já vive no Grafana ou no Datadog para métricas e logs de infraestrutura. Enviar os eventos do CentralOps para o mesmo backend coloca os alertas de segurança ao lado dos dados de operação, sem trocar de ferramenta.
- **Correlacionar eventos de segurança com telemetria de aplicação.** Ao mandar os eventos para um backend OpenTelemetry, o time consegue cruzar uma tentativa de acesso suspeito com traces e métricas da mesma janela de tempo durante uma investigação.
- **Encaminhar para uma camada que a infraestrutura já mantém.** A equipe de infraestrutura já opera um coletor OpenTelemetry que distribui dados para outros sistemas. Você só aponta o destino do CentralOps para o endereço que ela informar e os eventos seguem o caminho já existente.

---

## O que você precisa antes de começar

Peça ao responsável pelo backend de observabilidade (ou à equipe de infraestrutura) as informações abaixo:

- **Endereço do destino (endpoint)** — a URL completa para onde os eventos serão enviados, normalmente terminando em `/v1/logs`. Exemplo: `https://otel.exemplo.com:4318/v1/logs`.
- **Token de autenticação** (se exigido) — alguns backends pedem um token para aceitar os dados.
- **Atributos de identificação** (opcional) — rótulos como `service.name` ou `deployment.environment` que ajudam a identificar a origem dos eventos no destino.

**Backends compatíveis:** Grafana, OpenTelemetry Collector, Datadog, Honeycomb, SigNoz, Jaeger e qualquer receptor que aceite OTLP/HTTP.

---

## Como configurar o destino

Todos os passos abaixo são feitos pela interface, no menu **Operação → Destinos** (visível apenas para administradores).

1. **Abrir o cadastro.** Em **Operação → Destinos**, use a ação de adicionar um novo destino.
2. **Escolher o tipo.** Selecione o tipo **OTLP/HTTP (OpenTelemetry)** e avance.
3. **Preencher os campos** (veja a tabela abaixo).
4. **Testar a conexão.** Use o botão de testar conexão. O CentralOps envia um evento de teste para o endereço informado; se o backend responder com sucesso, a conexão está válida.
5. **Salvar.** Ao salvar, o destino fica ativo e os eventos passam a ser enviados.

### Campos do formulário

| Campo | Obrigatório | O que preencher |
|-------|-------------|-----------------|
| **Nome** | Sim | Um nome para identificar o destino (ex: `OTel - Produção`). |
| **Endpoint** | Sim | O endereço completo do destino, incluindo o caminho `/v1/logs` (ex: `https://otel.exemplo.com:4318/v1/logs`). |
| **Headers** | Não | Cabeçalhos extras que o backend exija, no formato indicado no campo (ex: identificação de cliente). |
| **Atributos de recurso** | Não | Rótulos de identificação como `service.name` ou `deployment.environment`. |
| **Validar TLS** | Não | Mantém a verificação do certificado HTTPS (vem ativado por padrão). Desative apenas se o backend usar certificado próprio sem cadeia confiável. |

**Autenticação por token:** se o backend exigir um token, escolha a opção de usar um segredo armazenado durante a criação. O CentralOps adiciona o token automaticamente na requisição; cabeçalhos manuais nunca substituem essa autenticação.

:::note[Certificado de CA própria]
Se o seu backend usa uma autoridade certificadora (CA) própria, o certificado correspondente é instalado pela equipe de infraestrutura no momento do deploy. Se precisar disso, fale com o administrador da plataforma — não há upload de certificado pela interface.
:::

---

## O que é enviado para o destino

Cada evento normalizado vira um registro de log no formato OpenTelemetry. O CentralOps faz essa conversão automaticamente:

| Informação do evento | Vira, no destino |
|----------------------|------------------|
| Severidade | Nível de severidade equivalente no padrão OpenTelemetry. |
| Data/hora do evento | Carimbo de tempo do registro. |
| Mensagem | Corpo (texto) do registro de log. |
| Identificador do evento | Atributo de rastreamento, para localizar o mesmo evento depois. |
| Demais campos normalizados | Atributos do registro, preservados como chave e valor. |

Não é preciso configurar esse mapeamento: ele acontece de forma transparente para qualquer destino OTLP.

---

## O que esse destino garante

| Recurso | Disponível | Observação |
|---------|------------|------------|
| Envio por HTTPS (TLS) | Sim | Comunicação criptografada com verificação de certificado. |
| Envio em lotes | Sim | Os eventos são agrupados e enviados em blocos, o que melhora o desempenho. |
| Teste de conexão | Sim | Pelo botão de testar conexão na tela de cadastro. |
| Reenvio automático em falha | Sim | Se o destino responder com erro temporário (limite de taxa ou indisponibilidade), o CentralOps tenta novamente algumas vezes antes de desistir. |
| Remoção de duplicados | Não | O protocolo OTLP não remove duplicados. Use o identificador do evento para deduplicar no próprio backend, se precisar. |
| Apagamento retroativo (LGPD/GDPR) | Não | O OTLP não garante a remoção de eventos já entregues no destino. |

---

## Acompanhar e resolver problemas

Depois que o destino está ativo, acompanhe a entrega pela tela de **Operação → Destinos** e pela **Normalização → Saúde do Pipeline**. Quando um destino apresenta erros, o status fica visível ali — você não precisa de nenhuma ferramenta externa para diagnosticar.

A tabela abaixo lista as situações mais comuns e o que fazer **pela interface**:

| Sintoma | O que costuma significar | O que fazer |
|---------|--------------------------|-------------|
| Falha de conexão / destino inacessível | O endereço está incorreto ou o backend não está acessível pela rede. | Confirme com a equipe de infraestrutura se o endereço informado está correto e acessível, e revise o campo **Endpoint** no cadastro do destino. |
| Erro de autenticação | O token expirou ou está inválido. | Gere um novo token no backend de observabilidade e atualize o segredo do destino no cadastro. |
| Endereço recusado pelo destino | O **Endpoint** está sem o caminho esperado (ex: falta o `/v1/logs`). | Edite o destino e confirme que o endereço inclui o caminho completo informado pelo backend. |
| Limite de taxa atingido | O backend está recebendo eventos demais e pediu para reduzir o ritmo. | O CentralOps reduz o ritmo e tenta novamente sozinho. Se persistir, peça ao responsável pelo backend para aumentar o limite. |
| Destino indisponível ou reiniciando | O backend está fora do ar temporariamente. | O CentralOps reenviará automaticamente. Acompanhe o status na tela de **Destinos**; se não normalizar, avise a equipe que opera o backend. |
| Evento grande demais | O lote ou um evento individual ultrapassa o limite aceito pelo backend. | Peça ao responsável pelo backend para aumentar o limite de tamanho aceito. |
| Erro de certificado (TLS) | O certificado do backend não é confiável para o CentralOps. | Se for certificado próprio da sua organização, ele precisa ser instalado pela equipe de infraestrutura. Fale com o administrador da plataforma. |

Em todos esses casos, o CentralOps mantém os eventos não entregues em uma **fila de reenvio**, então nada é perdido enquanto o destino estiver indisponível.

---

## Exemplos de integração

### Grafana

1. No Grafana, obtenha o endereço OTLP e gere um token de acesso (nas configurações de conexões do Grafana).
2. No CentralOps, em **Operação → Destinos**, crie um destino OTLP com esse endereço e token.
3. Os eventos começam a aparecer na visualização de logs do Grafana.

### Datadog

1. No Datadog, gere uma chave de API (em configurações da organização → chaves de API).
2. Confirme com o Datadog qual é o endereço OTLP atual para ingestão de logs — esse endereço muda conforme a região e o método de ingestão, então use sempre o que estiver na documentação vigente do Datadog.
3. No CentralOps, crie um destino OTLP com o endereço e a chave informados.
4. Os eventos aparecem na seção de logs do Datadog.

### Coletor OpenTelemetry mantido pela infraestrutura

Quando a equipe de infraestrutura já opera um coletor OpenTelemetry que distribui dados para outros sistemas (por exemplo, um data lake ou um SIEM), você só precisa apontar o destino para ele:

1. Peça à equipe de infraestrutura o endereço do coletor (algo como `http://otel-collector:4318/v1/logs`).
2. No CentralOps, crie um destino OTLP com esse endereço.
3. A partir daí, o coletor cuida de distribuir os eventos para onde a infraestrutura já configurou.

---

## Próximos passos

- **Acompanhar a entrega:** veja o [Dashboard](../operations/dashboard.md) e a tela de **Normalização → Saúde do Pipeline**.
- **Configurar outros destinos:** veja a [visão geral de Destinos](./overview.md).
