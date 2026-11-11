# Explicit API credential permissions

New API keys default to **`["inference"]`**, even when owned by an administrator.
Pass `permissions` explicitly when creating a key through `/admin/api/keys`:

- `inference`: `/v1/models`, chat completions and embeddings. Model allowlists,
  budgets and rate limits still apply.
- `account`: self-service profile and that credential's own rotation/revocation.
  It cannot enumerate or modify sibling credentials.
- `admin`: administrator API, prompt registry and administration tools. The owner
  must also currently have the administrator role.
- `provisioning`: SCIM provisioning only, also requiring an administrator owner.
  SCIM also accepts `admin` for compatibility. A provisioning-only credential has
  no inference, self-service or general administrative access.

Scopes do not replace role or active-user/team/expiry/revocation checks. An `admin`
credential can issue new credentials, so it is inherently powerful; reserve it for
trusted operators. An admin-only key does not itself permit inference or account
routes. Browser sessions keep their existing role-based authority and CSRF rules.
Agent credentials and their short-lived tokens always have inference-only effective
authority, even if stored scope metadata is excessive. Creation rejects agent
permissions other than `["inference"]`.

Bootstrap creates an administrator key with all four permissions. Both self-service
and administrative rotation preserve the exact permission list, model allowlist,
rate limits, team and existing expiry; rotation never expands authority. There is
no scope-edit endpoint. To change scopes, an administrator creates a replacement
credential and revokes the old one.

## Migration compatibility

Run revision 4 before serving this release. Existing API credentials retain their
prior practical authority: keys owned by administrators receive all four
permissions; member-owned API keys receive `inference` and `account`; agent keys
receive `inference` only. Revocation, expiration, digest and model policies are
preserved. This backfill is deliberate compatibility, not retroactive least
privilege: review existing administrator-owned application keys and replace them
with narrowly scoped keys. Future creations use inference-only defaults, including
a secure database default for inserts that omit the permissions column.

These are gateway permissions, not OAuth scopes advertised by an authorization
server. A least-privilege SCIM integration should use an expiring administrator-owned
API credential with only `["provisioning"]`, stored in the IdP's approved secret
configuration.
