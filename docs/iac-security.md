# Guarded Helm security profile and static checks

`values-security.yaml` is an **opt-in** deployment profile, not a statement that
all default or operator-customized values are secure. It enforces a non-default
namespace, production mode, digest-pinned gateway image, `Always` pull policy,
mounted secrets instead of secret environment references, enabled NetworkPolicy,
and TLS configuration when ingress is enabled. All rendered namespaced resources
now explicitly use Helm's release namespace, including migration/cleanup jobs.

The profile starts with **deny-all ingress and egress**. It deliberately cannot
serve requests or reach PostgreSQL/providers until the operator supplies precise
network rules, including DNS. The cluster must use a CNI that enforces these
policies. An enabled policy object alone is not evidence of effective isolation.
Do not blindly copy broad example egress rules or turn the security profile off
to make a failed install appear successful.

Example preparation (render/review first, no automatic deployment):

```sh
helm template gateway deploy/helm/llm-gateway --namespace llm-gateway \
  -f deploy/helm/llm-gateway/values-ha.yaml \
  -f deploy/helm/llm-gateway/values-security.yaml \
  -f operator-reviewed-values.yaml > rendered.yaml
```

Operator values must supply the real image repository and verified sha256 digest,
a precreated versioned `mountedSecrets.existingSecret`, exact public URL, network
peers/routes, and any ingress hostname/certificate. Kubernetes RBAC, Secret
rotation, database resilience/backups, external identity configuration, image
provenance/vulnerability review and actual TLS/network acceptance tests remain
operator responsibilities. Disabling `securityProfile.enabled` returns to the
configurable general chart, not an equivalent secure configuration.

## CI scope and evidence

The `IaC security` workflow runs open-source Checkov **3.4.3** over rendered
standard and HA security profiles, including migration and optional maintenance.
It uses all built-in Kubernetes checks with no suppressed checks, severity
filters or soft-fail. Cloud/platform API keys are not required; remote check
metadata download is disabled. Scanner transitive versions are recorded in the
artifact but not fully locked: the scanner version itself is pinned.

The render script uses an explicitly **nondeployable sentinel image digest** and
fake Secret name for structure-only validation. These artifacts are not release
manifests, evidence that an image exists, or verification of Secret contents.
Current coverage is 13 Kubernetes documents / 17 scanner resources and 444 passed
checks, zero failures, skips or parsing errors. The gate also rejects missing or
empty reports and insufficient resource coverage, so unsupported input cannot
silently count as success. Evidence retains rendered YAML, content hashes,
tested Git SHA, package inventory and JSON results for 14 days.

Checkov on this repository's Terraform `helm_release` wrapper currently yields
**zero applicable Terraform checks**. That is **unsupported coverage**, not a
clean Terraform security audit. Existing Terraform validation remains syntax/
provider-schema testing; Helm security comes from scanning its rendered
resources. CI does not validate every possible operator values file, an active
cluster, admission policy, a provider account or regulatory compliance.

To reproduce the exact fixture scan:

```sh
python -m venv .venv-checkov
.venv-checkov/bin/pip install checkov==3.4.3
bash tools/security/render_iac.sh /tmp/iac-rendered
.venv-checkov/bin/checkov --directory /tmp/iac-rendered --framework kubernetes \
  --skip-download --output json > /tmp/checkov.json
python tools/security/check_iac_report.py /tmp/checkov.json
```

Before production, separately scan the **actual** fully rendered operator values
and review every finding. The repository fixture coverage threshold is specific
to the two CI renders, not a universal deployment-resource count. No independent
audit or security/compliance certification is claimed.
