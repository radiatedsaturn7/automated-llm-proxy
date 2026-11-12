# Bounded request-parser property testing

`Parser properties` runs on pull requests and main, using existing hash-locked
runtime dependencies. No extra fuzzing dependency, provider call, credential,
database, or network access is needed by the harness. Installation uses PyPI.

Run locally with the project's installed environment:

```sh
python tests/fuzz/request_json.py --seed 20261112 --cases 10000
```

The harness tests `gateway.request_json.parse`, the inference JSON trust boundary:
- generated valid nested values preserve their semantics;
- accepted values serialize as finite UTF-8 JSON and parse back unchanged;
- byte mutations match an independent acceptance oracle that preserves key pairs
  and checks decoded duplicates, Unicode, finite numbers and depth afterward;
- escaped-equivalent duplicate keys fail even inside nested arrays;
- curated regression seeds cover malformed UTF-8, BOMs, duplicate keys, numeric
  overflow, giant integers, lone surrogates, trailing data, and depth boundaries.

CI runs two fixed seeds, 10,000 cases each, with a two-minute harness timeout and
six-minute job timeout. Each generated structure is capped at depth five and
branching four; mutations start from bounded inputs. Cases are capped at 20,000
per invocation. Failures report seed/index, not input contents. Reproduce with the
same seed and case count; minimize any failing input and add a synthetic corpus
regression. Never add production prompts, credentials or customer data.

This is deterministic grammar/mutation property testing, **not coverage-guided
fuzzing**, an OSS-Fuzz integration, a comprehensive security audit, or evidence of
any certification. The parser's network body-size limit remains a separate
middleware responsibility covered by ordinary request-limit tests. This harness
does not assess authentication, streaming, database concurrency or live providers.
