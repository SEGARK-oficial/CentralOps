---
sidebar_position: 5
title: Referência de endpoints
description: Todos os endpoints da API agrupados pelo caminho que um evento percorre, com a permissão exigida por cada um.
---

# Referência de endpoints

São 217 operações. Esta página agrupa todas pelo estágio do pipeline a que pertencem, porque é assim que você vai procurar na prática: o problema aparece em um estágio, e você quer os endpoints daquele estágio.

## Como ler as tabelas

A coluna **Permissão** traz o que o endpoint exige **além** de um token válido. Ela foi extraída da árvore de dependências da aplicação, não de leitura de código, então bate com o que roda.

- Uma permissão nomeada (`integration.write`, por exemplo) significa que o papel do dono do token precisa tê-la, e o token não pode tê-la recortado por scope.
- **`autenticado`** significa que basta um token válido. O recorte por organização continua valendo: você só vê o que está no seu escopo.
- **`admin global`** significa que não basta ser admin, é preciso escopo de plataforma. Admin de uma organização recebe `403`.

:::caution[Quase metade da API é de administrador]
92 das 217 operações exigem `user.manage`, a permissão de criar e remover usuários. Isso inclui coisas que não parecem administrativas, como toda a família de enriquecimento e boa parte da escrita de destinos e rotas.

Vale conferir esta página **antes** de decidir o papel de um token de automação. Se a tarefa cair numa dessas 92, não existe recorte por scope que resolva: o papel do dono precisa ser admin.
:::

Os caminhos estão exatamente como o servidor os declara, incluindo a barra final quando existe. Copie daqui em vez de digitar, por causa do redirecionamento explicado em [Convenções](conventions.md#barra-no-final-da-url).

## Coleta

Tudo que traz evento para dentro: as integrações com fornecedores, o estado de cada coletor, recoleta histórica e a ingestão por push.

É aqui que você olha quando a pergunta é "o dado está entrando?".

**Pontos de atenção nesta área:**

- `POST /api/collectors/integrations/{id}/trigger` dispara coleta de verdade contra a API do fornecedor e consome a quota dele. A resposta devolve só o identificador da tarefa, não o resultado.
- `reset-cursor` faz o coletor recomeçar a partir da janela padrão do fornecedor, tipicamente uma hora atrás. Isso gera duplicidade temporária até o prazo de deduplicação passar. É a operação certa para destravar um coletor parado, e é a única desta família que usa `integration.reset`.
- O backfill tem limites rígidos: janela máxima de 90 dias, e a data inicial não pode estar mais de 90 dias no passado. Integração pai de MSSP é recusada com `422`, porque ela não tem fluxo próprio.
- `test-connection` abre conexão real com o fornecedor e registra o resultado no histórico de saúde.
- `DELETE` de integração tem limite de 5 por minuto por usuário. Uma integração pai com filhas ativas devolve `409`, a menos que você peça cascata.
- `pipeline-health` não consulta o fornecedor: ele agrega o que já está no banco, com cache de 60 segundos **por usuário**. Duas leituras seguidas devolvem o mesmo instante de cache, então um número parado pode ser cache e não estagnação.
- Duas rotas degradam em silêncio sem a edição Enterprise: sincronizar tenants responde `200` com um aviso no corpo em vez de erro, e a seleção de tenants grava a escolha sem materializar nada.

#### integrations (18)

| Endpoint | Permissão |
|---|---|
| `GET /api/integrations/` | autenticado |
| `POST /api/integrations/` | `integration.write` |
| `POST /api/integrations/bulk/deactivate` | `integration.write` |
| `GET /api/integrations/platforms` | autenticado |
| `DELETE /api/integrations/{integration_id}` | `integration.write` |
| `GET /api/integrations/{integration_id}` | autenticado |
| `PUT /api/integrations/{integration_id}` | `integration.write` |
| `PATCH /api/integrations/{integration_id}/auto-approve-policy` | `integration.write` |
| `GET /api/integrations/{integration_id}/collection-filters` | `integration.read` |
| `PUT /api/integrations/{integration_id}/collection-filters` | `integration.write` |
| `GET /api/integrations/{integration_id}/discovered-tenants` | autenticado |
| `GET /api/integrations/{integration_id}/health` | autenticado |
| `GET /api/integrations/{integration_id}/overview` | autenticado |
| `GET /api/integrations/{integration_id}/sophos-tenants` | `integration.read` |
| `GET /api/integrations/{integration_id}/sync-status` | autenticado |
| `POST /api/integrations/{integration_id}/sync-tenants` | `integration.write` |
| `POST /api/integrations/{integration_id}/tenants/select` | `integration.write` |
| `POST /api/integrations/{integration_id}/test-connection` | `integration.write` |

#### collectors (7)

| Endpoint | Permissão |
|---|---|
| `GET /api/collectors/cost-summary` | autenticado |
| `GET /api/collectors/platforms-streams` | autenticado |
| `GET /api/collectors/state` | autenticado |
| `DELETE /api/collectors/state/{integration_id}/{stream}/cursor` | `integration.reset` |
| `POST /api/collectors/state/{integration_id}/{stream}/trigger` | autenticado |
| `GET /api/collectors/summary` | autenticado |
| `GET /api/collectors/vendors` | autenticado |

#### providers (3)

| Endpoint | Permissão |
|---|---|
| `GET /api/providers/platforms` | autenticado |
| `GET /api/providers/query-capabilities` | autenticado |
| `POST /api/providers/{platform}/test-connection` | autenticado |

#### backfill (5)

| Endpoint | Permissão |
|---|---|
| `GET /api/backfill-jobs/diagnostics` | `user.manage` |
| `GET /api/backfill-jobs/{job_id}` | `integration.read` |
| `POST /api/backfill-jobs/{job_id}/cancel` | `integration.write` |
| `POST /api/integrations/{integration_id}/backfill` | `integration.write` |
| `GET /api/integrations/{integration_id}/backfill-jobs` | `integration.read` |

#### collector-config (10)

| Endpoint | Permissão |
|---|---|
| `GET /api/collectors/config` | `user.manage` |
| `PUT /api/collectors/config` | `user.manage` |
| `GET /api/collectors/config/capture-sessions` | `user.manage` |
| `POST /api/collectors/config/capture-sessions` | `user.manage` |
| `DELETE /api/collectors/config/capture-sessions/{session_id}` | `user.manage` |
| `GET /api/collectors/config/capture-sessions/{session_id}/events` | `user.manage` |
| `GET /api/collectors/config/capture-sessions/{session_id}/events/{event_id}` | `user.manage` |
| `GET /api/collectors/config/capture-sessions/{session_id}/export` | `user.manage` |
| `POST /api/collectors/config/capture-sessions/{session_id}/stop` | `user.manage` |
| `GET /api/collectors/config/capture-vendors` | `user.manage` |

#### ingest (4)

| Endpoint | Permissão |
|---|---|
| `GET /api/ingest/integrations/{integration_id}` | `user.manage` |
| `DELETE /api/ingest/integrations/{integration_id}/token` | `user.manage` |
| `POST /api/ingest/integrations/{integration_id}/token` | `user.manage` |
| `POST /api/ingest/{stream}` | autenticado |

#### pipeline-health (2)

| Endpoint | Permissão |
|---|---|
| `GET /api/integrations/pipeline-health` | `integration.read` |
| `GET /api/integrations/{integration_id}/pipeline-health` | `integration.read` |

## Normalização

Como o evento cru vira OCSF, e o que fazer quando não vira: mappings e versões, campos novos detectados, e a quarentena.

É aqui que você olha quando o dado entrou mas não chegou ao destino no formato certo.

**Pontos de atenção nesta área:**

- Descartar quarentena é remoção definitiva, sem lixeira. O registro de auditoria e a remoção acontecem na mesma transação.
- Descartar e reprocessar compartilham a **mesma** permissão. Quem pode reprocessar pode apagar.
- O descarte em lote aceita até 500 identificadores. Identificador de outra organização aparece como "não encontrado" na lista de erros, em vez de `403`, para não permitir varredura.
- Se o reprocessamento falhar na normalização, o evento continua na quarentena com o erro atualizado. Ele não é marcado como reprocessado.
- Eventos em quarentena expiram sozinhos pelo prazo de retenção, por padrão 7 dias.
- Publicar uma versão de mapping vale para os coletores em cerca de 30 segundos. Não há janela de confirmação.

#### mappings (12)

| Endpoint | Permissão |
|---|---|
| `GET /api/mappings` | `mapping.read` |
| `POST /api/mappings/dry-run` | `mapping.read` |
| `GET /api/mappings/normalize/type-casts` | `mapping.read` |
| `GET /api/mappings/samples` | `mapping.read` |
| `GET /api/mappings/{definition_id}` | `mapping.read` |
| `GET /api/mappings/{definition_id}/audit` | `audit.read` |
| `GET /api/mappings/{definition_id}/discover-fields` | `mapping.read` |
| `POST /api/mappings/{definition_id}/rollback` | `mapping.rollback` |
| `GET /api/mappings/{definition_id}/versions` | `mapping.read` |
| `POST /api/mappings/{definition_id}/versions` | `mapping.write` |
| `GET /api/mappings/{definition_id}/versions/{version_a_id}/diff/{version_b_id}` | `mapping.read` |
| `GET /api/mappings/{definition_id}/versions/{version_id}` | `mapping.read` |

#### ocsf (3)

| Endpoint | Permissão |
|---|---|
| `GET /api/ocsf/compliance` | `user.manage` |
| `GET /api/ocsf/policies` | `user.manage` |
| `PUT /api/ocsf/policies/{org_id}` | `user.manage` |

#### drift (6)

| Endpoint | Permissão |
|---|---|
| `GET /api/drift` | `drift.read` |
| `POST /api/drift/bulk/ignore` | `drift.ignore` |
| `POST /api/drift/bulk/mark_mapped` | `drift.mark_mapped` |
| `DELETE /api/drift/{field_id}` | `drift.delete` |
| `POST /api/drift/{field_id}/ignore` | `drift.ignore` |
| `POST /api/drift/{field_id}/mark_mapped` | `drift.mark_mapped` |

#### quarantine (7)

| Endpoint | Permissão |
|---|---|
| `GET /api/quarantine` | `quarantine.read` |
| `POST /api/quarantine/bulk/discard` | `quarantine.discard` |
| `GET /api/quarantine/bulk/ids` | `quarantine.read` |
| `POST /api/quarantine/bulk/reprocess` | `quarantine.discard` |
| `GET /api/quarantine/{event_id}` | `quarantine.read` |
| `POST /api/quarantine/{event_id}/discard` | `quarantine.discard` |
| `POST /api/quarantine/{event_id}/reprocess` | `quarantine.discard` |

## Enriquecimento

Acrescenta contexto ao evento já normalizado, antes de ele ser roteado.

**Pontos de atenção nesta área:**

- A família inteira exige `user.manage`. Não há permissão de leitura separada para enriquecimento.
- Tabela e política pertencem **sempre** a uma organização. Não existe recurso global aqui, e a organização é obrigatória.
- Compartilhar uma fonte entre organizações exige a edição Enterprise. Sem ela, a resposta é `403` com o código `enrichment.source_sharing_requires_enterprise`.
- O ensaio a seco (`dry-run`) exercita apenas as regras locais. As regras que consultam serviço externo voltam marcadas como ignoradas, para não gastar cota a cada tecla do operador. **Um ensaio verde não prova que a fonte remota responde**; para isso existe o teste da fonte.
- As métricas têm teto de 180 minutos, porque a série viva só guarda três horas. Pedir uma janela maior devolveria zero, que é indistinguível de "a regra não disparou".
- Uma regra inválida é recusada na publicação, com `422`. Ela não falha depois, no processamento.

#### enrichment (24)

| Endpoint | Permissão |
|---|---|
| `GET /api/collectors/enrichment/activity` | `user.manage` |
| `POST /api/collectors/enrichment/dry-run` | `user.manage` |
| `GET /api/collectors/enrichment/enrichers` | `user.manage` |
| `GET /api/collectors/enrichment/key-sources` | `user.manage` |
| `GET /api/collectors/enrichment/metrics` | `user.manage` |
| `GET /api/collectors/enrichment/policies` | `user.manage` |
| `POST /api/collectors/enrichment/policies` | `user.manage` |
| `POST /api/collectors/enrichment/policies/{policy_id}/enable` | `user.manage` |
| `POST /api/collectors/enrichment/policies/{policy_id}/rollback` | `user.manage` |
| `GET /api/collectors/enrichment/policies/{policy_id}/versions` | `user.manage` |
| `POST /api/collectors/enrichment/policies/{policy_id}/versions` | `user.manage` |
| `GET /api/collectors/enrichment/policies/{policy_id}/versions/{version_id}` | `user.manage` |
| `GET /api/collectors/enrichment/sources` | `user.manage` |
| `POST /api/collectors/enrichment/sources` | `user.manage` |
| `DELETE /api/collectors/enrichment/sources/{source_id}` | `user.manage` |
| `PATCH /api/collectors/enrichment/sources/{source_id}` | `user.manage` |
| `POST /api/collectors/enrichment/sources/{source_id}/test` | `user.manage` |
| `GET /api/collectors/enrichment/tables` | `user.manage` |
| `POST /api/collectors/enrichment/tables` | `user.manage` |
| `DELETE /api/collectors/enrichment/tables/{table_id}` | `user.manage` |
| `GET /api/collectors/enrichment/tables/{table_id}` | `user.manage` |
| `POST /api/collectors/enrichment/tables/{table_id}/rollback` | `user.manage` |
| `GET /api/collectors/enrichment/tables/{table_id}/versions` | `user.manage` |
| `POST /api/collectors/enrichment/tables/{table_id}/versions` | `user.manage` |

## Roteamento e entrega

Para onde o evento vai e se chegou: regras de rota, destinos, saúde da entrega e fila de mortos.

É aqui que você olha quando o dado foi normalizado mas não apareceu no SIEM.

**Pontos de atenção nesta área:**

- Criar um destino habilitado cria junto uma rota que já começa a receber evento no ciclo seguinte. Não existe passo de ativação.
- Toda rota criada ou editada vale no próximo ciclo de despacho, sem confirmação.
- Reordenar rotas exige a lista completa e ordenada. Um identificador fora do seu escopo devolve `403`, e um inexistente devolve `404` desfazendo tudo.
- `POST /{id}/test` abre conexão real com o destino e decifra a credencial em memória pela duração do teste.
- Revogar a credencial de um destino também **desabilita** o destino. A entrega para até você configurar a nova chave.
- Reprocessar a fila de mortos de um destino global reenvia os eventos de **todas** as organizações.
- Credenciais nunca aparecem em resposta, log ou auditoria. As leituras expõem apenas se existe credencial, não qual é.
- A linhagem depende de um recurso opcional. Com ele desligado, a resposta é uma lista **vazia**, não um erro: vazio aqui não significa "não entregou". Ela vive em cache com prazo de cerca de 7 dias e não serve como arquivo de conformidade.
- Na saúde de uma rota, zero eventos casados aparece como ociosa, não como doente.

#### routes (13)

| Endpoint | Permissão |
|---|---|
| `GET /api/collectors/routes` | `route.read` |
| `POST /api/collectors/routes` | `user.manage` |
| `POST /api/collectors/routes/dry-run` | `user.manage` |
| `GET /api/collectors/routes/flow` | `route.read` |
| `POST /api/collectors/routes/reorder` | `user.manage` |
| `GET /api/collectors/routes/topology` | `route.read` |
| `DELETE /api/collectors/routes/{route_id}` | `user.manage` |
| `GET /api/collectors/routes/{route_id}` | `route.read` |
| `PUT /api/collectors/routes/{route_id}` | `user.manage` |
| `GET /api/collectors/routes/{route_id}/audit` | `route.read` |
| `GET /api/collectors/routes/{route_id}/health` | `route.read` |
| `GET /api/collectors/routes/{route_id}/metrics` | `route.read` |
| `POST /api/collectors/routes/{route_id}/rollback` | `user.manage` |

#### destinations (20)

| Endpoint | Permissão |
|---|---|
| `GET /api/collectors/destinations` | `destination.read` |
| `POST /api/collectors/destinations` | `user.manage` |
| `GET /api/collectors/destinations/destination-types` | `destination.read` |
| `GET /api/collectors/destinations/health` | `destination.read` |
| `DELETE /api/collectors/destinations/{destination_id}` | `user.manage` |
| `GET /api/collectors/destinations/{destination_id}` | `destination.read` |
| `PUT /api/collectors/destinations/{destination_id}` | `user.manage` |
| `GET /api/collectors/destinations/{destination_id}/audit` | `destination.read` |
| `GET /api/collectors/destinations/{destination_id}/credential/audit` | `user.manage` |
| `POST /api/collectors/destinations/{destination_id}/credential/revoke` | `user.manage` |
| `POST /api/collectors/destinations/{destination_id}/credential/rotate` | `user.manage` |
| `GET /api/collectors/destinations/{destination_id}/dlq` | `destination.read` |
| `POST /api/collectors/destinations/{destination_id}/dlq/reprocess` | `user.manage` |
| `GET /api/collectors/destinations/{destination_id}/health` | `destination.read` |
| `GET /api/collectors/destinations/{destination_id}/lineage` | `destination.read` |
| `GET /api/collectors/destinations/{destination_id}/metrics` | `destination.read` |
| `POST /api/collectors/destinations/{destination_id}/shadow` | `user.manage` |
| `GET /api/collectors/destinations/{destination_id}/tap` | `user.manage` |
| `POST /api/collectors/destinations/{destination_id}/test` | `user.manage` |
| `GET /api/collectors/lineage/{event_id}` | `user.manage` |

## Consulta e detecção

Consulta ao vivo na fonte do cliente, agendamentos, resultados e triagem de detecções.

**Pontos de atenção nesta área:**

- `query.run` executa consulta no ambiente do fornecedor. Isso pode gerar custo e carga no cliente. Não coloque essa permissão em token de automação que não precisa dela.
- Criar agendamento é o gatilho de execução recorrente, e o resultado sai por e-mail só para destinatários da mesma organização.
- O eixo de permissão muda dentro da mesma família: criar agendamento exige `query.save`, mas listar e ver histórico exigem `mapping.read`.
- Ler o histórico de resultados **poda** resultados vencidos como efeito colateral.
- O arquivo CSV de um resultado expira antes do resultado em si. Passado o prazo, o download devolve `410` enquanto o JSON continua visível.
- Agendamento fora do seu escopo devolve `404`, não `403`, para não confirmar que existe.
- Em detecções, ler exige apenas token válido, mas mudar o status exige `query.run`.

#### queries (5)

| Endpoint | Permissão |
|---|---|
| `GET /api/queries/` | `mapping.read` |
| `POST /api/queries/` | `query.save` |
| `DELETE /api/queries/{query_id}` | `query.save` |
| `GET /api/queries/{query_id}` | `mapping.read` |
| `PUT /api/queries/{query_id}` | `query.save` |

#### schedules (4)

| Endpoint | Permissão |
|---|---|
| `GET /api/schedules/` | `mapping.read` |
| `POST /api/schedules/` | `query.save` |
| `DELETE /api/schedules/{sched_id}` | `query.save` |
| `GET /api/schedules/{sched_id}/history` | `mapping.read` |

#### results (3)

| Endpoint | Permissão |
|---|---|
| `GET /api/search/history` | autenticado |
| `GET /api/search/history/result/{search_id}` | autenticado |
| `GET /api/search/history/result/{search_id}/csv` | autenticado |

#### history (3)

| Endpoint | Permissão |
|---|---|
| `GET /api/history/` | autenticado |
| `GET /api/history/audit` | `user.manage` |
| `GET /api/history/audit/csv` | `user.manage` |

#### detections (3)

| Endpoint | Permissão |
|---|---|
| `GET /api/detections` | autenticado |
| `GET /api/detections/{detection_id}` | autenticado |
| `PATCH /api/detections/{detection_id}` | `query.run` |

## Administração

Usuários, organizações, tokens, contas de serviço, identidade, licença e configuração da plataforma.

**Pontos de atenção nesta área:**

- Apagar os dados de uma organização é irreversível e exige um texto de confirmação exato no corpo. A limpeza do índice de busca é feita com melhor esforço: se ele estiver fora do ar, o trabalho termina como parcial e o resto conclui.
- Apagar uma organização exige escopo **global**. Ser admin daquela organização não basta.
- Criar organização encosta na licença, porque respeita o teto de organizações do plano.
- Ativar ou desativar licença muda em tempo real o que a instância inteira pode fazer, e exige admin com escopo global. Um token de licença inválido é recusado com `400` e nunca é armazenado.
- No estado da licença, `expired_in_grace` verdadeiro significa licença **já vencida**, ainda dentro da carência. As funções continuam ligadas e vão cair depois. Tratar isso como "tudo certo" é o erro clássico.
- O histórico geral pede apenas token válido, mas a trilha de auditoria e o CSV dela exigem `user.manage`.
- A captura ao vivo grava **eventos reais do cliente**, com dados pessoais. A exportação vem mascarada por padrão, e desligar a máscara tira o dado cru do sistema.
- A verificação do IRIS faz chamada de saída a cada chamada, então não convém colocá-la em monitor de alta frequência. O estado "não configurado" é válido e não é erro.

#### auth (18)

| Endpoint | Permissão |
|---|---|
| `GET /api/auth/admin-access` | `user.manage` |
| `POST /api/auth/bootstrap` | autenticado |
| `POST /api/auth/login` | autenticado |
| `POST /api/auth/logout` | autenticado |
| `GET /api/auth/me` | autenticado |
| `PATCH /api/auth/me` | autenticado |
| `PUT /api/auth/me/locale` | autenticado |
| `POST /api/auth/me/password` | autenticado |
| `GET /api/auth/me/profile` | autenticado |
| `POST /api/auth/me/sessions/revoke-others` | autenticado |
| `GET /api/auth/permissions` | autenticado |
| `GET /api/auth/sso/callback` | autenticado |
| `GET /api/auth/sso/login` | autenticado |
| `GET /api/auth/status` | autenticado |
| `GET /api/auth/users` | `user.manage` |
| `POST /api/auth/users` | `user.manage` |
| `DELETE /api/auth/users/{user_id}` | `user.manage` |
| `PUT /api/auth/users/{user_id}` | `user.manage` |

#### organizations (11)

| Endpoint | Permissão |
|---|---|
| `GET /api/organizations/` | autenticado |
| `POST /api/organizations/` | `user.manage` |
| `POST /api/organizations/bulk/deactivate` | `user.manage` |
| `DELETE /api/organizations/{org_id}` | `user.manage` |
| `GET /api/organizations/{org_id}` | autenticado |
| `PUT /api/organizations/{org_id}` | `user.manage` |
| `GET /api/organizations/{org_id}/customer-mappings` | `org.manage` |
| `DELETE /api/organizations/{org_id}/data` | `org.manage` |
| `GET /api/organizations/{org_id}/retention` | `integration.read` |
| `PUT /api/organizations/{org_id}/retention` | `org.manage` |
| `POST /api/organizations/{org_id}/sync-iris-customer` | `org.manage` |

#### api-tokens (4)

| Endpoint | Permissão |
|---|---|
| `GET /api/v1/tokens` | autenticado |
| `POST /api/v1/tokens` | autenticado |
| `GET /api/v1/tokens/scopes` | autenticado |
| `DELETE /api/v1/tokens/{token_id}` | autenticado |

#### service-accounts (8)

| Endpoint | Permissão |
|---|---|
| `GET /api/v1/service-accounts` | `user.manage` |
| `POST /api/v1/service-accounts` | `user.manage` |
| `DELETE /api/v1/service-accounts/{service_account_id}` | `user.manage` |
| `GET /api/v1/service-accounts/{service_account_id}` | `user.manage` |
| `PATCH /api/v1/service-accounts/{service_account_id}` | `user.manage` |
| `GET /api/v1/service-accounts/{service_account_id}/tokens` | `user.manage` |
| `POST /api/v1/service-accounts/{service_account_id}/tokens` | `user.manage` |
| `DELETE /api/v1/service-accounts/{service_account_id}/tokens/{token_id}` | `user.manage` |

#### identity (5)

| Endpoint | Permissão |
|---|---|
| `GET /api/identity/config` | `user.manage` |
| `PUT /api/identity/config` | `user.manage` |
| `POST /api/identity/config/sync` | `user.manage` |
| `GET /api/identity/config/sync-status` | `user.manage` |
| `POST /api/identity/config/test` | `user.manage` |

#### sso (2)

| Endpoint | Permissão |
|---|---|
| `GET /api/auth/sso/callback` | autenticado |
| `GET /api/auth/sso/login` | autenticado |

#### emails (6)

| Endpoint | Permissão |
|---|---|
| `GET /api/emails/` | `user.manage` |
| `POST /api/emails/` | `user.manage` |
| `GET /api/emails/config` | `user.manage` |
| `PUT /api/emails/config` | `user.manage` |
| `POST /api/emails/test` | `user.manage` |
| `DELETE /api/emails/{email_id}` | `user.manage` |

#### licenses (3)

| Endpoint | Permissão |
|---|---|
| `DELETE /api/licenses` | `user.manage` |
| `POST /api/licenses/activate` | `user.manage` |
| `GET /api/licenses/status` | autenticado |

#### edition (1)

| Endpoint | Permissão |
|---|---|
| `GET /api/edition` | autenticado |

#### config-bundle (2)

| Endpoint | Permissão |
|---|---|
| `GET /api/collectors/config/export` | `user.manage` |
| `POST /api/collectors/config/import` | `user.manage` |

#### dashboard (1)

| Endpoint | Permissão |
|---|---|
| `GET /api/dashboard/summary` | autenticado |

#### iris (1)

| Endpoint | Permissão |
|---|---|
| `GET /api/iris/health` | `org.manage` |

## Interno

Resolução de tenant entre serviços. **Não é superfície pública**, e não deve ser usada por integração de cliente.

Ela aceita dois modos de autenticação: token com `internal.tenant.read`, ou uma chave compartilhada em header, que a própria documentação do código marca como obsoleta. Se os dois vierem, o token vence. A resposta traz caminho de credencial e identificador do fornecedor, ou seja, dado sensível de tenant.

#### internal (3)

| Endpoint | Permissão |
|---|---|
| `GET /api/internal/tenants/by-iris-customer/{iris_customer_id}` | autenticado |
| `GET /api/internal/tenants/by-sophos-tenant/{external_id}` | autenticado |
| `GET /api/internal/tenants/{organization_id}` | autenticado |

## A lista sempre atual

Esta página é escrita à mão e pode ficar para trás do código. A fonte que nunca fica é o esquema publicado pela própria instância. Veja [Esquema OpenAPI](openapi.md) para consultá-lo e gerar um cliente.
