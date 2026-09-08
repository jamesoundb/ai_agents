#!/usr/bin/env python3
"""
tcreview.py — review TeamCity project settings (Kotlin DSL or XML) for cost, hygiene and
Kubernetes-executor efficiency.

  tcreview.py PATH... [--policy tc-review.json] [--fail-on high] [--json] [--rules]

PATH is a .teamcity directory, a settings.kts / *.kt file, or XML (project-config.xml,
buildTypes/*.xml). Pod templates embedded in Kubernetes cloud images / executor settings are
extracted and reviewed with k8s-manifest-review (../../k8s-manifest-review) when present.
Rules are regex-based over the DSL/XML text, scoped per build configuration block.
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
from collections import defaultdict

SEVERITIES = ["info", "low", "medium", "high", "critical"]
DEFAULT_POLICY = {
    "max_execution_timeout_min": 180,
    "require_branch_filter": True,
    "k8s_review_policy": None,
    "teardown_patterns": ["kubectl delete", "helm uninstall", "helm delete", "kubectl scale.*--replicas=0", "teardown", "cleanup", "destroy"],
    "deploy_patterns": ["kubectl apply", "kubectl scale", "helm upgrade", "helm install", "kubectl create", "kustomize build"],
    "disabled_rules": [],
    "severity_overrides": {},
}
K8S_REVIEW = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "k8s-manifest-review", "scripts", "k8sreview.py")


class Report:
    def __init__(self, policy):
        self.policy, self.findings, self.tools = policy, [], []

    def add(self, rule, sev, file, line, obj, message, fix=None):
        if rule in self.policy["disabled_rules"]:
            return
        sev = self.policy["severity_overrides"].get(rule, sev)
        self.findings.append({"rule": rule, "severity": sev, "file": file, "line": line, "object": obj, "message": message, "fix": fix})


def line_of(text, pos):
    return text.count("\n", 0, pos) + 1


# ----------------------------------------------------------------------------------------------
# Kotlin DSL: split into top-level objects (object X : BuildType({ ... }), Project, cloud images)
# ----------------------------------------------------------------------------------------------
def kotlin_blocks(text):
    """Yield (name, kind, body_text, start_pos) for `object Name : Kind({ ... })` and `Name = Kind { ... }` forms."""
    for m in re.finditer(r"(?:object|val)\s+(\w+)\s*[:=]\s*(BuildType|Project|Template|KubernetesCloudImage|kubernetesCloudImage|KubernetesCloudProfile)\s*\(?\s*\{", text):
        start = m.end() - 1
        depth, i = 0, start
        while i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        yield m.group(1), m.group(2), text[start:i + 1], m.start()
    # anonymous kubernetes cloud images inside project features
    for m in re.finditer(r"kubernetesCloudImage\s*\{", text):
        start = m.end() - 1
        depth, i = 0, start
        while i < len(text):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        yield f"kubernetesCloudImage@{line_of(text, m.start())}", "KubernetesCloudImage", text[start:i + 1], m.start()


def review_kotlin(path, text, rep):
    P = rep.policy
    seen_any = False
    for name, kind, body, pos in kotlin_blocks(text):
        seen_any = True
        line = line_of(text, pos)
        obj = f"{kind} {name}"
        if kind in ("BuildType", "Template"):
            review_build_type(path, line, obj, body, rep)
        if kind == "Project":
            if "cleanup" not in body:
                rep.add("TC002", "medium", path, line, obj, "project has no cleanup rules", "cleanup { keepRule { ... } } or baseRule { history/artifacts days } so build artifacts and history do not grow forever")
        if kind.lower().startswith("kubernetes"):
            review_pod_templates(path, line, obj, body, rep)
    if not seen_any:
        rep.add("TC000", "info", path, 1, "-", "no BuildType/Project/cloud image blocks recognised in this Kotlin file", None)


def review_build_type(path, line, obj, body, rep):
    P = rep.policy
    m = re.search(r"executionTimeoutMin\s*=\s*(\d+)", body)
    if not m:
        rep.add("TC001", "high", path, line, obj, "no executionTimeoutMin: a hung build holds its agent/pod indefinitely", "failureConditions { executionTimeoutMin = 60 }")
    elif int(m.group(1)) > P["max_execution_timeout_min"]:
        rep.add("TC001", "low", path, line, obj, f"executionTimeoutMin {m.group(1)} exceeds policy max {P['max_execution_timeout_min']}", "shorten; long timeouts turn hangs into cost")
    trig = re.search(r"triggers\s*\{", body)
    if trig:  # only vcs { } blocks inside triggers { } are triggers; the top-level vcs { } is checkout settings
        depth, i = 0, trig.end() - 1
        while i < len(body):
            depth += body[i] == "{"
            depth -= body[i] == "}"
            if depth == 0:
                break
            i += 1
        triggers_body, offset = body[trig.start():i + 1], trig.start()
        for t in re.finditer(r"vcs\s*\{([^}]*)\}", triggers_body):
            inner = t.group(1)
            t_start = offset + t.start()
            if P["require_branch_filter"] and "branchFilter" not in inner:
                rep.add("TC003", "medium", path, line + body[:t_start].count("\n"), obj, "VCS trigger without branchFilter builds every branch", "branchFilter = \"+:<default>\\n+:pull/*\" (or the branches that matter)")
            if "quietPeriodMode" not in inner:
                rep.add("TC003", "low", path, line + body[:t_start].count("\n"), obj, "VCS trigger without quiet period: every push starts a build", "quietPeriodMode = VcsTrigger.QuietPeriodMode.USE_DEFAULT")
    if "maxRunningBuilds" not in body and re.search(r"kubernetes|docker|k8s", body, re.I):
        rep.add("TC007", "low", path, line, obj, "no maxRunningBuilds cap", "maxRunningBuilds = N bounds concurrent pods for this configuration")
    # secrets
    for m in re.finditer(r"param\(\s*\"([^\"]*(?:password|secret|token|key)[^\"]*)\"\s*,\s*\"([^\"]+)\"\s*\)", body, re.I):
        if not m.group(2).startswith(("%", "credentialsJSON:", "zxx")):
            rep.add("TC009", "critical", path, line + body[:m.start()].count("\n"), obj, f"parameter {m.group(1)} holds a plain-text secret", "password(\"name\", \"credentialsJSON:...\") and store the value in TeamCity, or a secret store")
    # scripts: deploy without teardown; caches
    scripts = "\n".join(m.group(1) for m in re.finditer(r"scriptContent\s*=\s*\"\"\"(.*?)\"\"\"", body, re.S))
    scripts += "\n" + "\n".join(m.group(1) for m in re.finditer(r"scriptContent\s*=\s*\"([^\"]*)\"", body))
    if scripts.strip():
        deploys = any(re.search(p, scripts) for p in P["deploy_patterns"])
        teardown = any(re.search(p, scripts, re.I) for p in P["teardown_patterns"])
        if deploys and not teardown:
            rep.add("TC012", "high", path, line, obj, "build creates/scales Kubernetes resources but has no teardown step", "add a final step (executeStep = ALWAYS) that deletes the test environment, or annotate it with janitor/ttl")
        if re.search(r"--replicas=(\d+)", scripts):
            n = max(int(x) for x in re.findall(r"--replicas=(\d+)", scripts))
            if n > 2:
                rep.add("TC013", "medium", path, line, obj, f"build scales a test deployment to {n} replicas", "tests rarely need more than 1-2 replicas")
        if re.search(r"docker build", scripts) and "--cache-from" not in scripts and "buildx" not in scripts:
            rep.add("TC004", "low", path, line, obj, "docker build without --cache-from/buildx cache", "reuse layers from the registry to cut build minutes")
        if re.search(r"\bgradle\b|gradlew", scripts) and "--build-cache" not in scripts and "org.gradle.caching" not in body:
            rep.add("TC004", "low", path, line, obj, "gradle without build cache", "--build-cache (and a remote cache) shortens test builds")
    if re.search(r"gradle\s*\{", body) and "--build-cache" not in body and "org.gradle.caching" not in body:
        rep.add("TC004", "low", path, line, obj, "gradle runner without build cache flags", "gradleParams = \"--build-cache\"")
    if "artifactRules" in body and re.search(r"artifactRules\s*=\s*\"\+?:?\*\*", body):
        rep.add("TC008", "medium", path, line, obj, "artifactRules publish everything (**)", "publish only the outputs consumers need; artifacts cost storage and transfer")
    if "requirements" not in body and re.search(r"kubernetes|docker", body, re.I) is None:
        rep.add("TC006", "info", path, line, obj, "no agent requirements: build may land on any agent pool", "requirements { equals(\"system.cloud.profile_id\", \"...\") } or a pool restriction")
    review_pod_templates(path, line, obj, body, rep)


def review_pod_templates(path, line, obj, body, rep):
    """Extract embedded pod YAML (Kubernetes cloud image customTemplate / executor podSpec) and review it."""
    for m in re.finditer(r"\"\"\"(.*?)\"\"\"", body, re.S):
        yaml_text = m.group(1)
        if "containers:" not in yaml_text and "apiVersion:" not in yaml_text:
            continue
        yaml_text = re.sub(r"^\s*\|\s*\n", "", yaml_text)
        # de-indent
        lines = [l for l in yaml_text.splitlines() if l.strip()]
        indent = min(len(l) - len(l.lstrip()) for l in lines) if lines else 0
        yaml_text = "\n".join(l[indent:] for l in yaml_text.splitlines())
        if "kind:" not in yaml_text:
            yaml_text = "apiVersion: v1\nkind: Pod\nmetadata:\n  name: teamcity-agent-template\n" + ("spec:\n" + "\n".join("  " + l for l in yaml_text.splitlines()) if not yaml_text.lstrip().startswith("spec:") else yaml_text)
        run_k8s_review(path, line + body[:m.start()].count("\n"), obj, yaml_text, rep)
    # Kubernetes executor settings in build steps (script with image + resources) are not YAML; check flags
    for m in re.finditer(r"dockerImage\s*=\s*\"([^\"]+)\"", body):
        img = m.group(1)
        if ":" not in img.split("/")[-1] or img.endswith(":latest"):
            rep.add("TC010", "low", path, line + body[:m.start()].count("\n"), obj, f"step image '{img}' is untagged or :latest", "pin a tag or digest for reproducible, cacheable builds")


def run_k8s_review(path, line, obj, yaml_text, rep):
    if not os.path.exists(K8S_REVIEW):
        rep.add("TC005", "info", path, line, obj, "pod template found but k8s-manifest-review is not installed beside this skill", None)
        return
    extra = ["--policy", rep.policy["k8s_review_policy"]] if rep.policy.get("k8s_review_policy") else []
    p = subprocess.run([sys.executable, K8S_REVIEW, "-", "--no-tools", "--json", "--build"] + extra, input=yaml_text, capture_output=True, text=True, timeout=120)
    try:
        data = json.loads(p.stdout)
    except ValueError:
        rep.add("TC005", "medium", path, line, obj, f"pod template could not be reviewed: {(p.stderr or p.stdout)[:200]}", None)
        return
    for f in data.get("findings", []):
        if f["severity"] in ("critical", "high", "medium"):
            rep.add("TC005-" + f["rule"], f["severity"], path, line, obj, f"pod template: {f['message']}", f["fix"])
    rep.tools.append({"tool": f"k8s-manifest-review on pod template ({obj})", "ok": not data.get("fail"), "output": f"{len(data.get('findings', []))} finding(s)"})


# ----------------------------------------------------------------------------------------------
# XML settings (project-config.xml, buildTypes/*.xml, pluginData)
# ----------------------------------------------------------------------------------------------
def review_xml(path, text, rep):
    P = rep.policy
    base = os.path.basename(path)
    name = re.search(r'<name>([^<]+)</name>', text)
    obj = f"{'BuildType' if '/buildTypes/' in path.replace(os.sep, '/') or base.endswith('.xml') and '<build-type' in text else 'Project'} {name.group(1) if name else base}"
    if "<build-type" in text or "<settings" in text and "<build-runners" in text:
        m = re.search(r'name="executionTimeoutMin"\s+value="(\d+)"', text)
        if not m:
            rep.add("TC001", "high", path, 1, obj, "no executionTimeoutMin option", '<option name="executionTimeoutMin" value="60"/>')
        elif int(m.group(1)) > P["max_execution_timeout_min"]:
            rep.add("TC001", "low", path, 1, obj, f"executionTimeoutMin {m.group(1)} exceeds policy max", "shorten")
        for t in re.finditer(r'<build-trigger[^>]*type="vcsTrigger"[^>]*>(.*?)</build-trigger>', text, re.S):
            inner = t.group(1)
            if P["require_branch_filter"] and 'name="branchFilter"' not in inner:
                rep.add("TC003", "medium", path, line_of(text, t.start()), obj, "VCS trigger without branchFilter", "add branchFilter")
            if 'name="quietPeriodMode"' not in inner:
                rep.add("TC003", "low", path, line_of(text, t.start()), obj, "VCS trigger without quiet period", "quietPeriodMode=USE_DEFAULT")
        for m in re.finditer(r'<param name="([^"]*(?:password|secret|token|key)[^"]*)" value="([^"]+)"', text, re.I):
            if not m.group(2).startswith(("%", "credentialsJSON:", "zxx")):
                rep.add("TC009", "critical", path, line_of(text, m.start()), obj, f"parameter {m.group(1)} holds a plain-text secret", "use a password parameter (stored as credentialsJSON)")
        scripts = "\n".join(m.group(1) for m in re.finditer(r'<param name="script.content"[^>]*>\s*<!\[CDATA\[(.*?)\]\]>', text, re.S))
        scripts += "\n" + "\n".join(m.group(1) for m in re.finditer(r'<param name="script.content" value="([^"]*)"', text))
        if scripts.strip():
            if any(re.search(p, scripts) for p in P["deploy_patterns"]) and not any(re.search(p, scripts, re.I) for p in P["teardown_patterns"]):
                rep.add("TC012", "high", path, 1, obj, "build creates/scales Kubernetes resources but has no teardown step", "add an always-run teardown step or a janitor/ttl annotation")
        if re.search(r'name="artifactRules"\s+value="\+?:?\*\*', text):
            rep.add("TC008", "medium", path, 1, obj, "artifactRules publish everything (**)", "publish only needed outputs")
        for m in re.finditer(r'<param name="plugin.docker.imageId" value="([^"]+)"', text):
            if ":" not in m.group(1).split("/")[-1] or m.group(1).endswith(":latest"):
                rep.add("TC010", "low", path, line_of(text, m.start()), obj, f"step image '{m.group(1)}' is untagged or :latest", "pin a tag")
    if "<project" in text and "<cleanup" not in text and base == "project-config.xml":
        rep.add("TC002", "medium", path, 1, obj, "project-config.xml has no cleanup policy", "add <cleanup> rules")
    # pod templates in cloud profile params (CDATA or attribute)
    for m in re.finditer(r'<param name="(?:podSpecification|customTemplate|podTemplate|yaml)"[^>]*>\s*<!\[CDATA\[(.*?)\]\]>', text, re.S):
        yaml_text = m.group(1)
        if "kind:" not in yaml_text:
            yaml_text = "apiVersion: v1\nkind: Pod\nmetadata:\n  name: teamcity-agent-template\n" + yaml_text
        run_k8s_review(path, line_of(text, m.start()), obj, yaml_text, rep)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--policy")
    ap.add_argument("--fail-on", default="high", choices=SEVERITIES)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--rules", action="store_true")
    a = ap.parse_args(argv)
    if a.rules:
        for rid, sev in sorted(set(re.findall(r'rep\.add\("([A-Z]+\d+)", "(\w+)"', open(__file__).read()))):
            print(f"{rid}\t{sev}")
        return 0
    files = []
    for p in a.paths:
        if os.path.isdir(p):
            files += sorted(glob.glob(os.path.join(p, "**", "*.kt*"), recursive=True)) + sorted(glob.glob(os.path.join(p, "**", "*.xml"), recursive=True))
        else:
            files.append(p)
    P = json.loads(json.dumps(DEFAULT_POLICY))
    for c in ([a.policy] if a.policy else []) + [os.path.join(os.path.dirname(os.path.abspath(files[0])) if files else ".", "tc-review.json"), "tc-review.json"]:
        if c and os.path.exists(c):
            P.update(json.load(open(c))); P["_policy_file"] = c
            break
    rep = Report(P)
    for f in files:
        text = open(f, encoding="utf-8", errors="replace").read()
        if f.endswith((".kt", ".kts")):
            review_kotlin(f, text, rep)
        elif f.endswith(".xml"):
            review_xml(f, text, rep)
    seen, uniq = set(), []
    for x in rep.findings:
        k = (x["rule"], x["file"], x["object"], x["message"])
        if k not in seen:
            seen.add(k); uniq.append(x)
    rep.findings = sorted(uniq, key=lambda x: (-SEVERITIES.index(x["severity"]), x["file"], x["line"]))
    gate = SEVERITIES.index(a.fail_on)
    failing = [x for x in rep.findings if SEVERITIES.index(x["severity"]) >= gate]
    if a.json:
        print(json.dumps({"files": files, "findings": rep.findings, "tools": rep.tools, "fail": bool(failing)}, indent=1))
        return 1 if failing else 0
    counts = defaultdict(int)
    for x in rep.findings:
        counts[x["severity"]] += 1
    print(f"## TeamCity settings review: {len(files)} file(s)" + (f"  policy {P['_policy_file']}" if P.get("_policy_file") else ""))
    print("Findings: " + (", ".join(f"{s} {counts[s]}" for s in reversed(SEVERITIES) if counts[s]) or "none"))
    if rep.findings:
        print("\n| severity | rule | location | object | finding | fix |\n|---|---|---|---|---|---|")
        for x in rep.findings:
            print(f"| {x['severity']} | {x['rule']} | {x['file']}:{x['line']} | {x['object']} | {x['message']} | {x['fix'] or ''} |")
    if rep.tools:
        print("\n### Tools")
        for t in rep.tools:
            print(f"- {t['tool']}: {'ok' if t['ok'] else 'FAILED'} ({t['output']})")
    print(f"\nGate (--fail-on {a.fail_on}): " + ("FAIL" if failing else "PASS"))
    return 1 if failing else 0


if __name__ == "__main__":
    sys.exit(main())
