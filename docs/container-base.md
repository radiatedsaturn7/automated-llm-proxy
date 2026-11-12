# Container base security maintenance

The Dockerfile pins the official multi-platform `python:3.12-slim-trixie` index
`sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b`.
The amd64 image manifest inspected on 2026-11-12 was
`sha256:05a994d2344eb364cc186910f4fc8786a38215f39f00d0acc25bff33a08f4177`.
Other architectures require their own acceptance tests; an index pin does not
assert that every platform has been tested.

Both builder and runtime derive from one patched base. It uses the image's
Debian archive keyring and configured `trixie-security` repository, rejects
unsigned APT metadata/packages, installs the available zlib1g update and asserts
that the resulting version is at least
`1:1.3.dfsg+really1.3.1-1+deb13u1`. Missing metadata, signatures, package downloads
or an older installed version fail the build. No `trusted=yes`, authentication
bypass or old-version fallback is used. APT validates the signed InRelease chain
and package hashes; no new third-party package repository/key is introduced.
The inspected `/etc/apt/sources.list.d/debian.sources` uses
`http://deb.debian.org/debian-security`, suite `trixie-security`, and
`Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp`.

This update addresses [DSA-6613-1](https://security-tracker.debian.org/tracker/DSA-6613-1),
including [CVE-2026-76844](https://security-tracker.debian.org/tracker/CVE-2026-76844)
and [CVE-2026-85091](https://security-tracker.debian.org/tracker/CVE-2026-85091).
The pinned upstream image predates those packages. Repository updates are allowed
above the asserted minimum, so the build is **not bit-reproducible**. Record the
actual resulting image digest and package SBOM for each build; periodically
review and update the base digest and minimum patched version.

## Remaining risk is not waived

The initial container scan found 61 High package matches across 19 unique CVEs,
with no Critical matches. The two zlib matches have this available security fix;
other matches require package-level applicability analysis and remediation.
Several Debian source-package advisories map onto multiple installed binary
packages. A vendor `no-dsa`/scanner `wont-fix` status is not proof of absence of
risk. Do not suppress all such results or filter out unfixed vulnerabilities.

A comparison of the official `python:3.12-slim-bookworm` amd64 base with the same
scanner generation found 82 High and 3 Critical matches across 31 unique CVEs,
so switching to that older distro was rejected as a blanket remediation. These
are time-specific scanner findings, not exploitation claims or a complete risk
assessment. A patched zlib alone does not establish a clean container or a green
security gate. Review fresh scan results and full runtime/deployment CI before
releasing an image; keep unrelated unresolved findings visible.
