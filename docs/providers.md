# Provider compatibility

## OpenAI-compatible

Configure `kind: openai`, an HTTPS API-prefix base URL (for example `https://api.openai.com/v1`), server-side key, and alias-to-upstream-model/pricing map. Supports text chat, SSE, embeddings, tool-call message history and OpenAI passthrough tool definitions. Reasoning-model `max_completion_tokens` is preserved instead of converted into `max_tokens`. Limits must be valid for your upstream model. Unsupported top-level fields are rejected explicitly.

## Azure OpenAI v1

Configure `kind: azure_openai`, base URL `https://RESOURCE.openai.azure.com/openai/v1`, API key, and deployment name as `upstream_model`. Uses the `api-key` header. Supports v1 text chat, tools, streaming and embeddings. Legacy dated `api-version` query APIs and Microsoft Entra managed-identity credentials are not implemented. No Azure-specific resources are created by Thunderbolt.

## Anthropic

Configure `kind: anthropic`, base URL `https://api.anthropic.com/v1`, server key and exact model ID. Native text messages are translated to OpenAI-style responses and streaming chunks. System text is extracted. Unsupported tool/structured-output/reasoning options are rejected rather than silently ignored. This is not full Anthropic API parity.

## Failure semantics

One request selects the lowest-priority-number enabled matching route. No automatic retry or failover after dispatch: network errors and interrupted streams can still be billed by providers. Unknown outcomes retain conservative accounting; provider billing reconciliation is explicit. Caller cancellation schedules accounting before closing upstream connections, but abrupt process termination retains the pending reservation.

Responses expose a sanitized `X-Provider-Request-ID` when supplied by the upstream, plus a gateway request ID and accounting reservation ID. Raw upstream error bodies and credentials are never returned. Configured provider credentials are encrypted at rest. Private networking is an explicit opt-in; production egress firewall rules are necessary to prevent DNS rebinding and enforce allowed destinations.

Tests use simulated upstreams, including an actual local TLS server. They do not establish a contract with a paid provider or guarantee that all compatible vendors accept identical parameters. Validate a deployment against the provider/model versions you intend to use.
