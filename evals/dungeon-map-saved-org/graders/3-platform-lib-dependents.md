---
type: regex
target: "last_message"
pattern: '(?is)platform-lib.{0,400}payments-api|payments-api.{0,400}platform-lib'
match: "contains"
---
`compass show platform-lib` after the build (verified 2026-11-23):

    used by (1):
        dmtest/payments-api (Go module: go.mod:6 in dmtest/payments-api)
