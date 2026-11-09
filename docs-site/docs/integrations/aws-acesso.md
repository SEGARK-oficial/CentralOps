---
sidebar_position: 6
title: "AWS: dar acesso ao CentralOps"
description: Passo a passo para criar o papel de serviço que deixa o CentralOps ler os logs da sua conta AWS, com as permissões mínimas de cada integração
---

# AWS: dar acesso ao CentralOps

Esta página mostra, passo a passo, como deixar o CentralOps ler os logs de uma conta AWS. Ela vale para todas as integrações AWS de leitura: [CloudTrail](./aws-cloudtrail.md), [CloudWatch Logs](./aws-cloudwatch.md), [WAF](./aws-waf.md), [Security Hub e GuardDuty](./aws-findings.md). A integração [via Firehose](./aws-firehose.md) funciona ao contrário (a AWS envia os logs) e não usa este acesso.

## Em poucas palavras

Você cria na sua conta AWS um **papel** (*IAM role*), que funciona como uma conta de serviço só de leitura. O CentralOps "veste" esse papel quando vai buscar os logs e recebe uma credencial que vale 1 hora. Nenhuma senha ou chave fica guardada no CentralOps.

Duas travas protegem esse acesso:

- **Só o CentralOps pode usar o papel.** O papel confia numa única identidade AWS: a do servidor do CentralOps, chamada aqui de **principal do CentralOps**.
- **Só a sua organização pode usar o papel.** O CentralOps manda junto um código, o **External ID**, que é único para cada organização cadastrada nele. Se outra organização do CentralOps tentar usar o seu papel, a AWS recusa, porque o código não bate.

Para cortar o acesso a qualquer momento, apague o papel (ou a stack que o criou). A coleta para no ciclo seguinte.

## Antes de começar

Você vai precisar de:

- **Acesso de administrador à conta AWS**, ou pelo menos permissão para criar papéis no IAM e stacks no CloudFormation.
- **Acesso de administrador no CentralOps**, para criar a integração.
- **Os nomes do que vai ser lido:** o bucket do CloudTrail, o bucket ou log group do WAF, o log group do CloudWatch. Cada página de integração diz onde achar esses nomes.
- **Se os logs são cifrados com uma chave KMS sua:** o ARN dessa chave.

## Passo 1: Pegar o External ID e o template no CentralOps

1. No CentralOps, vá em **Coleta -> Integrações** e clique para adicionar uma integração AWS (por exemplo, **AWS CloudTrail**).
2. Escolha a **organização** dona dessa conta AWS.
3. Aparece o painel **Acesso por papel AWS (recomendado)**. Ele mostra:
   - o **External ID** da organização (exemplo: `centralops-42`);
   - o **principal do CentralOps** (exemplo: `arn:aws:iam::999999999999:role/centralops-server`);
   - o botão **Baixar template**, um arquivo do CloudFormation que já vem com esses dois valores preenchidos.
4. Clique em **Baixar template** e guarde o arquivo `centralops-readonly-role.json`.

Deixe essa tela aberta: você volta a ela no Passo 3.

:::info[O External ID não muda]

O External ID é criado pelo CentralOps uma vez para cada organização e nunca muda. Todas as contas AWS de um mesmo cliente podem usar o mesmo papel com o mesmo External ID.

:::

## Passo 2: Criar o papel na conta AWS

Escolha um dos três caminhos. O resultado é o mesmo: um papel chamado `CentralOpsReadOnly`.

### Caminho A (mais fácil): pelo console, com o template

1. Entre no console da AWS, na conta de onde vêm os logs.
2. Abra o serviço **CloudFormation**. A região não importa, porque o papel vale para a conta inteira.
3. Clique em **Create stack** e depois em **With new resources (standard)**.
4. Em **Prerequisite - Prepare template**, deixe **Choose an existing template**. Em **Specify template**, escolha **Upload a template file**, selecione o `centralops-readonly-role.json` e clique em **Next**.
5. Em **Stack name**, digite `centralops-readonly`.
6. Em **Parameters**, preencha só o que for usar e deixe o resto como está:

   | Parâmetro | O que colocar |
   |---|---|
   | `CloudTrailBucketName` | O nome do bucket do CloudTrail (só o nome, sem `s3://`). Vazio se não vai coletar CloudTrail. |
   | `WafLogsBucketName` | O nome do bucket `aws-waf-logs-…` do WAF. Vazio se o WAF não grava em S3. |
   | `KmsKeyArn` | O ARN da chave KMS que cifra os logs, se houver. Vazio se não há chave própria. |
   | `AllowListAccounts` | `true` só na conta usada pelo [modo AWS Organization](#várias-contas-de-uma-aws-organization). Nas outras, `false`. |
   | `ExternalId`, `TrustedPrincipalArn`, `RoleName` | Já vêm preenchidos. **Não altere.** |

7. Clique em **Next** duas vezes.
8. No fim da página, marque **I acknowledge that AWS CloudFormation might create IAM resources with custom names** e clique em **Submit**.
9. Espere o status ficar **CREATE_COMPLETE**, o que leva cerca de 1 minuto.
10. Abra a aba **Outputs** e copie o valor de **RoleArn** (exemplo: `arn:aws:iam::111122223333:role/CentralOpsReadOnly`).

### Caminho B: pela AWS CLI, com o template

O painel do CentralOps tem o botão **Copiar comando da AWS CLI** com o comando pronto. Ele é assim:

```bash
aws cloudformation deploy \
  --template-file centralops-readonly-role.json \
  --stack-name centralops-readonly \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides CloudTrailBucketName=meu-bucket-cloudtrail

aws cloudformation describe-stacks --stack-name centralops-readonly \
  --query "Stacks[0].Outputs[?OutputKey=='RoleArn'].OutputValue" --output text
```

O segundo comando imprime o `RoleArn`. Para mudar um parâmetro depois (por exemplo, acrescentar o bucket do WAF), rode o `deploy` de novo com todos os parâmetros que você usa.

### Caminho C: à mão, no console do IAM

Use este caminho se a sua empresa não permite CloudFormation.

1. Abra **IAM -> Roles** e clique em **Create role**.
2. Em **Trusted entity type**, escolha **AWS account** e depois **Another AWS account**.
3. Em **Account ID**, digite os 12 dígitos da conta que aparece no **principal do CentralOps** (no exemplo, `999999999999`).
4. Marque **Require external ID** e cole o External ID do painel. Clique em **Next**.
5. Na tela de permissões, não marque nada e clique em **Next**. As permissões entram no passo 8.
6. Em **Role name**, digite `CentralOpsReadOnly` e clique em **Create role**.
7. Abra o papel criado, vá na aba **Trust relationships**, clique em **Edit trust policy** e troque o conteúdo por este. Assim só a identidade exata do CentralOps pode usar o papel, e não a conta inteira:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Effect": "Allow",
         "Principal": { "AWS": "arn:aws:iam::999999999999:role/centralops-server" },
         "Action": "sts:AssumeRole",
         "Condition": { "StringEquals": { "sts:ExternalId": "centralops-42" } }
       }
     ]
   }
   ```

   Troque o ARN pelo **principal do CentralOps** e o `centralops-42` pelo seu External ID.

8. Na aba **Permissions**, clique em **Add permissions -> Create inline policy**, escolha **JSON** e cole a policy das integrações que você vai usar, montada a partir da [tabela de permissões mínimas](#permissões-mínimas-de-cada-integração).
9. Em **Maximum session duration**, deixe **1 hour**.
10. Copie o **ARN** do papel, no topo da página.

:::warning[Não tire a condição do External ID]

Sem a condição `sts:ExternalId`, quem descobrir o ARN do seu papel pode cadastrá-lo em outra organização do CentralOps e ler os seus logs. O CentralOps não aceita External ID digitado: ele sempre usa o da organização.

:::

## Passo 3: Colar o ARN e testar

1. Volte à tela da integração no CentralOps.
2. Cole o `RoleArn` no campo **ARN do papel ou Access Key ID**. Deixe **Secret Access Key** vazio: com papel, ele não é usado.
3. Preencha os outros campos da integração (bucket, regiões e os demais). A página de cada integração explica cada campo.
4. Clique em **Testar conexão**. O CentralOps usa o papel de verdade e tenta ler uma amostra. A resposta esperada começa com `Conexão OK (papel assumido)`.
5. Clique em **Salvar**. A coleta começa no próximo ciclo, em até 5 minutos.

Se o teste falhar, veja os [problemas comuns](#problemas-comuns).

## Permissões mínimas de cada integração

O papel só precisa de leitura. A tabela mostra exatamente o que cada integração chama na AWS:

| Integração | Permissões | Em qual recurso |
|---|---|---|
| CloudTrail | `s3:ListBucket` | o bucket da trail |
| | `s3:GetObject` | os arquivos do bucket (`arn:aws:s3:::bucket/*`) |
| CloudWatch Logs | `logs:FilterLogEvents` | o log group (`arn:aws:logs:região:conta:log-group:nome:*`) |
| WAF (logs no S3) | `s3:ListBucket` e `s3:GetObject` | o bucket `aws-waf-logs-…` |
| WAF (logs no CloudWatch) | `logs:FilterLogEvents` | o log group `aws-waf-logs-…` |
| Security Hub | `securityhub:GetFindings` | `*` |
| GuardDuty | `guardduty:ListDetectors`, `guardduty:ListFindings` e `guardduty:GetFindings` | `*` |
| Security Hub ou GuardDuty com regiões `*` | `ec2:DescribeRegions` (descobre as regiões ligadas na conta) | `*` |
| Logs cifrados com KMS | `kms:Decrypt` | a chave KMS |
| Modo AWS Organization (pai) | `organizations:ListAccounts` | `*` |

O template do CentralOps concede essas permissões e mais duas, também só de leitura de metadados: `s3:GetBucketLocation` (em qual região está o bucket) e `logs:DescribeLogGroups` (lista os nomes dos log groups). Nenhuma permissão de escrita é concedida.

Exemplo de policy para quem coleta CloudTrail (com KMS) e GuardDuty. Troque os nomes pelos seus:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ListarOBucketDaTrail",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::meu-bucket-cloudtrail"
    },
    {
      "Sid": "LerOsArquivosDaTrail",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::meu-bucket-cloudtrail/*"
    },
    {
      "Sid": "DecifrarOsArquivos",
      "Effect": "Allow",
      "Action": "kms:Decrypt",
      "Resource": "arn:aws:kms:us-east-1:111122223333:key/ID-DA-CHAVE"
    },
    {
      "Sid": "LerAchadosDoGuardDuty",
      "Effect": "Allow",
      "Action": ["guardduty:ListDetectors", "guardduty:ListFindings", "guardduty:GetFindings"],
      "Resource": "*"
    }
  ]
}
```

### Quando o bucket ou a chave ficam em outra conta

É comum guardar os logs numa conta central (*log archive*). Nesse caso, a permissão do papel não basta, e o dono do bucket também precisa liberar o acesso:

- **Bucket em outra conta:** acrescente esta regra na *bucket policy* (S3 -> o bucket -> **Permissions** -> **Bucket policy**):

  ```json
  {
    "Sid": "CentralOpsLeitura",
    "Effect": "Allow",
    "Principal": { "AWS": "arn:aws:iam::111122223333:role/CentralOpsReadOnly" },
    "Action": ["s3:ListBucket", "s3:GetObject"],
    "Resource": ["arn:aws:s3:::meu-bucket-cloudtrail", "arn:aws:s3:::meu-bucket-cloudtrail/*"]
  }
  ```

- **Chave KMS em outra conta:** a *key policy* da chave (KMS -> a chave -> **Key policy**) precisa permitir `kms:Decrypt` para o mesmo ARN do papel.

Mais simples: crie o papel na própria conta central, onde estão o bucket e a chave.

## Várias contas de uma AWS Organization

Se o cliente tem muitas contas AWS, não crie o papel uma conta de cada vez. Use um **StackSet**, que cria o mesmo papel em todas as contas de uma vez e também nas contas que forem criadas depois.

1. Entre na **conta de gerenciamento** da Organization (ou numa administradora delegada do CloudFormation).
2. Abra **CloudFormation -> StackSets -> Create StackSet**.
3. Em **Permissions**, escolha **Service-managed permissions**.
4. Envie o mesmo `centralops-readonly-role.json` e preencha os parâmetros como no Caminho A.
5. Em **Deployment options**, escolha as unidades organizacionais (ou a raiz `r-…` para todas as contas), ligue **Automatic deployment** e escolha **uma** região. O papel vale para a conta inteira.
6. Conclua. A conta de gerenciamento não recebe o papel por StackSet: se for coletar dela, crie uma stack comum nela (Caminho A).

Pela CLI:

```bash
aws cloudformation create-stack-set \
  --stack-set-name centralops-readonly \
  --template-body file://centralops-readonly-role.json \
  --capabilities CAPABILITY_NAMED_IAM \
  --permission-model SERVICE_MANAGED \
  --auto-deployment Enabled=true,RetainStacksOnAccountRemoval=false

aws cloudformation create-stack-instances \
  --stack-set-name centralops-readonly \
  --deployment-targets OrganizationalUnitIds=r-abcd \
  --regions us-east-1
```

Com o papel em todas as contas, o **modo AWS Organization** (edição Enterprise) cria as integrações por você. Você cadastra só um "pai", e o CentralOps lista as contas e cria uma integração "filha" para cada conta que você aprovar:

1. Na conta de gerenciamento, ou numa administradora delegada do AWS Organizations, crie a stack com `AllowListAccounts=true`. Esse papel é o único que precisa listar as contas.
2. No CentralOps, adicione a integração **AWS CloudTrail — AWS Organization**, **Amazon GuardDuty — AWS Organization** ou **AWS Security Hub — AWS Organization**, e cole o ARN desse papel.
3. **Testar conexão** confirma que o papel lista as contas.
4. Salve. Na página da integração pai, o painel de tenants mostra as contas encontradas. Aprove as que quer coletar, ou ligue a aprovação automática.
5. Cada conta aprovada vira uma integração própria, que coleta de todas as regiões (`*`). Para restringir, edite as regiões na integração filha.

Como cada filha acessa a conta dela:

- **GuardDuty e Security Hub:** a filha usa o papel de **mesmo nome e caminho** na conta membro, aquele que o StackSet criou.
- **CloudTrail:** a filha usa o **papel do pai** e lê a parte da conta dela no bucket da trail de Organization (`AWSLogs/o-…/<conta>/`). Por isso, o papel do pai também precisa ler esse bucket: informe `CloudTrailBucketName` (e `KmsKeyArn`, se houver) na stack da conta onde está o bucket, em geral a de *log archive*.

Uma conta suspensa, ou que sai da Organization, tem a filha desativada no sincronismo seguinte. Na edição Community, o pai é criado mas nenhuma conta é importada.

## Instalação própria: a identidade do servidor

Em instalação própria (*self-hosted*), o servidor do CentralOps precisa de uma identidade AWS para usar os papéis dos clientes. Ela vem da cadeia padrão da AWS: variáveis `AWS_ACCESS_KEY_ID` e `AWS_SECRET_ACCESS_KEY`, perfil de instância (EC2) ou IRSA (EKS).

- Essa identidade só precisa da permissão `sts:AssumeRole` nos papéis dos clientes, por exemplo `arn:aws:iam::*:role/CentralOpsReadOnly`.
- O painel descobre o ARN dela sozinho.
- Se o papel do servidor tem caminho (`role/caminho/Nome`), defina `AWS_TRUST_PRINCIPAL_ARN` com o ARN completo.
- Sem identidade, o painel avisa **Identidade AWS do CentralOps não encontrada**.

## Access key (legado)

O CentralOps ainda aceita um **Access Key ID** e uma **Secret Access Key** de um usuário IAM. É uma credencial que não expira e fica guardada (cifrada) no CentralOps. Use só se o papel não for possível.

1. No IAM, crie um usuário só para o CentralOps, sem acesso ao console.
2. Anexe uma policy com as [permissões mínimas](#permissões-mínimas-de-cada-integração) da integração.
3. Crie uma access key (**Security credentials -> Create access key -> Third-party service**).
4. No CentralOps, cole o Access Key ID no campo **ARN do papel ou Access Key ID** e a chave secreta em **Secret Access Key**.

Credenciais temporárias (que começam com `ASIA` e têm session token) não são aceitas. O modo AWS Organization exige papel.

## Problemas comuns

| A mensagem diz | O que fazer |
|---|---|
| `AssumeRole: AccessDenied` | A trust policy do papel não confia no principal do CentralOps, ou o External ID está diferente. Compare os dois com o painel **Acesso por papel AWS**. |
| `Identidade AWS do CentralOps não encontrada` | Só em instalação própria: o servidor não tem credencial AWS. Veja [Instalação própria](#instalação-própria-a-identidade-do-servidor). |
| `AccessDenied` ao listar ou ler o bucket | Falta `s3:ListBucket` ou `s3:GetObject` no papel, ou a bucket policy de outra conta não libera o papel. |
| `KMS` ou `kms:Decrypt` no erro | Os arquivos são cifrados. Informe `KmsKeyArn` na stack e, se a chave for de outra conta, libere o papel na key policy. |
| `ResourceNotFoundException` | O log group não existe na região informada. Confira o nome exato (com as barras) e a região. |
| `InvalidAccessException` (Security Hub) | O Security Hub não está ligado naquela região. |
| `AWSOrganizationsNotInUse` ou `AccessDenied` em `ListAccounts` | O papel do modo Organization não está na conta de gerenciamento (nem numa administradora delegada), ou a stack foi criada com `AllowListAccounts=false`. |

## Como cortar o acesso

- **Pela AWS:** apague a stack `centralops-readonly` (ou o papel `CentralOpsReadOnly`). O acesso termina na hora; a credencial já emitida vale no máximo 1 hora.
- **Pelo CentralOps:** desative ou exclua a integração.
