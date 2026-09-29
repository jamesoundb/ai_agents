---
type: llm
target: "last_message"
criteria: |
  The reply summarizes the structure of src/gateway.py (its class and its methods).
  It does NOT paste the full body of the file, and it does not reproduce the
  implementation of the private helper methods line by line. Listing method names
  and signatures is expected and correct; pasting method bodies is a failure.
focus: "structure summarized, bodies not dumped"
---
