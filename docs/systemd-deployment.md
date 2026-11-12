# Native Linux VM installation (systemd)

This kit is an operator-reviewed alternative to containers, not automated cloud
provisioning or a verified production topology. It does not install anything by
itself. Minimum supported target: Linux with systemd **249+**, Python **3.12+**,
a supported external PostgreSQL database, CA certificates, and a separately
managed TLS reverse proxy. Test on your actual distribution; static unit checks
are not end-to-end VM deployment evidence. Do not run these units on macOS,
Windows, or a container without a systemd service manager.

## Layout and prerequisites

- A dedicated `brrrtgate` system user/group with no login shell and no home.
- Root-owned, service-nonwritable releases at `/opt/brrrtgate/releases/<revision>`.
  `/opt/brrrtgate/current` points to the selected release. Never give the service
  account ownership of source, its Python environment, unit files or configuration.
- Root-owned `/etc/brrrtgate` and `/etc/brrrtgate/secrets`, mode `0750`, group
  `brrrtgate`; secret files root-owned, group `brrrtgate`, mode `0640` (or more
  restrictive if still readable by the service). No group-writable directories,
  hard links or symlink components in the secret root. Each file contains one
  UTF-8 line. Files must satisfy the existing [secret loader](secret-files.md).
- `gateway.env` contains non-secret settings and absolute file references, not
  passwords. The example's `.invalid` URL must be replaced. Configure production
  PostgreSQL, never SQLite: the service filesystem is intentionally read-only.

Provision all allowlisted files referenced by the example using your approved
secret manager. `database-url` contains a SQLAlchemy `postgresql+asyncpg://...`
URL for a dedicated database role. Configure and verify transport TLS and CA trust
against your PostgreSQL provider; do not copy an unverified SSL query string from
another driver. Use a genuine Fernet master key, a strong signing key and separate
bootstrap/metrics tokens. Do not put secrets in shell command arguments, source,
journals or this repository. Direct secret variables must not coexist with their
`_FILE` equivalents. All replicas require compatible shared installation keys.

## Prepare an immutable release

As a separate build/operator identity, obtain a reviewed exact source revision,
verify provenance and use the committed `requirements.lock` with
`python -m pip install --require-hashes -r requirements.lock` in a Python 3.12+
virtual environment. Install this project with `python -m pip install --no-deps .`.
The resulting environment must be at `<release>/.venv`, with the gateway package
and static files installed. Build at its final absolute path: Python virtualenvs
are not relocatable. Use your controlled build dependencies/wheelhouse and artifact
verification policy; a dependency lock alone does not authenticate the source.
`pip install --no-deps .` still uses separate PEP 517 build dependencies
(including Hatchling); they are not covered by the runtime lock. Supply reviewed
build tooling through a controlled wheelhouse/build environment. This is not a
claim of a hermetic build.
After installation, make the release root-owned and service-nonwritable. Preserve
previous immutable releases for rollback; do not `pip install` into a live release.
No credentials are needed to build the package. The command sequence below puts
only the installed virtualenv in the release directory. If you also place a
source checkout there, `python -m` searches that working directory before the
installed wheel: its code can shadow the package. Both checkout and virtualenv
then require the same verified provenance and immutable ownership; never leave
unreviewed source in the runtime working directory.

## Install and migrate explicitly

Review and copy `deploy/systemd/brrrtgate*.service` into `/etc/systemd/system/` and
the edited `gateway.env.example` to `/etc/brrrtgate/gateway.env`. Point `current`
to the reviewed release. Validate installed units with
`systemd-analyze verify /etc/systemd/system/brrrtgate*.service`, then run
`systemctl daemon-reload`. The service is not enabled or started by this document.

Before first startup or a schema-changing upgrade, take and verify a database
backup, arrange an appropriate maintenance window, and explicitly run
`systemctl start brrrtgate-migrate.service`. Confirm successful exit with
`systemctl show brrrtgate-migrate.service -p Result -p ExecMainStatus` and inspect
redacted operational logs. The migration unit is a separate oneshot without an
Install section: it is **not** automatically required or run on gateway restart.
Do not start gateways on a failed or incomplete migration. Serializing migrations
in the database does not make incompatible old/new versions safe to run together.

Only after successful migration, use `systemctl enable --now brrrtgate.service`.
`Type=exec` confirms execution, not application readiness. Check
`http://127.0.0.1:8080/health/ready`; an active systemd unit is not sufficient.
Perform synthetic inference and authentication smoke tests with disposable data
before routing sensitive traffic. Bootstrap once through your controlled access
path, then remove the bootstrap capability according to your operating procedure.

## TLS, monitoring and multiple VMs

The supplied unit binds **only loopback 127.0.0.1:8080**. Place a reviewed local
TLS reverse proxy in front of it on every VM; an external load balancer cannot
reach that loopback listener directly. The proxy/LB must enforce connection/body
limits, streaming timeouts, health checks and appropriate access control. Preserve
the public HTTPS origin for OIDC configuration. Uvicorn does not trust arbitrary
forwarded headers. Restrict metrics access and provision its bearer token through
monitoring secrets, not public URLs. See [observability](observability.md).

For multiple VMs, use one service process per VM, a protected shared PostgreSQL
service, consistent keys/configuration, synchronized clocks, independently
scraped metrics and an LB over the local proxies. This kit does not provision
HA PostgreSQL, cloud firewalls, DNS, certificates, autoscaling or failover.

## Drain, upgrade and rollback

Remove a VM from the LB and allow your measured routing-propagation window before
stopping it. The unit sends SIGTERM, allows the CLI **360 seconds** to drain
requests, and gives systemd **420 seconds** before forced termination. The extra
60 seconds is cleanup allowance, not a guarantee against a broken database or
cancellation-resistant transport. The unit does not provide an automatic ingress
preStop hook. Preserve conservative pending reservations after a forced kill.

Stop the service before atomically switching `current` to a new immutable release.
Run migrations explicitly when needed, start the service, verify readiness and
synthetic smoke tests, then return the VM to the LB. Rolling upgrades require
schema and key compatibility across every simultaneously running version; otherwise
use a coordinated maintenance window. Monitor journals without enabling raw HTTP
access logs or logging secrets. Restart-on-failure is bounded by systemd's start
limit; repeated failure requires diagnosis, not unlimited restart loops.

Rollback the symlink only if the old binary is compatible with the current schema
and keys. There is no general automatic down-migration guarantee. Otherwise use a
reviewed database/secret restore procedure and account for lost writes, credential
revocations and billing reservations before reopening traffic. Never silently
restore an old database while accepting current inference.

## Copyable operator sequence (not run automatically)

These are templates, not an unattended installer. Replace `REPLACE_WITH_REVIEWED_SHA`,
start in that reviewed source checkout, and first provision the account, writable
new release directory, configuration and secret files described above. Never run
this against a live release. The first block runs as your build identity with
write access only to the new release; the second requires administrative rights.
No command fetches a moving branch or handles plaintext secret values.

```bash
set -eu
REV=REPLACE_WITH_REVIEWED_SHA
RELEASE="/opt/brrrtgate/releases/$REV"
test "$(git rev-parse HEAD)" = "$REV"
test -z "$(git status --porcelain)"
test -d "$RELEASE"
test ! -e "$RELEASE/.venv"
python3.12 -m venv "$RELEASE/.venv"
"$RELEASE/.venv/bin/python" -m pip install --require-hashes -r requirements.lock
"$RELEASE/.venv/bin/python" -m pip install --no-deps .
```

After reviewing the installed artifact and applicable schema compatibility,
run as administrator from the same verified checkout. Set `REV` again to the
same reviewed full SHA. This intentionally stops serving before activation;
remove the VM from load-balancer traffic and wait for propagation first.

```bash
set -eu
REV=REPLACE_WITH_REVIEWED_SHA
RELEASE="/opt/brrrtgate/releases/$REV"
test "$(git rev-parse HEAD)" = "$REV"
test -x "$RELEASE/.venv/bin/python"
chown -R root:root "$RELEASE"
chmod -R a+rX,go-w "$RELEASE"
install -o root -g root -m 0644 deploy/systemd/brrrtgate.service /etc/systemd/system/
install -o root -g root -m 0644 deploy/systemd/brrrtgate-migrate.service /etc/systemd/system/
systemctl stop brrrtgate.service || test "$(systemctl show brrrtgate.service -p LoadState --value)" = not-found
test ! -e /opt/brrrtgate/current.next
test ! -L /opt/brrrtgate/current.next
ln -s "$RELEASE" /opt/brrrtgate/current.next
mv -Tf /opt/brrrtgate/current.next /opt/brrrtgate/current
systemd-analyze verify /etc/systemd/system/brrrtgate.service /etc/systemd/system/brrrtgate-migrate.service
systemctl daemon-reload
systemctl start brrrtgate-migrate.service
systemctl show brrrtgate-migrate.service -p Result -p ExecMainStatus
systemctl enable --now brrrtgate.service
curl --fail --silent --show-error http://127.0.0.1:8080/health/ready
```

The final readiness check is one attempt, not a startup waiter: if it fails, inspect
the unit and retry the health check only after startup completes. Do not restore LB
traffic until readiness and the synthetic smoke tests succeed. Preflight backup,
config/secret provisioning and migration review are mandatory manual gates, not
implemented by this command sequence.
