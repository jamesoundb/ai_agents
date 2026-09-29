---
type: regex
target: "last_message"
pattern: '(HV00[1246]|HG00[14]|HC004)'
match: "contains"
---
Proof the review output was used rather than just run -- these ids exist only in the skill's
catalogue. Verified against real output on this fixture (2026-09-29):

    critical HV004 chart/values.yaml           secret-looking value: database.password
    high     HG001 gitops/application-test.yaml targetRevision 'HEAD' is a moving branch
    medium   HV006 chart/values-test.yaml       replicaCount 3 for a test-like environment
    medium   HV001 chart/values.yaml            default image tag is latest
    medium   HC004 chart/Chart.yaml             redis version '>=18.0.0' is a range
