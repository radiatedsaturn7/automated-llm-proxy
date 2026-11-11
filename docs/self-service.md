# Member self-service

OIDC members land at `/portal`; administrators land at `/admin`. The portal shows the current user's monthly settled spend, pending reservations, personal budget, and latest 100 keys. It never displays another user's records or raw stored credentials.

SSO sessions can revoke or rotate their user's existing keys, with CSRF validation and confirmation prompts. Rotation preserves model scope, request/token limits, team binding, kind and original expiration; it never extends authorization. Expired, revoked or former-team keys cannot be revived. New keys and scope changes still require administrator provisioning. The replacement secret is displayed once, held only in page memory and cleared when its dialog closes. No browser local/session storage is used.

`/account/api/profile` and `/account/api/keys` also accept an ordinary API bearer key. Such callers see/manage only that exact key, not more powerful sibling keys belonging to the same user. Short-lived local-relay tokens cannot use credential-management endpoints. Session and caller validity are rechecked under the policy writer gate before mutations.

Revoking/rotating a key immediately breaks clients using it. Updating client applications, secure local storage and expiry renewal remain explicit operations. The local relay automatically renews its short-lived access tokens but does not silently extend its bootstrap agent credential's lifetime.
