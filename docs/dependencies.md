# Dependency verification

`uv.lock` is the source of resolved Python dependencies. `requirements.lock` is
its hash-pinned runtime-only export used by the container. Python and PostgreSQL
CI install the same lock with the `dev` and `sdk-test` extras; they do not resolve
fresh package versions on each run. CI pins its uv installer version and uses
`--locked`, so inconsistent project metadata fails instead of updating the lock.
The deployment/chart job uses this locked test environment too.

For local checks (Python 3.12, matching CI):

```sh
uv sync --locked --extra dev --extra sdk-test
uv run --no-sync python -m ruff check .
uv run --no-sync python -m pytest -q
uv run --no-sync python -m pip_audit
```

To deliberately update dependencies, review the advisories and compatibility
impact, update the project constraints as needed, run `uv lock` (or a targeted
`uv lock --upgrade-package PACKAGE`), then regenerate the runtime export:

```sh
uv export --locked --no-dev --no-emit-project --format requirements-txt --no-header > requirements.lock
```

Commit both files with any metadata changes. CI compares this exact export with
the committed requirements before tests. Offline tests also check every exported
version and artifact hash against the lock and verify that the test environment
uses the runtime versions applicable to its platform. The export comparison is
important: a subset of valid packages alone is not a complete dependency graph.

After successful Python tests and dependency audit, CI retains a 14-day artifact
containing the tested `pyproject.toml`, both locks, installed package versions and
GitHub's tested commit SHA. On pull requests this SHA may identify GitHub's test
merge commit rather than the branch head. This is diagnostic evidence, **not**
signed build provenance, an SBOM, an independent audit or a release attestation.
The inventory contains package names/versions, not environment variables or
application secrets. Normal retention expiry means it is not a durable archive.

## Remaining release responsibilities

Locking Python packages does not make image builds bit-for-bit reproducible.
The Python base image tag, OS packages, action tags and isolated Python build
backend remain separate supply-chain inputs. Controlled production releases
must pin/review these inputs, record the final image digest and preserve
appropriate evidence. The package build backend is not covered by the runtime
lock. Dependency audit checks currently known advisories, not the absence of
vulnerabilities. A lock must be refreshed deliberately when fixes are published;
it is not an automatic patch service. No image publication, deployment or
credential access is performed by this change.
