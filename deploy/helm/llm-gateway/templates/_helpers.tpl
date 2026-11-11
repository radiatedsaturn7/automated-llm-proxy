{{- define "gateway.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- define "gateway.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "gateway.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- define "gateway.selectorLabels" -}}
app.kubernetes.io/name: {{ include "gateway.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: gateway
{{- end -}}
{{- define "gateway.labels" -}}
{{ include "gateway.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
{{- end -}}
{{- define "gateway.image" -}}
{{- if .Values.image.digest -}}
{{ printf "%s@%s" .Values.image.repository .Values.image.digest }}
{{- else -}}
{{ printf "%s:%s" .Values.image.repository .Values.image.tag }}
{{- end -}}
{{- end -}}
{{- define "gateway.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{ default (include "gateway.fullname" .) .Values.serviceAccount.name }}
{{- else -}}
{{ default "default" .Values.serviceAccount.name }}
{{- end -}}
{{- end -}}
{{- define "gateway.env" -}}
envFrom:
  - secretRef:
      name: {{ required "existingSecret is required; pre-create the database and gateway keys" .Values.existingSecret | quote }}
env:
  - name: ENVIRONMENT
    value: {{ .Values.environment | quote }}
  - name: PUBLIC_URL
    value: {{ .Values.publicUrl | quote }}
  - name: PROVIDER_TIMEOUT_SECONDS
    value: {{ .Values.providerTimeoutSeconds | quote }}
  - name: CIRCUIT_BREAKER_ENABLED
    value: {{ .Values.circuitBreaker.enabled | quote }}
  - name: CIRCUIT_FAILURE_THRESHOLD
    value: {{ .Values.circuitBreaker.failureThreshold | quote }}
  - name: CIRCUIT_COOLDOWN_SECONDS
    value: {{ .Values.circuitBreaker.cooldownSeconds | quote }}
  - name: CIRCUIT_PROBE_SECONDS
    value: {{ .Values.circuitBreaker.probeSeconds | quote }}
  - name: DB_POOL_SIZE
    value: {{ .Values.database.poolSize | quote }}
  - name: DB_MAX_OVERFLOW
    value: {{ .Values.database.maxOverflow | quote }}
  - name: DB_POOL_TIMEOUT_SECONDS
    value: {{ .Values.database.poolTimeoutSeconds | quote }}
  - name: DB_POOL_RECYCLE_SECONDS
    value: {{ .Values.database.poolRecycleSeconds | quote }}
  - name: DB_STATEMENT_TIMEOUT_MS
    value: {{ .Values.database.statementTimeoutMs | quote }}
  - name: DB_LOCK_TIMEOUT_MS
    value: {{ .Values.database.lockTimeoutMs | quote }}
  - name: DB_MIGRATION_STATEMENT_TIMEOUT_MS
    value: {{ .Values.database.migrationStatementTimeoutMs | quote }}
  - name: DB_MIGRATION_LOCK_TIMEOUT_MS
    value: {{ .Values.database.migrationLockTimeoutMs | quote }}
  - name: GUARDRAILS_ENABLED
    value: {{ .Values.guardrails.enabled | quote }}
  - name: GUARDRAILS_ACTION
    value: {{ .Values.guardrails.action | quote }}
  - name: GUARDRAILS_DETECTORS
    value: {{ .Values.guardrails.detectors | toJson | quote }}
  - name: GUARDRAILS_MAX_CHARACTERS
    value: {{ .Values.guardrails.maxCharacters | quote }}
  - name: ENROLLMENT_ENABLED
    value: {{ .Values.enrollment.enabled | quote }}
  - name: ENROLLMENT_ALLOWED_MODELS
    value: {{ .Values.enrollment.allowedModels | toJson | quote }}
  - name: ENROLLMENT_KEY_DAYS
    value: {{ .Values.enrollment.keyDays | quote }}
  - name: ENROLLMENT_RPM
    value: {{ .Values.enrollment.rpm | quote }}
  - name: ENROLLMENT_TPM
    value: {{ .Values.enrollment.tpm | quote }}
  - name: ENROLLMENT_MAX_PENDING
    value: {{ .Values.enrollment.maxPending | quote }}
{{- with .Values.extraEnv }}
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}
