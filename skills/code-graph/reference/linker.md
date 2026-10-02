# How the linker resolves names

Moved out of `SKILL.md` so the skill loads lean; read this when an edge (or a missing edge)
needs explaining. Confidence labels themselves are summarised in `SKILL.md`.

The linker is receiver-aware and import-aware:

- Imports bind local names to files: `from pkg import mod` binds `mod` to `pkg/mod.py`,
  `import pandas as pd` binds `pd` to `pandas/__init__.py`, and re-exports are followed up to
  three levels (`pd.DataFrame` reaches `pandas/core/frame.py`; `test.TestCase` reaches the class
  it aliases). A qualified call (`ops.convert_to_tensor(...)`) resolves only inside the bound
  files, with `import` confidence, never to a same-named function elsewhere.
- `x.f()` is resolved only when the linker knows what `x` is (a declared or inferred type, a
  field type, `self`/`this`, an import binding, or the return type of a call: `r = make()`,
  `let s = build()?`, `var g = getGateway()`, `getStyle().getNullText()`, `self.repo().save()`
  are all typed from the callee's declared return type, unwrapping `Result`/`Option`/`Promise`
  where the code does). A field typed by a generic parameter resolves through the parameter's
  bound (`sink: S` with `S: Sink` reaches `Sink.matched`), and an unbounded one yields no edge. Members are matched by the resolved type
  node and its resolved ancestors, so two classes named `TestCase` are never confused. If `x`
  belongs to an external package or is a builtin (`error`, `string`, `List`), the call is left
  unresolved. A call on a value of unknown type yields a `same_file` edge at most, otherwise up
  to three `ambiguous` leads in the same language; more candidates than that is noise, not a lead.
- An unqualified call (`f()`) resolves through a from-import binding, then the same file, then the
  same Go/Java/Kotlin package, then wildcard imports. It never targets a method (except the
  implicit `this` of Java and Kotlin), never a language builtin (`len`, `type`, `map`, `require`,
  `make`), and is never guessed by name alone.
- Base classes and signature types carry their qualifier: `collections_abc.Iterable` is external,
  `data_types.DatasetV2` resolves inside the bound module, fully-qualified names
  (`org.apache.commons.lang3.builder.Builder<T>`, `pkg.sub.Class`) resolve through the package,
  a class's own nested members are never candidates for its `extends`/`implements` clause, and a
  bare type name that is neither imported nor in scope produces no edge.
- Re-exports are followed everywhere they occur: Python `from x import y` in `__init__.py`,
  JS/TS `export { a as b } from` / `export * from` barrels, Rust `pub use` (grouped paths
  expanded).
- Python: `Optional["X"]`, `X | None`, `Union[X, None]`, `Final[X]` and string annotations all
  mean `X`; an `Enum` member (`Status.PAID`) has the enum's type; `x = flask.Blueprint(...)` keeps
  its module qualifier; `from a import B as C` resolves `class D(C)` to `B`; `*args`/`**kwargs` are
  builtin containers (their `.pop()`/`.add()` never lead to repo methods); in a test file an
  untyped parameter that names a `@pytest.fixture` function with a return annotation (same file,
  then `conftest.py` up the tree) is typed from it, so `def test_x(app): app.route(...)` links.
  `self.x = param or Ctor()` and `x = A() if c else B()` take the first operand that names a type, and a
  `@property`/`@cached_property` reads as a field typed by its return annotation or by the `self._x` it
  returns -- so Django's `self.query.chain()` (`self._query = query or sql.Query(model)` behind
  `@property def query`) resolves to `Query.chain`.
- C/C++: a prototype outside a class is a free function. An unqualified call that is not defined in the
  same file resolves through the file's `#include`s (transitively, 4 levels): the prototype found there is
  linked to its definition (`import`); with no visible prototype, the single non-`static` definition in
  the C family (`unique`), else up to five `ambiguous` leads (vendored duplicates such as two `sds.c`).
- Kotlin: inside `fun T.f()` a bare property (`rules`) is the receiver's (`this.rules`), including as the
  receiver of a collection lambda (`rules.flatMap { it.visitFile() }`); an annotation on a supertype
  (`: @Suppress(..) api.MultiRule()`) is blanked before parsing; fully-qualified supertypes resolve by
  package as in Java.
- Java `new X(args)` (and other `instantiates` references with arguments): besides the edge to the class, a
  `calls` edge to the constructor overload chosen by argument count and types (`ambiguous` to up to three
  when they cannot be told apart), so `path` and `callers X.X@line` run through constructors.
- Kotlin/Java overrides need the same parameter types (an overload of the same name is not an override),
  and `super.f(x)` inside `override fun f(...)` whose repo candidate has other parameter types is left to
  the external base class it really calls.
  Loop and comprehension variables are typed from the iterable (`for it in self.items` with
  `items: list[Item]`, `[i.f() for i in xs]`, `for k, v in d.items()` on a `dict[K, V]`), a walrus
  (`(found := d.get(k))`) binds like an assignment and `dict[K, V].get/pop/setdefault` yields
  `V`, `with X() as x` types `x` as `X`, `self.x = ...` inside an `if`/`try` of `__init__` is still
  a field, `super().m()` resolves on the class's parents, `@property`/`@cached_property` segments
  type a chain (`item.heavy.area()`), and literal receivers (`", ".join(..)`, `{...}.get(..)`) or
  builtin-typed values (`dict`, `list`, `str` ...) never lead into repo methods.
- Kotlin: inside `fun T.f()` the receiver `this` (and `this@f`) is `T`; a top-level
  `val currentDialect: Dialect` is a typed variable node, so `currentDialect.functionProvider.f()`
  resolves through the property chain from any file that imports it (explicitly or by wildcard)
  or shares its package. Lambdas: the implicit receiver of `x.apply { }` / `x.run { }` and of a
  DSL builder `order(id) { add(..) }` (a function whose parameter is `T.() -> R`) is that type;
  `it` in `x.also { }` / `x.let { }` is `x`, and in `xs.forEach { }` / `map` / `filter` / `sumOf`
  / `first` ... on a `List<T>`/`Set<T>`/`Sequence<T>` (declared or `mutableListOf<T>()`) it is `T`.
  Builder setters `fun x(v) = apply { }` return the receiver; `a + b` / `*` / `-` / `/` / `%` on
  a repo-typed left operand are calls of `plus`/`times`/...; `val r = x as T` and `x ?: return`
  type the local; enum entries are fields typed as the enum; `chain: Interceptor.Chain` (nested
  type of an imported class) and `: Interceptor.Chain` supertypes resolve; an inner class calls
  outer members; multi-line builder chains (`Request\n  .Builder()\n  .url(u)`) are one chain.
  Smart casts type the variable inside `if (e is T)` (also `&&`-joined) and `when (e) { is T -> }`
  branches; `val x by lazy { X() }` types `x`; `Topic::slug` callable references are calls;
  `useCase()` on a parameter or property whose type declares `operator fun invoke` resolves to
  it; a `fun interface` constructor call `Listener { }` links the interface; plain (non-`val`)
  constructor parameters are in scope for property initialisers; data-class `copy(..)` and enum
  `valueOf(..)` keep the receiver's type without inventing a member edge; `@Composable` (any
  annotation) on a function type is blanked before parsing because the grammar rejects it, so
  Compose components are indexed.
- Terraform: references are resolved within a directory (root module or one module), `module.x.y`
  reaches `output.y` in the called module's directory, and a registry source whose `//subdir`
  exists in the repo (`ns/name/google//modules/x` with a local `modules/x`) gets an `ambiguous`
  lead to that directory next to the `external` edge, so `trace-deps modules/x
  --include-ambiguous` finds the examples that exercise it.
