{{/* Expand the name of the chart. */}}
{{- define "zbs.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* Create a default fully qualified app name. */}}
{{- define "zbs.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/* Fully qualified names of the two components. */}}
{{- define "zbs.api.fullname" -}}
{{- printf "%s-api" (include "zbs.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "zbs.ui.fullname" -}}
{{- printf "%s-ui" (include "zbs.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
URL the UI's nginx proxies /api to. Defaults to the in-cluster API Service;
override ui.apiUpstream for custom topologies.
*/}}
{{- define "zbs.ui.apiUpstream" -}}
{{- default (printf "http://%s.%s.svc.cluster.local:%s" (include "zbs.api.fullname" .) .Release.Namespace (.Values.config.listenPort | toString)) .Values.ui.apiUpstream }}
{{- end }}

{{/*
Selector labels; expects a dict with "ctx" and optional "component"
(omit component for shared resources).
*/}}
{{- define "zbs.selectorLabels" -}}
app.kubernetes.io/name: {{ include "zbs.name" .ctx }}
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
{{- with .component }}
app.kubernetes.io/component: {{ . }}
{{- end }}
{{- end }}

{{/* Common labels; expects a dict with "ctx" and optional "component". */}}
{{- define "zbs.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .ctx.Chart.Name .ctx.Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "zbs.selectorLabels" . }}
{{- if .ctx.Chart.AppVersion }}
app.kubernetes.io/version: {{ .ctx.Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .ctx.Release.Service }}
{{- end }}

{{/* Service account name. */}}
{{- define "zbs.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "zbs.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Name of the Secret holding credentials: an existing user-provided Secret,
or the one this chart creates.
*/}}
{{- define "zbs.secretName" -}}
{{- default (include "zbs.fullname" .) .Values.secret.existingSecret }}
{{- end }}
