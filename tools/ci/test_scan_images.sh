#!/bin/sh
# scan_images.sh retries a Trivy scan only when the registry rate-limited it, never on a finding.
# Runs scan_images.sh against a fake trivy (SCAN_TRIVY) that answers per mode:
#   quota_once    first call per target fails with the registry's TOOMANYREQUESTS, then clean
#   quota_always  every call rate-limited
#   finding       every call reports a HIGH finding (exit 1, no rate-limit text)
# Run: sh tools/ci/test_scan_images.sh
set -eu
cd "$(dirname "$0")/../.."
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

cat > "$work/trivy" <<'EOF'
#!/bin/sh
# Fake trivy: counts calls per target in $FAKE_DIR, answers per $FAKE_MODE.
target=$(eval "echo \${$#}" | tr '/:@' '___')
n=$(cat "$FAKE_DIR/$target" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$FAKE_DIR/$target"
echo "call" >> "$FAKE_DIR/calls"
case "$FAKE_MODE" in
  quota_once)   [ "$n" -gt 1 ] && exit 0 ;;
  finding)      echo "Total: 1 (HIGH: 1, CRITICAL: 0)"; exit 1 ;;
esac
echo "FATAL Fatal error run error: image scan error: ... GET https://mirror.gcr.io/v2/x/manifests/sha256:0: TOOMANYREQUESTS: Quota exceeded for quota metric 'Requests per project in the US multi-region'" >&2
exit 1
EOF
chmod +x "$work/trivy"

targets=$(( $(grep -oE '[a-z0-9.-]+(/[a-z0-9._-]+)+(:[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}' .gitlab-ci.yml | sort -u | wc -l) + 1 ))
fails=0
check() {   # mode, expected exit, expected calls
  rm -rf "$work/fake"; mkdir -p "$work/fake"
  rc=0
  SCAN_TRIVY="$work/trivy" SCAN_RETRY_SLEEP=0 FAKE_MODE="$1" FAKE_DIR="$work/fake" \
    sh tools/ci/scan_images.sh > "$work/out" 2>&1 || rc=$?
  calls=0; [ -f "$work/fake/calls" ] && calls=$(wc -l < "$work/fake/calls"); calls=$((calls + 0))
  if [ "$rc" = "$2" ] && [ "$calls" = "$3" ]; then
    echo "ok   $1: exit $rc, $calls trivy calls"
  else
    echo "FAIL $1: exit $rc (want $2), $calls trivy calls (want $3)"; tail -5 "$work/out"; fails=$((fails + 1))
  fi
}
check quota_once 0 $((targets * 2))      # every target retried once, then clean
check finding 1 "$targets"               # a finding is never retried
check quota_always 1 $((targets * 3))    # three attempts, then the job fails
if [ "$fails" != 0 ]; then echo "test_scan_images: $fails failed"; exit 1; fi
echo "test_scan_images: ok"
