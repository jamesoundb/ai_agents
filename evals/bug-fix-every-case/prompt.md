---
name: "Fix a bug in every place the report names, each case proven by a test"
tags: ["triggering", "bug-fix"]
runs: 3
max_turns: 30
timeout_seconds: 600
allowed_tools: ["Bash", "Read", "Glob", "Grep", "Edit", "Write", "Skill"]
---
Totals lose a cent. A cart with 3 items at 0.335 shows 1.00, but amounts must round half up to the
cent, so it should be 1.01. An invoice with the same lines also shows 1.00 instead of 1.01.
Whole-cent totals such as 2 x 2.50 = 5.00 must stay as they are. Can you fix it?
