#!/bin/sh
# Trivy scan of what CI actually runs, for HIGH and CRITICAL vulnerabilities that have a fixed
# version (ones a newer pin or package removes). Exits 1 on any finding or failed scan.
#   1. every image .gitlab-ci.yml pins by digest, pulled straight from its registry;
#   2. this job's own filesystem, which the CI job sets up like the other jobs first (git from
#      apt, the pip packages they install): what the image scan cannot see, the Trivy binary
#      included. The requirements files use version floors (>=), so this is also the only place
#      the versions pip actually resolved get scanned.
# Trivy is a release binary pinned by version and SHA-256, not an image: its image's base
# packages carried fixable vulnerabilities, and its releases were compromised in March 2026
# (0.69.4-0.69.6). To update: take the tarball's line from trivy_<version>_checksums.txt on the
# GitHub release.
# Accepted, expiring exceptions live in .trivyignore at the repository root (one ID per line with
# exp:YYYY-MM-DD and the reason); after the date they fail the scan again.
set -eu
TRIVY_VERSION=0.76.2
TRIVY_SHA256=bc0fc726e017b8927a62b2d3531d7a84ee01769daf5c99ed866dced54089fdbf
cd "$(dirname "$0")/../.."
root=$(pwd)

# A registry rate limit is infrastructure, not a finding: GitLab.com's shared runners pull through
# one Artifact Registry quota ("TOOMANYREQUESTS: Quota exceeded ... per minute"), which failed this
# job on 2026-11-24 with every image it reached clean. Such a scan is re-run, waiting
# SCAN_RETRY_SLEEP (30 s) and then twice that; findings and any other failure return at once.
retry_sleep=${SCAN_RETRY_SLEEP:-30}
scan() {
  attempt=1
  while :; do
    err=$(mktemp)
    r=0
    "$trivy" "$@" 2>"$err" || r=$?
    cat "$err" >&2
    if [ "$r" != 0 ] && [ "$attempt" -lt 3 ] && grep -qiE 'toomanyrequests|429 too many requests|quota exceeded' "$err"; then
      rm -f "$err"
      wait=$((retry_sleep * attempt))
      echo "scan_images: registry rate limit, attempt $attempt of 3; retrying in ${wait}s" >&2
      sleep "$wait"
      attempt=$((attempt + 1))
      continue
    fi
    rm -f "$err"
    return "$r"
  done
}

# Download and verify Trivy outside the checkout, so the filesystem scan below does not see it twice.
# SCAN_TRIVY replaces the download with a given binary (tools/ci/test_scan_images.sh).
if [ -n "${SCAN_TRIVY:-}" ]; then
  trivy=$SCAN_TRIVY
else
bin=$(mktemp -d)
tarball="trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz"
url="https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/${tarball}"
if command -v wget >/dev/null 2>&1; then
  wget -q -O "$bin/$tarball" "$url"
else
  python3 -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' "$url" "$bin/$tarball"
fi
echo "$TRIVY_SHA256  $bin/$tarball" | sha256sum -c - >/dev/null || { echo "scan_images: $tarball checksum mismatch" >&2; exit 2; }
tar -xzf "$bin/$tarball" -C "$bin" trivy
trivy="$bin/trivy"
fi

images=$(grep -oE '[a-z0-9.-]+(/[a-z0-9._-]+)+(:[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}' .gitlab-ci.yml | sort -u)
[ -n "$images" ] || { echo "scan_images: no digest-pinned images in .gitlab-ci.yml" >&2; exit 2; }

ignore="$root/.trivyignore"
[ -f "$ignore" ] || ignore=/dev/null

rc=0
for img in $images; do
  echo "=== image $img"
  scan image --image-src remote --scanners vuln --ignore-unfixed --severity HIGH,CRITICAL \
    --ignorefile "$ignore" --exit-code 1 "$img" || rc=1
done
echo "=== this job's filesystem (packages installed at job start, Trivy itself)"
scan rootfs --scanners vuln --ignore-unfixed --severity HIGH,CRITICAL --ignorefile "$ignore" --exit-code 1 \
  --skip-dirs /proc,/sys,/dev,"$root" / || rc=1

[ "$rc" = 0 ] && echo "scan_images: no fixable HIGH or CRITICAL vulnerabilities"
exit "$rc"
