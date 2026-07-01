# k8s-resource-auditor

A read-only Kubernetes auditor that flags common resource-hygiene issues
before they turn into incidents: pods with no resource limits, workloads
with no readiness probes, and PVCs nobody is using anymore.

## Why

None of these problems crash a cluster on day one. They just quietly cause
noisy-neighbor CPU throttling, OOM-killed pods with no memory ceiling,
rolling updates that route traffic to not-yet-ready pods, and storage bills
for volumes nothing is mounting anymore. This script surfaces them in one
pass so you can fix them before they page you.

## Checks

| Check | Default severity | What it flags |
|---|---|---|
| `resources` | HIGH | Containers missing any of: CPU request, memory request, CPU limit, memory limit |
| `readiness-probes` | MEDIUM | Deployments / StatefulSets / DaemonSets with containers that have no `readinessProbe` |
| `orphaned-pvcs` | HIGH (Bound) / MEDIUM (Pending) | PVCs not referenced by any pod's volumes |
| `liveness-probes` | LOW | Containers with no `livenessProbe` |
| `latest-tag` | LOW | Containers using the `:latest` image tag instead of a pinned version |
| `single-replica` | LOW | Deployments running with `replicas: 1` |

Each check can be run independently via `--checks`.

## Requirements

- Python 3.10+ (standard library only — no `pip install` needed)
- `kubectl` on PATH, configured with a context that has **read** access to
  pods, deployments, statefulsets, daemonsets, and persistentvolumeclaims

This tool never modifies cluster state — it only runs `kubectl get ... -o json`.

## Usage

```bash
chmod +x k8s-audit.py

# Audit everything the current context can see
./k8s-audit.py --all-namespaces

# Audit a single namespace
./k8s-audit.py -n production

# Only check for missing resource limits/requests
./k8s-audit.py -A --checks resources

# Run two specific checks
./k8s-audit.py -A --checks resources,orphaned-pvcs

# JSON output (for piping into other tools / CI artifacts)
./k8s-audit.py -A --output json > audit-report.json

# Markdown output (e.g. for posting as a PR/CI comment)
./k8s-audit.py -A --output markdown

# Use a specific kubeconfig context
./k8s-audit.py -A --context staging-cluster

# CI mode: fail the pipeline if anything HIGH severity is found
./k8s-audit.py -A --fail-on high
```

### Options

| Flag | Description |
|---|---|
| `-n, --namespace NS` | Audit a single namespace |
| `-A, --all-namespaces` | Audit all namespaces |
| `--context CTX` | kubeconfig context to use (default: current context) |
| `--checks LIST` | Comma-separated list of checks to run (default: all — see table above) |
| `--output {table,json,markdown}` | Output format (default: table) |
| `--fail-on {high,medium,low,none}` | Exit code 1 if a finding at/above this severity exists (default: none) |

### Exit codes

- `0` — ran successfully (regardless of findings, unless `--fail-on` is set)
- `1` — findings at or above the `--fail-on` threshold were found
- `2` — kubectl error (not installed, bad context, cluster unreachable, etc.)

## Sample output

```
SEVERITY  KIND        NAMESPACE   NAME            CONTAINER  MESSAGE
--------  ----------  ----------  --------------  ---------  ------------------------------------------
HIGH      Pod         production  checkout-7f8d9   web        Missing: cpu limit, memory limit
HIGH      PVC         production  old-cache-data   -          Not mounted by any pod (status: Bound)
MEDIUM    Deployment  production  checkout         web        No readinessProbe defined
LOW       Deployment  staging     worker           worker     Image 'myrepo/worker:latest' uses the ':latest' tag

3 finding(s): 2 high, 1 medium, 0 low
```

## Using it in CI

```yaml
# GitHub Actions example
- name: Audit cluster hygiene
  run: |
    python3 k8s-resource-auditor/k8s-audit.py -A --output markdown --fail-on high >> "$GITHUB_STEP_SUMMARY"
```

This posts a readable report to the workflow summary and fails the job if
any HIGH-severity issue (missing resources, orphaned bound PVC) is present.

## Required RBAC

The identity running this needs read (`get`, `list`) access to:
`pods`, `deployments`, `statefulsets`, `daemonsets`, `persistentvolumeclaims`
— across whichever namespaces you're auditing. A minimal ClusterRole:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: k8s-resource-auditor
rules:
  - apiGroups: [""]
    resources: ["pods", "persistentvolumeclaims"]
    verbs: ["get", "list"]
  - apiGroups: ["apps"]
    resources: ["deployments", "statefulsets", "daemonsets"]
    verbs: ["get", "list"]
```

## Limitations

- Orphaned-PVC detection checks pod volume references only; PVCs referenced
  only by not-yet-scheduled Jobs/CronJobs may show as false positives.
- Doesn't inspect `Jobs`/`CronJobs` pod templates for probes (they don't use
  readiness probes in the same way); resource checks still apply to their pods.

## License

MIT — see the top-level [LICENSE](../LICENSE) in this repo.
