---
sidebar_position: 11
title: Destinos — catálogo e tipos disponíveis
description: O que são destinos no CentralOps, quais tipos existem e como o administrador adiciona um destino pela interface.
---

# Destinos: catálogo e tipos disponíveis

Um **destino** é para onde o CentralOps envia os eventos já normalizados depois de coletá-los: um SIEM, um servidor Syslog, um data lake, etc. Cada destino aparece no **catálogo** da plataforma com um nome legível e os campos que você precisa preencher para conectá-lo.

Esta página explica, para o administrador, o que o catálogo de destinos oferece, quais tipos existem hoje e como configurar um destino pela interface web. A tela **Operação -> Destinos** só aparece para administradores.

## Quando usar

- **Encaminhar alertas para o SIEM do cliente** — você quer que os eventos normalizados cheguem ao Elasticsearch/OpenSearch usado pelo time de SOC para busca e dashboards.
- **Atender um sistema legado de logs** — a operação ainda depende de um servidor Syslog antigo, e você precisa entregar os eventos nesse formato.
- **Manter uma cópia de longo prazo para auditoria** — você quer guardar os eventos em um repositório de objetos (data lake) para retenção e investigações futuras.

## Como funciona o catálogo

O catálogo é a lista de **tipos de destino** que a plataforma sabe falar. Cada tipo já vem com:

- Um **nome legível** (por exemplo, "Elasticsearch / OpenSearch") que aparece quando você adiciona um destino.
- O **formulário de configuração** próprio daquele tipo — os campos mudam conforme o destino (uma URL de cluster e um índice para o Elasticsearch; um endereço e uma porta para o Syslog).
- As **capacidades** que o tipo suporta, como envio em lote, conexão segura (TLS) e remoção de eventos por identificador para atender pedidos de privacidade.

Você não precisa escrever nada: escolhe o tipo no catálogo, preenche os campos e salva. A plataforma cuida de formatar e enviar os eventos no formato que cada serviço espera.

## Tipos de destino disponíveis

| Tipo | Para que serve | Campos típicos |
|------|----------------|----------------|
| **Elasticsearch / OpenSearch** | Enviar os eventos ao SIEM do cliente para busca e dashboards. | URL do cluster, índice de destino, forma de autenticação, conexão segura (TLS). |
| **Syslog (RFC 3164)** | Encaminhar eventos a um servidor Syslog, inclusive sistemas legados. | Endereço do servidor, porta, conexão segura (TLS). |
| **Repositório de objetos / data lake** | Guardar uma cópia dos eventos para retenção e auditoria de longo prazo. | Localização do repositório e credencial de acesso. |

> A lista exata exibida no catálogo depende da versão instalada e do que a equipe da plataforma habilitou no seu ambiente. Se um tipo que você precisa não aparece, fale com o administrador da plataforma.

## Como adicionar um destino (administrador)

1. Abra o menu **Operação -> Destinos**.
2. Inicie a criação de um novo destino.
3. **Escolha o tipo** no catálogo (por exemplo, Elasticsearch / OpenSearch ou Syslog).
4. **Preencha os campos** do formulário daquele tipo. As credenciais sensíveis (chave de API, usuário e senha) são guardadas com segurança pela plataforma; você não vê o valor depois de salvo.
5. Marque se a conexão deve usar **TLS** (recomendado para qualquer destino exposto na rede).
6. Use **Testar conexão** para validar endereço, porta e credencial antes de salvar. O teste apenas verifica a conexão — nenhum evento é enviado nessa etapa.
7. **Salve** o destino. A partir daí ele fica disponível para receber eventos conforme as regras definidas em **Operação -> Roteamento**.

Depois de salvo, o destino aparece na própria tela de **Destinos** com seu status. Para começar a enviar eventos para ele, configure uma regra que o aponte em **Operação -> Roteamento**.

### Preço por GB (opcional)

O formulário tem um bloco **Custo (FinOps)** com dois campos que **não interferem na entrega** — nenhum evento muda de caminho, de formato ou de velocidade por causa deles:

| Campo | O que informar |
|-------|----------------|
| **Preço por GB** | Quanto este destino cobra por GB ingerido (o valor do contrato com o SIEM, o lake, etc.). |
| **Moeda** | O código de três letras da moeda desse preço (por exemplo, `USD` ou `BRL`). |

O preço serve só para converter volume em dinheiro no card **Redução de volume & custo**: sem ele, a plataforma continua mostrando quantos bytes foram evitados, mas não tem como dizer quanto isso vale. A conversão em valor monetário é recurso **Enterprise**.

Deixar o campo em branco não quebra nada, e o card não mente por isso: em vez de exibir "zero" — que se leria como "não economizamos nada" — ele avisa que o **preço por GB não está configurado** no destino. Se você quer o número em dinheiro, preencha o preço em cada destino que cobra por volume.

Veja [Redução de volume & custo](./reducao-de-volume.md) para entender o que entra nessa conta.

## Entrega, reenvio e proteção contra falhas

Você não precisa gerenciar nada disso manualmente, mas é útil saber o que a plataforma faz nos bastidores:

- **Envio em lote** — os eventos são enviados em grupos, não um a um, para mais desempenho.
- **Fila de reenvio** — se um evento for rejeitado pelo destino (por exemplo, credencial inválida ou evento grande demais), ele vai para uma fila de reenvio em vez de ser perdido. Você acompanha e pode reprocessar essa fila pela tela de **Destinos**.
- **Proteção contra destino instável** — se um destino começar a falhar repetidamente, a plataforma pausa os envios para ele por um tempo e tenta de novo depois, evitando acúmulo de erros.
- **Remoção por identificador** — para atender pedidos de privacidade (LGPD), alguns tipos de destino permitem apagar eventos específicos já entregues.

## Resolução de problemas

| Sintoma na interface | Provável causa | O que fazer |
|----------------------|----------------|-------------|
| "Testar conexão" falha com erro de autenticação | Chave de API, usuário ou senha incorretos. | Confira as credenciais e salve de novo. |
| "Testar conexão" falha com erro de conexão / tempo esgotado | Endereço ou porta errados, ou o destino está inacessível pela rede. | Verifique endereço e porta; confirme com o responsável pelo destino se ele está acessível. |
| Erro relacionado a certificado ao usar TLS | O certificado do destino não é confiável no ambiente. | Confirme com o administrador da plataforma; o ajuste de certificados é feito no ambiente. |
| Eventos parados na fila de reenvio | O destino rejeitou os eventos ou ficou indisponível. | Corrija a causa (credencial, formato, disponibilidade) e reprocese a fila de reenvio na tela de **Destinos**. |

## Adicionar um novo tipo de destino ao catálogo

Adicionar um **tipo** de destino novo ao catálogo (por exemplo, um serviço que ainda não está na lista acima) não é feito pela interface. É um trabalho da equipe da plataforma, que implementa o conector e o disponibiliza em uma nova versão.

Se você precisa entregar eventos a um serviço que não aparece no catálogo, fale com o administrador da plataforma para avaliar a inclusão. Já configurar um destino de um tipo **existente** é feito por você mesmo, na tela **Operação -> Destinos**, como descrito acima.
