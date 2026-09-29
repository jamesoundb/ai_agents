# Per-language coverage and limits

Grammars come from `tree-sitter-language-pack`; no compiler, LSP, or build is required. The
trade-off (from the article): a fast syntactic map gets ~90% of the value of a compiler-grade
index. Resolution is by name plus lightweight type tracking, never by a real type checker.

| language | definitions extracted | relationships | type tracking used for `typed` calls |
|---|---|---|---|
| Python | classes (bases, decorators), functions/methods (params, return annotation), class-level and `self.x` fields (enum members typed as the enum), module variables | imports (absolute, relative, `from x import y`, `as` aliases), calls, instantiation via call of a capitalized name (`mod.Class(...)` keeps the qualifier) | annotated parameters (`Optional[X]`, `X \| None`, `"X"` unwrapped), `x: T`, `x = T(...)`, `self.f = T(...)` / `= typed_param`, `x = f()` return types, `*args`/`**kwargs` as tuple/dict, pytest fixture parameters from the fixture's return annotation, loop/comprehension variables from `list[T]`/`dict[K, V]`, `with X() as x`, walrus, `super()` |
| JavaScript / TypeScript / TSX | classes (extends/implements, decorators), interfaces, type aliases, enums, namespaces, functions, arrow-function consts, methods, fields, TS parameter properties | ESM imports, `require()`, calls, `new X()`, interface extends | typed parameters, `const x: T`, `const x = new T()`, field types |
| Go | structs (fields, embedded types), interfaces (method set), funcs, methods (attached to receiver struct in the same package), package vars/consts | imports (module path from `go.mod`), calls, composite literals | receiver, typed parameters, `x := T{}` / `&T{}`, `var x T`, field chains (`h.S.Get`) |
| Java | classes/interfaces/enums/records/annotations (extends, implements, annotations), methods, constructors, fields | imports (single, wildcard, static), method invocations, `new X()`; same package resolved without import | fields, parameters, local declarations, enhanced-for variables, `this.field` chains |
| Kotlin | classes/interfaces/enums (entries as typed fields)/data/sealed/objects/companion objects (supertypes, annotations, generics with bounds), primary-constructor `val`/`var` properties, class properties, functions/methods (params incl. `vararg`, return type, extension receiver; `= apply { }` bodies return the receiver), top-level functions and top-level `val`/`var` properties (typed variables) | `import` (single, wildcard, `as` alias) resolved through the declared package and the top-level definitions of each file (shared JVM package index with Java, so Kotlin and Java in one package see each other), calls (`f()`, `a.b()`, `a?.b()`, `Outer.member()` incl. companions, `(x as T).m()`, `a + b` as `plus`, calls inside lambdas with their implicit receiver or `it`), supertypes incl. nested types of imported classes | typed parameters and `vararg` (as arrays), `val x: T`, `val x = T(...)`, `val x = f()` (return type), properties incl. inherited ones used without `this.`, generic bounds on constructor properties, `Registry.lookup()` on `object`s |
| Rust | structs (fields), enums, traits, impl blocks (methods attached to the type), functions, mods, type aliases, attributes | `use`, calls (plain, `Type::fn`, `x.method()`), macros (`name!`), struct literals, trait impls | typed parameters, `let x: T`, `let x = T { .. }`, `let x = T::new()`, `self` |
| C / C++ (and CUDA `.cu`/`.cuh`) | namespaces (as modules), classes/structs/unions (bases; `public`/`private` is not recorded), enums and enumerators, constructors, destructors (`~T`, kept distinct from methods), methods, free functions, fields; a declaration in the `.h` and its out-of-line definition in the `.cc` share one qualified name and are paired by a `defines` edge, so callers do not split across the two | `#include "x.h"` resolves to the header's file node; `#include <vector>` stays external and is never matched to a repo file of the same name; calls (`obj->m()`, `ns::fn()`) with a receiver hint, `new T()` as `instantiates`, base classes as `extends` | receiver hints from pointer/namespace qualifiers; C++ is the language where declaration/definition pairing does most of the work, not local type inference |
| Terraform (HCL) | `resource`, `data`, `module`, `variable`, `output`, `provider`, `locals`, `terraform` (required providers, backend), `moved` and `import` blocks | `var.`, `local.`, `module.x(.output)`, `data.t.n`, `type.name` references across all `.tf` files of the same directory, each recorded at the line of the reference (not of the enclosing attribute); `depends_on`; `moved`/`import` -> their `to` address; local module sources and `.terraform/modules/modules.json` for registry/git modules; a registry source with a `//subdir` that exists in the repo is an `ambiguous` lead to that directory. Iterators of `dynamic` blocks (`node_config.value`, or the `iterator =` name) are not references | n/a |
| Kubernetes YAML | every document with `apiVersion` + `kind` (name, namespace, labels, images, selectors, pod-template labels), `List` items, Kustomization resources, Helm `values*.yaml` keys | ConfigMap/Secret/PVC/ServiceAccount/StorageClass/Ingress backend/HPA target/RoleBinding refs, Service->workload label selection, kustomization imports | n/a |

## Python <-> C++ bridge

A Python symbol whose implementation is a C++ kernel used to have its dependency chain cut at the
language boundary, so a blast radius reported no dependents for something with many. Two binding
styles are followed:

- **pybind11**: `PYBIND11_MODULE(_pywrap_x, m)` becomes a `py_module` node and each `m.def("Name",
  ...)` a `py_binding`. A Python call to `Name` links to that binding, and the binding links on to
  the C++ function it exports -- both the lambda body and the `&Fn` pointer form -- so the chain
  reaches the real implementation instead of stopping at the export.
- **TensorFlow-style `REGISTER_OP`** registration, the same way.

These edges carry their own confidence, **`binding`**, rather than being passed off as ordinary
calls:

```
callers of RealCompute  (cpp/bindings.cc:5)
  RealCompute <- _pywrap_demo.DemoExecute  [calls, same_file]  (cpp/bindings.cc:18)
    _pywrap_demo.DemoExecute <- run_it     [calls, binding]    (python/bridge/caller.py:6)
```

A call with a Python definition in scope is never diverted to a binding of the same name. Limits:
the binding must be visible in the repository as source -- generated wrapper modules that only
exist after a build (Bazel `gen_*_ops.py`) are not there to be linked, and a Python caller of
those stops at the generated name.

## Known limits (state them when relevant)

- No real type checker. Overloads are attributed only by argument count and the argument types
  the linker can see, and generics only through a declared bound (both described below); dynamic
  dispatch, reflection, DI-by-annotation and monkeypatching are not followed at all. Interface
  calls resolve to the interface method, not to implementations; use
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
- Kotlin lambdas: `apply`/`run` and DSL `T.() -> R` parameters give the lambda an implicit
  receiver; `also`/`let` and the collection functions (`forEach`, `map`, `filter`, `sumOf`,
  `first`, ...) type `it` from the receiver or its element type (`List<T>`, `Set<T>`,
  `Sequence<T>`, `mutableListOf<T>()`); `it` on a receiver whose type or element type the graph
  does not know (a call-chain receiver such as `xs.map { }.filter { }`, a `Map`, an external
  collection) stays a lead; a named lambda parameter (`{ list -> list.f() }`) is not typed. Operators record `plus`/`minus`/`times`/`div`/`rem` only when the
  left operand is a constructor call or a local of a non-builtin declared type, never for
  literals or builtin numbers; a parenthesised binary expression as a receiver (`(a + b).f()`) is
  a lead.
- Kotlin: `this` and `this@label` inside an extension function `fun T.f()` are the receiver `T`;
  top-level properties are `variable` nodes with their declared type, so a chain through an
  imported `val` (`currentDialect.functionProvider.charLength()`) is typed. A method card lists
  `Overrides` / `Overridden by` (same-named methods across `extends`/`implements`, three levels),
  which is how dialect-specific overrides are found; `callers` of the base method still lists only
  direct callers of that definition.
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
- Overloads (Java, Kotlin, TS declarations, Go/Rust same-named functions in different scopes): each
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
- Terraform: `*.tf.tmpl` / Jinja templates (the `autogen/` source of generated module copies) are
  not indexed; only the rendered `.tf` files are. Go/Terratest fixtures are not linked to the
  resources they exercise, so `trace-deps` on Terraform reports test files by convention only.
  Directories named `build`, `dist`, `target`, `vendor` (and the rest of `DEFAULT_EXCLUDE_DIRS`)
  are skipped unless `--keep-dir NAME` re-includes them.
- Helm templates are not parsed as YAML (Go template syntax); only their `kind:` lines are noted.
- Kotlin grammar limits that remain (Exposed: 17 of 888 files): single-line `object X : B() { .. }`
  bodies, `when` conditions with `(x as? T)?.m() == true`, assignments to a property named
  `where`. Annotations on function types (`@Composable () -> Unit`) are handled by blanking the
  annotation before parsing (same byte offsets, so lines are exact).
- Files with syntax errors produce partial skeletons flagged `parse errors (first at LN)`.
- Extensions not in `EXT_LANG` (astgraph.py) are skipped; add a mapping and, if needed, a handler.
