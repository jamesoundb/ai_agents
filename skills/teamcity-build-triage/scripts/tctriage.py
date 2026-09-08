#!/usr/bin/env python3
"""
tctriage.py — classify why a TeamCity build failed and what to do, from the REST API or from
saved artifacts (build JSON + build log). Stdlib only, read-only.

  tctriage.py --build-id N                       # needs TEAMCITY_URL and TEAMCITY_TOKEN
  tctriage.py --build-json build.json --log build.log [--tests tests.json]
  tctriage.py --recent 50 [--build-type ID]      # failure-class histogram over recent failed builds

Classes (first match wins, evidence lines quoted):
  infra/agent      no compatible agent, pod pending/unschedulable, image pull, agent disconnected
  infra/resources  OOMKilled / exit 137, disk full, ephemeral-storage eviction, timeout (executionTimeoutMin)
  deps             artifact/snapshot dependency failure, registry/npm/maven/gradle resolution errors
  compile          compiler errors
  tests            failing tests (counts, first names)
  config           script/step misconfiguration (command not found, missing parameter, permission)
  flaky            same tests failed then passed on rerun, or network blips
  unknown          nothing matched; the tail of the log is returned
"""
import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections import Counter

CLASSES = [
    ("infra/agent", [r"No compatible agents", r"waiting for a compatible agent", r"Unschedulable|FailedScheduling|Insufficient (cpu|memory)", r"ImagePullBackOff|ErrImagePull|image can't be pulled",
                     r"agent .* (disconnected|lost)|Agent was unregistered", r"pod .* (Pending|failed to start)"],
     "The build never ran or lost its executor. Check the Kubernetes cloud profile: pod template requests vs LimitRange/quota, spot pool capacity, image name; see gke-cost-discovery for pending pods."),
    ("infra/resources", [r"OOMKilled|exit code 137|Killed process|Out of memory|java\.lang\.OutOfMemoryError", r"No space left on device|disk quota exceeded|ephemeral-storage|Evicted",
                          r"execution timeout|executionTimeoutMin|exceeded the build timeout|Build was terminated"],
     "The build exhausted a resource limit. Raise memory only with evidence (p95 from gke-cost-discovery), add ephemeral-storage requests, bound the timeout, or split the build."),
    ("deps", [r"Artifact dependency .* failed|Failed to download artifact|snapshot dependency .* failed", r"Could not resolve|Could not find artifact|Could not GET|Unable to resolve|ENOTFOUND|EAI_AGAIN|npm ERR! (code E404|code ETARGET|network)",
              r"403 Forbidden|401 Unauthorized|denied: requested access|unauthorized: authentication required", r"Read timed out|Connection reset|TLS handshake|SSL routines"],
     "Dependency or registry problem. Check credentials/tokens, registry mirrors and pinned versions; retries are only justified for transient network errors."),
    ("compile", [r"error: cannot find symbol|\berror\[E\d+\]|error TS\d+|SyntaxError|compilation failed|Compilation failure|error: expected|undefined reference|cannot find package|: error C\d+"],
     "Source does not compile; fix the code, do not rerun."),
    ("tests", [r"Tests failed: (\d+)|(\d+) (tests? )?failed|FAILED\s+\(|AssertionError|Test .* failed|##teamcity\[testFailed"],
     "Tests failed; read the first failing test, not the last log line."),
    ("config", [r"command not found|No such file or directory|Permission denied|is not recognized as an internal|Missing required parameter|parameter .* is not defined|Cannot find script|unresolved reference"],
     "Build configuration or script error; fix the step, not the code."),
]
FLAKY_HINTS = [r"Connection reset by peer", r"timed out", r"flaky", r"retry"]


def api(path, fields=None):
    url, token = os.environ.get("TEAMCITY_URL"), os.environ.get("TEAMCITY_TOKEN")
    if not url or not token:
        sys.exit("TEAMCITY_URL and TEAMCITY_TOKEN are required for REST access; or pass --build-json/--log")
    q = f"{url.rstrip('/')}{path}" + (("&" if "?" in path else "?") + "fields=" + urllib.parse.quote(fields) if fields else "")
    req = urllib.request.Request(q, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310
        data = r.read().decode("utf-8", "replace")
    return data


def fetch_build(build_id):
    b = json.loads(api(f"/app/rest/builds/id:{build_id}", "id,buildTypeId,number,status,statusText,state,queuedDate,startDate,finishDate,agent(name),problemOccurrences(count,problemOccurrence(type,details)),testOccurrences(count,failed,muted,newFailed),statistics(property(name,value))"))
    tests = {}
    try:
        tests = json.loads(api(f"/app/rest/testOccurrences?locator=build:(id:{build_id}),status:FAILURE,count:50", "testOccurrence(name,status,details,firstFailed(id),newFailure)"))
    except Exception:  # noqa: BLE001
        pass
    url, token = os.environ["TEAMCITY_URL"].rstrip("/"), os.environ["TEAMCITY_TOKEN"]
    req = urllib.request.Request(f"{url}/downloadBuildLog.html?buildId={build_id}", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=120) as r:  # noqa: S310
        log = r.read().decode("utf-8", "replace")
    return b, log, tests


def classify(build, log, tests):
    lines = log.splitlines()
    evidence, klass, advice = [], "unknown", "No known pattern matched; read the log tail below and the build problems."
    for name, patterns, adv in CLASSES:
        hits = []
        for pat in patterns:
            rx = re.compile(pat, re.I)
            for i, l in enumerate(lines):
                if rx.search(l):
                    hits.append((i + 1, l.strip()[:200]))
                    if len(hits) >= 5:
                        break
            if len(hits) >= 5:
                break
        if hits:
            klass, advice, evidence = name, adv, hits
            break
    problems = [p.get("type") + ": " + (p.get("details") or "")[:160] for p in ((build.get("problemOccurrences") or {}).get("problemOccurrence") or [])]
    failed_tests = [(t.get("name"), bool(t.get("newFailure"))) for t in (tests.get("testOccurrence") or [])]
    if klass == "tests" or (failed_tests and klass == "unknown"):
        klass = "tests"
    flaky = klass in ("deps", "infra/agent", "unknown") and any(re.search(p, log, re.I) for p in FLAKY_HINTS)
    stats = {p["name"]: p["value"] for p in ((build.get("statistics") or {}).get("property") or [])}
    dur = stats.get("BuildDuration")
    queue = stats.get("TimeSpentInQueue")
    out = {"build": {k: build.get(k) for k in ("id", "buildTypeId", "number", "status", "statusText", "agent")},
           "class": klass, "flaky_suspect": flaky, "advice": advice, "evidence": evidence, "problems": problems,
           "failed_tests": failed_tests[:20], "new_failures": sum(1 for _, n in failed_tests if n),
           "duration_s": int(dur) // 1000 if dur and dur.isdigit() else None, "queue_s": int(queue) // 1000 if queue and queue.isdigit() else None,
           "log_tail": lines[-15:] if klass == "unknown" else []}
    return out


def render(r):
    b = r["build"]
    L = [f"## Build triage: {b.get('buildTypeId')} #{b.get('number')} ({b.get('status')})", "",
         f"**Class:** {r['class']}" + ("  (flaky suspect)" if r["flaky_suspect"] else ""),
         f"**Advice:** {r['advice']}", ""]
    if b.get("statusText"):
        L.append(f"TeamCity status: {b['statusText']}")
    if r["duration_s"] is not None:
        L.append(f"Duration {r['duration_s']}s, queue {r['queue_s'] or 0}s" + (" (queue time > duration: capacity problem, not a code problem)" if (r["queue_s"] or 0) > (r["duration_s"] or 0) else ""))
    if r["problems"]:
        L += ["", "Build problems:"] + [f"- {p}" for p in r["problems"]]
    if r["evidence"]:
        L += ["", "Evidence (log line: text):"] + [f"- {n}: `{t}`" for n, t in r["evidence"]]
    if r["failed_tests"]:
        L += ["", f"Failed tests ({len(r['failed_tests'])} shown, {r['new_failures']} new):"] + [f"- {n}{' (new)' if new else ''}" for n, new in r["failed_tests"]]
    if r["log_tail"]:
        L += ["", "Log tail:", "```"] + r["log_tail"] + ["```"]
    return "\n".join(L)


def recent(n, build_type):
    loc = f"status:FAILURE,count:{n}" + (f",buildType:{build_type}" if build_type else "")
    data = json.loads(api(f"/app/rest/builds?locator={loc}", "build(id,buildTypeId,number)"))
    hist, rows = Counter(), []
    for b in data.get("build", []):
        try:
            bj, log, tests = fetch_build(b["id"])
            r = classify(bj, log, tests)
            hist[r["class"]] += 1
            rows.append((b["buildTypeId"], b["number"], r["class"]))
        except Exception as e:  # noqa: BLE001
            hist["error"] += 1
            rows.append((b.get("buildTypeId"), b.get("number"), f"error: {e}"))
    print(f"## Failure classes over {len(rows)} recent failed builds\n")
    for k, v in hist.most_common():
        print(f"- {k}: {v}")
    print("\n| build type | number | class |\n|---|---|---|")
    for bt, num, k in rows:
        print(f"| {bt} | {num} | {k} |")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build-id")
    ap.add_argument("--build-json")
    ap.add_argument("--log")
    ap.add_argument("--tests")
    ap.add_argument("--recent", type=int)
    ap.add_argument("--build-type")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.recent:
        recent(a.recent, a.build_type)
        return 0
    if a.build_id:
        build, log, tests = fetch_build(a.build_id)
    elif a.build_json and a.log:
        build = json.load(open(a.build_json))
        log = open(a.log, encoding="utf-8", errors="replace").read()
        tests = json.load(open(a.tests)) if a.tests else {}
    else:
        sys.exit("give --build-id (REST) or --build-json + --log")
    r = classify(build, log, tests)
    print(json.dumps(r, indent=1) if a.json else render(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
