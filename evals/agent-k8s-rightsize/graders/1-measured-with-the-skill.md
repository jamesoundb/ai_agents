---
type: tool_used
tool: Bash
input_match: 'k8s-(manifest-review|rightsize)/scripts/(run\.sh|k8sreview\.py|rightsize\.py)'
min: 1
---
AGENT.md's first sentence: "Your first instinct is to measure". Either skill satisfies it --
`k8s-rightsize` produces the new requests, `k8s-manifest-review` gates them -- and the correct
answer on this fixture usually runs both. What this rules out is a right-size done by eye.
