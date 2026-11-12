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
{{- $mounted := .Values.mountedSecrets.existingSecret -}}
{{- if and $mounted .Values.existingSecret -}}
{{- fail "Use either existingSecret or mountedSecrets.existingSecret, not both" -}}
{{- end -}}
{{- range .Values.extraEnv -}}
{{- if or (eq .name "LOCAL_PASSWORD_ENABLED") (eq .name "LOCAL_MFA_POLICY") (eq .name "GATEWAY_SECRETS_DIR") -}}
{{- fail "Use dedicated chart settings rather than duplicate extraEnv configuration" -}}
{{- end -}}
{{- if $mounted -}}
{{- if or (has .name (list "DATABASE_URL" "GATEWAY_MASTER_KEY" "GATEWAY_SIGNING_KEY" "BOOTSTRAP_TOKEN" "METRICS_TOKEN" "OIDC_CLIENT_SECRET" "GATEWAY_MASTER_DECRYPT_KEYS")) (hasSuffix "_FILE" .name) -}}
{{- fail "Mounted-secret mode rejects competing secret extraEnv entries" -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if not $mounted }}
envFrom:
  - secretRef:
      name: {{ required "existingSecret is required; pre-create the database and gateway keys" .Values.existingSecret | quote }}
{{- end }}
env:
  {{- if $mounted }}
  - name: GATEWAY_SECRETS_DIR
    value: /run/secrets/thunderbolt
  {{- range $name, $key := .Values.mountedSecrets.keys }}
  - name: {{ printf "%s_FILE" $name }}
    value: {{ printf "/run/secrets/thunderbolt/%s" $name | quote }}
  {{- end }}
  {{- end }}
  - name: LOCAL_PASSWORD_ENABLED
    value: {{ .Values.localPassword.enabled | quote }}
  - name: LOCAL_MFA_POLICY
    value: {{ .Values.localPassword.mfaPolicy | quote }}
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

{{- define "gateway.secretMount" -}}
{{- if .Values.mountedSecrets.existingSecret -}}
- name: installation-secrets
  mountPath: /run/secrets/thunderbolt
  readOnly: true
{{- end -}}
{{- end -}}
{{- define "gateway.secretVolume" -}}
{{- if .Values.mountedSecrets.existingSecret -}}
- name: installation-secrets
  secret:
    secretName: {{ .Values.mountedSecrets.existingSecret | quote }}
    defaultMode: 0440
    items:
      {{- range $name, $key := .Values.mountedSecrets.keys }}
      - key: {{ $key | quote }}
        path: {{ $name | quote }}
      {{- end }}
{{- end -}}
{{- end -}}
