# Brrrtgate branding and compatibility

Brrrtgate is the project display name. The tagline remains **Brrrrt it into production**.

This is a branding-only change, not an installation migration. Existing deployments do not need to rotate credentials, re-enroll MFA, reinstall charts or change monitoring selectors.

Preserved compatibility identifiers include:

- Repository and Python distribution `automated-llm-proxy`, module `gateway` and CLI `llm-gateway`.
- API paths, environment variables, database schemas, credential formats and cryptographic domain separators.
- Existing TOTP issuer `Thunderbolt`, so this release does not change authenticator enrollment identity.
- Helm secret mount paths, existing Prometheus alert names/job selectors, and Grafana dashboard UID. Display titles and alert summaries use the new name.
- The previous banner URL remains available for cached or external references; current UI and README use `brrrtgate-banner.png`.

## Name-screening scope

On 2026-11-12 an initial exact-name web search, GitHub-indexed search, and searches of indexed USPTO, Justia and Trademarkia pages found no results for “Brrrtgate”. This is preliminary screening only, not a comprehensive trademark-register, phonetic-similarity, common-law, international or legal clearance search. No claim that the name is untrademarked, legally cleared, or accepted by CNCF is made. Obtain appropriate clearance before a public commercial launch or foundation submission.
