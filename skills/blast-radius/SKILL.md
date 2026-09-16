---
name: blast-radius
description: >
  Compute the downstream impact of changing a file, class, function, DTO, Terraform resource or
  module, or Kubernetes object: which files and symbols depend on it, through which relationship,
  and which tests to run. Use before refactors, signature changes, schema/DTO changes, infra
  changes, and when reviewing a diff for unintended consequences.
allowed-tools: Bash(*/code-graph/scripts/run.sh *), Bash(*/blast-radius/../code-graph/scripts/run.sh *), Bash(git diff *), Bash(git status *), Read
---

# blast-radius: deterministic downstream impact

Produces the "Blast Radius & Downstream Impact Matrix" described in the source article: for a
component that will change, list every dependent file with the dependent symbol, the relationship
(`calls`, `imports`, `extends`, `implements`, `instantiates`, `references` via a signature or
Terraform/K8s reference, `uses_module`, `selects`, `depends_on`), and the resolution confidence.

Engine: `../code-graph/scripts/run.sh`, relative to this skill folder (the three skills are
installed side by side).

## Procedure

1. Make sure the graph is current (a git-stamped unchanged tree returns instantly; otherwise
   changed files are re-parsed and everything is re-linked):
   ```bash
   ../code-graph/scripts/run.sh build --root .
   ```
2. Identify targets. If the user gave a diff or branch, take the changed files from
   `git diff --name-only` (or `git status`) and, for each, the changed symbols from
   `query file PATH` (line ranges vs. the hunks).
3. For each target run:
   ```bash
   ../code-graph/scripts/run.sh query trace-deps TARGET --depth 3
   ```
   `TARGET` may be a file path, a symbol, a Terraform address (`module.vpc`, `aws_vpc.this`) or a
   Kubernetes object (`ConfigMap/web-config`). Add `--include-ambiguous` when the target's name
   is common and you want the leads too; add `--json` when you will post-process. For a hub
   (hundreds of callers) start with `--summary` (directories, most-connected dependents,
   relationship mix) and `--files-only`; the per-edge table is only useful below a few hundred
   rows and degrades to the summary on its own above `--max-rows` (200).
4. Report, in this shape (one block per modified component):

   | Component modified | Immediate impact | Dependent files and blast radius |
   |---|---|---|
   | `DataDTO` (app/dto.py) | field additions/renames | 1. api/controller.py (API contract, `references`, exact) 2. services/data_service.py (`convert_to_entity`, typed) 3. tests/test_data_service.py |

   Then: files affected (direct / transitive), tests likely to exercise the change (the tool's
   "Tests reached through resolved edges" line lists dependents that are test files by language
   convention: `_test.go`, `test_*.py`/`*_test.py`/`conftest.py`, `*.test.ts`/`*.spec.js`,
   `*Test.java`/`*IT.java`, `*Test.kt`/`*Spec.kt`, `_test.rs`, or anything under a `test`, `tests`,
   `__tests__`, `spec`, `specs` or `testing` directory), and anything reported as unresolved or
   ambiguous that a human should double-check. The tests line is a lower bound: Python tests
   whose fixture functions carry a return annotation are linked (the untyped parameter is typed
   from the fixture), but tests that reach the target through unannotated fixtures, untyped
   receivers or a framework (acceptance tests, DI) are not; name those from the package
   convention instead. When the target overrides a base method, the note under the header says
   how many callers reach it through the base by dynamic dispatch; include them.
5. Only after the matrix, open specific line ranges to confirm the riskiest edges. Do not read
   whole files.

## Interpreting results

- Hop 1 rows are direct dependents; hop 2+ are transitive through those symbols.
- `references ... via signature` means the type appears in a parameter, return or field type: an
  API contract, exactly the case where a DTO change ripples outward.
- For Terraform, `references -> output.X` inside a module directory means a caller reads that
  module output; `depends_on` is explicit; a `moved.<address>` dependent is a `moved` block that
  names the target, so renaming the resource also means updating it. In a module repository the
  examples usually call the registry address of the module they mirror; those show up only
  with `--include-ambiguous` (an `ambiguous` `uses_module` lead to the local directory). For Kubernetes, `selects` is a Service/PDB label
  selector matching a workload's pod template, and `references` covers ConfigMap/Secret/PVC/
  ServiceAccount/Ingress-backend/HPA-target links.
- "no dependents found" is a real result (dead code, or an entry point), but confirm the graph
  covers the relevant directories with `query stats` before saying so.
