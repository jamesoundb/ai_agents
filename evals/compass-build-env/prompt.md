---
name: "Everything an application's build needs, at any depth, with the gaps reported"
tags: ["triggering", "compass", "ai-task", "build-environment"]
runs: 3
max_turns: 16
timeout_seconds: 540
allowed_tools: ["Bash", "Read", "Glob", "Grep", "Skill", "Edit", "Write"]
---
I need to build checkout-web locally and run its tests. Our developers keep missing repositories
when they set this up, so set this workspace up with every repository its build needs, and tell
me if anything could still be missing.
