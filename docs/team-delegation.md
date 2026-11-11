# Multiple teams and scoped delegation

Migration 8 adds explicit secondary memberships. Existing users retain their primary team unchanged. No new administrator privilege is backfilled.

A primary team is an implicit member relationship. Global administrators may grant an explicit admin membership, including on the primary team. This is a team role, not the global user role. OIDC mappings and SCIM Groups continue to manage only the legacy primary team: they do not synchronize secondary memberships. Operators must reconcile explicit grants separately when IdP group access changes. Disabling a user disables every team.

Keys select exactly one team, not a union. Global-admin key creation accepts team_id for an active membership; omitted means primary. User monthly budgets are shared across teams; the selected team budget also applies. Team disable denies that team's credentials. Exchanges, inference admission, rotations and delegated mutations recheck membership. Removing a secondary membership atomically revokes all that user's keys in the team, including agents. Re-adding does not resurrect revoked keys. Already dispatched requests cannot be recalled.

## API

The prefix is /teams/api. Browser sessions require CSRF for mutations. Bearer delegates require team_admin permission plus explicit admin membership in that key's team. Global administrators with admin-scoped keys retain global authority.

- GET /teams/api: administrable teams (id, name, budget_micros).
- GET /{team}/members: user_id, role, primary flag.
- PUT /{team}/members/{user}: body role member or admin. Existing enabled users only. Only global admins can change admin roles.
- DELETE /{team}/members/{user}: secondary memberships only. Primary returns 409; manage via identity source. To remove primary-team delegated authority, global-admin PUT role member.
- GET /{team}/keys: sanitized inference-only key metadata.
- POST /{team}/keys: existing KeyCreate payload; permissions must be inference only.
- POST /{team}/keys/{key}/rotate or /revoke: inference keys in that team only.

Bearer delegation cannot exceed caller model allowlist, expiry, RPM or TPM. Expiry defaults are not silently clamped: choose a shorter explicit lifetime when rejected. Browser team admins issue under schema limits and team budgets. Rate limits remain per key, not aggregate team caps.

Delegates cannot create identities, change global/team/user budgets, manage providers, grant global roles or mint administration credentials. Global admins issue team_admin keys through /admin/api/keys. Audit stores IDs/roles/metadata, not secrets. Lists are bounded to 1000 entries; large-directory pagination is not implemented.

SQL lock ordering remains selected team, global user, key under the shared policy gate. The global user lock serializes user budget admission across teams.

Existing browser sessions authenticate the enabled user independently of primary
team availability; each selected administration resource enforces its team.
OIDC sign-in still rejects a disabled mapped primary team as source policy.
Account rotation requires active membership in the key's selected team; revoking
one's own credential remains allowed when its former team is unavailable.
Read-only team lists take the shared policy gate, so they do not acquire the
exclusive administration lock that blocks inference admission.
