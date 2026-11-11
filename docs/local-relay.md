# Local relay

The relay lets an SDK keep using one local token while the relay obtains short-lived central gateway access tokens. It does **not** rotate provider-owned API keys or remove the need to secure a bootstrap credential. The central administrator creates and revokes that agent credential; rotating it requires updating the relay environment and restarting it. This is an exchange protocol, not an OAuth refresh-token or device-authorization implementation.

Run after installing the package:

```sh
export CENTRAL_GATEWAY_URL=https://gateway.example.com
# Supply these via your secret manager; do not put real values in shell history.
export RELAY_CREDENTIAL='<central-agent-credential>'
export RELAY_LOCAL_TOKEN='<distinct-random-local-token-at-least-32-characters>'
python -m gateway.relay
```

Configure SDK base URL `http://127.0.0.1:8090/v1` and API key equal to `RELAY_LOCAL_TOKEN`. Supported routes: chat completions (including SSE), embeddings and models. The local API token is mandatory. The bootstrap credential only goes to `/auth/agent/exchange`, never to client code or the inference endpoint. Cached access tokens live only in memory, refresh before expiry, and concurrent calls share a single exchange. Revocation is enforced by the central gateway; an exchange failure fails closed rather than using an expired token.

Binding defaults to `127.0.0.1:8090`. A nonloopback `RELAY_BIND` requires `RELAY_ALLOW_REMOTE_BIND=true`. Do not expose the plain HTTP relay directly to a network. Put TLS and network access controls in front if remote access is required. There is no CORS support, generic URL forwarding, or unauthenticated inference route. `/health/live` only reports process liveness.

Requests are buffered up to 4 MiB; responses stream without being fully buffered. The relay strips client authentication and sends only its short-lived central access token. A 401 can trigger one token exchange and retry before any downstream bytes are sent. Timeouts, 5xx and streams that have started are never replayed, avoiding accidental duplicate inference/billing. Redirects are rejected. A dropped stream must be handled by the SDK as a failed/partial response.

Access logs are disabled by the relay CLI. Environment secrets are still accessible to the same OS account and privileged processes; use a dedicated user and your platform's secret manager. This first release does not integrate OS keychains, OIDC device flow, hardware-backed credentials or unattended bootstrap-credential rollover. See OAuth 2.0 Security Best Current Practice for production credential-lifecycle design: https://www.rfc-editor.org/rfc/rfc9700.html.
