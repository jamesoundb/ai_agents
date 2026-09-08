#!/usr/bin/env python3
"""
synth.py — generate a realistic synthetic discovery directory so analyze.py can be tested and
demonstrated without cluster credentials. Shapes match what collect.sh writes.

  synth.py OUT_DIR [--seed 7] [--days 14]
"""
import argparse
import json
import os
import random
from datetime import datetime, timedelta, timezone


def q(v, unit=""):
    return f"{v}{unit}"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--days", type=int, default=14)
    a = ap.parse_args(argv)
    random.seed(a.seed)
    os.makedirs(a.out, exist_ok=True)
    now = datetime.now(timezone.utc)
    days = a.days
    json.dump({"collected_at": now.isoformat(), "days": days, "project": "acme-ci", "cluster": "ci-standard", "location": "us-central1",
               "build_namespaces": "teamcity-agents,test-envs"}, open(f"{a.out}/meta.json", "w"))

    # cluster: an on-demand build pool with min 2 per zone across 3 zones, BALANCED profile, no VPA
    cluster = {"name": "ci-standard", "location": "us-central1", "currentNodeCount": 14, "autoscaling": {"autoscalingProfile": "BALANCED"},
               "verticalPodAutoscaling": {"enabled": False},
               "nodePools": [
                   {"name": "system", "config": {"machineType": "e2-standard-2"}, "autoscaling": {"enabled": True, "minNodeCount": 1, "maxNodeCount": 3}, "locations": ["us-central1-a"]},
                   {"name": "build-agents", "config": {"machineType": "n2-standard-16"}, "autoscaling": {"enabled": True, "minNodeCount": 2, "maxNodeCount": 10}, "locations": ["us-central1-a", "us-central1-b", "us-central1-c"]},
                   {"name": "test-envs", "config": {"machineType": "e2-standard-8", "spot": False}, "autoscaling": {"enabled": True, "minNodeCount": 1, "maxNodeCount": 6}, "locations": ["us-central1-a", "us-central1-b"]}]}
    json.dump(cluster, open(f"{a.out}/cluster.json", "w"))

    # nodes
    nodes, node_names = [], []
    def node(name, pool, mtype, cpu_, mem_gi):
        node_names.append(name)
        nodes.append({"metadata": {"name": name, "labels": {"cloud.google.com/gke-nodepool": pool, "node.kubernetes.io/instance-type": mtype}},
                      "status": {"allocatable": {"cpu": q(int(cpu_ * 1000 - 100), "m"), "memory": q(int(mem_gi * 1024 - 1500), "Mi")}}})
    node("gke-ci-system-1", "system", "e2-standard-2", 2, 8)
    for i in range(1, 7):
        node(f"gke-ci-build-agents-{i}", "build-agents", "n2-standard-16", 16, 64)
    for i in range(1, 8):
        node(f"gke-ci-test-envs-{i}", "test-envs", "e2-standard-8", 8, 32)
    json.dump({"items": nodes}, open(f"{a.out}/nodes.json", "w"))

    # pods: build agents (over-requested), test envs (some stale), a bare pod, completed jobs, pods without requests
    pods, workloads = [], []
    def pod(ns, name, node_, containers, phase="Running", owner=True, age_h=1.0, annotations=None):
        pods.append({"metadata": {"namespace": ns, "name": name, "annotations": annotations or {}, "ownerReferences": [{"kind": "Job"}] if owner else []},
                     "spec": {"nodeName": node_, "containers": containers},
                     "status": {"phase": phase, "startTime": (now - timedelta(hours=age_h)).strftime("%Y-%m-%dT%H:%M:%SZ")}})
    for i in range(8):
        pod("teamcity-agents", f"tc-agent-unit-{i}", f"gke-ci-build-agents-{i % 6 + 1}",
            [{"name": "agent", "resources": {"requests": {"cpu": "4", "memory": "16Gi"}, "limits": {"cpu": "16", "memory": "16Gi"}}}], age_h=random.uniform(0.2, 1.5))
    for i in range(3):
        pod("teamcity-agents", f"tc-agent-integration-{i}", f"gke-ci-build-agents-{i + 1}",
            [{"name": "agent", "resources": {"requests": {"cpu": "8", "memory": "32Gi"}, "limits": {"cpu": "8", "memory": "32Gi"}}}], age_h=random.uniform(0.5, 2))
    pod("teamcity-agents", "tc-agent-nolimits-0", "gke-ci-build-agents-6", [{"name": "agent", "resources": {}}])
    for i in range(5):
        pod("test-envs", f"api-pr-{100 + i}-0", f"gke-ci-test-envs-{i + 1}",
            [{"name": "api", "resources": {"requests": {"cpu": "2", "memory": "4Gi"}, "limits": {"cpu": "2", "memory": "4Gi"}}},
             {"name": "db", "resources": {"requests": {"cpu": "1", "memory": "2Gi"}, "limits": {"cpu": "1", "memory": "2Gi"}}}], age_h=[2, 30, 75, 120, 300][i])
        workloads.append({"kind": "Deployment", "metadata": {"namespace": "test-envs", "name": f"api-pr-{100 + i}", "creationTimestamp": (now - timedelta(hours=[2, 30, 75, 120, 300][i])).strftime("%Y-%m-%dT%H:%M:%SZ")},
                         "spec": {"replicas": 3 if i % 2 else 1}})
    pod("test-envs", "debug-shell", "gke-ci-test-envs-7", [{"name": "sh", "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}}}], owner=False, age_h=200,
        annotations={"cluster-autoscaler.kubernetes.io/safe-to-evict": "false"})
    for i in range(12):
        pod("teamcity-agents", f"tc-agent-old-{i}", None, [{"name": "agent", "resources": {"requests": {"cpu": "4", "memory": "16Gi"}}}], phase="Succeeded" if i % 3 else "Failed", age_h=40)
    pod("kube-system", "kube-dns-1", "gke-ci-system-1", [{"name": "dns", "resources": {"requests": {"cpu": "250m", "memory": "70Mi"}}}])
    json.dump({"items": pods}, open(f"{a.out}/pods.json", "w"))
    for i in range(6):
        workloads.append({"kind": "Job", "metadata": {"namespace": "teamcity-agents", "name": f"tc-job-{i}"}, "spec": {} if i < 4 else {"ttlSecondsAfterFinished": 600, "activeDeadlineSeconds": 7200}})
    # kube-janitor deployed, but in dry-run and excluding test-envs: the classic reason environments linger
    workloads.append({"kind": "Deployment", "metadata": {"namespace": "kube-janitor", "name": "kube-janitor", "creationTimestamp": (now - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")},
                      "spec": {"replicas": 1, "template": {"spec": {"containers": [{"name": "janitor", "image": "hjacobs/kube-janitor:23.3.0",
                               "args": ["--dry-run", "--interval=300", "--exclude-namespaces=kube-system,test-envs", "--include-resources=deployments,jobs"]}]}}}})
    json.dump({"items": workloads}, open(f"{a.out}/workloads.json", "w"))
    json.dump({"items": [{"kind": "LimitRange", "metadata": {"namespace": "test-envs"}}]}, open(f"{a.out}/quotas.json", "w"))
    json.dump({"items": []}, open(f"{a.out}/pdb.json", "w"))

    # time series: hourly points over the window
    hours = days * 24
    def ts(labels, values, system=None):
        return {"resource": {"type": "k8s_container", "labels": labels}, "metadata": {"systemLabels": system or {}},
                "points": [{"interval": {"endTime": (now - timedelta(hours=h)).isoformat()}, "value": {"doubleValue": v}} for h, v in enumerate(values)]}
    def wl(ns, ctl, cont, req_c, req_m_gib, util_c, util_m, present):
        n = int(hours * present)
        return (ts({"namespace_name": ns, "container_name": cont}, [max(0.01, random.gauss(util_c, util_c * 0.15)) for _ in range(n)], {"top_level_controller_name": ctl}),
                ts({"namespace_name": ns, "container_name": cont}, [max(0.01, random.gauss(util_m, util_m * 0.1)) for _ in range(n)], {"top_level_controller_name": ctl}),
                ts({"namespace_name": ns, "container_name": cont}, [req_c] * n, {"top_level_controller_name": ctl}),
                ts({"namespace_name": ns, "container_name": cont}, [req_m_gib * 1024 ** 3] * n, {"top_level_controller_name": ctl}))
    W = [wl("teamcity-agents", "tc-agent-unit", "agent", 4, 16, 0.18, 0.22, 0.9), wl("teamcity-agents", "tc-agent-integration", "agent", 8, 32, 0.35, 0.30, 0.6),
         wl("test-envs", "api-pr", "api", 2, 4, 0.08, 0.35, 1.0), wl("test-envs", "api-pr", "db", 1, 2, 0.05, 0.4, 1.0), wl("kube-system", "kube-dns", "dns", 0.25, 0.07, 0.6, 0.7, 1.0)]
    for name, idx in (("cpu_req_util", 0), ("mem_req_util", 1), ("cpu_req_cores", 2), ("mem_req_bytes", 3)):
        json.dump({"timeSeries": [w[idx] for w in W]}, open(f"{a.out}/ts_{name}.json", "w"))
    # node-level: requested vs allocatable (build nodes idle at night/weekends)
    req, alloc = [], []
    for n in nodes:
        cap = float(n["status"]["allocatable"]["cpu"][:-1]) / 1000
        pool = n["metadata"]["labels"]["cloud.google.com/gke-nodepool"]
        vals = []
        for h in range(hours):
            t = now - timedelta(hours=h)
            busy = 9 <= t.hour <= 18 and t.weekday() < 5
            frac = {"system": 0.6, "build-agents": (0.7 if busy else 0.05), "test-envs": 0.55}[pool]
            vals.append(cap * frac)
        req.append({"resource": {"type": "k8s_node", "labels": {"node_name": n["metadata"]["name"]}}, "points": [{"value": {"doubleValue": v}} for v in vals]})
        alloc.append({"resource": {"type": "k8s_node", "labels": {"node_name": n["metadata"]["name"]}}, "points": [{"value": {"doubleValue": cap}} for _ in range(hours)]})
    json.dump({"timeSeries": req}, open(f"{a.out}/ts_node_req_cores.json", "w"))
    json.dump({"timeSeries": alloc}, open(f"{a.out}/ts_node_alloc_cores.json", "w"))

    # autoscaler visibility logs
    logs = []
    for i in range(40):
        logs.append({"jsonPayload": {"noDecisionStatus": {"noScaleDown": {"nodes": [{"node": {"name": f"gke-ci-build-agents-{i % 6 + 1}"}, "reason": {"messageId": random.choice(["no.scale.down.node.pod.not.backed.by.controller", "no.scale.down.node.minimal.resource.limits.exceeded", "no.scale.down.node.pod.kube.system.unmovable", "no.scale.down.node.no.place.to.move.pods"])}}]}}}})
    for i in range(25):
        logs.append({"jsonPayload": {"decision": {"scaleUp" if i % 2 else "scaleDown": {}}}})
    json.dump(logs, open(f"{a.out}/autoscaler_logs.json", "w"))

    # TeamCity builds
    builds = []
    bts = [("Api_UnitTests", 30, 12), ("Api_Integration", 45, 6), ("Web_Build", 20, 15), ("Nightly_Regression", 180, 0.3)]
    bid = 1
    for d in range(days):
        day = now - timedelta(days=d)
        for name, dur, per_day in bts:
            for _ in range(int(per_day) + (1 if random.random() < per_day % 1 else 0)):
                st = day.replace(hour=random.randint(8, 18), minute=random.randint(0, 59))
                qd = st - timedelta(minutes=random.randint(0, 12))
                fi = st + timedelta(minutes=random.gauss(dur, dur * 0.2))
                fmt = "%Y%m%dT%H%M%S%z"
                builds.append({"id": bid, "buildTypeId": name, "status": "SUCCESS", "queuedDate": qd.strftime(fmt), "startDate": st.strftime(fmt), "finishDate": fi.strftime(fmt), "agent": {"name": "k8s-agent"}})
                bid += 1
    json.dump({"count": len(builds), "build": builds}, open(f"{a.out}/teamcity_builds.json", "w"))
    json.dump({"buildType": [{"id": "Api_Integration", "name": "Integration", "projectName": "Api", "steps": {"step": [{"name": "deploy env", "type": "simpleRunner", "properties": {"property": [{"name": "script.content", "value": "kubectl apply -f env.yaml && kubectl scale deploy api --replicas=3"}]}}]}},
                              {"id": "Api_UnitTests", "name": "Unit", "projectName": "Api", "steps": {"step": [{"name": "test", "type": "gradle-runner"}]}}]}, open(f"{a.out}/teamcity_buildtypes.json", "w"))
    open(f"{a.out}/MISSING-top", "w").write("metrics-server not available")
    print(f"synthetic discovery written to {a.out}: {len(pods)} pods, {len(nodes)} nodes, {len(builds)} builds")


if __name__ == "__main__":
    main()
