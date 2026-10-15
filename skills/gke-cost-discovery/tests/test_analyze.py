#!/usr/bin/env python3
"""analyze.py regression tests: which pods' requests count as reserved (and billed) capacity.

    python3 skills/gke-cost-discovery/tests/test_analyze.py
"""
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


if __name__ == "__main__":
    unittest.main()
