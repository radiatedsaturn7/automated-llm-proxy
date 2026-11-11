# Inbound content guardrails

These are deterministic **heuristics**, not a DLP certification, malware detector,
prompt-injection defense, or guarantee that private information cannot escape.
The gateway makes no external scanner calls and retains no matched content.

## Global opt-in policy

- `GUARDRAILS_ENABLED=false` by default: existing requests remain unchanged.
- `GUARDRAILS_ACTION=block` by default when enabled. `redact` is an explicit
  operator choice and may change the meaning or usability of the prompt.
- `GUARDRAILS_DETECTORS='["secrets","private_key"]'` defaults to two built-ins.
  Explicitly add `email`, `us_ssn`, or `payment_card` as needed. Unknown, duplicate,
  or empty detector lists are rejected at startup. No arbitrary regex is accepted.
- `GUARDRAILS_MAX_CHARACTERS=262144` bounds aggregate inspected text, including
  metadata, per request (allowed configuration range 1,024–1,048,576). There are
  also fixed limits of 10,000 visited nodes and 24 nesting levels. Requests that
  exceed inspection limits fail closed when enabled, not silently bypassed.

All gateway replicas must share these settings. Initial policy is global only,
not a per-user/team or hot-reloaded policy. Invoke `preflight` after request
validation and authorization but before reservations, provider selection, or
transmission. Its result replaces the outbound payload; rejected content must
never reach a provider. HTTP/audit integrations must report only the static
reason and detector-ID counts, not values, text excerpts, locations, or prompts.

## Coverage and behavior

Message content is inspected across roles, including system prompts and tool
results. Tool call arguments are parsed as JSON, inspected recursively and
re-serialized, retaining JSON structure and scalar types. Malformed arguments
fail closed. Embeddings accept strings or arrays of strings under this policy;
pre-tokenized input is rejected because token IDs cannot be inspected without
the model tokenizer. Image/audio or other non-text message parts are rejected
when enabled; there is no OCR or binary-content scanning.

Redaction substitutes `[REDACTED]` for detected spans, merging overlaps first.
A private-key PEM header consumes through its END marker, or the rest of that
text if truncated, so redaction never removes only its header while retaining
its private key material. A detector match in an object key, routing metadata,
message name, tool ID/name/schema, or other non-content parameter **blocks even
in redact mode**: changing such fields could invalidate their semantics.

Current secret patterns recognize selected `sk-`/`sk-proj-`, GitHub legacy
prefixes and AWS `AKIA` identifiers. They are neither a complete provider-key
catalog nor validity checks. PEM private-key headers are detected for common
RSA/EC/DSA/OpenSSH/encrypted variants. Email detection is ASCII-oriented. US SSN
heuristics require hyphens and exclude common invalid prefixes/groups. Payment
card detection accepts 13–19 digits with spaces/hyphens and a Luhn checksum;
false positives remain possible. Detection does not verify anyone's identity.

Encoded/obfuscated text, split values across fields, nonstandard identifiers,
unknown secret formats, other languages/identifiers, provider outputs, and
model-generated data are **not** reliably covered. Existing data or secrets
in upstream systems are not inspected. Use provider data policies, restricted
egress, source access controls and independent security review as additional
controls. Guardrails are not a substitute for those measures.

## Trusted extension boundary

`Detector` is a source-level protocol: an immutable detector has a stable ID
and returns spans, never matched values. The built-in registry is read-only;
patterns are reviewed fixed source, not uploaded expressions or dynamic code.
New detectors require a source change, tests and image deployment. Do not add
an arbitrary-code loader or let tenants select Python modules. Tests exercise
span-only output, immutable registry, metadata-only failures, invalid payloads,
bounds, JSON-preserving redaction and detector configuration validation.

Numeric values in structured content/tool arguments are checked using their
Python numeric representation. A matching numeric identifier blocks even in
redact mode rather than changing its type. Scientific notation, floating-point
rounding, encoding, split fields, or alternative formatting may still evade
heuristics. Prefer strings for exact identifiers; do not rely on numeric
inspection as an exhaustive data classification layer.

## Helm deployment

The chart exposes `guardrails.enabled` (false), `guardrails.action` (block),
`guardrails.detectors` (`[secrets, private_key]`), and
`guardrails.maxCharacters` (262144). Prefer a reviewed values file:

```yaml
guardrails:
  enabled: true
  action: block
  detectors: [secrets, private_key, email]
  maxCharacters: 262144
```

Run `helm template`/`helm lint` with your existing Secret reference before
upgrading. Detector IDs/action/limits are validated by both chart schema and
application startup. The environment helper applies the same configuration to
app replicas and operational jobs; jobs do not perform inference or scan stored
data. The settings are not secret. Restart/roll out all replicas together for
consistent policy; during a rolling deployment old and new policy can coexist.
Do not define duplicate `GUARDRAILS_*` names in `extraEnv`. Enabling guards can
reject previously accepted traffic, particularly multimodal/tokenized inputs.
Test with non-sensitive fixtures before rollout, and review metadata-only block
counts. There is no monitor-only bypass or silent fail-open mode.
