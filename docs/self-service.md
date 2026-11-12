# Member self-service

OIDC members land at `/portal`; administrators land at `/admin`. The portal shows the current user's monthly settled spend, pending reservations, personal budget, and latest 100 keys. It never displays another user's records or raw stored credentials.

SSO sessions can revoke or rotate their user's existing keys, with CSRF validation and confirmation prompts. Rotation preserves model scope, request/token limits, team binding, kind and original expiration; it never extends authorization. Expired, revoked or former-team keys cannot be revived. New keys require administrator provisioning unless an administrator enables a bounded [member key policy](member-key-creation.md). Members then choose a name only; team, model scope, permissions, expiry and shared rate limits remain administrator-controlled. Scope changes always require an administrator. The replacement secret is displayed once, held only in page memory and cleared when its dialog closes. No browser local/session storage is used.

`/account/api/profile` and `/account/api/keys` also accept an ordinary API bearer key. Such callers see/manage only that exact key, not more powerful sibling keys belonging to the same user. Short-lived local-relay tokens cannot use credential-management endpoints. Session and caller validity are rechecked under the policy writer gate before mutations.

Revoking/rotating a key immediately breaks clients using it. Updating client applications, secure local storage and expiry renewal remain explicit operations. The local relay automatically renews its short-lived access tokens but does not silently extend its bootstrap agent credential's lifetime.


## Account security

Browser sessions can use **Sign out other browsers** in `/portal` (or
`POST /account/api/sessions/revoke-others` with CSRF) to invalidate their other
gateway sessions. The current browser remains signed in. API keys, local relay
credentials and the identity provider's own SSO session are not revoked; SSO
can sign in again. API bearer credentials cannot manage browser sessions.

For explicitly provisioned local accounts, the portal offers **Change local
password** when local login is enabled. It requires the current password, a
15–128 character replacement and a fresh authenticator code if enrolled. The
operation is shared-throttled, CSRF/same-origin protected and revalidated under
the policy writer lock. It signs out **all** gateway browsers, including the
caller, and invalidates pending MFA enrollment/login challenges without removing
an active authenticator. API keys remain valid; revoke them separately if
compromise is suspected. Reauthenticate with the new password after success.

The endpoint is `POST /account/api/password` with JSON
`{current_password,new_password,code?}`. It is browser-only, never available to
API keys or federated identities. SSO users manage their passwords at their
identity provider. There is no self-service forgot-password recovery, signup,
email reset or MFA recovery bypass; those still require administrator help.
