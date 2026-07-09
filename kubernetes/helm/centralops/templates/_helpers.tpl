{{/*
Nome curto do chart (respeita nameOverride).
*/}}
{{- define "centralops.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Fullname idiomático Helm: inclui o Release.Name para que dois releases no mesmo
namespace não colidam. Para o release canônico `centralops` o resultado segue
sendo `centralops` (Release.Name já contém o nome do chart) — back-compat.
*/}}
{{- define "centralops.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Chart name + version (label helm.sh/chart).
*/}}
{{- define "centralops.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Labels comuns (recomendação app.kubernetes.io/*). NÃO usar como selector — o
selector é o subconjunto estável em centralops.selectorLabels.
*/}}
{{- define "centralops.labels" -}}
helm.sh/chart: {{ include "centralops.chart" . }}
app.kubernetes.io/part-of: centralops
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end -}}

{{/*
Nome do ServiceAccount dedicado (least-privilege). Sem auto-criação cai no
default — mas o default deste chart é criar um SA próprio sem permissões.
*/}}
{{- define "centralops.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "centralops.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
Referência de imagem: usa digest @sha256 quando fornecido (imutável,
reproduzível), senão cai na tag. Uso: include "centralops.image" (dict "img" .Values.image)
*/}}
{{- define "centralops.image" -}}
{{- $img := .img -}}
{{- $context := .context -}}
{{- if $img.digest -}}
{{- printf "%s@%s" $img.repository $img.digest -}}
{{- else -}}
{{- printf "%s:%s" $img.repository (default $context.Chart.AppVersion $img.tag) -}}
{{- end -}}
{{- end -}}

{{/*
Keyring PÚBLICO de licença : os <kid>.pem da chave PÚBLICA do
billing-plane que verificam a assinatura EdDSA do token OFFLINE. Habilitado quando
`secrets.licenseKeyring` (mapa inline <arquivo.pem>→PEM) OU `secrets.existingLicenseKeyring`
(nome de um ConfigMap pré-criado) é fornecido. É montado em /licensing em TODO pod que
resolve a edição (api, workers, kafka-dispatcher) e pareado com
CENTRALOPS_LICENSE_KEYS_DIR=/licensing no ConfigMap. SEM ele, um token válido não pode
ser verificado e edition.current fail-close para Community — mesmo na imagem EE.
*/}}
{{- define "centralops.licenseKeyring.enabled" -}}
{{- if or (not (empty .Values.secrets.licenseKeyring)) (not (empty .Values.secrets.existingLicenseKeyring)) -}}true{{- end -}}
{{- end -}}

{{/* Nome do ConfigMap com o keyring: o existente tem precedência sobre o inline. */}}
{{- define "centralops.licenseKeyring.configMapName" -}}
{{- if .Values.secrets.existingLicenseKeyring -}}
{{- .Values.secrets.existingLicenseKeyring -}}
{{- else -}}
{{- printf "%s-license-keyring" (include "centralops.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/* Volume do pod para o keyring (usar em api/workers/kafka-dispatcher). */}}
{{- define "centralops.licenseKeyring.volume" -}}
- name: license-keyring
  configMap:
    name: {{ include "centralops.licenseKeyring.configMapName" . }}
{{- end -}}

{{/* volumeMount do container para o keyring em /licensing (read-only). */}}
{{- define "centralops.licenseKeyring.volumeMount" -}}
- name: license-keyring
  mountPath: /licensing
  readOnly: true
{{- end -}}
