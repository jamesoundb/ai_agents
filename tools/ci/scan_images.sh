#!/bin/sh
# Scans every image .gitlab-ci.yml pins by digest (the Trivy image included) with Trivy, for
# HIGH and CRITICAL vulnerabilities that have a fixed version, i.e. ones a newer image can remove.
# Exits 1 when any image has one or a scan fails. Runs inside the Trivy image (Alpine, busybox sh).
#
# Only images are scanned: the requirements.txt files use version floors (>=), which Trivy cannot
# resolve, so a dependency scan of the repository would report nothing either way.
set -eu
cd "$(dirname "$0")/../.."

# Every image reference pinned by digest, wherever it appears (variables, job images).
images=$(grep -oE '[a-z0-9.-]+(/[a-z0-9._-]+)+(:[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}' .gitlab-ci.yml | sort -u)
[ -n "$images" ] || { echo "scan_images: no digest-pinned images in .gitlab-ci.yml" >&2; exit 2; }

rc=0
for img in $images; do
  echo "=== $img"
  # --image-src remote: pull straight from the registry; CI jobs have no Docker daemon.
  trivy image --image-src remote --scanners vuln --ignore-unfixed --severity HIGH,CRITICAL \
    --exit-code 1 "$img" || rc=1
done
[ "$rc" = 0 ] && echo "scan_images: no fixable HIGH or CRITICAL vulnerabilities"
exit "$rc"
