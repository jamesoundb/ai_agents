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
| Kotlin | classes/interfaces/enums/data/sealed/objects/companion objects (supertypes, annotations, generics with bounds), primary-constructor `val`/`var` properties, class properties, functions/methods (params incl. `vararg`, return type, extension receiver), top-level functions | `import` (single, wildcard, `as` alias) resolved through the declared package and the top-level definitions of each file (shared JVM package index with Java, so Kotlin and Java in one package see each other), calls (`f()`, `a.b()`, `a?.b()`, `Outer.member()` incl. companions, `(x as T).m()`), supertypes | typed parameters and `vararg` (as arrays), `val x: T`, `val x = T(...)`, `val x = f()` (return type), properties incl. inherited ones used without `this.`, generic bounds on constructor properties, `Registry.lookup()` on `object`s |
| Rust | structs (fields), enums, traits, impl blocks (methods attached to the type), functions, mods, type aliases, attributes | `use`, calls (plain, `Type::fn`, `x.method()`), macros (`name!`), struct literals, trait impls | typed parameters, `let x: T`, `let x = T { .. }`, `let x = T::new()`, `self` |
| Terraform (HCL) | `resource`, `data`, `module`, `variable`, `output`, `provider`, `locals`, `terraform` (required providers, backend) | `var.`, `local.`, `module.x(.output)`, `data.t.n`, `type.name` references across all `.tf` files of the same directory; `depends_on`; local module sources and `.terraform/modules/modules.json` for registry/git modules | n/a |
| Kubernetes YAML | every document with `apiVersion` + `kind` (name, namespace, labels, images, selectors, pod-template labels), `List` items, Kustomization resources, Helm `values*.yaml` keys | ConfigMap/Secret/PVC/ServiceAccount/StorageClass/Ingress backend/HPA target/RoleBinding refs, Service->workload label selection, kustomization imports | n/a |

## Known limits (state them when relevant)

- No overload resolution, generics, dynamic dispatch, reflection, DI-by-annotation, or
  monkeypatching. Interface calls resolve to the interface method, not to implementations; use
  `query callers <Interface.method>` plus `implements` edges to find them.
- Calls through untyped locals (`x = make_thing(); x.run()`) are recorded as `same_file` when a
  same-file definition exists, otherwise as up to three `ambiguous` same-language leads; they are
  never matched by name to another package. Calls on external or builtin receivers (`d.Get()`
  with `d *schema.ResourceData`, `err.Error()`, `list.add()`) and unqualified builtins (`len`,
  `type`, `map`, `make`, `require`) are left unresolved.
- Re-exports are followed three levels deep (`pandas/__init__.py` -> `core/api.py` ->
  `core/frame.py`; `from x import A as B`). Names bound by assignment (`DataFrame = _make()`),
  `__all__` manipulation or `importlib` are not.
- Python type aliases (`Axis = Union[...]`) are variables, not types: a signature that mentions
  one produces no `references` edge rather than a guess.
- Return-type inference: a local bound to a call result carries the callee expression (and its
  receiver's declared type) into the graph; the linker resolves the callee and uses its declared
  return type (`-> T`, `: T`, Go result, Java return type). `?`, `.unwrap()`, `.expect()` and
  `await` unwrap the first generic argument of `Result`/`Option`/`Promise`. Chains through calls
  (`a.b().c()`) follow method return types. Nothing is inferred for untyped return values
  (Python without annotations, JS functions) or for values built by dict/list literals.
- Generic bounds: `struct Holder<S: Sinker>`, `impl<S: Sink> Core<S>`, `where S: Sink`,
  `class Box<T extends Shape>` make a field of type `S`/`T` resolve through its first bound;
  without a bound the field's type is treated as external.
- Kotlin: `object` declarations, companions and anonymous `object : Base() { ... }` initialisers
  are classes (the anonymous one is named `<property>$object` and types the property); extension
  functions, including member extensions declared inside another class, are methods with a
  receiver type and match by that receiver anywhere in the repo (and appear on the receiver's
  card tagged `[extension]`); infix calls `a eq b` are calls of `eq` on `a`; every call in a
  navigation chain is recorded (`t.selectAll().where { }` yields `selectAll` and `where`);
  class properties initialised from calls (`val name = varchar("n", 50)`) are typed from the
  callee's return type; Kotlin stdlib scope and collection functions on untyped receivers
  (`apply`, `let`, `forEach`, `first`, ...) produce no edge instead of a lead. Overloads use
  subtype-aware scoring (exact > subtype > `Any` > generic). Not resolved: expression-bodied
  functions without a declared return type, lambda parameters (`it`, receiver lambdas), `by`
  delegation, annotation processors, reflection, `.kts` DSL calls; grammar parse errors on
  `(x as? T)?.m() == true ->` conditions and on assignments to a property named `where`.
- Overloads (Java, TS declarations, Go/Rust same-named functions in different scopes): each
  definition is its own node; a call is attributed by argument count, then by argument type where
  the arguments are declared parameters (varargs count as arrays), typed locals, literals,
  `this.`/`self.` fields or calls with a declared return type; otherwise the call becomes an
  `ambiguous` lead to every same-arity overload (at most five). Java arrays keep their `[]` in
  scope so `String[]` picks `isEmpty(Object[])`, not `isEmpty(boolean[])`. Argument expressions
  the linker does not type (casts, arithmetic, indexing, ternaries) leave the call undecided.
- JS/TS barrel files: `export { a as b } from './x'`, `export * from './x'` and
  `export * as ns from './x'` are followed, so `import { a } from 'pkg'` reaches the definition
  behind the package's `index.ts`. `export type * as X from` (TS 5.0) is a grammar parse error
  and is skipped.
- Rust: grouped `use a::{b, c::{d, e as f}}` is expanded into one import per leaf; `pub use`
  re-exports are followed (a trait behind `pub use crate::sink::Sink` resolves with `import`
  confidence from another crate).
- JS/TS path aliases: `@/`, `~/` and `compilerOptions.paths` + `baseUrl` from the nearest
  `tsconfig.json`/`jsconfig.json` above the importing file (JSONC tolerated), following relative
  `extends` chains (child config wins; base names such as `@tsconfig/node18` in node_modules are
  skipped). Workspace packages: any `package.json` `name` in the repo resolves to that package's
  source entry (`exports["."]` -> `import`/`default`/`types`, then `module`, `main`, `types`;
  `dist/x.js` is mapped to `src/x.ts`; fallback `src/index.*`), and `pkg/sub/path` deep imports
  to files under it. Imports of non-code assets (`.css`, `.svg`, `.png`, `.json`, `.vue`,
  `.svelte`, `.wasm`, `?url`/`?worker` queries) are counted as `asset_imports` and produce no
  edge and no external node.
- Rust: `crate::` resolves against the crate's source root (Cargo.toml `[lib]`/`[[bin]] path`
  directories, else `<crate dir>/src`), `super::`/`self::` follow the file-module layout
  (`foo.rs` <-> `foo/`, `mod.rs`/`lib.rs`/`main.rs`) and, inside an inline `mod`, refer to the
  file itself; `use other_crate::...` resolves to a workspace member by its Cargo package name
  (dashes become underscores). `impl` blocks roll up to their struct/enum in `overview`, and
  `--no-tests` also ignores inline `#[cfg(test)] mod tests` usage.
- Python namespace packages without `__init__.py` resolve only when the module file exists.
- Helm templates are not parsed as YAML (Go template syntax); only their `kind:` lines are noted.
- Files with syntax errors produce partial skeletons flagged `parse errors`.
- Extensions not in `EXT_LANG` (astgraph.py) are skipped; add a mapping and, if needed, a handler.
