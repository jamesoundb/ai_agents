---
name: "build-pipeline agent names the cause instead of recommending a rerun"
agent: build-pipeline
tags: ["agent", "teamcity", "triage", "routing"]
runs: 3
max_turns: 12
timeout_seconds: 420
allowed_tools: ["Bash", "Read", "Glob", "Grep", "Skill"]
---
`Api_IntegrationTests` #2417 went red again. There is no TeamCity token on this machine, but
`build.json` and `build.log` for that build are in this directory. Should I just rerun it?
