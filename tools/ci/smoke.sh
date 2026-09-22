#!/usr/bin/env bash
# smoke.sh — run the skills' scripts end to end on fixtures and synthetic data (no cluster,
# no TeamCity, no cloud credentials). Catches a skill that crashes, not review-rule quality;
# rule behaviour is covered by each engine's own tests.
#
#   tools/ci/smoke.sh        # exit 0 = every step ran
#
# Review scripts exit 0 (gate PASS), 1 (gate FAIL: findings at/above --fail-on) or 2 (error), so a
# review step passes when it exits 0 or 1 and prints its Gate line. External tools (terraform,
# helm) are disabled with --no-tools so results do not depend on what the runner has installed.
# Needs Python 3.10+ with tree-sitter and tree-sitter-language-pack. k8s-rightsize is started
# through its run.sh, which installs PyYAML on first use (the path a new developer hits).
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
S="$REPO/skills"
FIX="$S/code-graph/tests/fixture"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Same interpreter discovery as the skills' run.sh: first Python that has the tree-sitter deps.
PY=""
for cand in "${PYTHON:-}" "${ASTGRAPH_VENV:-$HOME/.cache/astgraph/venv}/bin/python" python3; do
  [ -n "$cand" ] && command -v "$cand" >/dev/null 2>&1 \
    && "$cand" -c "import tree_sitter, tree_sitter_language_pack" 2>/dev/null && { PY="$cand"; break; }
done
[ -n "$PY" ] || { echo "no Python with tree-sitter found (pip install -r skills/code-graph/scripts/requirements.txt)"; exit 2; }
export ASTGRAPH_PYTHON="$PY"   # make the skills' run.sh use the same interpreter
echo "python: $PY ($("$PY" -V 2>&1))"

fail=0
step() {  # step "description" command...   (passes on exit 0)
  local desc="$1"; shift
  if "$@" > "$WORK/out" 2>&1; then echo "  ok   $desc"
  else echo "  FAIL $desc (exit $?)"; sed 's/^/       /' "$WORK/out" | tail -n 15; fail=1; fi
}
review() {  # review "description" command...   (passes on exit 0/1 with a Gate line)
  local desc="$1" rc; shift
  "$@" > "$WORK/out" 2>&1; rc=$?
  if [ "$rc" -le 1 ] && grep -q '^Gate (' "$WORK/out"; then echo "  ok   $desc ($(grep '^Gate (' "$WORK/out"))"
  else echo "  FAIL $desc (exit $rc)"; sed 's/^/       /' "$WORK/out" | tail -n 15; fail=1; fi
}

echo "== every skill script starts (--help) =="
for f in "$S"/*/scripts/*.py; do
  case "$f" in
    */yamlload.py) continue ;;                      # helper module, not a CLI
    */k8s-rightsize/scripts/rightsize.py)           # needs PyYAML: go through its launcher
      step "${f#"$REPO"/} (via run.sh)" "$S/k8s-rightsize/scripts/run.sh" --help; continue ;;
  esac
  step "${f#"$REPO"/}" "$PY" "$f" --help
done

echo "== code-graph =="
step "skeleton of a Terraform file" "$S/code-graph/scripts/run.sh" skeleton "$FIX/main.tf"

echo "== reviews on the fixture =="
review "terraform-review on fixture/infra"  "$S/terraform-review/scripts/run.sh" "$FIX/infra" --no-tools
review "k8s-manifest-review on fixture/k8s" "$S/k8s-manifest-review/scripts/run.sh" "$FIX/k8s"

echo "== scaffold -> terraform-review (a fresh module must pass the company review) =="
step "terraform-module-scaffold module demo" "$PY" "$S/terraform-module-scaffold/scripts/scaffold.py" \
  module demo --dir "$WORK/modules/demo" --description "CI smoke module"
# --fail-on info: any finding at all fails, since a fresh scaffold must come out clean.
if "$S/terraform-review/scripts/run.sh" "$WORK/modules/demo" --no-tools --fail-on info > "$WORK/out" 2>&1; then
  echo "  ok   scaffolded module passes terraform-review ($(grep '^Findings' "$WORK/out"))"
else
  echo "  FAIL scaffolded module fails terraform-review: scaffold templates and review rules disagree"
  sed 's/^/       /' "$WORK/out" | tail -n 20; fail=1
fi

# With terraform installed (developer machines; the CI image has none), run the review WITH tools
# and check it leaves the reviewed directory exactly as it was (no .terraform/, no lock file).
if command -v terraform >/dev/null 2>&1; then
  before="$(cd "$WORK/modules/demo" && find . | sort)"
  "$S/terraform-review/scripts/run.sh" "$WORK/modules/demo" > "$WORK/out" 2>&1
  after="$(cd "$WORK/modules/demo" && find . | sort)"
  if [ "$before" = "$after" ]; then echo "  ok   terraform-review with tools leaves the directory unchanged"
  else echo "  FAIL terraform-review with tools left files behind:"; diff <(echo "$before") <(echo "$after") | sed 's/^/       /'; fail=1; fi
else
  echo "  skip terraform-review with tools (terraform not installed)"
fi

echo "== gke-cost-discovery -> k8s-guardrails (synthetic cluster data) =="
step "synth.py generates a discovery directory" "$PY" "$S/gke-cost-discovery/scripts/synth.py" "$WORK/disc" --seed 1
step "analyze.py writes a report and build tiers" "$PY" "$S/gke-cost-discovery/scripts/analyze.py" \
  "$WORK/disc" --tiers-out "$WORK/tiers.json"
step "guardrails.py generates namespace guardrails" "$PY" "$S/k8s-guardrails/scripts/guardrails.py" \
  --tiers "$WORK/tiers.json" --namespaces teamcity-agents --out "$WORK/guardrails"
step "guardrails wrote LimitRange and ResourceQuota" \
  test -s "$WORK/guardrails/teamcity-agents-limitrange.yaml" -a -s "$WORK/guardrails/teamcity-agents-resourcequota.yaml"

if [ "$fail" -ne 0 ]; then echo "smoke test FAILED"; exit 1; fi
echo "smoke test passed"
