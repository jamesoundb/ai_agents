---
name: "Answer dependency questions from a teammate's saved org snapshot"
tags: ["triggering", "dungeon-map", "compass"]
runs: 3
max_turns: 12
timeout_seconds: 420
allowed_tools: ["Bash", "Read", "Glob", "Grep", "Skill"]
---
Our Gitea server (localhost:3300, org dmtest) is down for maintenance, but a teammate saved its API
responses with dungeon-map into `org-snapshot/`. If we change the API of platform-lib, which
repositories are affected? And what does payments-api depend on?
