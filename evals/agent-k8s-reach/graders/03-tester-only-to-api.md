---
type: llm
focus: "last_message"
criteria: >
  Check the FAIL conditions first. FAIL if any is true: (a) the answer's fix lets tester reach db
  (for example adds app: tester, or any selector tester matches, to the policy that guards db);
  (b) it removes default-deny-ingress or adds an allow-all ingress or egress rule; (c) it says
  tester -> api works once the policies are enforced on the new cluster; (d) it admits more than
  tester to api, or tester on more than api's port 8080. Changes for the other two paths (for
  example DNS egress so api can resolve db) are not part of this check.
  Otherwise PASS only if the answer (1) says tester -> api is denied by the current policies once
  they are enforced (it may add that the current cluster does not enforce them, so the path works
  there today), because the api ingress policy admits only pods labelled tier: web and tester has
  no such label, and (2) fixes it by admitting tester to api only (for example a podSelector for
  app: tester on port 8080, or a label on tester pods that the api policy admits).
---
Verified on this snapshot (2026-10-15): `reach tester api` -> "DENIED: selected by
NetworkPolicy/kg-net/api-from-frontend, NetworkPolicy/kg-net/default-deny-ingress ... no rule admits
the source on this port"; `reach tester db` is denied as well and must stay so ("nothing else opens
up"). A judge, because the right fix has several valid spellings.

Reworded 2026-11-24: "denied today" contradicted grader 04 (the current cluster runs kindnet and does
not enforce NetworkPolicy, so tester -> api works there today). Claude runs answered exactly that and
"breaks after the move"; the judge failed 8 of 9 votes on the word "today". Then scoped to the
tester change: with "today" fixed the judge still failed answers whose separate api -> kube-dns:53
egress rule (the api -> db fix) it counted as "opening more than tester -> api". Restructured with
the FAIL checks first: with the PASS-first wording haiku also passed an answer that opened db to
tester (2 of 3 votes) and one calling tester -> api allowed (3 of 3).
