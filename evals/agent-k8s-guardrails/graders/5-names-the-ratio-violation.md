---
type: regex
target: "last_message"
pattern: '(?is)(\blint\b.{0,400}(ratio|8x|8:1|4x|limit.{0,60}request|request.{0,60}limit)|(ratio|maxLimitRequestRatio).{0,400}\blint\b)'
match: "contains"
---
`lint-job.yaml`: request cpu 250m, limit 2, a ratio of 8 against `maxLimitRequestRatio` cpu 4.
Admission rejects it although both numbers are far below the tier caps, so it is the consequence
an answer that only compares requests with tiers misses.
