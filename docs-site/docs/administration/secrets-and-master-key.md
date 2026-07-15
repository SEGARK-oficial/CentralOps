---
sidebar_position: 4
title: Credenciais e segredos das integrações
description: Como o CentralOps protege as credenciais das integrações e como substituir um segredo comprometido pela interface
---

# Credenciais e segredos das integrações

Toda integração conectada ao CentralOps (Sophos, Wazuh, etc.) precisa de credenciais para coletar eventos: chaves de API, segredos de cliente, tokens OAuth ou senhas. O CentralOps guarda essas credenciais criptografadas e nunca mostra o valor real na tela — você só vê que o segredo existe e quando foi atualizado pela última vez.

**Público**: administrador da plataforma.

## Quando usar

| Cenário | O que fazer |
|---------|-------------|
| Um Client Secret da Sophos vazou em um canal de chat e precisa ser invalidado | Gere uma credencial nova no painel do fornecedor e substitua o segredo pela tela de edição da integração (ver abaixo). |
| Durante uma auditoria de SOC você precisa confirmar quais integrações têm credencial configurada e quando foram atualizadas | Use a lista de integrações em **Visão geral -> Integrações** para conferir o estado das credenciais. |
| Um analista relata que uma integração parou de coletar logo após a troca de uma senha no fornecedor | Edite a integração e regrave a credencial atualizada; o CentralOps testa a conexão antes de salvar. |

## Como suas credenciais ficam protegidas

Você não precisa configurar nada para que isso aconteça. A plataforma cuida da proteção automaticamente:

- Todas as credenciais de integração são **criptografadas em repouso** pela plataforma.
- O valor real **nunca aparece na interface** — nem para o administrador. Você vê apenas que o segredo existe e a data da última atualização.
- Se alguém tentar adulterar o valor armazenado, ele é rejeitado em vez de ser usado.
- Senhas de **usuários da plataforma** são tratadas de forma diferente das credenciais de integração: elas usam um resumo de mão única (não podem ser recuperadas por ninguém, nem pelo administrador) e só podem ser **redefinidas** — veja **Administração -> Usuários**.

:::info[Configuração de infraestrutura]
A forma como a chave de criptografia da plataforma é gerada, armazenada e rotacionada é definida pela equipe de infraestrutura no momento do deploy. Se precisar alterá-la ou rotacioná-la, fale com o administrador da plataforma. Nada disso é feito pela interface.
:::

## Quais dados são criptografados

Os campos sensíveis de cada integração são protegidos, tanto nas colunas da tabela `integrations` quanto no **store centralizado `integration_credentials`**:

| Campo | Armazenamento | Exemplo |
|-------|---------------|---------|
| **Segredos de integração (F1b+)** | `integration_credentials.secret_ref` | Client Secret, API Key, Token OAuth, Senha de API |
| **Segredos de destino** | `destination.secret_ref` | Token Splunk HEC, AWS Access Key, Chave de Kafka |
| **Identidade (SSO)** | `identity_config.entra_client_secret` | Microsoft Entra ID client secret |
| **Email** | `email_config.smtp_password` | Senha SMTP da plataforma |
| **Threat Intel** | `threat_intel_api_key.api_key` | VirusTotal, etc. |
| **Colunas legadas** (deprecadas) | `integrations.{client_secret, manager_api_username, ...}` | Sophos/Wazuh pré-F1b (hoje NULL na maioria) |

## Conferir quais integrações têm credenciais

1. Acesse o menu **Visão geral -> Integrações**.
2. A lista mostra cada integração e seu estado de conexão.
3. Para cada integração você consegue identificar se a credencial está configurada e quando foi atualizada pela última vez.

Você nunca verá o valor do segredo — apenas a indicação de que ele existe. Isso é proposital: valores de credencial jamais são exibidos na interface.

## Substituir um segredo comprometido

Use este procedimento sempre que uma credencial vazar ou precisar ser renovada (por exemplo, um Client Secret que apareceu em um canal de chat).

1. **No painel do fornecedor**, gere uma credencial nova (ex.: um novo Client Secret na Sophos). Isso invalida a credencial antiga na origem.
2. No CentralOps, acesse **Visão geral -> Integrações** e abra a integração afetada.
3. Use a opção de **editar** a integração.
4. Preencha o novo segredo no formulário de edição.
5. Salve.

O que o CentralOps faz ao salvar:

- Cifra a nova credencial e a guarda em repouso.
- Testa a conexão com o fornecedor usando o novo segredo.
- Se o teste passar, a credencial antiga é substituída. Se o teste falhar, a credencial anterior é mantida para não derrubar a coleta.

:::tip[Boa prática]
Nunca compartilhe credenciais de fornecedor por chat ou e-mail. Sempre cadastre-as diretamente pelo formulário de edição da integração — assim elas já entram criptografadas e nunca trafegam em texto puro.
:::

## Rotação de chave de criptografia

Quando a equipe de infraestrutura rotaciona a chave de criptografia (KMS provider):

1. Todos os campos listados acima são **re-cifrados automaticamente** sem downtime.
2. O script `scripts/reencrypt_secrets.py` (P0) re-cifra:
   - `integration_credentials.secret_ref`
   - `destination.secret_ref` (novos destinos pluggable)
   - `identity_config.entra_client_secret` (Entra ID)
   - `email_config.smtp_password` (SMTP)
   - `threat_intel_api_key.api_key` (Threat Intel)
   - Colunas legadas em `integrations` (se houver)

Se um operador **não rodar `reencrypt_secrets.py`** após mudar a chave do KMS:
- Segredos cifrados com a chave ANTIGA ficam **ilegíveis** pela chave NOVA.
- Coletas e destinos param com erro de decifragem.
- ⚠️ **Crítico:** sempre rodar a re-encrypt como parte do plano de rotação.

Detalhes técnicos no script `scripts/reencrypt_secrets.py`.

## Conformidade (LGPD / GDPR e PCI DSS)

Para fins de auditoria, vale registrar que:

- As credenciais de integração ficam **criptografadas em repouso** (todas as colunas acima).
- O valor das credenciais **nunca é exibido** na interface.
- Trocas de credencial e ações administrativas relacionadas ficam registradas no **Histórico** (menu **Operação -> Histórico**).
- Se Vault está ativo (Fase 2+), cada leitura de segredo aparece em **Vault audit log** com identidade do serviço e timestamp.

Os detalhes técnicos do padrão de criptografia usado pela plataforma são definidos pela equipe de infraestrutura no deploy. Se um auditor pedir essas evidências técnicas, fale com o administrador da plataforma.

## Próximos passos

- **Roteamento e destinos?** Veja [Roteamento](../outputs/routing.md).
- **Auditoria e histórico?** Veja [Histórico e auditoria](../operations/history-audit.md).
- **Conformidade LGPD?** Veja [LGPD e GDPR](../compliance/lgpd-gdpr.md).
