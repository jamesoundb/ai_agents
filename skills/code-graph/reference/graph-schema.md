# Graph schema (`.ast-graph/graph.json`, version 1)

```json
{
  "version": 1, "root": "/abs/repo", "built_at": "2026-09-07T13:42:32",
  "stats": {"files": 28, "nodes": 151, "edges": 225, "unresolved_refs": 20,
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
| `extra` | kind-specific: `type` (fields), `package`, `receiver_type`, `source`/`resolved_dir` (module_call), `apiVersion`/`namespace`/`labels`/`images`/`selector`/`template_labels` (k8s_object), `required_providers`, `has_errors`, `language` |

Node kinds: `file`, `package`, `terraform_module`, `external`, `external_module`;
code: `class`, `interface`, `struct`, `enum`, `trait`, `impl`, `record`, `annotation`, `type`,
`module`, `function`, `method`, `constructor`, `field`, `variable`;
Terraform: `resource`, `data`, `module_call`, `variable` (`var.x`), `output`, `provider`, `local`,
`terraform`; Kubernetes/YAML: `k8s_object`, `helm_template`, `value` (Helm values.yaml keys).

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

## Refs (per file, pre-link)

Raw, unresolved observations kept so that incremental builds can re-link without re-parsing:
`{"kind": "call|import|extends|implements|instantiates|references|depends_on|uses_module|uses_type",
"name", "src", "line", "hint", "hint_type", "chain", "names", "attr", "via", "namespace"}`.
