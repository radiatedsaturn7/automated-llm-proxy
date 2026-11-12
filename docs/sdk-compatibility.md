# Official Python SDK compatibility acceptance

Tested client: **OpenAI Python SDK 3.49.0**, pinned in the optional `sdk-test`
extra and `uv.lock`. It is **not a gateway runtime dependency**; the production
`requirements.lock` is unchanged. Install tests with:

```sh
python -m pip install -e '.[dev,sdk-test]'
python -m pytest -q tests/test_openai_sdk.py
```

These tests run the real gateway ASGI application, authentication, provider
translation, budget admission and accounting against a database. Only the
upstream provider HTTP transport is simulated. CI runs them with SQLite and
PostgreSQL. They validate SDK serialization, typed response parsing, streaming
iteration and exception mapping, **not real OpenAI service behavior**, TCP
stream latency, TLS, proxy ingress or transport cancellation. Separate Compose
acceptance tests exercise actual gateway processes and a TLS mock provider.

| Client operation | Verified subset |
| --- | --- |
| `chat.completions.create` | Text chat, `max_completion_tokens`, typed choices and usage, raw response/request and reservation IDs |
| Streaming chat | SDK async stream iteration, content chunks, finish event, final usage chunk and completion sentinel |
| `embeddings.create` | Float vectors and SDK default base64 negotiation/automatic decoding; token accounting |
| `responses.create` | Stateless text, instructions, `store=False`, `output_text`, completion status and usage |
| Local failures | SDK `AuthenticationError` (401), `RateLimitError` (429 budget), `BadRequestError` (400 unsupported stateful Responses); no upstream dispatch |
| Upstream failure | Sanitized SDK `InternalServerError` (502), one dispatch, conservative estimated accounting |

This is a deliberately bounded contract matrix, not full OpenAI API or SDK
parity. Responses streaming, retrieval, tools and stored conversation state
remain unsupported; see [Responses subset](responses.md) and
[provider contracts](providers.md). Synchronous clients, other SDK versions,
other languages, advanced SDK helpers, realtime, files, audio and image APIs
are not covered here. Native provider-specific embedding restrictions still
apply; use explicit `encoding_format="float"` when required by the adapter.

## Client retries are a separate policy

The official SDK defaults to retries for certain transport/status failures.
The gateway's no-replay rule does **not** disable retries performed by clients,
load balancers or service meshes. An uncertain request may have reached the
provider even if the client receives an error. These tests explicitly set
`max_retries=0`, including the single-dispatch 502 assertion. A conservative
client setup is:

```python
import os
from openai import AsyncOpenAI

client = AsyncOpenAI(
    base_url="https://gateway.example/v1",
    api_key=os.environ["THUNDERBOLT_API_KEY"],
    max_retries=0,
)
```

Choose any retry policy explicitly according to the application's tolerance
for duplicate upstream work and charges. No idempotency or exactly-once
execution guarantee is implied by this example.
