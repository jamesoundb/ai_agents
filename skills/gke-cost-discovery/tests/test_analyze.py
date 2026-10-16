#!/usr/bin/env python3
"""analyze.py regression tests: which pods' requests count as reserved (and billed) capacity; a directory
that is not collect.sh output is refused, not reported as empty.

    python3 skills/gke-cost-discovery/tests/test_analyze.py
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import analyze  # noqa: E402

PRICING = json.load(open(os.path.join(HERE, "..", "reference", "pricing.json")))


def pod(ns, name, node, phase, cpu, mem="1Gi"):
    return {"metadata": {"namespace": ns, "name": name, "ownerReferences": [{"kind": "ReplicaSet"}]},
            "spec": {"nodeName": node, "containers": [{"name": "app", "resources": {"requests": {"cpu": cpu, "memory": mem}}}]},
            "status": {"phase": phase}}


class ReservedRequests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d)
        node = {"metadata": {"name": "n1", "labels": {"node.kubernetes.io/instance-type": "e2-standard-8"}},
                "status": {"allocatable": {"cpu": "8", "memory": "32Gi"}}}
        pods = [pod("app", "running", "n1", "Running", "1"),
                pod("app", "toobig", None, "Pending", "64"),         # unschedulable: holds no node, not billed
                pod("app", "pulling", "n1", "Pending", "2")]         # scheduled, ImagePullBackOff: holds its requests
        for name, obj in (("nodes.json", {"items": [node]}), ("pods.json", {"items": pods})):
            with open(os.path.join(self.d, name), "w") as f:
                json.dump(obj, f)
        self.rep = analyze.analyze(self.d, [], 1.25, PRICING, 4)

    def test_unscheduled_pod_is_not_namespace_cost(self):
        self.assertEqual(self.rep["snapshot"]["namespaces"]["app"]["req_cpu"], 3.0)

    def test_scheduled_pending_pod_holds_node_requests(self):
        n1 = self.rep["snapshot"]["unallocated_now"]["nodes"][0]
        self.assertEqual(n1["cpu_req_pct"], 3 / 8 * 100)


class NotACollectDirectory(unittest.TestCase):
    def run_main(self, files):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        for name, obj in files.items():
            with open(os.path.join(d, name), "w") as f:
                json.dump(obj, f)
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = analyze.main([d])
        return rc, err.getvalue()

    def test_published_report_is_pointed_at_not_reanalyzed(self):
        rc, err = self.run_main({"report.json": {"tiers": {}}, "build-tiers.json": {"tiers": {}}})
        self.assertEqual(rc, 2)
        self.assertIn("published discovery report", err)
        self.assertIn("report.json", err)

    def test_directory_without_inputs_is_refused(self):
        rc, err = self.run_main({"notes.json": {}})
        self.assertEqual(rc, 2)
        self.assertIn("none of collect.sh's outputs", err)


if __name__ == "__main__":
    unittest.main()
