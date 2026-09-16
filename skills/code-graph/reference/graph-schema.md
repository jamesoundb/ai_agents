# Graph schema (`.ast-graph/graph.json`, version 5)

```json
{
  "version": 5, "engine": "<sha1 of astgraph.py>", "root": "/abs/repo", "built_at": "2026-09-16T10:00:00",
  "stats": {"files": 28, "nodes": 151, "edges": 225, "unresolved_refs": 20, "asset_imports": 0,
            "languages": {"python": 6}, "edge_confidence": {"typed": 12}},
  "nodes": [ {node}, ... ],
  "edges": [ {edge}, ... ],
  "files": { "rel/path.py": {"hash": "sha1", "file_node": {node}, "nodes": [...], "refs": [...]} }
}
```

## Node

| field | meaning |
|---|---|
| `id` | `file::qname@line` (files: the path; directories: `package:dir` / `terraform_module:dir`; externals: `external:name`) |
| `kind` | see table below |
| `name` / `qname` | simple name / dotted qualified name (`Class.method`, `aws_vpc.this`, `Deployment/web`) |
| `file`, `line`, `end_line` | 1-based inclusive range |
| `signature` | display form: `def f(x) -> int`, `func (s *S) Get(id string)`, `resource "aws_vpc" "this"` |
| `annotations` | decorators / Java annotations / Rust attributes (text without `@`) |
| `parent` | id of the containing node (file, class, struct, impl ...) |
| `extra` | kind-specific: `type` (fields; `inferred: true` when taken from the initializer rather than a declaration), `package`, `receiver_type` (Go methods, Kotlin extension functions), `companion` / `anonymous` (Kotlin companion objects and `object : Base() {}` initialisers, both `class` nodes), `source`/`version`/`resolved_dir` (module_call), `description` (Terraform variable/output), `required_providers`/`required_version` (terraform block), `from`/`to` (moved), `to`/`id` (import_block), `apiVersion`/`namespace`/`labels`/`images`/`selector`/`template_labels` (k8s_object), `has_errors` and `first_error_line` (file), `language` |

Node kinds: `file`, `package`, `terraform_module`, `external`, `external_module`;
code: `class`, `interface`, `struct`, `enum`, `trait`, `impl`, `record`, `annotation`, `type`,
`module`, `function`, `method`, `constructor`, `field`, `variable` (module/file-level variables,
including Kotlin top-level properties with their declared `type`);
Terraform: `resource`, `data`, `module_call`, `variable` (`var.x`), `output`, `provider`, `local`,
`terraform`, `moved`, `import_block`; Kubernetes/YAML: `k8s_object`, `helm_template`, `value`
(Helm values.yaml keys).

## Edge

`{"src", "dst", "type", "line", "confidence", ...}` where `src` depends on `dst`.

| type | meaning |
|---|---|
| `contains` | structural parent -> child (file -> class -> method) |
| `imports` | file -> file/package/external (`names` lists the imported symbols) |
| `calls` | function/method -> function/method/class (constructor call) |
| `extends`, `implements` | inheritance; Go embedding and interface embedding count as `extends` |
| `instantiates` | `new Foo()`, `Foo{}` composite literal, Rust struct literal |
| `references` | signature type use (`via: "signature"`), HCL address reference, K8s object reference (`via: key`) |
| `uses_module` | Terraform `module` block -> module directory (or `external_module`) |
| `depends_on` | Terraform explicit `depends_on` |
| `selects` | K8s Service/PDB/NetworkPolicy selector -> workload with matching pod-template labels |

Confidence: `exact`, `typed`, `same_file`, `package`, `import`, `unique`, `ambiguous`, `external`.
`typed` means the member was matched by the identity of the resolved receiver type node (or one
of its resolved ancestors, up to three levels), never by the bare type name. `import` covers both
"target lives in an imported file" and "the call/type is qualified by an import binding and the
target was found in the bound files or through their re-exports". Calls whose receiver is
external (external import binding, external or builtin declared type, or a field chain that
leaves the repo) produce no edge and count as unresolved; calls on a receiver of unknown type are
`same_file` when a same-file definition exists, else up to three `ambiguous` same-language leads.
`unique` survives only for types in Rust and for free calls nowhere: a name that is not imported
and not in scope is not guessed.

`stats` also carries `asset_imports`: JS/TS imports of non-code files, counted, never linked.
`query stats` prints the same object plus `node_kinds` and `edge_types` histograms and the
`root`/`built_at` of the graph.

## Build artifacts

`graph.json` (`version` = `GRAPH_VERSION`, currently 5, and `engine` = SHA-1 of `astgraph.py`; a
mismatch of either forces a full re-parse) and, in
git checkouts, `graph.json.stamp`: `{"version", "stamp", "stats", "built_at"}` where `stamp` is a
SHA-1 over HEAD, the porcelain status of indexed source files under the root, their content, and
the include/exclude/keep-dir filters. A matching stamp makes `build` return without loading the
graph.

## Refs (per file, pre-link)

Raw, unresolved observations kept so that incremental builds can re-link without re-parsing:
`{"kind": "call|import|extends|implements|instantiates|references|depends_on|uses_module|uses_type",
"name", "src", "line", "hint", "hint_full", "hint_type", "chain", "names", "alias", "alias_map", "reexport",
"argc", "arg_types", "attr", "via", "namespace", "module_name"}`.
`argc` is the number of call arguments (absent when a spread/splat is present) and `arg_types`
their declared or literal types where known, used to choose among overloads; `hint_full` is the
full qualifier text of a type reference; `reexport` marks `export ... from` / `pub use`.
`hint_type` may be `<call>expr|roottype` or `<call?>expr|roottype` (a local bound to a call
result, `?` = unwrapped/awaited); the linker types it from the callee's return type at link
time.
`hint` is the receiver text of a call (`self.repo`), the qualifier of a type reference
(`schema` in `schema.Resource{}`, `base_a` in `class T(base_a.TestCase)`) or None; `hint_type` is
the receiver's declared type with its qualifier kept (`schema.ResourceData`); `alias` is the local
name a whole-module import binds (`import json as js`, `import * as util`,
`transport_tpg "…/transport"`, `const fs = require("fs")`); `names` are the imported names as
written and `alias_map` maps them to their local aliases (`from a import B as C`).
