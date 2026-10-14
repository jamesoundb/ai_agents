#!/usr/bin/env bash
# setup_check.sh — the skills' one-time engine set-up (`run.sh --setup`, run by install.sh), from an
# empty cache, so a sandboxed session (no writes outside the project, no network) only reads.
#
#   tools/ci/setup_check.sh        # exit 0 = every case passed
#
# Cases:
#   1. code-graph --setup with pip: creates the venv and downloads every grammar the engine parses
#   2. offline after set-up: a build parses Python, Terraform and YAML without the network; the same
#      build with an empty grammar cache fails offline (proves the check sees the download)
#   3. terraform-review, k8s-manifest-review, k8s-rightsize --setup extend the same venv
#   4. uv on PATH: the venv is made by uv, still has pip (k8s-rightsize extends it), set-up passes
#   5. no write access to the cache: one line naming `--setup`, exit 2, no traceback
#   6. install.sh runs the set-up, and --no-setup skips it
#   7. a venv holding a language pack whose download() fetches nothing (< 1.21, upstream #201): each
#      grammar skill's --setup still leaves its grammars in an empty cache
# Needs network for cases 1, 3, 4, 7 (PyPI and the grammar downloads). Case 2 needs `unshare -rn`
# (user namespaces) and case 4 needs uv; each is skipped with a note when missing.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
S="$REPO/skills"
WORK="$(mktemp -d)"
trap 'chmod -R u+w "$WORK" 2>/dev/null; rm -rf "$WORK"' EXIT
# Isolate from the developer's caches: the venv, grammars and pip/uv caches all go under WORK.
export ASTGRAPH_VENV="$WORK/cache/astgraph/venv" XDG_CACHE_HOME="$WORK/cache"
unset ASTGRAPH_PYTHON INSTALL_NO_SETUP
# PATH without uv, so case 1 takes the pip path; case 4 puts uv back.
UV="$(command -v uv || true)"
NOUV_PATH="$PATH"
if [ -n "$UV" ]; then NOUV_PATH="$(printf '%s' "$PATH" | tr ':' '\n' | grep -vx "$(dirname "$UV")" | paste -sd: -)"; fi
# k8s-rightsize uses any python3/python that has PyYAML and then needs no venv: its venv cases only apply
# when the system python3 lacks it.
SYS_YAML=0; for p in python3 python; do PATH="$NOUV_PATH" command -v "$p" >/dev/null && PATH="$NOUV_PATH" "$p" -c "import yaml" 2>/dev/null && SYS_YAML=1; done

fail=0
ok() { echo "  ok   $*"; }
bad() { echo "  FAIL $*"; sed 's/^/       /' "$WORK/out" 2>/dev/null | tail -n 15; fail=1; }
grammars() { find "$XDG_CACHE_HOME"/tree-sitter-language-pack -name 'libtree_sitter_*.so' 2>/dev/null | sed -n 's/.*libtree_sitter_\(.*\)\.so$/\1/p' | sort -u | tr '\n' ' '; }

echo "== 1. code-graph --setup (pip) =="
if PATH="$NOUV_PATH" "$S/code-graph/scripts/run.sh" --setup > "$WORK/out" 2>&1 \
   && "$ASTGRAPH_VENV/bin/python" -c "import tree_sitter, tree_sitter_language_pack" 2>/dev/null; then
  ok "venv created at \$XDG_CACHE_HOME/astgraph/venv"
else bad "code-graph --setup"; fi
want="$("$ASTGRAPH_VENV/bin/python" -B -c "import sys; sys.path.insert(0, sys.argv[1]); from astgraph import EXT_LANG; print(' '.join(sorted(set(EXT_LANG.values()))))" "$S/code-graph/scripts" 2>/dev/null)"
have=" $(grammars)"
missing=""; for l in $want; do case "$have " in *" $l "*) ;; *) missing="$missing $l" ;; esac; done
if [ -n "$want" ] && [ -z "$missing" ]; then ok "grammars downloaded: $want"; else echo "  FAIL grammars missing:${missing:- (no language list)}"; fail=1; fi

echo "== 2. offline after set-up =="
mkdir -p "$WORK/repo"
printf 'def f():\n    return 1\n' > "$WORK/repo/a.py"
printf 'variable "x" {\n  type = string\n}\n' > "$WORK/repo/main.tf"
printf 'kind: ConfigMap\nmetadata:\n  name: x\n' > "$WORK/repo/cm.yaml"
if ! unshare -rn true 2>/dev/null; then
  echo "  skip unshare -rn is not available (no user namespaces)"
else
  if unshare -rn "$S/code-graph/scripts/run.sh" build --root "$WORK/repo" > "$WORK/out" 2>&1; then
    ok "build without network: $(grep -o '[0-9]* files' "$WORK/out" | head -1)"
  else bad "offline build after set-up"; fi
  rm -rf "$WORK/repo/.ast-graph"
  if XDG_CACHE_HOME="$WORK/empty" unshare -rn "$S/code-graph/scripts/run.sh" build --root "$WORK/repo" > "$WORK/out" 2>&1; then
    echo "  FAIL control: a build with an empty grammar cache succeeded offline, so case 2 proves nothing"; fail=1
  else ok "control: the same build with an empty grammar cache fails offline"; fi
fi

echo "== 3. the other skills' --setup on the same venv =="
for s in terraform-review k8s-manifest-review k8s-rightsize; do
  if PATH="$NOUV_PATH" "$S/$s/scripts/run.sh" --setup > "$WORK/out" 2>&1; then ok "$s"; else bad "$s --setup"; fi
done
if [ "$SYS_YAML" = 1 ]; then echo "  skip PyYAML check: the system python3 has it, so k8s-rightsize needs no venv"
elif "$ASTGRAPH_VENV/bin/python" -c "import yaml" 2>/dev/null; then ok "PyYAML in the shared venv"
else echo "  FAIL PyYAML missing"; fail=1; fi

echo "== 4. uv =="
if [ -z "$UV" ]; then
  echo "  skip uv is not on PATH"
else
  export ASTGRAPH_VENV="$WORK/cache/astgraph-uv/venv"
  if "$S/code-graph/scripts/run.sh" --setup > "$WORK/out" 2>&1 && grep -q '^uv' "$ASTGRAPH_VENV/pyvenv.cfg" 2>/dev/null; then
    ok "venv made by uv ($(sed -n 's/^uv = //p' "$ASTGRAPH_VENV/pyvenv.cfg"))"
  else bad "code-graph --setup with uv"; fi
  if "$ASTGRAPH_VENV/bin/python" -m pip --version > "$WORK/out" 2>&1; then ok "pip seeded into the uv venv"; else bad "no pip in the uv venv"; fi
  if "$S/k8s-rightsize/scripts/run.sh" --setup > "$WORK/out" 2>&1 \
     && { [ "$SYS_YAML" = 1 ] || "$ASTGRAPH_VENV/bin/python" -c "import yaml" 2>/dev/null; }; then
    ok "k8s-rightsize extends the uv venv"
  else bad "k8s-rightsize --setup with uv"; fi
  export ASTGRAPH_VENV="$WORK/cache/astgraph/venv"
fi

echo "== 5. no write access: one line, no traceback =="
mkdir -p "$WORK/ro" && chmod 555 "$WORK/ro"
RO="$WORK/ro"
# root ignores directory modes (CI containers run as root): a regular file in the path blocks it instead
[ "$(id -u)" = 0 ] && { touch "$WORK/ro-file"; RO="$WORK/ro-file"; }
for s in code-graph terraform-review k8s-manifest-review k8s-rightsize; do
  [ "$s" = k8s-rightsize ] && [ "$SYS_YAML" = 1 ] && { echo "  skip k8s-rightsize: the system python3 has PyYAML"; continue; }
  ASTGRAPH_VENV="$RO/venv" PATH="$NOUV_PATH" "$S/$s/scripts/run.sh" --help > "$WORK/out" 2>&1; rc=$?
  if [ "$rc" = 2 ] && grep -q -- '--setup' "$WORK/out" && ! grep -q Traceback "$WORK/out"; then ok "$s: $(grep -- '--setup' "$WORK/out" | head -1 | cut -c1-90)"
  else bad "$s with an unwritable cache (exit $rc)"; fi
done

echo "== 6. install.sh runs the set-up; --no-setup skips it =="
mkdir -p "$WORK/target"
if "$REPO/install.sh" --harness claude --target "$WORK/target" --skills code-graph > "$WORK/out" 2>&1 \
   && grep -q '^  setup  code-graph' "$WORK/out"; then ok "$(grep '^  setup  code-graph' "$WORK/out" | sed 's/^ *//' | cut -c1-90)"
else bad "install.sh did not run the code-graph set-up"; fi
if "$REPO/install.sh" --harness claude --target "$WORK/target" --skills code-graph --no-setup > "$WORK/out" 2>&1 \
   && ! grep -q '^  setup  ' "$WORK/out" && grep -q -- '--no-setup' "$WORK/out"; then ok "--no-setup skips it"
else bad "--no-setup"; fi

echo "== 7. language pack with a no-op download() (1.20.0) =="
# An existing venv is never upgraded, so one made before 1.21 keeps this pack; --setup must not trust download().
OLDPACK="$WORK/cache/oldpack/venv"
if python3 -m venv "$OLDPACK" > "$WORK/out" 2>&1 \
   && "$OLDPACK/bin/pip" install --quiet tree-sitter tree-sitter-language-pack==1.20.0 >> "$WORK/out" 2>&1; then
  for case in "code-graph:$want" "terraform-review:hcl" "k8s-manifest-review:yaml"; do
    s="${case%%:*}"; need="${case#*:}"
    # an empty grammar cache per skill, so one skill's set-up cannot fetch another's grammars
    if XDG_CACHE_HOME="$WORK/cache-$s" ASTGRAPH_VENV="$OLDPACK" PATH="$NOUV_PATH" "$S/$s/scripts/run.sh" --setup > "$WORK/out" 2>&1; then
      have=" $(XDG_CACHE_HOME="$WORK/cache-$s" grammars)"
      missing=""; for l in $need; do case "$have " in *" $l "*) ;; *) missing="$missing $l" ;; esac; done
      if [ -z "$missing" ]; then ok "$s: grammars in the cache: $need"; else echo "  FAIL $s: grammars missing:$missing"; fail=1; fi
    else bad "$s --setup with language pack 1.20.0"; fi
  done
else bad "could not make a venv with tree-sitter-language-pack 1.20.0"; fi

[ "$fail" = 0 ] && echo "setup check passed" || echo "setup check FAILED"
exit "$fail"
