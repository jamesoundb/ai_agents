## What and why

<!-- What does this change, and why? Link the issue or the problem it fixes. -->

## Agents and skills touched

<!-- e.g. skills/terraform-review, agents/helm. Write "none" for docs/CI-only changes. -->

## Checklist

- [ ] Pipeline is green (repo checks, lint, engine tests, install check, smoke test)
- [ ] Edited the canonical files only (`agents/`, `skills/`, `tools/`), not generated harness folders
- [ ] Added or renamed an agent or skill: ran `./install.sh --harness all` and committed `AGENTS.md`
- [ ] Changed `skills/code-graph/scripts/astgraph.py`: added a regression case to the fixture
- [ ] Tried the changed agent or skill locally (`git checkout <this branch>` in `~/ai_agents`, re-run the install)
- [ ] README or the skill's `SKILL.md` updated if usage changed

## How to roll back

<!-- Usually: revert this MR's merge commit. Note anything else (config, data) a revert would not undo. -->
