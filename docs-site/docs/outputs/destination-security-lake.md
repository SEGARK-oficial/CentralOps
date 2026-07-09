---
sidebar_position: 20
title: "Destino: Amazon Security Lake (OCSF Parquet)"
description: Grava eventos em Parquet OCSF no data lake da AWS — retenção longa, conformidade, análise.
---

# Destino: Amazon Security Lake (OCSF Parquet)

O destino **Amazon Security Lake** grava seus eventos normalizados como arquivos Parquet OCSF no seu data lake da AWS. Use-o para armazenamento durável nativo OCSF, análise sobre dados históricos e conformidade com retenção de longo prazo — sem duplicatas, com layout determinístico.

Esta tela só aparece para administradores da plataforma.

## Quando usar

- **Data lake OCSF nativo na AWS.** O Security Lake do seu cliente já está provisionado; você quer enviar eventos já normalizados em Parquet, pronto para consultas com Athena, QuickSight ou Glue.
- **Retenção de longo prazo em Parquet.** Você precisa guardar eventos por anos (auditoria, conformidade) em um formato de consulta aberto e eficiente (Parquet comprimido).
- **Idempotência e deduplicação garantidas.** A mesma chave (lote) sobrescreve os dados existentes — nenhuma duplicação, nenhuma galeria de versões S3 desorganizada.
- **Conformidade com schema OCSF rigoroso.** Alguns clientes exigem que eventos de segurança sejam armazenados em um format OCSF formal, já mapeado pelas custom sources do Security Lake.

## O que você precisa antes de começar

**Pré-requisito crítico:** a custom source precisa estar registrada no Security Lake da AWS. A custom source define:

- O schema OCSF esperado.
- As partições (`region`, `accountId`, `eventDay`).
- O bucket S3 e as permissões de escrita.

Quem provisiona isso é a equipe de cloud/AWS. Peça que registrem uma custom source para o CentralOps se ainda não existe.

Tenha em mãos:

- **Nome da custom source** — como ela aparece no Security Lake (ex.: `centralops`).
- **Bucket do Security Lake** — o bucket S3 onde os dados vão (ex.: `aws-security-data-lake-us-east-1-123456789012`).
- **AWS Account ID** — usado para particionar os dados.
- **Região AWS** — onde o bucket reside (ex.: `us-east-1`).
- **Credencial de acesso:**
  - **IAM Role do host** — mais simples; a infraestrutura já concede permissão à plataforma.
  - **AWS Access Key ID + Secret Access Key** — alternativa se IAM role não está disponível.

> IAM role é recomendado. Se não estiver disponível, peça ao admin AWS que gere um par de chaves com permissão `s3:PutObject` e `s3:GetObject` no bucket.

## Criar o destino

1. No menu lateral, abra **Operação → Destinos**.
2. Use a opção de criar um novo destino.
3. Escolha o tipo **Amazon Security Lake (OCSF Parquet)**.
4. Preencha os campos abaixo.

| Campo | O que informar |
|-------|----------------|
| **Nome** | Um nome que ajude a identificar (ex.: "Security Lake Prod"). |
| **Bucket** | O bucket S3 do Security Lake (ex.: `aws-security-data-lake-us-east-1-123456789012`). |
| **Account ID** | O AWS Account ID (12 dígitos, ex.: `123456789012`). |
| **Região** | A região do bucket (ex.: `us-east-1`). |
| **Custom Source** | Nome da custom source registrada no Security Lake (ex.: `centralops`). |
| **Compressão** | `zstd` (padrão) ou `snappy`. Zstd é mais eficiente. |
| **Usar IAM Role** | Ativado = usa a role do host; desativado = usa Access Key. |
| **Access Key ID** | Se não usa IAM Role, informe o access key ID. |
| **Secret Access Key** | Se não usa IAM Role, guarde a secret no cofre (criptografada). |

### Autenticação

**Com IAM Role (recomendado):**
- Marque "Usar IAM Role".
- Deixe os campos de Access Key em branco.
- A plataforma usa a permissão já concedida pela infraestrutura.

**Com Access Keys:**
- Desmarque "Usar IAM Role".
- Informe o Access Key ID.
- Guarde a Secret Access Key no cofre.

### Testar e salvar

Antes de salvar, use o botão **Testar conexão**. O CentralOps verifica:

- se consegue alcançar o bucket (HEAD request);
- se as credenciais são válidas.

Se o teste passar, salve. O destino fica **ativo** (badge verde).

## Como os eventos são entregues

- **Conversão para Parquet OCSF.** Cada lote de eventos é convertido em um arquivo Parquet com compressão (zstd ou snappy).
- **Particionamento determinístico.** Os arquivos são gravados em:
  ```
  ext/{source}/region={region}/accountId={account_id}/eventDay={YYYYMMDD}/{hash}.parquet
  ```
  - `{source}` = nome da custom source.
  - `{region}` = região configurada.
  - `{account_id}` = ID da conta configurada.
  - `{YYYYMMDD}` = data do primeiro evento do lote (OCSF time).
  - `{hash}` = SHA1 determinístico dos event IDs do lote.

- **Idempotência.** Um lote reenviado sobrescreve o arquivo anterior — sem duplicatas.
- **Nova tentativa automática.** Falhas transitórias (timeouts, throttling) disparam reenvio automático.
- **Proteção contra destino instável.** Se o bucket ficar inacessível persistentemente, o CentralOps pausa e retoma automaticamente.

## Acompanhar a saúde do destino

Abra **Operação → Destinos** e selecione o seu destino Security Lake.

O badge de saúde mostra:

| Cor | Significado |
|-----|-------------|
| Verde | Eventos sendo entregues normalmente. |
| Amarelo | Eventos chegando, mas há itens na fila de reenvio. |
| Vermelho | Envio pausado ou bucket inacessível. |
| Cinza | Destino desativado. |

Na visão do destino você acompanha:

- **Eventos por segundo** — taxa de entrega na última hora.
- **Latência média** — tempo de escrita no S3.
- **Itens na fila de reenvio (24h)** — eventos recusados.

Para ver o que não foi entregue, abra a **fila de reenvio**. Cada item mostra o motivo (ex.: "sem credencial AWS", "access denied") e o conteúdo.

## Resolver problemas comuns

| Sintoma | O que verificar |
|---------|-----------------|
| **"Sem credencial AWS"** | Se usa IAM Role: confirme que está marcado em "Usar IAM Role". Se usa Access Keys: confirme que a secret foi guardada no cofre. |
| **"Access denied" / "403 Forbidden"** | A credencial ou permissão está incorreta. Se IAM Role: peça ao admin AWS que confirme a role. Se Access Key: confirme que tem `s3:PutObject` e `s3:GetObject` no bucket. |
| **"Bucket não encontrado"** | O nome do bucket está correto? A região está correta? Teste com `aws s3 ls s3://seu-bucket --region sua-region`. |
| **"Custom source inválida"** | A custom source precisa estar registrada no Security Lake. Confirme o nome em **Security Lake → Custom sources** na console AWS. |
| **"Parquet schema não bate"** | Os eventos têm campos que não coincidem com o schema da custom source. Consulte o schema registrado no Security Lake e verifique quais campos estão sendo enviados. Se precisa ajustar, fale com o administrador da plataforma. |
| **pyarrow/aioboto3 não instalado** | O ambiente de backend precisa dessas bibliotecas. Peça ao administrador que instale `pip install -r requirements-sinks.txt`. |

## Consultar os dados no Security Lake

Depois que os dados chegam, você consegue consultá-los:

1. Na console AWS, abra **Athena**.
2. Conecte-se à tabela ou `glue_table` do Security Lake para sua custom source.
3. Use SQL para consultar: ex., `SELECT * FROM your_table WHERE eventDay='20240621'`.
4. Exporte para QuickSight, pandas, ou qualquer ferramenta que leia Parquet.

## Próximos passos

- **Confirmar que os dados estão chegando:** abra **Operação → Destinos**, selecione o Security Lake e veja as métricas de eventos por segundo.
- **Investigar eventos recusados:** abra a fila de reenvio na visão do destino.
- **Adicionar outros destinos:** veja a [visão geral de destinos](./overview.md).
- **Decidir quais eventos vão para cada destino:** use a tela de [Roteamento](./routing.md).
