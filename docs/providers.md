# Provider compatibility

## OpenAI-compatible

Configure `kind: openai`, an HTTPS API-prefix base URL (for example `https://api.openai.com/v1`), server-side key, and alias-to-upstream-model/pricing map. Supports text chat, SSE, embeddings, tool-call message history and OpenAI passthrough tool definitions. Reasoning-model `max_completion_tokens` is preserved instead of converted into `max_tokens`. Limits must be valid for your upstream model. Unsupported top-level fields are rejected explicitly.

## Azure OpenAI v1

Configure `kind: azure_openai`, base URL `https://RESOURCE.openai.azure.com/openai/v1`, API key, and deployment name as `upstream_model`. Uses the `api-key` header. Supports v1 text chat, tools, streaming and embeddings. Legacy dated `api-version` query APIs and Microsoft Entra managed-identity credentials are not implemented. No Azure-specific resources are created by Thunderbolt.

## Anthropic

Configure `kind: anthropic`, base URL `https://api.anthropic.com/v1`, server key and exact model ID. Native text and function-tool messages are translated to OpenAI-style responses and
streaming chunks. System/developer text must precede conversation turns. Function
schemas must describe JSON objects; tool names/IDs and JSON arguments are validated.
`tool_choice` supports auto, required, none and an explicit named function;
`parallel_tool_calls` maps to native parallel-tool control where applicable.

Assistant tool history must be followed by all matching results before the next turn.
Duplicate/dangling IDs, malformed/non-finite JSON, unknown fields and unsupported
strict-schema controls fail explicitly. Multiple adjacent results are grouped into
the native user tool-result turn. Tool calls are returned to the client, never executed
by the gateway. Streaming validates block indices, JSON argument completion and native
terminal events; interrupted or malformed streams retain conservative accounting.

Structured output, thinking/reasoning blocks, native server tools, image/audio content
and unknown native block/finish types are not supported. Model-specific limitations
still apply; a model may reject forced tool choices even if the adapter supports their
syntax. This is not full Anthropic API parity or live-model certification.

Contract references checked 2026-11-11: [define tools](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools),
[handle tool calls](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls),
[parallel tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/parallel-tool-use),
and [streaming](https://platform.claude.com/docs/en/build-with-claude/streaming).

## Failure semantics

One request selects the lowest-priority-number enabled matching route. No automatic retry or failover after dispatch: network errors and interrupted streams can still be billed by providers. Unknown outcomes retain conservative accounting; provider billing reconciliation is explicit. Caller cancellation schedules accounting before closing upstream connections, but abrupt process termination retains the pending reservation.

Responses expose a sanitized `X-Provider-Request-ID` when supplied by the upstream, plus a gateway request ID and accounting reservation ID. Raw upstream error bodies and credentials are never returned. Configured provider credentials are encrypted at rest. Private networking is an explicit opt-in; production egress firewall rules are necessary to prevent DNS rebinding and enforce allowed destinations.

Tests use simulated upstreams, including an actual local TLS server. They do not establish a contract with a paid provider or guarantee that all compatible vendors accept identical parameters. Validate a deployment against the provider/model versions you intend to use.

## Google Gemini native API

Configure `kind: gemini`, base URL `https://generativelanguage.googleapis.com/v1beta`,
a server-side API key, and an exact native model ID (optionally prefixed `models/`).
Credentials use `x-goog-api-key`, never URL parameters. Model IDs cannot inject
paths, query strings or method suffixes. The existing HTTPS/egress restrictions apply.

Supported: text user/assistant messages, system/developer text, temperature, top-p,
stop sequences, `max_tokens`, SSE text chat, and float embeddings (up to 100 texts
per native batch; optional dimensions 1–3072, subject to model support). Native
`generateContent`, `streamGenerateContent?alt=sse` and `batchEmbedContents` map to
the existing OpenAI-compatible endpoints. Each operation dispatches once, without
retries or splitting batches. There are no Vertex AI/Google IAM credentials,
multimodal inputs, tools, grounding, cached-content controls, structured output or
reasoning controls; unsupported fields are rejected before billing admission.

Native candidate completion reasons are required. Missing terminal events fail
streams and retain conservative accounting. Safety blocks map to `content_filter`;
thought text/signatures are not returned. Reported thinking tokens are included in
output-token accounting; model-default thinking behavior may still apply despite
unsupported explicit reasoning controls. Configure prices and output limits for the
selected model. Unsupported native parts or finish reasons fail explicitly rather
than silently losing semantics. Cumulative native usage is translated when present;
missing/invalid usage uses the existing conservative estimate. Embedding batches
without reported usage are marked estimated. Existing 16 MiB response and 1 MiB SSE
line/event bounds remain enforced.

Contract references: [Google content generation API](https://ai.google.dev/api/generate-content)
and [Google embedding API](https://ai.google.dev/api/embeddings). Tests exercise
simulated native contracts and failure paths without credentials or paid API calls;
validate your chosen live model/version before production adoption.


## Strict response parsing

Provider JSON with duplicate object keys, non-finite numeric values or excessive
nesting is rejected rather than interpreted ambiguously. This includes tool arguments.
A malformed response does not establish that the provider did no work: the gateway
retains conservative billing and returns a generic failure without the raw body.
Optional generated-output guards apply after supported normalization; see
[guardrails](guardrails.md) for streaming exclusions and billing semantics.

## Bounded upstream streams

Every streaming adapter has finite, configurable response limits:

- `PROVIDER_STREAM_DURATION_SECONDS=300`: absolute time from upstream dispatch,
  including waiting for response headers (range 1–3600 seconds). This does not reset
  when tokens or keepalive comments arrive. `PROVIDER_TIMEOUT_SECONDS` remains the
  separate HTTP connect/read-inactivity timeout; whichever expires first applies.
- `PROVIDER_STREAM_MAX_BYTES=16777216`: total decoded upstream response-body bytes,
  including comments, ignored fields and delimiters (range 1024–268435456 bytes).
- `PROVIDER_STREAM_MAX_EVENTS=100000`: total nonempty SSE frames, including
  comment-only frames and the completion marker (range 1–1000000).

The existing 1 MiB individual line/event limit also applies. Exact byte/event limits
are inclusive; an additional byte or event fails the stream. Endlessly empty lines
are still bounded by total bytes and time. Set the same limits on every replica
through environment configuration (or Helm `extraEnv`) and tune them for legitimate
long-running workloads before rollout. Limits cannot be disabled.

After headers reach the client, exceeding a limit produces a generic SSE error,
never a successful `[DONE]`. The upstream response is closed and the original
reservation is conservatively settled once, even if an earlier chunk reported usage.
The provider circuit records failure. A deadline while awaiting headers instead
returns generic HTTP 502 with conservative accounting. No provider response text is
included in errors. Client cancellation retains the existing shielded accounting
cleanup; it is not misclassified as a provider limit failure.

Deadline checks occur before each upstream read and output chunk; pending upstream
reads are interrupted at the remaining deadline. An independent watchdog also closes
the upstream response and conservatively settles the reservation when downstream
header/body sends are stalled. Normal completion, limit errors, cancellation and
the watchdog share a one-shot settlement task, including provider-health updates.
Limit errors settle before yielding an SSE error; successful completion settles
before yielding `[DONE]`. Response closure runs independently of database settlement.

The watchdog does not cancel the client's transport task. Ingress/server write
timeouts remain necessary to bound downstream network lifetime. Shutdown drains
retained watchdog/accounting tasks; a wholly unconsumed response can keep draining
until its configured deadline. Configure termination grace periods accordingly.

## Bounded nonstream responses and encoding

`PROVIDER_RESPONSE_DURATION_SECONDS=300` sets a finite absolute deadline for
nonstream inference, from upstream dispatch through response headers and the entire
body (range 1–3600 seconds). Body chunks do not reset it. The existing 16 MiB body
cap and separate `PROVIDER_TIMEOUT_SECONDS` connect/read-inactivity timeout also
apply. Reaching the deadline, including during a continuous slow trickle, returns
a generic 502, closes upstream, records provider failure, and settles conservative
usage once. Client cancellation preserves conservative accounting without declaring
a provider failure. This is a response-receipt deadline, not a database settlement
or downstream write deadline. Configure it consistently across replicas and allow
sufficient time for your chosen models.

All inference adapters request `Accept-Encoding: identity`. Successful upstream
responses must omit `Content-Encoding` or specify only `identity` (case-insensitive).
Compressed, stacked, duplicate or empty encoding values are rejected before any
body read, including streams; no decompressor is allowed to expand a response ahead
of the body-size check. Providers/reverse proxies that force compression must be
configured to honor identity encoding. Error-status response bodies are never read.
Tests use synthetic upstream responses, not paid/live provider calls.
