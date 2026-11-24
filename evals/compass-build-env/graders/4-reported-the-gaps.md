---
type: llm
focus: "last_message"
criteria: >
  PASS only if the answer names design-tokens among the repositories checkout-web's build needs,
  reports that @dmtest/analytics (an internal npm package) has no repository in the map, reports
  that the brand group / brand fonts repository (a submodule of design-tokens) is not in the map,
  and says the clones did not succeed because the Git server could not be reached. FAIL if it
  says the environment is complete or set up, or leaves out either missing dependency.
---
`compass deps checkout-web` (verified 2026-11-24) ends with:

    verdict: incomplete (2 gaps); ...
      - dmtest/checkout-web -> @dmtest/analytics (npm, package.json:6; npm scope @dmtest is the org's): no repository in the map provides it
      - localhost:3300/brand is not in the map (dmtest/design-tokens -> localhost/brand/fonts (submodule, .gitmodules:3)); add it: .../run.sh build localhost:3300/brand --provider gitea

With `--kinds build` the brand submodule is still in the closure (submodule is a build edge).
