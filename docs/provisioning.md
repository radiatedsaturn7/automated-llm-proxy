# SCIM provisioning

Thunderbolt exposes a conservative SCIM 2.0 subset at `/scim/v2`. Authenticate with an expiring administrator API credential. This credential has administrative authority: protect it in your identity provider's secret store, limit its lifetime, and revoke it when the integration is removed. A dedicated limited-scope SCIM principal is not yet available.

Supported: ServiceProviderConfig; list/get/create Users and Groups; userName/displayName/externalId exact equality filters; pagination up to 100 resources; persistent unique externalId; User PATCH replace active/userName/externalId; User DELETE as audited deactivation; Group PATCH replace members. A user belongs to one gateway team. Conflicting team assignment is rejected instead of silently moving access. Administrator accounts and teams containing them cannot be changed through SCIM. Unsupported attributes/operations fail explicitly, not silently.

Deactivation revokes all existing keys and browser sessions immediately. Reactivation does not resurrect keys; issue new ones explicitly. Moving group membership invalidates old keys through team binding; new keys are needed. Audit and billing history remains intact. SCIM provisioning does not automatically link OIDC identities by email: use explicit administrator-verified issuer/subject linking.

This is not yet a claim of interoperability certification with Okta/Entra/Ping. Bulk, sorting, ETags, schema discovery, arbitrary filters, multi-group membership, password provisioning and IdP-specific extensions are not supported. Validate your IdP's operation/attribute mapping in a test tenant before rollout.
