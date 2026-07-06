#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
k8s-audit.py — Kubernetes resource hygiene auditor.

Scans a cluster (via `kubectl`) for common misconfigurations that don't
break anything today but cause pain later:

  - Containers missing CPU/memory requests or limits
  - Deployments / StatefulSets / DaemonSets missing readiness probes
  - PersistentVolumeClaims not referenced by any pod ("orphaned")

Bonus checks (on by default, easy to disable):
  - Containers missing liveness probes
  - Containers using the `:latest` image tag
  - Deployments running with a single replica

No external Python dependencies — only the standard library and a working
`kubectl` on PATH, pointed at the cluster you want to audit.

Usage:
    python3 k8s-audit.py [OPTIONS]

Examples:
    # Audit everything the current kubeconfig context can see
    python3 k8s-audit.py --all-namespaces

    # Audit one namespace, table output
    python3 k8s-audit.py -n production

    # Only check for missing resource limits/requests, output JSON for CI
    python3 k8s-audit.py -A --checks resources --output json

    # Audit all namespaces but skip the noisy system ones
    python3 k8s-audit.py -A --exclude-namespace kube-system --exclude-namespace kube-public

    # Scope to your own workloads with a label selector
    python3 k8s-audit.py -A -l app=web

    # Fail the CI job if anything HIGH severity is found
    python3 k8s-audit.py -A --fail-on high
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}


@dataclass
class Finding:
    severity: str          # "low" | "medium" | "high"
    check: str              # short machine-readable check id
    kind: str                # Pod | Deployment | StatefulSet | DaemonSet | PVC
    namespace: str
    name: str
    container: str | None
    message: str

    def row(self) -> list[str]:
        return [
            self.severity.upper(),
            self.kind,
            self.namespace,
            self.name,
            self.container or "-",
            self.message,
        ]


@dataclass
class AuditResult:
    findings: list[Finding] = field(default_factory=list)
    resources_scanned: dict[str, int] = field(default_factory=dict)

    def add(self, *findings: Finding) -> None:
        self.findings.extend(findings)

    def by_severity(self, severity: str) -> list[Finding]:
        return [f for f in self.findings if f.severity == severity]


# ---------------------------------------------------------------------------
# kubectl wrapper
# ---------------------------------------------------------------------------

class KubectlError(Exception):
    """A recoverable kubectl failure (e.g. RBAC denial for one resource)."""


def run_kubectl(args: list[str], context: str | None, selector: str | None = None) -> dict[str, Any]:
    cmd = ["kubectl"]
    if context:
        cmd += ["--context", context]
    cmd += args
    if selector:
        cmd += ["-l", selector]
    cmd += ["-o", "json"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=60)
    except FileNotFoundError:
        # kubectl itself missing is fatal — nothing will work.
        die("kubectl is not installed or not on PATH.")
    except subprocess.TimeoutExpired:
        raise KubectlError(f"timed out: {' '.join(cmd)}")

    if proc.returncode != 0:
        raise KubectlError(proc.stderr.strip() or f"exit {proc.returncode}")

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise KubectlError(f"could not parse JSON from: {' '.join(cmd)}")


def die(msg: str) -> None:
    print(f"Error: {msg}", file=sys.stderr)
    sys.exit(2)


def warn(msg: str) -> None:
    print(f"Warning: {msg}", file=sys.stderr)


def current_namespace(context: str | None) -> str:
    """Best-effort resolve the namespace of the active kubeconfig context."""
    try:
        cmd = ["kubectl"]
        if context:
            cmd += ["--context", context]
        cmd += ["config", "view", "--minify", "-o", "jsonpath={..namespace}"]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=15)
        ns = proc.stdout.strip()
        return ns or "default"
    except Exception:
        return "default"


def ns_args(namespace: str | None, all_namespaces: bool) -> list[str]:
    if all_namespaces:
        return ["-A"]
    if namespace:
        return ["-n", namespace]
    return []


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------

def fetch(
    kind: str,
    namespace: str | None,
    all_namespaces: bool,
    context: str | None,
    selector: str | None = None,
    exclude_namespaces: set[str] | None = None,
    failures: list[str] | None = None,
) -> list[dict]:
    """Fetch resources of `kind`. On a recoverable kubectl error (e.g. the token
    can read pods but not PVCs), warn and return [] instead of aborting the run."""
    try:
        data = run_kubectl(["get", kind] + ns_args(namespace, all_namespaces), context, selector)
    except KubectlError as e:
        warn(f"could not fetch '{kind}': {e}. Skipping this resource.")
        if failures is not None:
            failures.append(kind)
        return []
    items = data.get("items", [])
    if exclude_namespaces:
        items = [
            it for it in items
            if it.get("metadata", {}).get("namespace") not in exclude_namespaces
        ]
    return items


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def iter_pod_containers(pod: dict) -> Iterable[dict]:
    spec = pod.get("spec", {})
    for c in spec.get("containers", []):
        yield c
    for c in spec.get("initContainers", []):
        yield c


def check_resources(pods: list[dict]) -> list[Finding]:
    findings = []
    for pod in pods:
        ns = pod["metadata"]["namespace"]
        name = pod["metadata"]["name"]
        # Skip pods owned by Jobs/CronJobs that are already completed — noisy, low value.
        phase = pod.get("status", {}).get("phase")
        if phase in ("Succeeded", "Failed"):
            continue
        for c in iter_pod_containers(pod):
            resources = c.get("resources", {})
            requests = resources.get("requests", {})
            limits = resources.get("limits", {})
            missing = []
            if "cpu" not in requests:
                missing.append("cpu request")
            if "memory" not in requests:
                missing.append("memory request")
            if "cpu" not in limits:
                missing.append("cpu limit")
            if "memory" not in limits:
                missing.append("memory limit")
            if missing:
                findings.append(Finding(
                    severity="high",
                    check="resources",
                    kind="Pod",
                    namespace=ns,
                    name=name,
                    container=c["name"],
                    message=f"Missing: {', '.join(missing)}",
                ))
    return findings


def check_readiness_probes(workloads: list[tuple[str, dict]]) -> list[Finding]:
    findings = []
    for kind, wl in workloads:
        ns = wl["metadata"]["namespace"]
        name = wl["metadata"]["name"]
        template = wl.get("spec", {}).get("template", {}).get("spec", {})
        for c in template.get("containers", []):
            if "readinessProbe" not in c:
                findings.append(Finding(
                    severity="medium",
                    check="readiness-probe",
                    kind=kind,
                    namespace=ns,
                    name=name,
                    container=c["name"],
                    message="No readinessProbe defined",
                ))
    return findings


def check_liveness_probes(workloads: list[tuple[str, dict]]) -> list[Finding]:
    findings = []
    for kind, wl in workloads:
        ns = wl["metadata"]["namespace"]
        name = wl["metadata"]["name"]
        template = wl.get("spec", {}).get("template", {}).get("spec", {})
        for c in template.get("containers", []):
            if "livenessProbe" not in c:
                findings.append(Finding(
                    severity="low",
                    check="liveness-probe",
                    kind=kind,
                    namespace=ns,
                    name=name,
                    container=c["name"],
                    message="No livenessProbe defined",
                ))
    return findings


def image_tag(image: str) -> str:
    """Extract the tag from an image ref, treating an untagged image as ':latest'.
    Handles registries with a port (e.g. 'registry:5000/app' -> latest)."""
    return image.rsplit(":", 1)[-1] if ":" in image.rsplit("/", 1)[-1] else "latest"


def check_latest_tag(workloads: list[tuple[str, dict]]) -> list[Finding]:
    findings = []
    for kind, wl in workloads:
        ns = wl["metadata"]["namespace"]
        name = wl["metadata"]["name"]
        template = wl.get("spec", {}).get("template", {}).get("spec", {})
        for c in template.get("containers", []):
            image = c.get("image", "")
            if image_tag(image) == "latest":
                findings.append(Finding(
                    severity="low",
                    check="latest-tag",
                    kind=kind,
                    namespace=ns,
                    name=name,
                    container=c["name"],
                    message=f"Image '{image}' uses the ':latest' tag (not pinned)",
                ))
    return findings


def check_pull_policy(workloads: list[tuple[str, dict]]) -> list[Finding]:
    """Flag a pinned image (not ':latest') set to imagePullPolicy: Always — it
    forces a registry round-trip on every pod start for an image that can't
    change under that tag. (Untagged/':latest' images are covered by latest-tag.)"""
    findings = []
    for kind, wl in workloads:
        ns = wl["metadata"]["namespace"]
        name = wl["metadata"]["name"]
        template = wl.get("spec", {}).get("template", {}).get("spec", {})
        for c in template.get("containers", []):
            image = c.get("image", "")
            if image_tag(image) == "latest":
                continue
            if c.get("imagePullPolicy") == "Always":
                findings.append(Finding(
                    severity="low",
                    check="pull-policy",
                    kind=kind,
                    namespace=ns,
                    name=name,
                    container=c["name"],
                    message=f"Pinned image '{image}' has imagePullPolicy: Always (pulls on every start)",
                ))
    return findings


def check_single_replica(deployments: list[dict]) -> list[Finding]:
    findings = []
    for wl in deployments:
        ns = wl["metadata"]["namespace"]
        name = wl["metadata"]["name"]
        replicas = wl.get("spec", {}).get("replicas", 1)
        if replicas == 1:
            findings.append(Finding(
                severity="low",
                check="single-replica",
                kind="Deployment",
                namespace=ns,
                name=name,
                container=None,
                message="Running with replicas=1 (no redundancy on pod loss/eviction)",
            ))
    return findings


def check_orphaned_pvcs(pods: list[dict], pvcs: list[dict]) -> list[Finding]:
    used_claims: set[tuple[str, str]] = set()
    for pod in pods:
        ns = pod["metadata"]["namespace"]
        for vol in pod.get("spec", {}).get("volumes", []):
            claim = vol.get("persistentVolumeClaim", {}).get("claimName")
            if claim:
                used_claims.add((ns, claim))

    findings = []
    for pvc in pvcs:
        ns = pvc["metadata"]["namespace"]
        name = pvc["metadata"]["name"]
        phase = pvc.get("status", {}).get("phase", "Unknown")
        if (ns, name) not in used_claims:
            severity = "high" if phase == "Bound" else "medium"
            findings.append(Finding(
                severity=severity,
                check="orphaned-pvc",
                kind="PVC",
                namespace=ns,
                name=name,
                container=None,
                message=f"Not mounted by any pod (status: {phase})",
            ))
    return findings


CHECK_REGISTRY = {
    "resources": "Missing resource requests/limits",
    "readiness-probes": "Missing readiness probes",
    "liveness-probes": "Missing liveness probes",
    "latest-tag": "Container images pinned to :latest",
    "pull-policy": "Pinned images with imagePullPolicy: Always",
    "single-replica": "Deployments running with replicas=1",
    "orphaned-pvcs": "PVCs not mounted by any pod",
}

DEFAULT_CHECKS = list(CHECK_REGISTRY.keys())


# ---------------------------------------------------------------------------
# Output rendering
# ---------------------------------------------------------------------------

def render_table(result: AuditResult) -> str:
    if not result.findings:
        return "No issues found. Cluster looks clean for the selected checks."

    headers = ["SEVERITY", "KIND", "NAMESPACE", "NAME", "CONTAINER", "MESSAGE"]
    ordered = sorted(result.findings, key=lambda f: (-SEVERITY_ORDER[f.severity], f.namespace, f.kind, f.name))
    rows = [f.row() for f in ordered]

    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = min(max(widths[i], len(cell)), 60)

    def fmt_row(row: list[str]) -> str:
        return "  ".join(
            (cell[:widths[i]]).ljust(widths[i]) for i, cell in enumerate(row)
        )

    lines = [fmt_row(headers), "  ".join("-" * w for w in widths)]
    lines += [fmt_row(r) for r in rows]

    summary = (
        f"\n{len(result.findings)} finding(s): "
        f"{len(result.by_severity('high'))} high, "
        f"{len(result.by_severity('medium'))} medium, "
        f"{len(result.by_severity('low'))} low"
    )
    return "\n".join(lines) + summary


def render_markdown(result: AuditResult) -> str:
    if not result.findings:
        return "**No issues found.** Cluster looks clean for the selected checks."

    ordered = sorted(result.findings, key=lambda f: (-SEVERITY_ORDER[f.severity], f.namespace, f.kind, f.name))
    lines = [
        "| Severity | Kind | Namespace | Name | Container | Message |",
        "|---|---|---|---|---|---|",
    ]
    for f in ordered:
        lines.append(
            f"| {f.severity.upper()} | {f.kind} | {f.namespace} | {f.name} | {f.container or '-'} | {f.message} |"
        )
    lines.append(
        f"\n**{len(result.findings)} finding(s):** "
        f"{len(result.by_severity('high'))} high, "
        f"{len(result.by_severity('medium'))} medium, "
        f"{len(result.by_severity('low'))} low"
    )
    return "\n".join(lines)


def render_json(result: AuditResult) -> str:
    payload = {
        "resources_scanned": result.resources_scanned,
        "summary": {
            "total": len(result.findings),
            "high": len(result.by_severity("high")),
            "medium": len(result.by_severity("medium")),
            "low": len(result.by_severity("low")),
        },
        "findings": [
            {
                "severity": f.severity,
                "check": f.check,
                "kind": f.kind,
                "namespace": f.namespace,
                "name": f.name,
                "container": f.container,
                "message": f.message,
            }
            for f in result.findings
        ],
    }
    return json.dumps(payload, indent=2)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Audit a Kubernetes cluster for common resource-hygiene issues.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    scope = p.add_mutually_exclusive_group()
    scope.add_argument("-n", "--namespace", help="Audit a single namespace")
    scope.add_argument("-A", "--all-namespaces", action="store_true", help="Audit all namespaces")
    p.add_argument("--context", help="kubeconfig context to use (default: current context)")
    p.add_argument(
        "--exclude-namespace",
        action="append",
        default=[],
        metavar="NS",
        help="Namespace to skip (repeatable). Handy for -A runs to drop kube-system etc.",
    )
    p.add_argument(
        "-l", "--selector",
        help="Label selector passed to kubectl (e.g. 'app=web,tier!=cache')",
    )
    p.add_argument(
        "--checks",
        default="all",
        help=f"Comma-separated checks to run: {', '.join(DEFAULT_CHECKS)} (default: all)",
    )
    p.add_argument("--output", choices=["table", "json", "markdown"], default="table")
    p.add_argument(
        "--fail-on",
        choices=["high", "medium", "low", "none"],
        default="none",
        help="Exit with code 1 if any finding at or above this severity exists (for CI)",
    )
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    if not args.namespace and not args.all_namespaces:
        ns = current_namespace(args.context)
        print(f"Note: no --namespace/-n or --all-namespaces/-A given, auditing current namespace '{ns}'.\n", file=sys.stderr)

    checks = DEFAULT_CHECKS if args.checks == "all" else [c.strip() for c in args.checks.split(",")]
    unknown = set(checks) - set(CHECK_REGISTRY)
    if unknown:
        die(f"Unknown check(s): {', '.join(sorted(unknown))}. Valid: {', '.join(DEFAULT_CHECKS)}")

    result = AuditResult()
    exclude = set(args.exclude_namespace)
    failures: list[str] = []

    def _fetch(kind: str) -> list[dict]:
        return fetch(
            kind, args.namespace, args.all_namespaces, args.context,
            selector=args.selector, exclude_namespaces=exclude, failures=failures,
        )

    needs_pods = {"resources", "orphaned-pvcs"} & set(checks)
    needs_workloads = {"readiness-probes", "liveness-probes", "latest-tag", "pull-policy", "single-replica"} & set(checks)
    needs_pvcs = "orphaned-pvcs" in checks

    pods = _fetch("pods") if needs_pods else []
    deployments = _fetch("deployments") if needs_workloads else []
    statefulsets = _fetch("statefulsets") if needs_workloads else []
    daemonsets = _fetch("daemonsets") if needs_workloads else []
    pvcs = _fetch("pvc") if needs_pvcs else []

    result.resources_scanned = {
        "pods": len(pods),
        "deployments": len(deployments),
        "statefulsets": len(statefulsets),
        "daemonsets": len(daemonsets),
        "pvcs": len(pvcs),
    }

    workloads: list[tuple[str, dict]] = (
        [("Deployment", d) for d in deployments]
        + [("StatefulSet", s) for s in statefulsets]
        + [("DaemonSet", ds) for ds in daemonsets]
    )

    if "resources" in checks:
        result.add(*check_resources(pods))
    if "readiness-probes" in checks:
        result.add(*check_readiness_probes(workloads))
    if "liveness-probes" in checks:
        result.add(*check_liveness_probes(workloads))
    if "latest-tag" in checks:
        result.add(*check_latest_tag(workloads))
    if "pull-policy" in checks:
        result.add(*check_pull_policy(workloads))
    if "single-replica" in checks:
        result.add(*check_single_replica(deployments))
    if "orphaned-pvcs" in checks:
        result.add(*check_orphaned_pvcs(pods, pvcs))

    renderer = {"table": render_table, "json": render_json, "markdown": render_markdown}[args.output]
    print(renderer(result))

    if failures:
        warn(
            f"some resources could not be read ({', '.join(failures)}); "
            "results may be incomplete. Check RBAC for the current context."
        )

    if args.fail_on != "none":
        threshold = SEVERITY_ORDER[args.fail_on]
        if any(SEVERITY_ORDER[f.severity] >= threshold for f in result.findings):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
