---
type: llm
target: "trace"
criteria: |
  The task is to read one value out of a two-line file.

  Before judging, IGNORE any tool call that reads, writes or mentions
  `.github/instructions.md`, `AGENTS.md`, `GEMINI.md` or a troubleshooting/worklog file.
  Those come from the operator's global instruction file and are not part of this task.

  Of the tool calls that remain:
  PASS if the assistant reached the answer with one or two of them -- typically a single
  file read, optionally preceded by one directory listing.
  PASS if it ran the code-skeleton once, saw the "source is no larger than its skeleton,
  shown in full" guard line, and answered straight from that output.
  FAIL if it built the relationship graph, or needed three or more task-related calls, to
  answer a question this small.
focus: "proportionate effort once operator-config noise is discounted"
---
Over-triggering costs tokens too. This is the counterweight to the triggering cases.
