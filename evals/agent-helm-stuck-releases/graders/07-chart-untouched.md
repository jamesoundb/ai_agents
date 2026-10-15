---
type: tool_used
tool: Edit
input_match: '/chart/'
min: 0
max: 0
---
Neither cause is in the chart: both images come from values (CI file, Flux inline values), and the
chart default `busybox:1.36` is what the last good revisions run. The first measurement (2026-10-15,
3/3 runs) also "addressed chart review findings" (HV003, HR-EFF002, HR-COR001) by editing
`chart/values.yaml` and both templates: unrelated changes riding along with the unblocking fix,
and without a chart version bump they never reach `kg-cart`, which Flux installs as chart 0.1.0 from
the `platform` repository. AGENT.md "A release that is stuck or failing", step 2.
