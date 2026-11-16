{{- define "semula.name" -}}{{ default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}{{- end -}}
{{- define "semula.fullname" -}}
{{- if .Values.fullnameOverride -}}{{ .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else if contains .Chart.Name .Release.Name -}}{{ .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else -}}{{ printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" }}{{- end -}}
{{- end -}}
{{- define "semula.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ include "semula.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
{{/* What the app's Deployment and Service select. The component keeps the bundled Postgres pod out of it. */}}
{{- define "semula.selectorLabels" -}}
app.kubernetes.io/name: {{ include "semula.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: server
{{- end -}}
{{- define "semula.postgresSelectorLabels" -}}
app.kubernetes.io/name: {{ include "semula.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: postgresql
{{- end -}}
{{- define "semula.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}{{ default (include "semula.fullname" .) .Values.serviceAccount.name }}
{{- else -}}{{ default "default" .Values.serviceAccount.name }}{{- end -}}
{{- end -}}

{{/* Postgres is in use when the chart runs one or the operator points at their own. */}}
{{- define "semula.usesPostgres" -}}
{{- if or .Values.postgresql.enabled .Values.externalDatabase.url .Values.externalDatabase.existingSecret -}}true{{- end -}}
{{- end -}}

{{/* The Postgres driver is in the -hosted image, which runs as the plain server unless SEMULA_HOSTED=on. */}}
{{- define "semula.image" -}}
{{- $tag := default .Chart.AppVersion .Values.image.tag -}}
{{- if and (include "semula.usesPostgres" .) (not (hasSuffix "-hosted" $tag)) }}{{ $tag = printf "%s-hosted" $tag }}{{ end -}}
{{ printf "%s:%s" .Values.image.repository $tag }}
{{- end -}}

{{- define "semula.authSecretName" -}}
{{- default (printf "%s-auth" (include "semula.fullname" .)) .Values.auth.existingSecret -}}
{{- end -}}
{{- define "semula.dbSecretName" -}}
{{- if .Values.postgresql.enabled -}}{{ printf "%s-db" (include "semula.fullname" .) }}
{{- else -}}{{ default (printf "%s-db" (include "semula.fullname" .)) .Values.externalDatabase.existingSecret }}{{- end -}}
{{- end -}}
{{- define "semula.dbSecretKey" -}}
{{- if and (not .Values.postgresql.enabled) .Values.externalDatabase.existingSecret -}}{{ .Values.externalDatabase.existingSecretKey }}{{- else -}}database-url{{- end -}}
{{- end -}}
