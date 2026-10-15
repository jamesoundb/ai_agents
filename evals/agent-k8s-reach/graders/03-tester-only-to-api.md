---
type: llm
target: "last_message"
criteria: >
  PASS only if the answer (1) says tester -> api is denied today, because the api ingress policy
  admits only pods labelled tier: web (tester has no such label), and (2) fixes it by admitting
  tester to api only (for example a podSelector for app: tester on port 8080, or giving tester
  pods a label the api policy admits), without opening db to tester, without removing
  default-deny-ingress and without allow-all rules. FAIL if tester is reported as allowed, or the
  fix opens more than tester -> api.
---
Verified on this snapshot (2026-10-15): `reach tester api` -> "DENIED: selected by
NetworkPolicy/kg-net/api-from-frontend, NetworkPolicy/kg-net/default-deny-ingress ... no rule admits
the source on this port"; `reach tester db` is denied as well and must stay so ("nothing else opens
up"). A judge, because the right fix has several valid spellings.
