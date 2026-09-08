#!/usr/bin/env python3
"""
helmreview.py — deterministic review of Helm charts and their GitOps delivery objects.

Covers three layers, because most Helm incidents live between them:
  chart      Chart.yaml (apiVersion v2, version/appVersion, pinned dependencies), values.yaml
             defaults (resources, image tag, pull policy, secrets), values.schema.json, NOTES.txt,
             README, `helm lint`, `helm template` per values file
  values     layering: which files/parameters override what; final replicaCount/resources per
             environment; secrets in values files; keys that no template references
  gitops     ArgoCD Application (targetRevision pinned, project, prune/selfHeal, destination
             namespace, valueFiles that exist) and Flux HelmRelease (chart version pinned,
             interval, remediation retries, valuesFrom)
Rendered manifests are handed to k8s-manifest-review (../../k8s-manifest-review) when present.

  helmreview.py CHART_DIR [GITOPS_PATH ...] [--values-glob 'values*.yaml'] [--env-values values-test.yaml]
                [--policy helm-review.json] [--fail-on high] [--no-tools] [--json] [--rules]
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from yamlload import load_all  # noqa: E402

SEVERITIES = ["info", "low", "medium", "high", "critical"]
DEFAULT_POLICY = {
    "allowed_projects": [],            # ArgoCD projects allowed (empty = any except "default")
    "require_pinned_revision": True,
    "test_namespaces": ["test-envs"],  # namespaces where Applications are test environments
    "max_test_replicas": 2,
    "k8s_review_policy": None,         # path to k8s-review.json for the rendered-manifest pass
    "disabled_rules": [],
    "severity_overrides": {},
}
SECRET_KEY_RE = re.compile(r"(password|secret|token|api[_-]?key|private[_-]?key|credential)", re.I)
K8S_REVIEW = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "k8s-manifest-review", "scripts", "k8sreview.py")


class Report:
    def __init__(self, policy):
        self.policy, self.findings, self.tools, self.layers = policy, [], [], {}

    def add(self, rule, sev, file, line, obj, message, fix=None):
        if rule in self.policy["disabled_rules"]:
            return
        sev = self.policy["severity_overrides"].get(rule, sev)
        if os.path.isabs(file):
            try:
                file = os.path.relpath(file)
            except ValueError:
                pass
        self.findings.append({"rule": rule, "severity": sev, "file": file, "line": line, "object": obj, "message": message, "fix": fix})


def yaml_file(path):
    docs, lines = load_all(open(path, encoding="utf-8").read())
    return docs, lines


def walk_secrets(obj, path, out):
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            if isinstance(v, str) and v and SECRET_KEY_RE.search(str(k)) and not v.startswith(("${", "<", "vault:", "ref+")):
                out.append((p, v))
            walk_secrets(v, p, out)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            walk_secrets(v, f"{path}[{i}]", out)


TEST_ENV_RE = re.compile(r"(^|[-_.])(test|tests|testing|pr|preview|dev|ci|sandbox|ephemeral)([-_.0-9]|$)")


def is_test_env(name):
    return bool(TEST_ENV_RE.search(name or ""))


def deep_merge(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            out[k] = deep_merge(a.get(k), v) if k in a else v
        return out
    return b if b is not None else a


def flatten(obj, prefix=""):
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    else:
        out[prefix] = obj
    return out


# ----------------------------------------------------------------------------------------------
# Chart layer
# ----------------------------------------------------------------------------------------------
def review_chart(chart_dir, rep, values_glob):
    cy = os.path.join(chart_dir, "Chart.yaml")
    if not os.path.exists(cy):
        rep.add("HC000", "high", cy, 1, "-", "Chart.yaml missing; not a chart directory", None)
        return None, {}
    docs, _ = yaml_file(cy)
    chart = docs[0] if docs and isinstance(docs[0], dict) else {}
    name = chart.get("name", os.path.basename(chart_dir))
    if chart.get("apiVersion") != "v2":
        rep.add("HC001", "medium", cy, 1, name, f"Chart apiVersion is {chart.get('apiVersion')!r}; v2 (Helm 3) expected", "apiVersion: v2 and move requirements.yaml dependencies into Chart.yaml")
    if not re.fullmatch(r"\d+\.\d+\.\d+([-+].*)?", str(chart.get("version", ""))):
        rep.add("HC002", "medium", cy, 1, name, f"chart version {chart.get('version')!r} is not semver", "version: MAJOR.MINOR.PATCH; bump on every change")
    if not chart.get("appVersion"):
        rep.add("HC003", "low", cy, 1, name, "no appVersion", "set appVersion to the application release it ships")
    for dep in chart.get("dependencies") or []:
        v = str(dep.get("version", ""))
        if not v or not re.fullmatch(r"[~^]?\d+\.\d+\.\d+([-+].*)?", v):
            rep.add("HC004", "high" if not v else "medium", cy, 1, f"{name} dep {dep.get('name')}", f"dependency {dep.get('name')} version {v!r} is missing or a range", "pin an exact version (or ~x.y.z) and commit Chart.lock")
    if (chart.get("dependencies") and not os.path.exists(os.path.join(chart_dir, "Chart.lock"))):
        rep.add("HC004", "medium", cy, 1, name, "dependencies declared but no Chart.lock", "run helm dependency update and commit Chart.lock")
    for fn, rule, sev, msg, fix in (("values.schema.json", "HC005", "low", "no values.schema.json", "add a JSON schema so bad values fail at template time"),
                                     ("README.md", "HC006", "low", "no README.md", "document values (helm-docs)"),
                                     (os.path.join("templates", "NOTES.txt"), "HC007", "info", "no templates/NOTES.txt", "print post-install hints")):
        if not os.path.exists(os.path.join(chart_dir, fn)):
            rep.add(rule, sev, os.path.join(chart_dir, fn), 1, name, msg, fix)
    # values.yaml defaults
    vals = {}
    vf = os.path.join(chart_dir, "values.yaml")
    if os.path.exists(vf):
        vdocs, _ = yaml_file(vf)
        vals = vdocs[0] if vdocs and isinstance(vdocs[0], dict) else {}
        img = vals.get("image") or {}
        if isinstance(img, dict):
            tag = str(img.get("tag", "") or "")
            if tag == "latest":
                rep.add("HV001", "medium", vf, 1, name, "default image tag is latest", "default tag to appVersion (\"\") or a pinned version")
            if img.get("pullPolicy") == "Always":
                rep.add("HV002", "low", vf, 1, name, "image.pullPolicy Always in defaults", "IfNotPresent when tags are immutable")
        res = vals.get("resources")
        if not res or not (isinstance(res, dict) and res.get("requests")):
            rep.add("HV003", "medium", vf, 1, name, "values.yaml sets no resources.requests defaults", "give every chart a small, explicit default; environments override upward")
        secrets = []
        walk_secrets(vals, "", secrets)
        for p, v in secrets:
            rep.add("HV004", "critical", vf, 1, name, f"secret-looking value in values.yaml: {p}", "use existingSecret/external-secrets; never commit secrets in values")
    return name, vals


def template_keys_used(chart_dir):
    used = set()
    for f in glob.glob(os.path.join(chart_dir, "templates", "**", "*"), recursive=True):
        if os.path.isfile(f):
            txt = open(f, encoding="utf-8", errors="replace").read()
            used |= set(re.findall(r"\.Values\.([A-Za-z0-9_.]+)", txt))
    return used


def review_values_files(chart_dir, base_vals, rep, values_glob, name):
    files = sorted(f for f in glob.glob(os.path.join(chart_dir, values_glob)) if os.path.basename(f) != "values.yaml")
    used = template_keys_used(chart_dir)
    layers = {}
    for f in files:
        docs, _ = yaml_file(f)
        v = docs[0] if docs and isinstance(docs[0], dict) else {}
        merged = deep_merge(base_vals, v)
        layers[os.path.basename(f)] = merged
        secrets = []
        walk_secrets(v, "", secrets)
        for p, val in secrets:
            rep.add("HV004", "critical", f, 1, name, f"secret-looking value in {os.path.basename(f)}: {p}", "move to a Secret managed outside Git (external-secrets, sealed-secrets, Secret Manager)")
        for key in flatten(v):
            top = key.split(".")[0]
            if used and not any(u == key or u.startswith(key + ".") or key.startswith(u + ".") or u.split(".")[0] == top for u in used) and top not in ("global",):
                rep.add("HV005", "low", f, 1, name, f"value '{key}' is not referenced by any template", "remove dead values; they mislead reviewers")
        env = os.path.basename(f).replace("values-", "").replace(".yaml", "").replace(".yml", "")
        rc = merged.get("replicaCount")
        if is_test_env(env) and isinstance(rc, int) and rc > rep.policy["max_test_replicas"]:
            rep.add("HV006", "medium", f, 1, name, f"{os.path.basename(f)} sets replicaCount {rc} for a test-like environment", f"cap at {rep.policy['max_test_replicas']}; scale-out belongs to load tests")
        res = merged.get("resources") or {}
        if isinstance(res, dict) and res.get("requests") and not (res.get("limits") or {}).get("memory"):
            rep.add("HV007", "low", f, 1, name, f"{os.path.basename(f)}: requests without a memory limit", "limits.memory = requests.memory")
    return layers


# ----------------------------------------------------------------------------------------------
# GitOps layer
# ----------------------------------------------------------------------------------------------
def review_gitops(paths, rep, chart_dir):
    files = []
    for p in paths:
        files += sorted(glob.glob(os.path.join(p, "**", "*.y*ml"), recursive=True)) if os.path.isdir(p) else [p]
    P = rep.policy
    for f in files:
        try:
            docs, lines = yaml_file(f)
        except Exception as e:  # noqa: BLE001
            rep.add("HG000", "high", f, 1, "-", f"YAML parse error: {e}", None)
            continue
        for doc, line in zip(docs, lines):
            if not isinstance(doc, dict):
                continue
            kind, api = doc.get("kind"), str(doc.get("apiVersion", ""))
            meta = doc.get("metadata") or {}
            spec = doc.get("spec") or {}
            obj = f"{kind}/{meta.get('name', '?')}"
            if kind == "Application" and api.startswith("argoproj.io"):
                src_list = spec.get("sources") or [spec.get("source") or {}]
                for src in src_list:
                    rev = str(src.get("targetRevision", ""))
                    if P["require_pinned_revision"] and rev in ("", "HEAD", "main", "master") or rev.endswith(("main", "master")):
                        rep.add("HG001", "high", f, line, obj, f"targetRevision '{rev or 'unset'}' is a moving branch", "pin a tag or chart version; promote by bumping it")
                    helm = src.get("helm") or {}
                    for vf in helm.get("valueFiles") or []:
                        if chart_dir and src.get("path") and not vf.startswith("$") and not os.path.exists(os.path.join(chart_dir, vf)):
                            rep.add("HG002", "high", f, line, obj, f"valueFiles entry '{vf}' does not exist in the chart", "fix the path; ArgoCD fails the sync otherwise")
                    for prm in helm.get("parameters") or []:
                        if prm.get("name") == "replicaCount" and spec.get("destination", {}).get("namespace") in P["test_namespaces"]:
                            try:
                                if int(prm.get("value")) > P["max_test_replicas"]:
                                    rep.add("HG003", "medium", f, line, obj, f"parameter replicaCount={prm.get('value')} in a test namespace", f"cap at {P['max_test_replicas']}")
                            except (TypeError, ValueError):
                                pass
                    if helm.get("values") and SECRET_KEY_RE.search(str(helm.get("values"))):
                        rep.add("HV004", "critical", f, line, obj, "inline helm.values contain a secret-looking key", "use a Secret/external-secrets and reference by name")
                proj = spec.get("project")
                if proj in (None, "default") or (P["allowed_projects"] and proj not in P["allowed_projects"]):
                    rep.add("HG004", "medium", f, line, obj, f"project '{proj}' (default or not allowed)", "use a dedicated AppProject with source/destination restrictions")
                auto = (spec.get("syncPolicy") or {}).get("automated")
                if auto is not None:
                    if auto.get("prune") and not auto.get("selfHeal"):
                        rep.add("HG005", "low", f, line, obj, "automated sync with prune but no selfHeal (drift persists until next commit)", "selfHeal: true, or drop prune for hand-managed environments")
                    if not auto.get("prune") and spec.get("destination", {}).get("namespace") in P["test_namespaces"]:
                        rep.add("HG005", "medium", f, line, obj, "test environment without prune: removed resources linger and keep costing", "prune: true for test namespaces")
                if not (spec.get("destination") or {}).get("namespace"):
                    rep.add("HG006", "medium", f, line, obj, "no destination.namespace", "set it explicitly")
                if spec.get("destination", {}).get("namespace") in P["test_namespaces"] and not any(k in (meta.get("annotations") or {}) for k in ("janitor/ttl", "janitor/expires")):
                    rep.add("HG007", "high", f, line, obj, "test-environment Application without a TTL annotation", "annotate janitor/ttl (or use ArgoCD ApplicationSet pull-request generator so the app disappears with the PR)")
            elif kind == "HelmRelease" and "fluxcd" in api:
                cs = ((spec.get("chart") or {}).get("spec") or {})
                ver = str(cs.get("version", "") or "")
                if not ver or ver in ("*", "latest") or ver.startswith(">"):
                    rep.add("HG001", "high", f, line, obj, f"chart version {ver or 'unset'} is unpinned", "version: x.y.z (Flux resolves ranges to the newest chart on every reconcile)")
                iv = str(spec.get("interval", ""))
                if re.fullmatch(r"\d+s|[1-4]m", iv):
                    rep.add("HG008", "low", f, line, obj, f"reconcile interval {iv} is aggressive", "5m-15m is typical; short intervals load the API server and registry")
                for phase in ("install", "upgrade"):
                    rem = ((spec.get(phase) or {}).get("remediation") or {})
                    if rem.get("retries") == -1:
                        rep.add("HG009", "medium", f, line, obj, f"{phase}.remediation.retries -1 retries forever", "bound retries (e.g. 3) and alert on failure")
                if spec.get("values") and SECRET_KEY_RE.search(json.dumps(spec.get("values"))):
                    rep.add("HV004", "critical", f, line, obj, "inline values contain a secret-looking key", "valuesFrom a Secret managed outside Git")
                if meta.get("namespace") in P["test_namespaces"] and not any(k in (meta.get("annotations") or {}) for k in ("janitor/ttl", "janitor/expires")):
                    rep.add("HG007", "high", f, line, obj, "test-environment HelmRelease without a TTL annotation", "annotate janitor/ttl or delete with the PR")


# ----------------------------------------------------------------------------------------------
# Tools: helm lint / template, then k8s-manifest-review on the render
# ----------------------------------------------------------------------------------------------
def run(cmd, cwd=None, timeout=120):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip(), p.stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, str(e), ""


def tools(chart_dir, rep, env_values, name):
    helm = shutil.which("helm")
    if not helm:
        rep.tools.append({"tool": "helm", "ok": None, "output": "helm not installed; lint/template skipped"})
        return
    rc, out, _ = run([helm, "lint", chart_dir] + (["-f", os.path.join(chart_dir, env_values)] if env_values else []))
    rep.tools.append({"tool": "helm lint", "ok": rc == 0, "output": out[:3000]})
    for m in re.finditer(r"\[(WARNING|ERROR)\]\s+(\S+):\s+(.*)", out):
        rep.add("HL001", "high" if m.group(1) == "ERROR" else "low", os.path.join(chart_dir, m.group(2)), 1, name, f"helm lint {m.group(1).lower()}: {m.group(3)}", None)
    render_dir, note = chart_dir, ""
    for vf in ([None] + ([env_values] if env_values else [])):
        cmd = [helm, "template", name or "release", render_dir] + (["-f", os.path.join(chart_dir, vf)] if vf else [])
        rc, out, stdout = run(cmd)
        if rc != 0 and "missing in charts/ directory" in out:
            # subcharts not fetched (no network / no Chart.lock): render the parent chart alone
            import shutil as _sh, tempfile
            tmp = tempfile.mkdtemp(prefix="helmreview-")
            render_dir = os.path.join(tmp, os.path.basename(chart_dir))
            _sh.copytree(chart_dir, render_dir)
            cy = os.path.join(render_dir, "Chart.yaml")
            txt = open(cy).read()
            txt = re.sub(r"^dependencies:\n(?:[ \t-].*\n?)*", "", txt, flags=re.M)
            open(cy, "w").write(txt)
            note = " (subcharts not fetched: rendered parent chart only)"
            cmd[3] = render_dir
            rc, out, stdout = run(cmd)
        label = f"helm template{(' -f ' + vf) if vf else ''}{note}"
        rep.tools.append({"tool": label, "ok": rc == 0, "output": "" if rc == 0 else out[:3000]})
        if rc == 0 and os.path.exists(K8S_REVIEW):
            extra = ["--policy", rep.policy["k8s_review_policy"]] if rep.policy.get("k8s_review_policy") else []
            p = subprocess.run([sys.executable, K8S_REVIEW, "-", "--no-tools", "--json"] + extra + (["--build"] if vf and is_test_env(vf.replace("values-", "").rsplit(".", 1)[0]) else []),
                               input=stdout, capture_output=True, text=True, timeout=120)  # noqa: E501
            try:
                data = json.loads(p.stdout)
                for f in data.get("findings", []):
                    if f["severity"] in ("high", "critical", "medium"):
                        rep.add("HR-" + f["rule"], f["severity"], f"{label} -> {f['object']}", 0, f["object"], f["message"], f["fix"])
                rep.tools.append({"tool": f"k8s-manifest-review on {label}", "ok": not data.get("fail"), "output": f"{len(data.get('findings', []))} finding(s), {data.get('objects')} object(s)"})
            except ValueError:
                rep.tools.append({"tool": f"k8s-manifest-review on {label}", "ok": False, "output": (p.stderr or p.stdout)[:1000]})


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("chart", help="chart directory (or - to skip the chart layer)")
    ap.add_argument("gitops", nargs="*", help="ArgoCD Application / Flux HelmRelease files or directories")
    ap.add_argument("--values-glob", default="values*.y*ml")
    ap.add_argument("--env-values", help="environment values file (relative to chart) to render and review as a build/test env")
    ap.add_argument("--policy")
    ap.add_argument("--fail-on", default="high", choices=SEVERITIES)
    ap.add_argument("--no-tools", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--rules", action="store_true")
    a = ap.parse_args(argv)
    if a.rules:
        for rid, sev in sorted(set(re.findall(r'rep\.add\("([A-Z]+\d+)", "(\w+)"', open(__file__).read()))):
            print(f"{rid}\t{sev}")
        return 0
    P = json.loads(json.dumps(DEFAULT_POLICY))
    for c in ([a.policy] if a.policy else []) + [os.path.join(a.chart if a.chart != "-" else ".", "helm-review.json"), "helm-review.json"]:
        if c and os.path.exists(c):
            P.update(json.load(open(c))); P["_policy_file"] = c
            break
    rep = Report(P)
    name, base_vals, layers = None, {}, {}
    chart_dir = None if a.chart == "-" else os.path.abspath(a.chart)
    if chart_dir:
        name, base_vals = review_chart(chart_dir, rep, a.values_glob)
        if name:
            layers = review_values_files(chart_dir, base_vals, rep, a.values_glob, name)
    if a.gitops:
        review_gitops(a.gitops, rep, chart_dir)
    if chart_dir and name and not a.no_tools:
        tools(chart_dir, rep, a.env_values, name)
    seen, uniq = set(), []
    # rendered-manifest findings (HR-*) are keyed without the render label so the same defect in
    # the base and the environment render is reported once (the environment render is kept last)
    rep.findings.reverse()
    for x in rep.findings:
        k = (x["rule"], x["object"], x["message"]) if x["rule"].startswith("HR-") else (x["rule"], x["file"], x["object"], x["message"])
        if k not in seen:
            seen.add(k); uniq.append(x)
    rep.findings = sorted(uniq, key=lambda x: (-SEVERITIES.index(x["severity"]), x["file"]))
    gate = SEVERITIES.index(a.fail_on)
    failing = [x for x in rep.findings if SEVERITIES.index(x["severity"]) >= gate] or [t for t in rep.tools if t["ok"] is False]
    summary = {f: {"replicaCount": v.get("replicaCount"), "resources": v.get("resources")} for f, v in layers.items()}
    if a.json:
        print(json.dumps({"chart": name, "findings": rep.findings, "tools": rep.tools, "values_layers": summary, "fail": bool(failing)}, indent=1))
        return 1 if failing else 0
    counts = defaultdict(int)
    for x in rep.findings:
        counts[x["severity"]] += 1
    print(f"## Helm review: {name or '(no chart)'}" + (f"  policy {P['_policy_file']}" if P.get("_policy_file") else ""))
    print("Findings: " + (", ".join(f"{s} {counts[s]}" for s in reversed(SEVERITIES) if counts[s]) or "none"))
    if summary:
        print("\n### Effective values per environment file\n\n| file | replicaCount | resources |\n|---|---|---|")
        for f, v in summary.items():
            print(f"| {f} | {v['replicaCount']} | {json.dumps(v['resources']) if v['resources'] else '-'} |")
    if rep.findings:
        print("\n| severity | rule | location | object | finding | fix |\n|---|---|---|---|---|---|")
        for x in rep.findings:
            loc = f"{x['file']}:{x['line']}" if x["line"] else x["file"]
            print(f"| {x['severity']} | {x['rule']} | {loc} | {x['object']} | {x['message']} | {x['fix'] or ''} |")
    if rep.tools:
        print("\n### Tools")
        for t in rep.tools:
            st = "ok" if t["ok"] else ("skipped" if t["ok"] is None else "FAILED")
            print(f"- {t['tool']}: {st}" + (f" ({t['output']})" if t["output"] and t["ok"] is not False else "") + (f"\n```\n{t['output']}\n```" if t["ok"] is False and t["output"] else ""))
    print(f"\nGate (--fail-on {a.fail_on}): " + ("FAIL" if failing else "PASS"))
    return 1 if failing else 0


if __name__ == "__main__":
    sys.exit(main())
