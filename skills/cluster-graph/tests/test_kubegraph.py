#!/usr/bin/env python3
"""Regression tests for kubegraph.py against tests/fixture/ (a sanitized snapshot of a minikube cluster with
the failures planted by faults.yaml + testenvs.yaml). One test per troubleshooting case; the snapshot is
built into a temp dir, never into ~/.cache. Stdlib only:  python3 tests/test_kubegraph.py
"""
import contextlib
import copy
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import kubegraph as kg  # noqa: E402

FIXTURE = os.path.join(HERE, "fixture")


def run(*argv):
    """Run the CLI in-process; returns (exit code, stdout)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        try:
            rc = kg.main(list(argv))
        except SystemExit as e:
            rc = e.code
    return rc, buf.getvalue()


class Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="kubegraph-test-")
        cls.db = os.path.join(cls.tmp, "fixture.db")
        rc, out = run("snapshot", "--from", FIXTURE, "--db", cls.db)
        assert rc == 0, out
        cls.g = kg.Graph(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.g.db.close()
        shutil.rmtree(cls.tmp)

    def q(self, *argv):
        rc, out = run(*argv, "--db", self.db, "--no-refresh")
        self.assertEqual(rc, 0, out)
        return out

    def health(self, oid):
        return self.g.h(oid)[:2]

    # ---- healthy control: never reported
    def test_healthy_control_not_reported(self):
        self.assertEqual(self.health("Deployment/kg-faults/ok-web"), ("ok", "Ready"))
        self.assertEqual(self.health("Service/kg-faults/ok-web")[0], "ok")
        out = self.q("health")
        self.assertNotIn("ok-web ", out)
        system = out.split("\nkube-system\n")[1].split("\n\n")[0] if "\nkube-system\n" in out else ""
        fails = [l for l in system.splitlines() if "FAIL" in l]
        self.assertTrue(all("NodeLost" in l for l in fails), fails)   # only pods stranded on the stopped node
        self.assertNotIn("etcd", system)   # restarts of long-lived instances (node reboot) are not reported

    # ---- pod failure classes
    def test_crashloop(self):
        pod = self.g.resolve("crashy", "kg-faults") and [i for i in self.g.nodes if i.startswith("Pod/kg-faults/crashy-")][0]
        self.assertEqual(self.health(pod), ("fail", "CrashLoopBackOff"))
        self.assertEqual(self.health("Service/kg-faults/crashy"), ("fail", "NoReadyEndpoints"))
        out = self.q("why", "svc/crashy")
        self.assertIn(f"cause: {pod} CrashLoopBackOff", out)
        self.assertIn("advice: the container exits right after start", out)

    def test_image_pull(self):
        pod = [i for i in self.g.nodes if i.startswith("Pod/kg-faults/badimage-")][0]
        self.assertIn(self.health(pod)[1], ("ImagePullBackOff", "ErrImagePull"))
        self.assertIn("registry.invalid/team/api:1.0", self.g.h(pod)[2])

    def test_unschedulable(self):
        pod = [i for i in self.g.nodes if i.startswith("Pod/kg-faults/toobig-")][0]
        self.assertEqual(self.health(pod), ("fail", "Unschedulable"))
        self.assertIn("Insufficient cpu", self.g.h(pod)[2])

    def test_missing_configmap(self):
        self.assertEqual(self.health("ConfigMap/kg-faults/app-settings"), ("fail", "missing"))
        out = self.q("why", "deploy/missing-config")
        self.assertIn("cause: ConfigMap/kg-faults/app-settings missing", out)
        self.assertIn("env LOG_LEVEL key log_level", out)

    def test_missing_key(self):
        self.assertEqual(self.health("ConfigMap/kg-faults/web-cfg"), ("fail", "missing key"))
        self.assertIn("feature_flags", self.g.h("ConfigMap/kg-faults/web-cfg")[2])
        out = self.q("why", "deploy/missing-key")
        self.assertIn("cause: ConfigMap/kg-faults/web-cfg missing key", out)

    def test_memory_kill(self):
        # the docker runtime on minikube records this kill as OOMKilled on some restarts and as exit 137
        # "Error" on others; both must name the memory limit
        pod = [i for i in self.g.nodes if i.startswith("Pod/kg-faults/oomer-")][0]
        h, r, s = self.g.h(pod)
        self.assertEqual(h, "fail")
        self.assertIn(r, ("OOMKilled", "Killed"))
        self.assertIn("memory limit 16Mi", s)

    def oomer_with(self, reason):
        o = json.loads(sqlite3.connect(self.db).execute(
            "SELECT raw FROM nodes WHERE id LIKE 'Pod/kg-faults/oomer-%'").fetchone()[0])
        cs = o["status"]["containerStatuses"][0]
        cs["lastState"] = {"terminated": {"reason": reason, "exitCode": 137}}
        cs["state"] = {"waiting": {"reason": "CrashLoopBackOff"}}
        return kg.pod_health(o, kg.ts(o["metadata"]["creationTimestamp"]) + 600)

    def test_oomkilled_recorded(self):
        h, r, s = self.oomer_with("OOMKilled")
        self.assertEqual((h, r), ("fail", "OOMKilled"))
        self.assertIn("memory limit 16Mi", s)

    def test_sigkill_without_oom_record(self):
        h, r, s = self.oomer_with("Error")
        self.assertEqual((h, r), ("fail", "Killed"))
        self.assertIn("137 = SIGKILL: out of memory, memory limit 16Mi", s)

    def test_probe_failing(self):
        pod = [i for i in self.g.nodes if i.startswith("Pod/kg-faults/unready-")][0]
        self.assertEqual(self.health(pod), ("fail", "ProbeFailing"))
        self.assertEqual(self.health("Service/kg-faults/unready"), ("fail", "NoReadyEndpoints"))
        out = self.q("why", "deploy/unready")
        self.assertIn("selects its pods: FAIL Service/kg-faults/unready", out)

    def test_selector_matches_nothing(self):
        self.assertEqual(self.health("Service/kg-faults/web-typo"), ("fail", "NoPods"))
        out = self.q("why", "svc/web-typo")
        first = out.split("closest pod labels")[1].splitlines()[1]
        self.assertIn("Deployment/kg-faults/ok-web", first)   # nearest label value ranked first

    def test_pvc_chain_to_missing_storageclass(self):
        self.assertEqual(self.health("PersistentVolumeClaim/kg-faults/data-pending"), ("fail", "ProvisioningFailed"))
        out = self.q("why", "deploy/uses-pvc")
        self.assertIn("cause: StorageClass/does-not-exist missing", out)

    def test_job_backoff(self):
        self.assertEqual(self.health("Job/kg-faults/migrate-db"), ("fail", "BackoffLimitExceeded"))

    def test_hpa(self):
        self.assertEqual(self.health("HorizontalPodAutoscaler/kg-faults/no-requests"), ("fail", "FailedGetResourceMetric"))
        out = self.q("why", "hpa/orphan-hpa")
        self.assertIn("cause: Deployment/kg-faults/renamed-away missing", out)

    def test_ingress_backend_missing(self):
        self.assertEqual(self.health("Ingress/kg-faults/shop"), ("fail", "BackendUnavailable"))
        self.assertEqual(self.health("Service/kg-faults/shop-frontend"), ("fail", "missing"))

    def test_quota(self):
        self.assertEqual(self.health("Deployment/kg-quota/quota-hit"), ("fail", "ReplicaFailure"))
        self.assertEqual(self.health("ResourceQuota/kg-quota/pods"), ("warn", "QuotaExhausted"))

    # ---- cluster-scoped components
    def test_apiservice_unavailable(self):
        self.assertEqual(self.health("APIService/v1beta1.broken.kg.example.com"), ("fail", "ServiceNotFound"))
        out = self.q("health")
        self.assertLess(out.index("(cluster-scoped)"), out.index("\nkg-faults"))   # cluster-wide causes first

    def test_webhook_without_backend(self):
        h, r, s = self.g.h("ValidatingWebhookConfiguration/kg-policy")
        self.assertEqual((h, r), ("fail", "WebhookUnavailable"))
        self.assertIn("blocks admission", s)

    def test_released_pv(self):
        self.assertEqual(self.health("PersistentVolume/kg-retained"), ("warn", "Released"))

    def test_custom_resources(self):
        self.assertEqual(self.health("TestEnv/kg-faults/te-broken"), ("fail", "Ready=False ProvisionFailed"))
        self.assertEqual(self.health("TestEnv/kg-faults/te-ok")[0], "ok")
        self.assertEqual(self.g.resolve("te/te-broken"), "TestEnv/kg-faults/te-broken")   # CRD short name
        out = self.q("crds")
        self.assertIn("TestEnv (kg.example.com, Namespaced): 2 instances, 1 unhealthy", out)
        self.assertIn("uses -    Secret/kg-faults/db-credentials", self.q("why", "te/te-broken"))  # spec.secretName

    def test_unavailable_api_group_reported(self):
        self.assertIn("broken.kg.example.com/v1beta1", " ".join(self.g.meta["errors"]))

    # ---- graph questions
    def test_used_by(self):
        out = self.q("used-by", "cm/web-cfg")
        self.assertIn("Deployment/kg-faults/ok-web", out)
        self.assertIn("Deployment/kg-faults/missing-key", out)
        self.assertIn("Ingress/kg-faults/shop", self.q("used-by", "svc/ok-web"))

    def test_options_before_the_command(self):
        rc, out = run("--db", self.db, "--no-refresh", "why", "deploy/uses-pvc")
        self.assertEqual(rc, 0, out)
        self.assertIn("cause: StorageClass/does-not-exist missing", out)

    def test_resolve(self):
        self.assertEqual(self.g.resolve("deploy/crashy"), "Deployment/kg-faults/crashy")
        self.assertEqual(self.g.resolve("crashy"), "Deployment/kg-faults/crashy")   # workload wins a bare name
        with self.assertRaises(kg.QueryError):
            self.g.resolve("svc/nope")

    def test_health_groups_under_root(self):
        out = self.q("health", "-n", "kg-faults")
        block = out.split("FAIL Deployment/kg-faults/crashy")[1].split("\n  FAIL")[0]
        self.assertIn("└ FAIL Pod/kg-faults/crashy-", block)
        self.assertNotIn("FAIL ReplicaSet/", block)   # intermediate owners mirror the root: hidden

    # ---- rollout, candidates, manifests, webhooks, waste, reach
    def test_rollout_stuck(self):
        h, r, s = self.g.h("Deployment/kg-faults/checkout")
        self.assertEqual((h, r), ("fail", "RolloutStuck"))
        self.assertRegex(s, r"revision \d+ 0/1 ready, its pods (ImagePullBackOff|ErrImagePull|NodeLost); revision \d+ still serving 2 pods")

    def test_reboot_restart_is_not_the_cause(self):
        # the unready pod's previous instance ran ~4h and died with the node: still a readiness problem
        pod = [i for i in self.g.nodes if i.startswith("Pod/kg-faults/unready-")][0]
        self.assertEqual(self.health(pod), ("fail", "ProbeFailing"))

    def test_missing_candidates(self):
        out = self.q("why", "deploy/uses-pvc", "deploy/missing-config", "deploy/missing-key", "--no-logs")
        self.assertIn("no StorageClass named does-not-exist in any namespace; existing: StorageClass/standard (default)", out)
        self.assertIn("no ConfigMap named app-settings in any namespace; existing: ConfigMap/kg-faults/web-cfg", out)
        self.assertNotIn("kube-root-ca.crt", out)
        self.assertIn("keys present: index.html, mode", out)

    def test_manifest_locations(self):
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(repo, "k8s"), exist_ok=True)
        shutil.copy(os.path.join(HERE, "faults.yaml"), os.path.join(repo, "k8s", "app.yaml"))
        out = self.q("why", "deploy/missing-config", "--repo", repo, "--no-logs")
        self.assertIn("defined at k8s/app.yaml:103", out)                         # block metadata
        self.assertIn("ConfigMap/kg-faults/app-settings not defined in this repository", out)
        out = self.q("why", "ingress/shop", "--repo", repo, "--no-logs")
        self.assertIn("Ingress/kg-faults/shop defined at k8s/app.yaml:", out)       # flow metadata
        self.assertNotIn("defined at", self.q("why", "deploy/crashy", "--no-repo", "--no-logs"))

    def test_webhook_scope(self):
        out = self.q("show", "ValidatingWebhookConfiguration/kg-policy")
        self.assertIn("CREATE/UPDATE deployments; failurePolicy Fail; matches no namespace now", out)

    def test_waste(self):
        out = self.q("waste", "--min-age", "0")
        self.assertIn("Deployment/kg-faults/crashy  1 pod(s) CrashLoopBackOff", out)          # held requests
        self.assertIn("Deployment/kg-preview-123/preview-app  2 pod(s) request cpu 1.00", out)  # over-requested
        self.assertIn("PersistentVolumeClaim/kg-preview-123/orphan-data  Bound 5Gi, no pod mounts it", out)
        self.assertNotIn("used-data", out)                                                    # mounted: not waste
        self.assertIn("PersistentVolume/kg-retained  Released 1Gi", out)
        self.assertIn("Service/kg-preview-123/preview-lb", out)
        self.assertIn("Job/kg-preview-123/seed-data  Complete, no ttlSecondsAfterFinished", out)
        self.assertNotIn("Job/kg-faults/migrate-db", out)                                     # has a TTL
        self.assertIn("Deployment/kg-preview-123/preview-app  up", out)                       # env without TTL
        self.assertNotIn("preview-ttl", out)                                                  # env with TTL
        self.assertIn("Pod/kg-preview-123/debug-shell  no controller (bare pod), local storage", out)
        self.assertIn("safe-to-evict=false", out)
        self.assertIn("PodDisruptionBudget/kg-preview-123/pinned  allows 0 disruptions", out)

    def test_reach(self):
        out = self.q("reach", "deploy/frontend", "svc/api", "-n", "kg-net")
        self.assertIn("port: Service port 80 -> targetPort http = 8080", out)       # named targetPort resolved
        self.assertIn("allowed by NetworkPolicy/kg-net/api-from-frontend ingress rule 1", out)
        self.assertIn("verdict: allowed", out)
        self.assertIn("no policy-enforcing CNI found", out)
        out = self.q("reach", "deploy/tester", "svc/api", "-n", "kg-net")
        self.assertIn("ingress to destination: DENIED", out)
        self.assertIn("NetworkPolicy/kg-net/default-deny-ingress: no ingress rules (deny all)", out)
        self.assertIn("verdict: denied", out)
        out = self.q("reach", "deploy/api", "svc/db", "-n", "kg-net")
        self.assertIn("egress from source: allowed by NetworkPolicy/kg-net/api-egress", out)
        self.assertIn("ingress to destination: allowed by NetworkPolicy/kg-net/db-from-api", out)   # named port pg
        self.assertIn("dns from source (kube-dns 53/UDP): DENIED", out)
        self.assertIn("podSelector: {matchLabels: {k8s-app: kube-dns}}", out)       # the rule to add
        repo = os.path.join(self.tmp, "netrepo")
        os.makedirs(repo, exist_ok=True)
        shutil.copy(os.path.join(HERE, "faults-2.yaml"), os.path.join(repo, "net.yaml"))
        out = self.q("reach", "deploy/api", "svc/db", "-n", "kg-net", "--repo", repo)
        self.assertIn("NetworkPolicy/kg-net/api-egress defined at net.yaml:", out)
        self.assertNotIn("pod-template-hash", out)

    # ---- Helm releases, GitOps objects, node failure
    def test_helm_release_failed_upgrade(self):
        h, r, s = self.g.h("Release/kg-helm/kg-shop")
        self.assertEqual((h, r), ("fail", "UpgradeFailed"))
        self.assertIn("revision 2 failed", s)
        self.assertIn("last good revision 1", s)
        out = self.q("why", "Release/kg-helm/kg-shop", "--no-logs", "--no-repo")
        self.assertRegex(out, r"cause: Pod/kg-helm/kg-shop-\S+ (ImagePullBackOff|ErrImagePull)")
        self.assertNotIn("(via Service", out)                        # the pod is the release's own descendant
        self.assertEqual(len(out.split("children:")[1].split("depends on:")[0].splitlines()),
                         len(set(out.split("children:")[1].split("depends on:")[0].splitlines())))   # no duplicates

    def test_helm_lock(self):
        # Flux reports Helm's lock: the pending revision is the cause, whatever its age
        self.assertEqual(self.health("Release/kg-helm/kg-cart"), ("fail", "PendingOperation"))
        out = self.q("why", "HelmRelease/kg-helm/cart", "--no-logs", "--no-repo")
        self.assertIn("cause: Release/kg-helm/kg-cart PendingOperation", out)
        self.assertIn("advice: a helm operation was interrupted", out)
        # without a manager: pending under helm's 5-minute timeout warns, past it fails
        rel = {"apiVersion": "helm.sh/v3", "revisions": [[1, "deployed", 1000.0], [2, "pending-upgrade", 2000.0]]}
        self.assertEqual(kg.release_health(rel, 2100.0)[:2], ("warn", "pending-upgrade"))
        self.assertEqual(kg.release_health(rel, 2400.0)[:2], ("fail", "PendingOperation"))

    def test_argo_application(self):
        out = self.q("why", "Application/kg-gitops/shop", "--no-logs", "--no-repo")
        self.assertIn("manages FAIL ConfigMap/kg-helm/kg-shop-flags  missing", out)   # Argo health Missing
        self.assertIn("[argo health Degraded, sync Synced]", out)
        self.assertRegex(out, r"cause: Pod/kg-helm/kg-shop-\S+ (ImagePullBackOff|ErrImagePull)")
        self.assertIn("also: ConfigMap/kg-helm/kg-shop-flags missing", out)

    def test_node_failure(self):
        self.assertEqual(self.health("Node/agents-test-m02"), ("fail", "NodeNotReady"))
        pods = [i for i in self.g.nodes if i.startswith("Pod/kg-faults/on-m02-")]
        self.assertTrue(pods and all(self.health(p) == ("fail", "NodeLost") for p in pods), pods)
        out = self.q("why", "deploy/on-m02", "--no-logs", "--no-repo")
        self.assertIn("cause: Node/agents-test-m02 NodeNotReady", out)
        out = self.q("why", "Node/agents-test-m02", "--no-logs", "--no-repo")
        self.assertNotIn("children:\nused by", out)                 # no empty section header
        self.assertIn("runs-on by FAIL Pod/kg-faults/on-m02-", out)
        self.assertRegex(out, r"taints: .*node.kubernetes.io/unreachable:NoExecute")    # no describe node needed
        self.assertIn("next: the machine and its kubelet", out)
        self.assertRegex(out, r"last heartbeat \d+m ago; Ready=Unknown")

    # ---- secrets never stored
    def test_no_secret_values_stored(self):
        dump = "\n".join(r[0] or "" for r in sqlite3.connect(self.db).execute("SELECT raw FROM nodes"))
        for needle in ("hunter2", "aHVudGVyMg"):
            self.assertNotIn(needle, dump)
        for name in os.listdir(FIXTURE):
            with open(os.path.join(FIXTURE, name)) as f:
                self.assertNotIn("hunter2", f.read(), name)
        sec = json.loads(sqlite3.connect(self.db).execute(
            "SELECT raw FROM nodes WHERE id='Secret/kg-faults/db-credentials'").fetchone()[0])
        self.assertNotIn("data", sec)
        self.assertEqual(sec["dataKeys"], ["password", "username"])


class Units(unittest.TestCase):
    def test_sanitize_secret_and_env(self):
        o = {"kind": "Secret", "metadata": {"name": "s", "managedFields": [1],
                                            "annotations": {kg.LAST_APPLIED: '{"stringData":{"p":"x"}}'}},
             "data": {"p": "eA=="}, "stringData": {"q": "y"}}
        kg.sanitize(o)
        self.assertEqual(o["dataKeys"], ["p", "q"])
        self.assertNotIn("data", o)
        self.assertNotIn("managedFields", o["metadata"])
        self.assertEqual(o["metadata"]["annotations"][kg.LAST_APPLIED], "<dropped>")
        pod = {"kind": "Pod", "spec": {"containers": [{"env": [{"name": "API_TOKEN", "value": "abc"},
                                                              {"name": "MODE", "value": "prod"}]}]},
               "status": {}, "metadata": {}}
        kg.sanitize(pod)
        env = pod["spec"]["containers"][0]["env"]
        self.assertEqual((env[0]["value"], env[1]["value"]), ("<redacted>", "prod"))
        cr = {"kind": "Db", "metadata": {}, "spec": {"password": "pw", "secretName": "keep-me"}}
        kg.sanitize(cr)
        self.assertEqual((cr["spec"]["password"], cr["spec"]["secretName"]), ("<redacted>", "keep-me"))

    def test_read_only_guard(self):
        for args in (["delete", "pod", "x"], ["apply", "-f", "x"], ["config", "use-context", "prod"],
                     ["scale", "deploy/x", "--replicas=0"], ["exec", "x", "--", "sh"]):
            with self.assertRaises(SystemExit, msg=args):
                kg.kubectl(None, args)

    def test_api_resources_columns(self):
        new = ("NAME          SHORTNAMES   APIVERSION   NAMESPACED   KIND        VERBS          CATEGORIES\n"
               "bindings                   v1           true         Binding     [create]\n"
               "pods          po           v1           true         Pod         [get list]     all\n"
               "deployments   deploy       apps/v1      true         Deployment  [get list]     all\n")
        rows = kg.parse_api_resources(new)
        self.assertEqual([(r["name"], r["group"], r["short"]) for r in rows],
                         [("bindings", "", []), ("pods", "", ["po"]), ("deployments", "apps", ["deploy"])])
        old = ("NAME     SHORTNAMES   APIGROUP   NAMESPACED   KIND   VERBS\n"
               "nodes    no                      false        Node   [get list]\n")
        self.assertEqual(kg.parse_api_resources(old)[0]["namespaced"], False)

    def test_quantities(self):
        self.assertAlmostEqual(kg.cpu("250m"), 0.25)
        self.assertAlmostEqual(kg.cpu("1500000n"), 0.0015)
        self.assertEqual(kg.mem("1Gi"), 1024 ** 3)
        self.assertEqual(kg.mem("128M"), 128e6)
        self.assertEqual(kg.pod_requests({"containers": [{"resources": {"limits": {"cpu": "1", "memory": "1Gi"}}}],
                                          "initContainers": [{"resources": {"requests": {"cpu": "2"}}}]}),
                         (2.0, 1024 ** 3))

    def test_node_conditions(self):
        base = json.load(open(os.path.join(FIXTURE, "nodes.json")))["items"][0]
        n = copy.deepcopy(base)
        n["spec"]["unschedulable"] = True
        self.assertEqual(kg.node_health(n)[:2], ("warn", "Cordoned"))
        n = copy.deepcopy(base)
        for c in n["status"]["conditions"]:
            if c["type"] == "Ready":
                c.update(status="Unknown", message="Kubelet stopped posting node status.")
        self.assertEqual(kg.node_health(n)[:2], ("fail", "NodeNotReady"))
        n = copy.deepcopy(base)
        for c in n["status"]["conditions"]:
            if c["type"] == "DiskPressure":
                c["status"] = "True"
        self.assertEqual(kg.node_health(n)[:2], ("fail", "DiskPressure"))

    def test_generic_custom_resource_shapes(self):
        argo = {"status": {"health": {"status": "Degraded", "message": "Deployment api unavailable"},
                           "sync": {"status": "Synced"}}}
        self.assertEqual(kg.generic_health(argo)[:2], ("fail", "health Degraded"))
        lag = {"metadata": {"generation": 4}, "status": {"observedGeneration": 3}}
        self.assertEqual(kg.generic_health(lag)[:2], ("warn", "NotReconciled"))
        self.assertEqual(kg.generic_health({"spec": {}})[0], "-")


if __name__ == "__main__":
    unittest.main(verbosity=1)
