---
name: teamcity-build-triage
description: >
  Use when a TeamCity build is red or being rerun: classifies the failure with quoted evidence; a
  failure-class histogram over recent builds finds systemic waste.
allowed-tools: Bash(python3 */teamcity-build-triage/scripts/tctriage.py *), Bash(*/teamcity-build-triage/scripts/tctriage.py *), Read
---

# teamcity-build-triage: classify before you rerun

```bash
export TEAMCITY_URL=https://teamcity.example.com TEAMCITY_TOKEN=<read-only token>
scripts/tctriage.py --build-id 12345                 # one build: class, advice, evidence, failed tests
scripts/tctriage.py --recent 50 [--build-type Api_UnitTests]   # histogram of failure classes
scripts/tctriage.py --build-json build.json --log build.log [--tests tests.json]   # offline
```

Read-only against TeamCity (builds, test occurrences, build log). No token yet: ask the
developer to download the build log and the build JSON (`/app/rest/builds/id:N`) and run the
offline form.

## Procedure

1. Classify. Quote the evidence lines and the class; do not paraphrase the log.
2. Act by class:
   - `infra/agent`, `infra/resources`: this is a capacity or sizing problem, not a code problem.
     Hand it to the kubernetes agent with the pod name/namespace and the evidence; check the
     tiers and LimitRange, spot pool capacity, and `executionTimeoutMin`.
   - `deps`: check credentials and pinning; a rerun is justified only for a transient network
     error, and then once.
   - `compile`, `tests`, `config`: developer action; a rerun changes nothing. For tests, name
     the first failing test and whether it is new.
   - `flaky_suspect`: look for the same test failing intermittently across recent builds before
     calling it flaky; quarantine it rather than retrying the whole build.
3. Note queue time versus duration: queue time larger than duration means the cluster or agent
   pool is undersized for peak, which `gke-cost-discovery` quantifies.
4. For weekly reviews use `--recent`: a high share of `infra/*` failures is wasted capacity twice
   (the failed run and the rerun). Report the share and the top build types affected.

## Output shape

Class and advice first, then TeamCity status text, duration/queue, build problems, evidence lines,
failed tests (new ones marked), log tail only when nothing matched.
