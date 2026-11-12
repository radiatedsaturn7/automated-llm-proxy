# Amazon Bedrock

## Native Converse adapter (bounded initial support)

Create a provider with `kind: bedrock_converse`, a regional HTTPS origin such as
`https://bedrock-runtime.us-east-1.amazonaws.com`, and an **Amazon Bedrock API
key** in the existing encrypted `api_key` field. This is a Bedrock bearer token,
not an AWS access-key ID/secret pair. Use the normal secret-safe administrative
workflow; never commit real tokens into example configuration.
The associated IAM principal still needs model access and `bedrock:InvokeModel`.
No AWS SDK, environment-variable credential discovery, role assumption,
EC2 metadata access, SigV4 signing, or automatic AWS credential refresh is
implemented by this adapter.

Configure gateway aliases with an upstream foundation-model ID or inference
profile ID supported in that region. Initial IDs are limited to128 characters;
ARNs, prompt resources and custom endpoint paths are unsupported. Model IDs are
URL-encoded as a single path segment. Native origins must match
`bedrock-runtime.<region>.amazonaws.com` (or the China partition `.com.cn`);
Mantle, proxies, private endpoint DNS names and FIPS variants are not supported
by this initial native adapter.

Supported: nonstreaming text chat, leading system/developer instructions,
user/assistant messages, `max_tokens`, `temperature`, `top_p`, and one to four
stop strings. Sampling numbers are in[0,1]; model-specific restrictions may be
stricter. The conversation must begin with a user message after any system
instructions. The request uses Converse `inferenceConfig` and text content
blocks; it does not silently discard unsupported request properties.

Unsupported requests (including streaming, tools, multimodal input, embeddings,
structured-output controls and model-specific extra parameters) fail before
budget reservation or upstream dispatch. Native ConverseStream uses AWS binary
EventStream, not SSE; this release does not pretend to translate that format.

Responses accept only assistant text blocks, recognized completion/filter stop
reasons, and consistent nonnegative integer input/output/total token counts.
Unsupported reasoning/tool blocks, unexpected nonzero cache-token billing, and
malformed responses fail with sanitized errors and conservative estimated
settlement. Valid usage settles at the configured alias prices. Current alias
prices do not model special cache/service-tier pricing. No tracing or provider
metadata is exposed as completion content.

The existing shared asynchronous HTTP client, response byte/deadline bounds,
cancellation cleanup, circuit health and user/team accounting remain in effect.
There are no automatic request retries or model calls in tests. Tests use
synthetic HTTP transports; live AWS credentials/region/model access have not
been validated.

## Existing OpenAI-compatible Bedrock endpoints

AWS also documents OpenAI-compatible Chat Completions on
`https://bedrock-runtime.<region>.amazonaws.com/openai/v1` and a Mantle endpoint.
These can use the existing `openai` adapter with a Bedrock bearer key; this
retains the gateway's OpenAI SSE stream handling. Model availability, supported
parameters, IAM actions and regional endpoint support differ: verify the AWS
model/endpoint documentation before configuration. This does not add native
ConverseStream or make every Bedrock model OpenAI-compatible. Do not use the
native adapter against an `/openai/v1` URL.

Official contracts consulted:
- https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys-use.html
- https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_Converse.html
- https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_ConverseStream.html
- https://docs.aws.amazon.com/bedrock/latest/userguide/endpoints.html
