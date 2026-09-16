# ast-treesitter: what to expect

A short guide for the first week with the agent. The full spec is [`README.md`](README.md); the
per-language details are in the `code-graph` skill's
[`languages.md`](../../skills/code-graph/reference/languages.md).

## What it is

`ast-treesitter` answers structural questions about a repository from a graph of its code:
classes, functions, fields, calls, imports, inheritance, Terraform resources and Kubernetes
objects. It is good at four kinds of question:

| ask | you get |
|---|---|
| "How is X built?" / "Walk me through Y" | the components, their fields and methods, the calls between them, with `file:line` for each |
| "What calls / instantiates / implements X?" | every dependent the graph can prove, plus leads it cannot |
| "What breaks if I change X?" | a blast-radius table: files, symbols, relationship, confidence, and the tests that reach X |
| "Where should this change go?" | the existing pattern to copy, the files to touch in order, and the tests to mirror |

It reads; it never edits. Its answers are built from the graph first and from targeted reads of
the lines it cites, not from dumping files into the model.

## Setup (once per repository)

```bash
git clone <this repo> ~/ai_agents
cd /path/to/your/project
~/ai_agents/install.sh --harness claude      # or: codex | gemini | antigravity | copilot | all
echo '.ast-graph/' >> .gitignore             # the graph is a build artifact
```

Requirements: Python 3.10 or newer (the first run creates a private venv under
`~/.cache/astgraph`, about 30 seconds). No Node, no build of your project, no IDE plugin.

Claude Code users: the subagent runs every engine command through `Bash`, and in the default
permission mode each one prompts. Add this to the project's `.claude/settings.json` once:

```json
{ "permissions": { "allow": ["Bash(*/code-graph/scripts/run.sh *)"] } }
```

## How to invoke it

- Claude Code: ask a structural question and Claude delegates to the `ast-treesitter`
  subagent, or address it directly ("use the ast-treesitter agent to ...").
- Codex / Gemini CLI: `$ast-treesitter` (it is installed as a persona skill).
- Copilot / Antigravity: pick `ast-treesitter` from the agent picker.

Give it the question a senior engineer would ask, with the symbol names you know. Good first
questions:

- "Trace how a request reaches a view function, from route registration to dispatch."
- "What is the blast radius of adding a required parameter to `Response.__init__`, and which
  tests exercise it?"
- "I want to add a `BitLength` SQL function modelled on `CharLength`. Where does it go and
  which tests do I mirror?"
- "Which classes implement `TopicsRepository`, and how does topic data get from the network to
  the For You screen?"

## Reading an answer

Every relationship carries a confidence label. Treat them as three tiers:

| label | meaning | trust |
|---|---|---|
| `exact`, `typed` | import edge, or a call whose receiver type the graph knows (field, parameter, local, return type, inherited type) | a fact |
| `same_file`, `package`, `import` | resolved by scope: the only definition in this file, package or imported file | a fact in practice |
| `ambiguous` | a lead: several candidates, or a call on a value whose type is unknown | verify before acting |
| `text match` | the agent found it with grep, not the graph | verify before acting |

Two lines to look for:

- **"note: X overrides Y ..."** on a `callers` or blast-radius answer. Callers of the base method
  reach your method at runtime; the agent should have included them.
- **"Tests reached through resolved edges (a lower bound ...)"**. Tests that reach the target
  through an unannotated fixture, a DI container or an acceptance harness are not linked. Add
  the tests you know from convention.

Every answer ends with **Evidence** (the commands it ran) and **Limits** (what it could not
resolve for that question). If the Limits section is empty on a hard question, be suspicious.

## Limits you will meet first

Python
- Untyped pytest fixtures: `def app(): ...` without `-> Flask` leaves `app.route(...)` in tests
  unresolved. Annotate the fixture's return type and the tests link.
- Dynamic dispatch, registries and decorators that register handlers (`@app.route` is fine;
  `HANDLERS[name](...)` is not). The agent will say "text match" for these.
- External libraries stop the graph: `httpcore`, `werkzeug`, `sqlalchemy` calls are reported as
  unresolved, which is correct, not a bug.

Kotlin
- Lambda parameters other than `it` (`{ list -> list.map { .. } }`) and `it` on a receiver the
  graph cannot type (a chain like `xs.map { }.filter { it.f() }`) stay leads.
- Gradle `.kts` build scripts, Hilt module wiring and `@Binds` are indexed as declarations but
  their DSL calls are not resolved.
- A handful of grammar limits remain (single-line `object X : B() { .. }`, a few `when`
  forms); the file is still indexed, with `parse errors (first at L<n>)` in its header.

Both
- Generated code that is not checked in (Room DAOs, protobufs, Bazel `gen_*`) is invisible.
- Helm templates are only listed by `kind`; Terraform Jinja templates (`*.tf.tmpl`) are not
  parsed.
- The graph is per checkout and refreshed on every question (instant when the tree is
  unchanged, seconds otherwise). Editing files and asking again is fine.

## Using the engine yourself

The agent's skills are plain scripts you can run:

```bash
S=.claude/skills/code-graph/scripts/run.sh           # or .agents/.gemini/.github equivalents
$S build --root .                                    # refresh the graph
$S query overview --no-tests --lang kotlin           # hubs, directories, entry points
$S query find OrderService                           # locate; exact matches first
$S query symbol OrderService                         # the card: members, calls, dependents, overrides
$S query callers Repo.save --depth 2                 # who calls it (add --summary on a hub)
$S query trace-deps Order --depth 3 --files-only     # blast radius
$S skeleton src/main/kotlin/com/acme/Order.kt        # a file's outline with line ranges
```

`query --root DIR` works from outside the repository; every query takes `--json`.

## When something looks wrong

1. Read the cited line. The agent cites `file:line` for every claim so this takes seconds.
2. If the graph missed or misattributed an edge, note the three things we need to reproduce
   it: the file and line, what you expected, and the confidence label it gave (or "nothing").
   The agent's own **Tool notes** format is ideal. Send it to the platform team or open an
   issue in this repository; fixes are small and regression-tested, and usually ship the same
   day.
3. A missing edge is more common than a wrong one. The agent is designed to say "lead" or
   "text match" rather than guess; if it presented a lead as a fact, that is the bug to report.
