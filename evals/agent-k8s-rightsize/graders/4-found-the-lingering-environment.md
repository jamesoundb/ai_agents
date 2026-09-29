---
type: regex
target: "last_message"
pattern: '(?i)(janitor/ttl|janitor/expires|ttlSecondsAfterFinished|teardown|left (up|running))'
match: "contains"
---
Substantive correctness on this fixture.

`preview-env.yaml` is a Deployment with 3 replicas, no TTL annotation and no owner -- the
"test environments left up" case AGENT.md calls a prime suspect and the review calls the
largest waste item. Verified against real output (2026-09-29):

    high EFF008 preview-env.yaml:2  test environment has no teardown TTL
                                    (annotation janitor/ttl or janitor/expires)

Shrinking requests while the environment runs forever misses most of the money, so an answer
that only rewrites CPU and memory has not solved the problem that was asked about.
