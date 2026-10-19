---
type: regex
target: {source: file, path: manifests/kg-preview-123.yaml}
pattern: '(?s)(?=.*cpu:\s*["'']?(500m|0\.5)\b)(?=.*memory:\s*["'']?512Mi\b)'
match: "contains"
---
`preview-app` requests cpu 500m / memory 512Mi; the snapshot has one metrics-server sample (`use cpu
0.00 mem 1Mi`), which `waste` labels "one sample; size from gke-cost-discovery p95". No discovery
report is in the workspace, and the requests are within the default small tier (1 cpu / 2Gi), so
`k8s-rightsize` itself leaves them untouched. All three runs of 2026-10-19 hand-edited them to
50m / 64Mi from that sample. No other object in the fixture requests 500m or 512Mi.
