---
type: regex
target: "last_message"
pattern: '(?is)(perf-tests.{0,400}(reject|exceed|above|over the|denied|forbidden|larger than|bigger than|max)|(reject|exceed|denied|forbidden).{0,400}perf-tests)'
match: "contains"
---
The tiers' large tier is 6 cpu / 24Gi and the LimitRange `max` encodes it; `perf-tests-job.yaml`
requests 8 cpu / 32Gi, so the nightly perf run is rejected at admission the night the LimitRange
lands. SKILL.md step 3 asks for the consequences before adoption; a generic "pods above the large
tier are rejected" without naming the one job it hits is not that.
