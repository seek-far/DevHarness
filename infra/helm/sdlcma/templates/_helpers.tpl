{{/*
Common metadata labels stamped on every object. NOTE: the per-component
`app: <name>` label is deliberately NOT folded in here — Deployment
.spec.selector and Service .spec.selector both match on `app:` and those
selectors are immutable after create, so each template sets `app:`
explicitly and merges these helm/standard labels alongside it.
*/}}
{{- define "sdlcma.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/part-of: sdlcma
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
Target namespace — single source of truth for every namespaced object.
*/}}
{{- define "sdlcma.namespace" -}}
{{- .Values.namespace.name -}}
{{- end -}}
