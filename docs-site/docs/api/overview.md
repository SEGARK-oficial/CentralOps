---
sidebar_position: 1
title: Visão geral da API
description: Como a API do CentralOps se organiza, o que dá para automatizar com ela e por onde começar quando você já tem um token.
---

# API do CentralOps

Tudo que o console faz, a API faz. A interface web não tem atalho privado: ela autentica e chama exatamente os mesmos endpoints que você chama de um script.

Esta seção é para quem tem um token na mão (ou vai criar um) e quer automatizar alguma coisa.

## Quando usar

- **Monitoramento externo.** Um Zabbix, um Grafana ou um script de plantão que pergunta de tempos em tempos se a coleta está em dia e se a entrega está de pé.
- **Automação de operação.** Destravar um coletor parado, reprocessar eventos em quarentena, disparar uma recoleta depois de corrigir um mapping.
- **Ingestão por push.** Mandar eventos de uma fonte que o CentralOps ainda não coleta sozinho.
- **Inventário e auditoria.** Exportar o que está configurado, quem mudou o quê e quando.

## Endereço base

Todos os endpoints ficam sob `/api`, no mesmo host do console:

```
https://centralops.example.com/api
```

Não existe subdomínio separado nem porta diferente para a API. Se o console abre em `https://centralops.example.com`, a API responde em `https://centralops.example.com/api`.

:::caution[O versionamento não é uniforme, e isso é fato, não recomendação]
Só a gestão de tokens e de contas de serviço vive sob `/api/v1` (12 operações). Todo o resto responde direto em `/api`, sem número de versão no caminho.

Ou seja: `/api/v1/tokens` existe, mas `/api/v1/integrations` não. É `/api/integrations`. Quando estiver montando URLs, siga a referência em vez de assumir que `/v1` vale para tudo.
:::

## Dois tipos de token, e eles não se substituem

Esta é a confusão mais comum, e ela custa tempo porque o erro que ela produz é um `401` seco, sem explicação.

| | Token de gestão | Token de ingestão |
|---|---|---|
| **Formato** | `copsk_...` | `coi_<id da integração>_...` |
| **Serve para** | Ler e operar a plataforma inteira | Só empurrar eventos para dentro |
| **Representa** | Uma pessoa ou uma conta de serviço | Uma integração específica |
| **Respeita papel e permissão?** | Sim | Não se aplica |
| **Onde nasce** | Perfil do usuário, ou contas de serviço | Na própria integração de push |
| **Usado em** | Todos os endpoints desta seção | Só `POST /api/ingest` |

Um token `copsk_` não autentica em `/api/ingest`, e um token `coi_` não lista integração nenhuma. Se você recebeu `401` num endpoint que jurava estar certo, confira antes de tudo qual das duas famílias está no header.

A ingestão por push tem documentação própria em [Ingestão por push](../integrations/push-ingestion.md). O resto desta seção trata do token de gestão.

## O modelo mental do sistema

A API espelha o caminho que um evento percorre. Saber o caminho ajuda a achar o endpoint certo sem procurar na lista inteira:

1. **Integração** puxa (ou recebe) eventos de um fornecedor. Sophos, Wazuh, CrowdStrike, Defender e afins.
2. **Mapping** normaliza o evento cru para OCSF, o formato canônico.
3. **Enriquecimento** acrescenta contexto ao evento já normalizado.
4. **Rota** decide para onde ele vai.
5. **Destino** entrega. SIEM, data lake, syslog.

Quando alguma coisa não chega ao destino, o problema está em um desses cinco pontos, e existe um grupo de endpoints para inspecionar cada um. A [referência](reference.md) está organizada nessa ordem.

Duas caixas de escape completam o desenho:

- **Quarentena**: evento que a normalização recusou.
- **DLQ**: evento normalizado que o destino recusou.

## Formato

Requisição e resposta são JSON. Mande `Content-Type: application/json` em tudo que tenha corpo.

Alguns poucos endpoints de exportação devolvem CSV ou NDJSON. Eles estão marcados na referência.

## Por onde começar

1. [Autenticação](authentication.md): criar o token, usar no header, entender o que acontece quando ele expira.
2. [Permissões e escopo](permissions.md): decidir qual papel e quais scopes o token precisa. Vale a leitura antes de criar, porque um token nasce com o poder do dono se você não restringir.
3. [Convenções](conventions.md): paginação, formato de erro, datas. O que vale para todo endpoint.
4. [Referência](reference.md): o mapa completo, por família.
5. [Receitas](recipes.md): tarefas prontas, com `curl`, para os casos mais pedidos.

:::tip[Explorando ao vivo]
A instância publica o esquema OpenAPI e uma interface interativa. Veja [Esquema OpenAPI](openapi.md) para os endereços e para gerar um cliente na sua linguagem.
:::
