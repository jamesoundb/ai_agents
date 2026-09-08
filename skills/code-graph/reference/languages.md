# Per-language coverage and limits

Grammars come from `tree-sitter-language-pack`; no compiler, LSP, or build is required. The
trade-off (from the article): a fast syntactic map gets ~90% of the value of a compiler-grade
index. Resolution is by name plus lightweight type tracking, never by a real type checker.

| language | definitions extracted | relationships | type tracking used for `typed` calls |
|---|---|---|---|
| Python | classes (bases, decorators), functions/methods (params, return annotation), class-level and `self.x` fields, module variables | imports (absolute, relative, `from x import y`), calls, instantiation via call of a capitalized name | annotated parameters, `x: T`, `x = T(...)`, `self.f = T(...)` / `= typed_param` |
| JavaScript / TypeScript / TSX | classes (extends/implements, decorators), interfaces, type aliases, enums, namespaces, functions, arrow-function consts, methods, fields, TS parameter properties | ESM imports, `require()`, calls, `new X()`, interface extends | typed parameters, `const x: T`, `const x = new T()`, field types |
| Go | structs (fields, embedded types), interfaces (method set), funcs, methods (attached to receiver struct in the same package), package vars/consts | imports (module path from `go.mod`), calls, composite literals | receiver, typed parameters, `x := T{}` / `&T{}`, `var x T`, field chains (`h.S.Get`) |
| Java | classes/interfaces/enums/records/annotations (extends, implements, annotations), methods, constructors, fields | imports (single, wildcard, static), method invocations, `new X()`; same package resolved without import | fields, parameters, local declarations, enhanced-for variables, `this.field` chains |
| Rust | structs (fields), enums, traits, impl blocks (methods attached to the type), functions, mods, type aliases, attributes | `use`, calls (plain, `Type::fn`, `x.method()`), macros (`name!`), struct literals, trait impls | typed parameters, `let x: T`, `let x = T { .. }`, `let x = T::new()`, `self` |
| Terraform (HCL) | `resource`, `data`, `module`, `variable`, `output`, `provider`, `locals`, `terraform` (required providers, backend) | `var.`, `local.`, `module.x(.output)`, `data.t.n`, `type.name` references across all `.tf` files of the same directory; `depends_on`; local module sources and `.terraform/modules/modules.json` for registry/git modules | n/a |
| Kubernetes YAML | every document with `apiVersion` + `kind` (name, namespace, labels, images, selectors, pod-template labels), `List` items, Kustomization resources, Helm `values*.yaml` keys | ConfigMap/Secret/PVC/ServiceAccount/StorageClass/Ingress backend/HPA target/RoleBinding refs, Service->workload label selection, kustomization imports | n/a |

## Known limits (state them when relevant)

- No overload resolution, generics, dynamic dispatch, reflection, DI-by-annotation, or
  monkeypatching. Interface calls resolve to the interface method, not to implementations; use
  `query callers <Interface.method>` plus `implements` edges to find them.
- Calls through untyped locals (`x = make_thing(); x.run()`) fall back to name matching
  (`unique` or `ambiguous`).
- JS/TS path aliases beyond `@/` and `~/` (tsconfig `paths`) are not resolved.
- Python namespace packages without `__init__.py` resolve only when the module file exists.
- Helm templates are not parsed as YAML (Go template syntax); only their `kind:` lines are noted.
- Files with syntax errors produce partial skeletons flagged `parse errors`.
- Extensions not in `EXT_LANG` (astgraph.py) are skipped; add a mapping and, if needed, a handler.
