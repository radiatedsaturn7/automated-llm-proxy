# Mounted installation secrets

The gateway can load selected installation secrets from a trusted local mount
instead of environment values. No Vault API, cloud credential discovery, or
secret-management network call is performed. Vault Agent, a CSI driver, or an
ExternalSecrets-managed Kubernetes Secret may supply the files externally.

Supported selectors (environment variables, uppercase):

- DATABASE_URL_FILE
- GATEWAY_MASTER_KEY_FILE
- GATEWAY_SIGNING_KEY_FILE
- BOOTSTRAP_TOKEN_FILE
- METRICS_TOKEN_FILE
- OIDC_CLIENT_SECRET_FILE

Set GATEWAY_SECRETS_DIR to the absolute trusted mount root. Each selector must
name an absolute file beneath it. Unrelated environment variables ending in
_FILE are ignored, not interpreted as secret instructions. Selectors themselves
are environment-only, not dynamically loaded from dotenv or remote sources.

Example container configuration, containing paths only:

```sh
export GATEWAY_SECRETS_DIR=/run/gateway-secrets
export GATEWAY_MASTER_KEY_FILE=/run/gateway-secrets/master-key
export GATEWAY_SIGNING_KEY_FILE=/run/gateway-secrets/signing-key
export BOOTSTRAP_TOKEN_FILE=/run/gateway-secrets/bootstrap-token
export DATABASE_URL_FILE=/run/gateway-secrets/database-url
```

Do not also set the corresponding direct environment value. Direct constructor
values, environment values, dotenv values and Pydantic secrets-directory values
conflicting with a configured selector are rejected rather than silently taking
precedence. Nonsecret settings continue using existing Pydantic precedence.
Changing delivery mechanisms should retain the *same* secret bytes.

## Filesystem contract

POSIX files must be regular, single-link, owned by root or the process UID,
without executable bits, group write, or any world permissions. Deliberate group
read is allowed: use mode0440 and an appropriate Kubernetes fsGroup, or mode0400
when ownership permits. A Kubernetes Secret's usual world-readable0644 mode is
**not** accepted; configure defaultMode: 0440 (decimal288 in JSON). The process
must actually have read access. The trusted root and traversed directories
inside it must be root/process-owned and not group/world writable. Mount the
secret volume read-only, and tightly restrict the external writer.

The mount root cannot contain symlink path components. Kubernetes projected
file links through ..data are supported beneath that root only when their
resolved targets remain within it. The resolved target is opened relative to
the already-open trusted directory using no-follow path traversal; symlink
escapes, FIFOs, devices, hardlinked files and unexpected directory targets fail
closed. This is not a sandbox against root or the same OS principal controlling
the mount; those principals are trusted.

Each secret is at most16KiB UTF-8, nonempty, single-line, with no NUL. One final
LF or CRLF is removed for normal secret-file creation; other whitespace is not
silently trimmed. Read/permission/conflict errors contain neither paths nor
secret values. Existing configuration validation hides input values as before.
Do not print Settings objects or include secret values in debug logging.

## Startup and rotation limits

The cached configuration reads mounted secrets once per process at startup or
first configuration access. Projection updates do not hot-reload existing
replicas. Restart all affected application, migration and maintenance processes
with consistent values. The test-only cache-clear mechanism is not an operational
rotation API. This feature supplies no automatic Helm volume injection: configure
trusted mounts and selectors in your deployment, avoiding envFrom direct-secret
conflicts. ExternalSecrets can also continue supplying the existing env-based
chart without this file mode.

**Mounted file replacement is not cryptographic key rotation.** The master key
is currently a single Fernet encryption key for provider credentials (and any
other master-key-protected material). Replacing it without a separate verified
reencryption procedure makes existing ciphertext unreadable. The signing key
also keys credential/session/OIDC digests, JWT signatures and export cursor
signatures. Replacing it can make existing keys/sessions unfindable and invalidate
tokens/cursors. There is no automatic multi-key migration or overlap window.
Retain stable keys for ordinary rollouts and preserve protected recovery copies
alongside encrypted backups. Plan an independently tested rotation/recovery
procedure before changing either key; do not roll mixed cryptographic values
across replicas and call that a seamless rotation.

Multiple files are read separately, not as an atomic multi-secret snapshot.
A projection swap during startup could otherwise mix generations. Use an
immutable/versioned Secret or mount for each rollout, and keep its contents
stable until every relevant process has started. Kubernetes atomic ..data swaps
make individual file lookup safe; they do not turn this loader into a coherent
multi-file hot-rotation protocol.
