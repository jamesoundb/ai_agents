---
type: llm
target: "last_message"
criteria: >
  PASS only if the answer lists all four dependencies of payments-api: proto (git submodule),
  base-image (Dockerfile base image), platform-lib (Go module) and ci-actions (CI action), and
  says payments-api is the only repository affected by a platform-lib API change. FAIL if any of
  the four is missing, or if react, numpy, debian, golang, redis or github.com/google/uuid are
  presented as repositories of the org, or if billing-sdk (an internal Go module the map has no
  repository for) is presented as a repository that exists in the map.
---
`compass show payments-api` (verified 2026-11-24): depends on proto (.gitmodules:3), base-image
(Dockerfile:4), platform-lib (go.mod:6), ci-actions (.gitea/workflows/ci.yml:7), and lists
localhost/dmtest/billing-sdk (go.mod:7) under "internal references with no repository in the map".
The public packages and images in its files, and github.com/dmtest/billing-sdk on another host,
are not repositories of the org and resolve to nothing.
