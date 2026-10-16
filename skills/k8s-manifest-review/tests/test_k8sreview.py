#!/usr/bin/env python3
"""k8sreview.py regression tests: YAML that is not a Kubernetes object is not schema-validated; workloads
are checked against the LimitRange, ResourceQuota and kube-janitor rule reviewed with them.

    python3 skills/k8s-manifest-review/tests/test_k8sreview.py
"""
import contextlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import k8sreview as kr  # noqa: E402

LIMITRANGE = """apiVersion: v1
kind: LimitRange
metadata:
  name: build-tiers
  namespace: ci-builds
spec:
  limits:
    - type: Container
      max: {cpu: "6", memory: 24Gi}
"""
# k8s-guardrails writes this next to its LimitRange: a kube-janitor rules-file entry, no apiVersion/kind
JANITOR_RULE = """rules:
  - id: default-ttl-ci-builds
    resources: [deployments]
    jmespath: "metadata.namespace == 'ci-builds'"
    ttl: 8h
"""
# Stands in for kubectl: like the real one, it cannot decode a document without a kind
FAKE_KUBECTL = """#!/bin/sh
for a in "$@"; do f="$a"; done
for a in "$@"; do [ "$prev" = "-f" ] && f="$a"; prev="$a"; done
echo "$f" >> "$KUBECTL_LOG"
grep -q '^kind:' "$f" || { echo "error: Object 'Kind' is missing" >&2; exit 1; }
"""


class NonObjectYaml(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d)
        bin_dir, out = os.path.join(self.d, "bin"), os.path.join(self.d, "guardrails")
        os.makedirs(bin_dir)
        os.makedirs(out)
        kubectl = os.path.join(bin_dir, "kubectl")
        with open(kubectl, "w") as f:
            f.write(FAKE_KUBECTL)
        os.chmod(kubectl, os.stat(kubectl).st_mode | stat.S_IEXEC)
        for name, text in (("ci-builds-limitrange.yaml", LIMITRANGE), ("ci-builds-kube-janitor-rule.yaml", JANITOR_RULE)):
            with open(os.path.join(out, name), "w") as f:
                f.write(text)
        self.out, self.log = out, os.path.join(self.d, "kubectl.log")
        env = {"PATH": bin_dir + os.pathsep + "/bin", "KUBECTL_LOG": self.log}   # no kubeconform on PATH
        self.old_env = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        self.addCleanup(self.restore_env)

    def restore_env(self):
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_rules_file_beside_manifests_does_not_fail_the_gate(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = kr.main([self.out, "--json"])
        rep = json.loads(buf.getvalue())
        self.assertEqual(rc, 0, rep["tools"])
        self.assertEqual(rep["objects"], 1)
        with open(self.log) as f:
            validated = f.read()
        self.assertIn("ci-builds-limitrange.yaml", validated)
        self.assertNotIn("kube-janitor-rule", validated)
        skipped = [t for t in rep["tools"] if "kube-janitor-rule" in t.get("file", "")]
        self.assertEqual([t["ok"] for t in skipped], [None])
        self.assertIn("no Kubernetes object", skipped[0]["output"])


# What k8s-guardrails generates for tiers small 1/4Gi, medium 2/8Gi, large 6/24Gi
GUARDRAILS = """apiVersion: v1
kind: LimitRange
metadata: {name: build-tiers, namespace: ci-builds}
spec:
  limits:
    - type: Container
      defaultRequest: {cpu: "1", memory: 4Gi}
      default: {cpu: "2", memory: 4Gi}
      max: {cpu: "6", memory: 24Gi}
      maxLimitRequestRatio: {cpu: "4", memory: "1"}
    - type: Pod
      max: {cpu: "6", memory: 24Gi}
---
apiVersion: v1
kind: ResourceQuota
metadata: {name: build-capacity, namespace: ci-builds}
spec:
  hard: {requests.cpu: "32", requests.memory: 128Gi, limits.memory: 128Gi, pods: "32", persistentvolumeclaims: "0"}
"""
JANITOR = """rules:
  - id: default-ttl-ci-builds
    resources: [deployments, statefulsets, jobs]
    jmespath: "metadata.namespace == 'ci-builds' && !contains(keys(metadata.annotations || `{}`), 'janitor/ttl')"
    ttl: 8h
"""


def job(name, req, lim, ns="ci-builds"):
    return f"""apiVersion: batch/v1
kind: Job
metadata: {{name: {name}, namespace: {ns}}}
spec:
  template:
    spec:
      containers:
        - name: main
          image: busybox:1.36
          resources: {{requests: {req}, limits: {lim}}}
"""


CACHE = """apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: gradle-cache, namespace: ci-builds}
spec: {accessModes: [ReadWriteOnce], resources: {requests: {storage: 100Gi}}}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: gradle-cache, namespace: ci-builds}
spec:
  template:
    spec:
      containers:
        - name: cache
          image: gradle/build-cache-node:18.1
          resources: {requests: {cpu: 500m, memory: 2Gi}, limits: {cpu: "1", memory: 2Gi}}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: preview, namespace: ci-builds, annotations: {janitor/ttl: 6h}}
spec:
  template:
    spec:
      containers:
        - name: web
          image: nginx:1.27
          resources: {requests: {cpu: 500m, memory: 1Gi}, limits: {cpu: "1", memory: 1Gi}}
"""


class AdmissionAgainstGuardrails(unittest.TestCase):
    """One test per consequence k8s-guardrails SKILL.md step 3 asks to explain before adoption."""

    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp()
        files = {"guardrails/ci-builds-limitrange.yaml": GUARDRAILS, "guardrails/ci-builds-kube-janitor-rule.yaml": JANITOR,
                 "manifests/perf.yaml": job("perf-tests", "{cpu: \"8\", memory: 32Gi}", "{cpu: \"8\", memory: 32Gi}"),
                 "manifests/lint.yaml": job("lint", "{cpu: 250m, memory: 1Gi}", "{cpu: \"2\", memory: 1Gi}"),
                 "manifests/big-heap.yaml": job("big-heap", "{cpu: \"1\", memory: 6Gi}", "{cpu: \"2\"}"),
                 "manifests/unit.yaml": job("unit-tests", "{cpu: \"1\", memory: 4Gi}", "{cpu: \"2\", memory: 4Gi}"),
                 "manifests/other-ns.yaml": job("perf-tests", "{cpu: \"8\", memory: 32Gi}", "{cpu: \"8\", memory: 32Gi}", ns="perf"),
                 "manifests/cache.yaml": CACHE}
        for name, text in files.items():
            p = os.path.join(cls.d, name)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as f:
                f.write(text)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            kr.main([os.path.join(cls.d, "manifests"), os.path.join(cls.d, "guardrails"), "--json", "--no-tools"])
        cls.adm = [f for f in json.loads(buf.getvalue())["findings"] if f["rule"].startswith("ADM")]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.d)

    def hits(self, rule, file):
        return [f["message"] for f in self.adm if f["rule"] == rule and f["file"].endswith(file)]

    def test_container_above_max_is_rejected(self):
        self.assertTrue(any("cpu limit 8 > max 6" in m for m in self.hits("ADM001", "perf.yaml")), self.adm)

    def test_limit_to_request_ratio_is_rejected(self):
        self.assertTrue(any("cpu limit/request 8.0 > maxLimitRequestRatio 4" in m for m in self.hits("ADM002", "lint.yaml")), self.adm)

    def test_default_limit_below_request_is_rejected(self):
        self.assertTrue(any("memory request 6Gi > default limit 4Gi" in m for m in self.hits("ADM003", "big-heap.yaml")), self.adm)

    def test_claim_over_the_quota_is_blocked(self):
        self.assertTrue(any("persistentvolumeclaims 0" in m for m in self.hits("ADM004", "cache.yaml")), self.adm)

    def test_unannotated_long_lived_object_expires(self):
        msgs = self.hits("ADM005", "cache.yaml")
        self.assertEqual(len(msgs), 1, self.adm)   # the cache, not the annotated preview
        self.assertIn("Deployment/gradle-cache", [f["object"] for f in self.adm if f["rule"] == "ADM005"])
        self.assertIn("8h", msgs[0])

    def test_fitting_and_other_namespace_workloads_untouched(self):
        self.assertEqual(self.hits("ADM001", "unit.yaml") + self.hits("ADM002", "unit.yaml"), [])
        self.assertEqual([f for f in self.adm if f["file"].endswith("other-ns.yaml")], [])


class BackoffLimit(unittest.TestCase):
    def eff007(self, spec_line):
        rep = kr.Report(kr.load_policy(None, None, []))
        kr.review_text(f"apiVersion: batch/v1\nkind: Job\nmetadata: {{name: t, namespace: ci}}\nspec:\n{spec_line}"
                       "  template: {spec: {containers: [{name: c, image: busybox:1.36}]}}\n", "job.yaml", rep, True)
        return [f["message"] for f in rep.findings if f["rule"] == "EFF007"]

    def test_zero_retries_is_not_flagged(self):
        self.assertEqual(self.eff007("  backoffLimit: 0\n"), [])

    def test_default_six_retries_is_flagged(self):
        self.assertEqual(self.eff007(""), ["backoffLimit 6 retries a failing build"])


if __name__ == "__main__":
    unittest.main()
