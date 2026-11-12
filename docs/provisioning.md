# SCIM provisioning

Brrrtgate exposes a conservative SCIM 2.0 subset at `/scim/v2`. Authenticate with an expiring, administrator-owned API key with only the `provisioning` permission. This scope permits SCIM operations but not general administrator APIs or inference. Protect it in your identity provider's secret store, limit its lifetime, and revoke it when the integration is removed. An administrator-owned key with `admin` scope is also accepted for compatibility but is broader than needed.

Supported: authenticated ServiceProviderConfig, ResourceTypes and Schemas discovery; list/get/create Users and Groups; userName/displayName/externalId exact equality filters; pagination up to 100 resources; persistent unique externalId; User PATCH replace active/userName/externalId; User DELETE as audited deactivation; User PUT replacement; Group PATCH add/replace/remove members, exact `members[value eq "id"]` removal, displayName rename and externalId updates. Up to 20 operations execute atomically. A user belongs to one gateway team. Conflicting team assignment is rejected instead of silently moving access. Administrator accounts and teams containing them cannot be changed through SCIM. Unsupported attributes/operations fail explicitly, not silently.

Deactivation revokes all existing keys and browser sessions immediately. Reactivation does not resurrect keys; issue new ones explicitly. Membership changes permanently revoke affected users’ keys and browser sessions; removing and re-adding someone cannot resurrect old credentials. New keys are needed. Audit and billing history remains intact. SCIM provisioning does not automatically link OIDC identities by email: use explicit administrator-verified issuer/subject linking.

This is not yet a claim of interoperability certification with Okta/Entra/Ping. Bulk, sorting, ETags, arbitrary filters, multi-group membership, password provisioning and IdP-specific extensions are not supported. Validate your IdP's operation/attribute mapping in a test tenant before rollout.

## Replacement and patch semantics

User PUT requires the supported core User schema and an email `userName`. Omitted `active` defaults to true and omitted `externalId` clears it. `id`, if supplied, must match the URL; `meta` is server-owned and ignored. PUT does not change role, budgets or the explicitly linked OIDC subject. As with PATCH/DELETE, administrators cannot be overwritten through SCIM.

Group PATCH accepts case-insensitive operation verbs and case-sensitive supported attribute paths. `add members` takes an array of member objects; `replace members` replaces the entire membership; `remove members` without a value clears membership. To remove one member, use `{"op":"remove","path":"members[value eq \"USER_UUID\"]"}`. Removing a missing member is idempotent. Complex/arbitrary filters, nested groups and multiple-team membership management through SCIM remain unsupported. Explicit secondary memberships use the separate team API and must be reconciled separately; see team-delegation.md. Attribute-less add/replace objects can contain `displayName`, `externalId` and `members`. Unsupported operations or a later failure roll back all preceding changes, including credential revocations.

Discovery describes only the implemented core subset and is bounded to User and Group resources. Common SCIM fields/extensions such as `name`, `emails`, enterprise department attributes and password are not accepted; configure your IdP mappings accordingly rather than assuming full standards or vendor certification. Group PUT replacement and Group DELETE tombstones are supported as described below. The API deliberately protects all administrator accounts and groups containing them from SCIM modification.


## Group replacement

Group PUT requires the core Group schema and a valid `displayName`; optional `id`
must match the URL. Omitted `members` clears primary membership; omitted `externalId`
clears that attribute. Server-owned `meta` is ignored. Budgets and enabled state are
not writable through this endpoint. The complete replacement is atomic, including
revocations, and a rejected member or uniqueness conflict rolls it back.

Removing a primary member also removes any duplicate explicit membership in that same
team, so a duplicate binding cannot preserve the access just removed. Unrelated explicit
secondary memberships remain separately managed. Primary membership changes conservatively
revoke all affected users' credentials and sessions, as before; re-adding does not revive
revoked credentials. PUT/PATCH cannot reactivate disabled teams.

PUT/PATCH reject groups containing a global administrator as either primary or explicit
member, or any explicit team-administrator membership. SCIM cannot dismantle those
administration grants or escalate roles. Mutations revalidate the caller's live provisioning
permission inside the policy transaction, serializing with revocation.

## Group deletion, recreation and historical records

DELETE `/scim/v2/Groups/{id}` atomically retires the group and returns 204. Subsequent
SCIM GET/PUT/PATCH for that identifier return 404, and SCIM lists and filters exclude it.
Repeated DELETE for a retained tombstone returns 204 without a second deletion audit;
an unknown identifier returns 404. A disabled but not deleted team is still cleaned up.

Use a maintenance upgrade: drain traffic and in-flight work, stop all older replicas
and background writers, then run the explicit v14 migration and start the tombstone-aware
release everywhere. Do not use Group DELETE until all writers run the new revision.
Old v13 binaries do not check the deletion marker and could re-enable tombstones; exact-head
readiness helps routing but does not fence direct old-binary writes. This is not a
zero-downtime mixed-version rollout guarantee.

The database retains the original team ID, budget, billing and audit history. Migration
v14 adds an explicit deletion timestamp and archived display name/external ID. Deletion
releases the live unique name/external ID while retaining their historical values, so an
IdP can recreate both values with a **new** team ID. Old credentials never regain access
to that new resource. Internal tombstone names are not public display names.

Deletion removes all explicit memberships in that team and detaches primary members.
It revokes every key bound to the deleted team and, conservatively, all keys belonging
to users whose primary team changed. It invalidates the browser sessions of primary and
explicit members. Unrelated secondary-team memberships and secondary members' unrelated
keys remain intact. Pending usage reservations and recorded usage retain their team IDs
for finalization/accounting. In-flight requests admitted before deletion may finish.

Groups containing a global administrator (primary or explicit member), or an explicit
team administrator, cannot be deleted through SCIM. An operator must reconcile those
privileged memberships explicitly first. Caller authorization is rechecked inside the
same transaction as membership removal, revocation and tombstoning.

Normal administrator team lists also exclude tombstones. Global administrators can use
`GET /admin/api/teams?include_deleted=true` for read-only historical visibility. Those
records expose their archived name/external ID and `deleted_at`, and report `enabled:false`.
They cannot be edited, re-enabled, assigned users or issued new team-bound credentials.
An old OIDC group mapping to a deleted team remains denied; update the mapping deliberately
to a newly created team rather than assuming matching display names rebind access.

This implements the documented subset, not a vendor interoperability certification.
