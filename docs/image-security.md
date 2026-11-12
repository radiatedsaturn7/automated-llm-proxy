# Container image inventory and vulnerability gate

[Image security CI](../.github/workflows/image-security.yml) builds a disposable
local Docker image and scans it without publishing to a registry, passing provider
credentials or calling models. It runs on pull requests, main pushes, manual
invocation and a weekly schedule so newly disclosed findings can affect unchanged
source. Checkout does not persist its GitHub token.

Syft 1.57.1 and Grype 0.121.3 Linux amd64 archives are pinned to SHA-256 hashes
verified against official Anchore release checksum assets. Upgrades require
reviewing version and checksum changes together. Checkout and artifact-upload actions
are pinned to verified official commit SHAs. Releases:

- https://github.com/anchore/syft/releases/tag/v1.57.1
- https://github.com/anchore/grype/releases/tag/v0.121.3

The scanner fails on **High or Critical** findings, including findings without a
published fix. There are no broad suppressions, `only-fixed` filtering or ignored
failure exits. Database update failure, stale database (over 120 hours), scanner
failure, empty inventory, mismatched SBOM image identity, or missing evidence does not count as success. A failing gate requires
triage of the actual affected component and image, not an automatic severity
waiver. Enabling this check as a required repository rule remains an administrator
responsibility; the workflow does not configure branch protection.

## Evidence

The uploaded artifact (30-day retention) includes available source/tested SHA and
run metadata, scanner versions, Syft native and CycloneDX image SBOMs, Grype JSON
findings, database build/status metadata, and the locally built Docker image ID.
Artifacts are uploaded even when scanning fails; partial evidence must not be
reported as a complete successful scan. No image or credentials are uploaded.
The image ID is the local image **configuration digest**, not a published registry
manifest digest, signature or provenance attestation. Pull-request checkout may
be a synthetic merge SHA; the recorded checked-out SHA is authoritative for the
scanned source.

This inventories detected packages in the final image, including OS packages; it
is not a source license audit, complete build-toolchain SBOM, proof of absent
vulnerabilities, independent penetration test, or signed/reproducible build.
Base image tags remain mutable, so source-identical builds can differ. Retain
release evidence beyond the CI artifact window in operator-controlled storage and
separately identify/sign the actual distributed artifact before making supply-chain
assurance claims. Findings and SBOMs can reveal package/application metadata; do
not add secrets, real datasets or local credential files to the build context.
