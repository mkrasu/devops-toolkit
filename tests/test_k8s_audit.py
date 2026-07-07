# SPDX-License-Identifier: MIT
"""Unit tests for k8s-resource-auditor/k8s-audit.py.

The check functions take plain dicts (parsed kubectl JSON), so everything
here runs against small in-memory fixtures — no cluster, no kubectl.

Run from the repo root:
    python3 -m unittest discover -s tests -v
"""

import importlib.util
import json
import sys
import unittest
from pathlib import Path


def _load_module():
    path = Path(__file__).resolve().parent.parent / "k8s-resource-auditor" / "k8s-audit.py"
    spec = importlib.util.spec_from_file_location("k8s_audit", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


ka = _load_module()


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

FULL_RESOURCES = {
    "requests": {"cpu": "100m", "memory": "128Mi"},
    "limits": {"cpu": "500m", "memory": "256Mi"},
}


def container(name="app", image="repo/app:v1.2.3", resources=None, **extra):
    c = {"name": name, "image": image}
    if resources is not None:
        c["resources"] = resources
    c.update(extra)
    return c


def pod(name="pod-1", ns="default", containers=None, init_containers=None,
        phase="Running", volumes=None):
    spec = {"containers": containers if containers is not None else [container()]}
    if init_containers:
        spec["initContainers"] = init_containers
    if volumes:
        spec["volumes"] = volumes
    return {
        "metadata": {"name": name, "namespace": ns},
        "spec": spec,
        "status": {"phase": phase},
    }


def workload(kind="Deployment", name="web", ns="default", containers=None, replicas=None):
    spec = {"template": {"spec": {"containers": containers if containers is not None else [container()]}}}
    if replicas is not None:
        spec["replicas"] = replicas
    return kind, {"metadata": {"name": name, "namespace": ns}, "spec": spec}


def pvc(name="data", ns="default", phase="Bound"):
    return {"metadata": {"name": name, "namespace": ns}, "status": {"phase": phase}}


# ---------------------------------------------------------------------------
# image_tag
# ---------------------------------------------------------------------------

class ImageTagTest(unittest.TestCase):
    def test_pinned_tag(self):
        self.assertEqual(ka.image_tag("repo/app:v1.2.3"), "v1.2.3")

    def test_untagged_is_latest(self):
        self.assertEqual(ka.image_tag("repo/app"), "latest")

    def test_explicit_latest(self):
        self.assertEqual(ka.image_tag("repo/app:latest"), "latest")

    def test_registry_with_port_untagged(self):
        self.assertEqual(ka.image_tag("registry:5000/app"), "latest")

    def test_registry_with_port_tagged(self):
        self.assertEqual(ka.image_tag("registry:5000/app:v2"), "v2")


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

class CheckResourcesTest(unittest.TestCase):
    def test_fully_specified_container_passes(self):
        findings = ka.check_resources([pod(containers=[container(resources=FULL_RESOURCES)])])
        self.assertEqual(findings, [])

    def test_missing_pieces_are_listed(self):
        p = pod(containers=[container(resources={"requests": {"cpu": "100m"}})])
        findings = ka.check_resources([p])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "high")
        self.assertIn("memory request", findings[0].message)
        self.assertIn("cpu limit", findings[0].message)
        self.assertIn("memory limit", findings[0].message)
        self.assertNotIn("cpu request", findings[0].message)

    def test_completed_pods_are_skipped(self):
        findings = ka.check_resources([pod(containers=[container()], phase="Succeeded")])
        self.assertEqual(findings, [])

    def test_init_containers_are_checked(self):
        p = pod(
            containers=[container(resources=FULL_RESOURCES)],
            init_containers=[container(name="init-db")],
        )
        findings = ka.check_resources([p])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].container, "init-db")


class ProbeChecksTest(unittest.TestCase):
    def test_missing_readiness_probe_flagged(self):
        findings = ka.check_readiness_probes([workload()])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "medium")

    def test_present_readiness_probe_passes(self):
        wl = workload(containers=[container(readinessProbe={"httpGet": {"path": "/healthz"}})])
        self.assertEqual(ka.check_readiness_probes([wl]), [])

    def test_missing_liveness_probe_flagged_low(self):
        findings = ka.check_liveness_probes([workload()])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "low")


class LatestTagCheckTest(unittest.TestCase):
    def test_latest_and_untagged_flagged(self):
        wls = [
            workload(name="a", containers=[container(image="repo/app:latest")]),
            workload(name="b", containers=[container(image="repo/app")]),
            workload(name="c", containers=[container(image="repo/app:v1")]),
        ]
        findings = ka.check_latest_tag(wls)
        self.assertEqual(sorted(f.name for f in findings), ["a", "b"])


class PullPolicyCheckTest(unittest.TestCase):
    def test_pinned_image_with_always_flagged(self):
        wl = workload(containers=[container(image="repo/app:v1", imagePullPolicy="Always")])
        self.assertEqual(len(ka.check_pull_policy([wl])), 1)

    def test_latest_image_not_double_reported(self):
        wl = workload(containers=[container(image="repo/app:latest", imagePullPolicy="Always")])
        self.assertEqual(ka.check_pull_policy([wl]), [])

    def test_pinned_image_without_always_passes(self):
        wl = workload(containers=[container(image="repo/app:v1", imagePullPolicy="IfNotPresent")])
        self.assertEqual(ka.check_pull_policy([wl]), [])


class SingleReplicaCheckTest(unittest.TestCase):
    def test_one_replica_flagged(self):
        _, wl = workload(replicas=1)
        self.assertEqual(len(ka.check_single_replica([wl])), 1)

    def test_multiple_replicas_pass(self):
        _, wl = workload(replicas=3)
        self.assertEqual(ka.check_single_replica([wl]), [])

    def test_unset_replicas_defaults_to_one(self):
        _, wl = workload()
        self.assertEqual(len(ka.check_single_replica([wl])), 1)


class OrphanedPvcCheckTest(unittest.TestCase):
    def test_unused_bound_pvc_is_high(self):
        findings = ka.check_orphaned_pvcs([], [pvc(phase="Bound")])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "high")

    def test_unused_pending_pvc_is_medium(self):
        findings = ka.check_orphaned_pvcs([], [pvc(phase="Pending")])
        self.assertEqual(findings[0].severity, "medium")

    def test_mounted_pvc_passes(self):
        p = pod(volumes=[{"name": "data", "persistentVolumeClaim": {"claimName": "data"}}])
        self.assertEqual(ka.check_orphaned_pvcs([p], [pvc(name="data")]), [])

    def test_same_name_in_other_namespace_is_still_orphaned(self):
        p = pod(ns="other", volumes=[{"name": "data", "persistentVolumeClaim": {"claimName": "data"}}])
        findings = ka.check_orphaned_pvcs([p], [pvc(name="data", ns="default")])
        self.assertEqual(len(findings), 1)


class CheckIdConsistencyTest(unittest.TestCase):
    """Every finding's `check` id must match a --checks selector name, so
    filtering JSON output by the name you passed on the CLI always works."""

    def test_finding_ids_match_registry_keys(self):
        bare_pod = pod(containers=[container(image="repo/app:latest", imagePullPolicy="Always")])
        pinned_wl = workload(containers=[container(image="repo/app:v1", imagePullPolicy="Always")])
        findings = (
            ka.check_resources([bare_pod])
            + ka.check_readiness_probes([workload()])
            + ka.check_liveness_probes([workload()])
            + ka.check_latest_tag([workload(containers=[container(image="repo/app:latest")])])
            + ka.check_pull_policy([pinned_wl])
            + ka.check_single_replica([workload(replicas=1)[1]])
            + ka.check_orphaned_pvcs([], [pvc()])
        )
        self.assertEqual(len({f.check for f in findings}), len(ka.CHECK_REGISTRY))
        for f in findings:
            self.assertIn(f.check, ka.CHECK_REGISTRY)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

class RenderTest(unittest.TestCase):
    def _result(self):
        result = ka.AuditResult()
        result.add(*ka.check_readiness_probes([workload()]))
        result.add(*ka.check_orphaned_pvcs([], [pvc()]))
        return result

    def test_json_output_is_valid_and_counted(self):
        payload = json.loads(ka.render_json(self._result()))
        self.assertEqual(payload["summary"]["total"], 2)
        self.assertEqual(payload["summary"]["high"], 1)
        self.assertEqual(payload["summary"]["medium"], 1)
        self.assertEqual(len(payload["findings"]), 2)

    def test_table_output_sorts_high_first(self):
        out = ka.render_table(self._result())
        self.assertIn("2 finding(s)", out)
        self.assertLess(out.index("HIGH"), out.index("MEDIUM"))

    def test_empty_result_renders_clean_message(self):
        self.assertIn("No issues found", ka.render_table(ka.AuditResult()))


if __name__ == "__main__":
    unittest.main()
