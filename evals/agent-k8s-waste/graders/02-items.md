---
type: regex
target: "last_message"
pattern: '(?is)(?=.*preview-app)(?=.*orphan-data)(?=.*kg-retained)(?=.*preview-lb)(?=.*seed-data)'
match: "contains"
---
The waste this snapshot holds beyond broken pods, verified with `waste` (2026-10-15):

    Deployment/kg-preview-123/preview-app  2 pod(s) request cpu 1.00 mem 1.0Gi, use cpu 0.00 mem 1Mi
    PersistentVolumeClaim/kg-preview-123/orphan-data  Bound 5Gi, no pod mounts it
    PersistentVolume/kg-retained  Released 1Gi, reclaim Retain
    Service/kg-preview-123/preview-lb  ... no external address yet  ~$18/month
    Job/kg-preview-123/seed-data  Complete, no ttlSecondsAfterFinished

All five, by name: a summary of "some idle disks and a load balancer" cannot be acted on.
