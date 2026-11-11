# Prompt registry and offline evaluation history

This opt-in administrative API **stores submitted template text in plaintext in
the policy database**, including backups. Only submit material approved for that
storage. Do not put credentials or regulated personal data into templates. All
registry routes require administrator authentication (browser sessions also
require CSRF protection). Regular users and relay credentials cannot access it.
Inference requests are not captured, added to templates or stored by this feature.

Base path: `/admin/api/prompt-registry`.

- `POST /templates`: `{"name":"summary","content":"Summarize {{text}}"}` creates
  a named registry entry and immutable version 1.
- `GET /templates`, `GET /templates/{id}`: list/read metadata.
- `PATCH /templates/{id}`: rename or set `archived` true/false.
- `POST /templates/{id}/versions`: `{"content":"New {{text}}"}` appends a version.
- `GET /templates/{id}/versions` and `.../versions/{number}`: read versions.
- `POST .../versions/{number}/render`: `{"variables":{"text":"hello"}}` returns
  rendered text without saving variable values or the rendered result.
- `POST .../versions/{number}/evaluations`: submit existing outputs for offline
  scoring; see below.
- `GET /evaluations?prompt_version_id={version-id}` and `GET /evaluations/{id}`:
  retrieve persisted reports. Filtering by version is optional.

Lists accept `limit` (1–100, default 100) and `offset`. Names are unique. Versions
cannot be edited or deleted through this API. Archiving prevents further versions
but retains historical templates, rendering and evaluation access. There is no
hard-delete endpoint, automatic retention, release channel, inference integration
or deployment promotion in this increment. Administrators must manage retention
and backups under their own data policy. Run schema migration through revision 3
before using the registry.

## Safe rendering

Only literal `{{identifier}}` placeholders are recognized. Identifiers contain
ASCII letters, digits and underscore, start with a letter/underscore, and have at
most 64 characters. No whitespace inside delimiters, property access, expressions,
filters, includes, evaluation, file access or recursively expanded replacements
are supported. Both missing and extra variables are rejected. Text is not HTML
escaped: clients displaying a template/rendered result must render it as plain text.
This is substitution, not a prompt-injection defense or a guarantee about model
behavior. Rendering does not call a model.

Templates are limited to 32,768 characters, 32 distinct variables, 8,192 characters
per value and 131,072 rendered characters. The server also enforces its global HTTP
body-size limit. Validation/audit logs do not contain template or variable values.

## Offline evaluation example

```
POST /admin/api/prompt-registry/templates/{id}/versions/1/evaluations
{
  "cases": [{"input": "optional context", "expected": "yes", "output": "yes"}],
  "rules": ["exact_match", "nonempty"]
}
```

Rules are `exact_match`, `contains`, `json_valid`, and `nonempty`, as described in
[evaluation.md](evaluation.md). There are at most 100 cases per run, 65,536
characters per case field, and a 2-million-character evaluator total limit
(the HTTP body cap may be lower). No provider request or paid model evaluation
occurs. The caller supplies previously collected outputs; linking a run to a
version is attribution supplied by the caller, not proof the version generated
those outputs.

Only the immutable version reference, rule names, per-case pass/fail results,
output lengths/approximate token counts, aggregate scores, actor and timestamp are
persisted. Raw case inputs, expected values and outputs are not saved. Reports
cannot reproduce the dataset; keep approved datasets separately. This feature
measures these deterministic checks, not factuality, safety, semantic quality or
production readiness. There is no scheduled/automatic evaluator or model judge.
