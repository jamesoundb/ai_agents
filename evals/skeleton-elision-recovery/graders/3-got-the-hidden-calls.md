---
type: regex
target: "last_message"
pattern: 'increment_counter'
match: "contains"
---
Corroborates grader 2 with the content of the answer.

`increment_counter` is the last of the five calls the cap hides. It is imported from
`.telemetry`, so unlike the private `self._*` helpers it does NOT appear anywhere
else in `gateway.py`'s default skeleton -- not as a definition, and not in the
`imports:` line, which shows only the module `.telemetry`.

Verified before writing this grader: `run.sh skeleton src/gateway.py` does not
contain the string `increment_counter`; `--max-calls 0` does. So naming it is
evidence the full call list was actually recovered rather than inferred from the
truncated output.
