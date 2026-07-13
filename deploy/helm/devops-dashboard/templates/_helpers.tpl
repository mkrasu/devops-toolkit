{{/* Chart name and fullname */}}
{{- define "devops-dashboard.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "devops-dashboard.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/* Image references, defaulting tags to the chart appVersion (pinned, never :latest) */}}
{{- define "devops-dashboard.image" -}}
{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}
{{- end -}}

{{- define "devops-dashboard.auditImage" -}}
{{ .Values.audit.image.repository }}:{{ .Values.audit.image.tag | default .Chart.AppVersion }}
{{- end -}}

{{/* Labels */}}
{{- define "devops-dashboard.labels" -}}
app.kubernetes.io/name: {{ include "devops-dashboard.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "devops-dashboard.selectorLabels" -}}
app.kubernetes.io/name: {{ include "devops-dashboard.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* DASHBOARD_TOKENS value: the in-cluster collector token plus any extras */}}
{{- define "devops-dashboard.tokens" -}}
{{- $pairs := list (printf "%s:%s" .Values.ingest.host .Values.ingest.token) -}}
{{- range $host, $token := .Values.ingest.extraTokens -}}
{{- $pairs = append $pairs (printf "%s:%s" $host $token) -}}
{{- end -}}
{{- join "," $pairs -}}
{{- end -}}
