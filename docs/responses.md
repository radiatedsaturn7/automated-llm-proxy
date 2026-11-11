# Stateless text Responses compatibility

`POST /v1/responses` supports a deliberately narrow, non-streaming text subset of
[OpenAI's Responses create API](https://platform.openai.com/docs/api-reference/responses/create).
It translates to the same chat inference pipeline, not a separate provider call.
The configured model alias may route to OpenAI-compatible, Azure, Anthropic or
Gemini adapters subject to those adapters' restrictions.

```json
{
  "model": "chat",
  "input": "Summarize this sentence.",
  "instructions": "Be concise.",
  "max_output_tokens": 128,
  "store": false,
  "stream": false
}
```

Accepted fields: `model`, `input`, optional text `instructions`, `max_output_tokens`,
`temperature`, `top_p`, and boolean-false `store`/`stream`. Missing `store` means
**false**, intentionally unlike a stateful Responses service. Missing output limit
uses the chat endpoint's 512-token default. Inputs may be a string or 1–999 text
messages with user/assistant/system/developer roles. Text content parts use
`input_text`; assistant history may also use `output_text`. No item IDs or references
are accepted. Instructions become a system message. Standard body size, message
count, model access, credential scope, rate and token limits, DLP, budgets, provider
health and conservative accounting all apply. Nothing bypasses admission, and
there is exactly one reservation and provider dispatch.

The response contains `object: response`, a generated `resp_` ID, one assistant
message with `output_text` or `refusal` content, status and translated token usage.
Token-limit or content-filter stops yield `status: incomplete` with a reason.
Missing trustworthy usage yields null rather than invented token counts; detailed
cache/reasoning token breakdowns are not synthesized. Request and reservation
correlation headers remain available. Input/instructions are not echoed in the
response. Returned IDs are ephemeral identifiers, not retrieval handles.

Explicitly unsupported: storage, conversations, `previous_response_id`, retrieval,
deletion, background jobs, streaming, tools/function calls, multimodal/file inputs,
reasoning controls, structured output, metadata and parallel-tool settings. Unknown
fields are rejected even when their value is null or false. Tool or non-text
upstream outputs fail with a sanitized 502 rather than silently discarding data;
the already-performed generation remains accounted for. Existing provider errors
and authorization failures retain their normal statuses. No retry is introduced.

No response or conversation content is persisted by this compatibility layer.
Normal gateway accounting/audit metadata remains, and upstream providers may have
their own retention policies: `store:false` is not a promise of provider-side zero
retention. This is not full Responses API or Agents SDK parity. Tests use simulated
providers through real ASGI routes, never paid API calls.
