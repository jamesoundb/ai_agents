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
set -eu
TRIVY_VERSION=0.76.2
TRIVY_SHA256=bc0fc726e017b8927a62b2d3531d7a84ee01769daf5c99ed866dced54089fdbf
cd "$(dirname "$0")/../.."
root=$(pwd)

# Download and verify Trivy outside the checkout, so the filesystem scan below does not see it twice.
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

images=$(grep -oE '[a-z0-9.-]+(/[a-z0-9._-]+)+(:[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}' .gitlab-ci.yml | sort -u)
[ -n "$images" ] || { echo "scan_images: no digest-pinned images in .gitlab-ci.yml" >&2; exit 2; }

rc=0
for img in $images; do
  echo "=== image $img"
  "$trivy" image --image-src remote --scanners vuln --ignore-unfixed --severity HIGH,CRITICAL \
    --exit-code 1 "$img" || rc=1
done
echo "=== this job's filesystem (packages installed at job start, Trivy itself)"
"$trivy" rootfs --scanners vuln --ignore-unfixed --severity HIGH,CRITICAL --exit-code 1 \
  --skip-dirs /proc,/sys,/dev,"$root" / || rc=1

[ "$rc" = 0 ] && echo "scan_images: no fixable HIGH or CRITICAL vulnerabilities"
exit "$rc"
