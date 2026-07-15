# deploy — the dashboard on Kubernetes

A Helm chart that runs the [dashboard](../dashboard) in a cluster together
with an in-cluster collector CronJob, so the demo is pleasingly recursive:
**a Kubernetes cluster that audits its own hygiene with
[k8s-resource-auditor](../k8s-resource-auditor) and reports to a dashboard
it hosts itself.**

```
┌────────────────────────── cluster ──────────────────────────┐
│  CronJob (k8s-audit, read-only RBAC)                         │
│      │  POST /api/v1/ingest/<host>/k8s-resource-auditor      │
│      ▼         (bearer token from a Secret)                  │
│  Deployment: dashboard ── PVC: SQLite history                │
│      ▲                                                       │
│  Service / Ingress                                           │
└──────┼───────────────────────────────────────────────────────┘
       └── you, in a browser
```

## What the chart includes

- **Deployment** — single replica by design (state is a local SQLite file;
  `strategy: Recreate` so two pods never share it), liveness/readiness
  probes on `/healthz`, resource requests and limits, non-root with a
  read-only root filesystem, no ServiceAccount token mounted.
- **PVC** for the SQLite history (switchable to emptyDir for throwaways).
- **Secret** carrying ingest tokens and optional notification webhooks.
- **CronJob** running `k8s-audit.py --all-namespaces` every 30 minutes with
  a ServiceAccount bound to the exact read-only ClusterRole the tool's
  README documents, POSTing the report to the dashboard's ingest API.
- **Service + optional Ingress** (minikube's ingress addon or any
  nginx-class controller).

The chart is written to pass the repo's own auditor: probes, resources,
and pinned image tags everywhere. The one accepted finding is
`single-replica` (LOW) on the dashboard — see above for why.

## Quick start on minikube

```bash
minikube start

# Build both images straight into minikube's Docker daemon
eval $(minikube docker-env)
docker build -t devops-dashboard:local dashboard/
docker build -t devops-toolkit-collector:local -f deploy/collector.Dockerfile .

# Install
helm install dash deploy/helm/devops-dashboard \
    -f deploy/minikube-values.yaml \
    --set ingest.token="$(openssl rand -hex 16)"

# Don't wait 30 minutes — trigger the first audit now
kubectl create job --from=cronjob/dash-devops-dashboard-audit audit-now
kubectl wait --for=condition=complete job/audit-now --timeout=120s

# Open it
kubectl port-forward svc/dash-devops-dashboard 8080:8080
# -> http://localhost:8080 : a "minikube" host card with the cluster's own audit
```

The audit of a fresh minikube is genuinely interesting: expect HIGH
findings for kube-system components without resource limits — that's the
tool working, not a bug.

Prefer an Ingress over port-forward? `minikube addons enable ingress`, add
`dashboard.local` to `/etc/hosts` pointing at `minikube ip`, and set
`ingress.enabled=true`.

### Published images

Local builds are the default workflow (as above — minikube and the CI kind
test both build their own images). Publishing to GHCR is for releases:
every version tag (`v*`) publishes both images as `:X.Y.Z`, and pushes to
main additionally publish `:latest` **only if** the repository variable
`PUBLISH_IMAGES` is `true` — see
[publish.yml](../.github/workflows/publish.yml). On a real cluster you can
then skip the local builds and use the chart's defaults
(`ghcr.io/mkrasu/devops-dashboard`, `ghcr.io/mkrasu/devops-toolkit-collector`),
which follow the chart's `appVersion`.

## Useful knobs (values.yaml)

| Value | What it does | Default |
|---|---|---|
| `ingest.host` / `ingest.token` | Identity + bearer token the in-cluster collector reports with (also seeds `DASHBOARD_TOKENS`) | `minikube` / `change-me` — **override the token** |
| `ingest.extraTokens` | Additional `host: token` pairs for collectors outside the cluster | `{}` |
| `audit.schedule` | How often the cluster audits itself | `*/30 * * * *` |
| `audit.extraArgs` | Extra k8s-audit.py flags, e.g. `["--all-namespaces", "--exclude-namespace", "kube-system"]` | `["--all-namespaces"]` |
| `notify.slackWebhook` / `notify.genericWebhook` | Tile state-change notifications | disabled |
| `persistence.enabled` / `persistence.size` | PVC for the SQLite history | `true` / `1Gi` |
| `ingress.enabled` / `ingress.host` | Expose via Ingress instead of port-forward | `false` / `dashboard.local` |

## CI

Every push runs `helm lint`, validates the rendered manifests with
kubeconform, then does the real thing on a [kind](https://kind.sigs.k8s.io)
cluster: build both images, `helm install`, trigger the audit CronJob, and
assert the report shows up in the dashboard's API. kind and minikube run
the same manifests — kind is just the CI-friendly stand-in.

## License

MIT — see the top-level [LICENSE](../LICENSE) in this repo.
