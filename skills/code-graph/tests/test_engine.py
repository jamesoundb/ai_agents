#!/usr/bin/env python3
"""Regression tests for astgraph.py against tests/fixture.

Covers: language coverage, receiver-aware call resolution (no name-only edges onto external or
unknown receivers, typed edges through fields/locals/inheritance, namespace calls into repo
packages), test-file detection, Terraform/Kubernetes edges, incremental == full, and the git
stamp fast path. Run via run_tests.sh (needs tree-sitter). Exit code 1 on any failure.
"""
import contextlib
import io
import json
import re
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(HERE, "..", "scripts", "astgraph.py")
FIXTURE = os.path.join(HERE, "fixture")
sys.path.insert(0, os.path.dirname(ENGINE))
import astgraph  # noqa: E402

FAILS = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def run(*args, cwd=None):
    p = subprocess.run([sys.executable, "-B", ENGINE, *args], cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        print(p.stdout, p.stderr)
    return p.stdout


def build(root, *extra):
    return run("build", "--root", root, *extra)


def load(root):
    """The whole graph as one dict, read back out of the SQLite store.

    Storage is a database so that a query never has to materialise the graph (a 1.3 GB JSON blob
    on TensorFlow cost 5.3 GB of RSS per query). Tests are the one caller that legitimately wants
    everything at once, including the per-file parse cache, so they reassemble it here."""
    db = os.path.join(root, ".ast-graph", "graph.db")
    st = astgraph.Store(db)
    try:
        g = {k: st.meta(k) for k in ("version", "engine", "root", "built_at", "stats")}
        g["nodes"] = list(st.iter_nodes())
        g["edges"] = list(st.iter_edges())
        g["files"] = st.parsed()
        return g
    finally:
        st.close()


class Graph:
    def __init__(self, g):
        self.g = g
        self.nodes = {n["id"]: n for n in g["nodes"]}

    def node(self, file_suffix, qname):
        m = [n for n in self.g["nodes"] if n["file"].endswith(file_suffix) and n["qname"] == qname]
        assert len(m) == 1, f"{file_suffix}:{qname} -> {len(m)} nodes"
        return m[0]

    def edges_from(self, src, typ="calls", name=None):
        return [e for e in self.g["edges"] if e["src"] == src["id"] and e["type"] == typ and (name is None or e.get("name") == name)]

    def edges_to(self, dst, typ=None):
        return [e for e in self.g["edges"] if e["dst"] == dst["id"] and (typ is None or e["type"] == typ) and e["type"] != "contains"]

    def confs(self, edges):
        return sorted({(self.nodes[e["dst"]]["qname"], e["confidence"]) for e in edges})


def test_resolution(root):
    print("# receiver-aware resolution")
    build(root, "--full")
    G = Graph(load(root))
    langs = G.g["stats"]["languages"]
    for lang in ("go", "python", "javascript", "typescript", "java", "rust", "kotlin", "hcl", "yaml"):
        check(langs.get(lang, 0) > 0, f"language parsed: {lang} ({langs.get(lang, 0)} files)")

    # --- Go
    serve = G.node("go/handler/handler.go", "Handler.Serve")
    gets = G.confs(G.edges_from(serve, name="Get"))
    check(("MemStore.Get", "typed") in gets, f"go: h.S.Get -> MemStore.Get typed  {gets}")
    check(("ResourceDataMock.Get", "typed") in gets, "go: m.Get -> ResourceDataMock.Get typed")
    bad = [e for e in G.edges_from(serve, name="Get") if e["confidence"] in ("import", "unique", "package")]
    check(not bad, f"go: no name-only Get edges (d.Get / h.Client...Get / c.Get) {[(G.nodes[e['dst']]['qname'], e['confidence'], e['line']) for e in bad]}")
    amb = [e for e in G.edges_from(serve, name="Get") if e["confidence"] == "ambiguous"]
    check(all(e["line"] == 26 for e in amb) and amb, f"go: only c.Get (unresolvable callee) is an ambiguous lead: lines {[e['line'] for e in amb]}")
    typed_get = sorted(e["line"] for e in G.edges_from(serve, name="Get") if e["confidence"] == "typed")
    check(28 in typed_get, f"go: g.Get typed via getStore's return type {typed_get}")
    news = G.confs(G.edges_from(serve, name="New"))
    check(("New", "import") in news, f"go: store.New() -> import confidence {news}")
    inst = G.confs(G.edges_from(serve, "instantiates"))
    check(("ResourceDataMock", "import") in inst and not any(q == "Resource" for q, _ in inst), f"go: instantiates {inst}")
    mock = G.node("go/mock/mock.go", "ResourceDataMock.Get")
    firm = [e for e in G.edges_to(mock) if e["confidence"] != "ambiguous"]
    check(len(firm) == 1 and firm[0]["confidence"] == "typed", f"go: ResourceDataMock.Get has exactly 1 non-ambiguous dependent (typed m.Get), got {G.confs(G.edges_to(mock))}")
    refs = [e for e in G.edges_to(G.node("go/mock/mock.go", "ResourceDataMock"), "references")]
    check(not refs, "go: no signature `references` edge onto ResourceDataMock from *schema.ResourceData")

    # --- Python
    run_ = G.node("python/app/service.py", "Service.run")
    saves = G.confs(G.edges_from(run_, name="save"))
    check(saves == [("Repo.save", "typed")], f"py: self.repo.save -> Repo.save typed {saves}")
    check(not G.edges_from(run_, name="dumps"), "py: js.dumps() (aliased external) not linked to models.dumps")
    check(not G.edges_from(run_, name="join"), "py: os.path.join not linked")
    helper = G.node("python/app/service.py", "helper")
    check(G.confs(G.edges_from(helper, name="save")) == [("Repo.save", "ambiguous")], "py: x.save (unknown receiver) is an ambiguous lead")

    ud = G.node("go/store/embed.go", "UseDerived")
    check(G.confs(G.edges_from(ud, name="Ping")) == [("Base.Ping", "typed")], f"go: method through embedded struct typed {G.confs(G.edges_from(ud, name='Ping'))}")

    # --- TypeScript path alias (nearest tsconfig.json, JSONC)
    co = G.node("ts/src/checkout.ts", "checkout")
    check(("Orchestrator.process", "typed") in G.confs(G.edges_from(co)), f"ts: @pay/payment alias via tsconfig extends chain -> Orchestrator typed {G.confs(G.edges_from(co))}")
    imp = [G.nodes[e["dst"]]["file"] for e in G.g["edges"] if e["type"] == "imports" and e["src"] == "ts/src/checkout.ts"]
    check(imp == ["ts/src/payment.ts"], f"ts: alias import edge to payment.ts {imp}")

    io_edges = [(G.nodes[e["dst"]]["id"]) for e in G.g["edges"] if e["type"] == "imports" and e["src"] == "python/app/consumer.py" and G.nodes[e["dst"]].get("name") in ("io", "other/io/__init__.py")]
    check(io_edges == ["external:io"], f"py: stdlib `import io` stays external despite a repo package named io {io_edges}")

    fr = G.node("python/app/service.py", "Factory.run")
    check(G.confs(G.edges_from(fr, name="save")) == [("Repo.save", "typed")] and len(G.edges_from(fr, name="save")) == 3,
          f"py: locals and call-segment receivers typed from return annotations {G.confs(G.edges_from(fr, name='save'))}")

    # --- Python: bindings, re-exports, shadowing, builtins, leads (consumer.py)
    fm = G.node("python/app/consumer.py", "Frame.merge")
    check(G.confs(G.edges_from(fm)) == [("merge", "import")] and all(G.nodes[e["dst"]]["file"].endswith("pkg/impl.py") for e in G.edges_from(fm)),
          f"py: shadowing local import wins over the same-named method {G.confs(G.edges_from(fm))}")
    use = G.node("python/app/consumer.py", "Frame.use")
    c = G.confs(G.edges_from(use))
    conv = [(G.nodes[e["dst"]]["file"], e["confidence"]) for e in G.edges_from(use, name="convert")]
    check(conv == [("python/pkg/ops.py", "import")], f"py: ops.convert -> pkg/ops.py import (module binding, not ambiguous) {conv}")
    acts = G.confs(G.edges_from(use, name="act"))
    check(acts == [("Thing.act", "typed")] and len(G.edges_from(use, name="act")) == 2, f"py: pkg.Thing().act and Thing().act typed via re-export {acts}")
    thing = [(G.nodes[e["dst"]]["file"], e["confidence"]) for e in G.edges_from(use, name="Thing")]
    check(thing and all(f.endswith("pkg/impl.py") and cf == "import" for f, cf in thing), f"py: Thing() constructor resolved through pkg/__init__ re-export {thing}")
    check(not G.edges_from(use, name="len"), "py: builtin len() not linked to Sized.len")
    check(not G.edges_from(use, name="lonely"), "py: bare call to a never-imported function is not linked (no `unique` guess)")
    check(not G.edges_from(use, name="render"), "py: unknown receiver with 4 candidates -> no lead edges")
    check(not G.edges_from(use, name="charge"), "py: unknown receiver whose only candidate is TypeScript -> no cross-language lead")
    cont = G.node("python/app/consumer.py", "Container")
    check(not G.edges_from(cont, "extends"), f"py: collections_abc.Iterable (external alias) not linked to repo Iterable {G.confs(G.edges_from(cont, 'extends'))}")
    mt = G.node("python/app/consumer.py", "MyTest.test_it")
    hel = [(G.nodes[e["dst"]]["file"], e["confidence"]) for e in G.edges_from(mt, name="helper")]
    check(hel == [("python/tests_support/base_a.py", "typed")], f"py: self.helper() typed to base_a.TestCase only (same-named base_b ignored) {hel}")
    ot = G.node("python/app/consumer.py", "OtherTest.test_it")
    check(all(e["confidence"] != "typed" for e in G.edges_from(ot, name="helper")), f"py: unresolvable base -> helper never typed {G.confs(G.edges_from(ot, name='helper'))}")
    mytest = G.node("python/app/consumer.py", "MyTest")
    ext = [(G.nodes[e["dst"]]["file"], e["confidence"]) for e in G.edges_from(mytest, "extends")]
    check(ext == [("python/tests_support/base_a.py", "import")], f"py: class MyTest(base_a.TestCase) extends the bound module's class {ext}")

    inner_go = [n for n in G.g["nodes"] if n["file"].endswith("python/app/nested.py") and n["qname"].endswith("Inner.go")]
    check(len(inner_go) == 2, f"py: two nested Inner.go methods present ({len(inner_go)})")
    saves = sorted((n["qname"].split(".")[1], G.nodes[e["dst"]]["qname"], e["confidence"]) for n in inner_go for e in G.edges_from(n, name="save"))
    # make_a's Inner has a Repo field -> typed; make_b's Inner has a User field (no save) -> at most an ambiguous lead
    check(saves == [("make_a", "Repo.save", "typed"), ("make_b", "Repo.save", "ambiguous")], f"py: same-named nested classes are not merged {saves}")

    ov = G.node("python/app/overloads.py", "caller")
    tgt = [(G.nodes[e["dst"]]["line"], e["confidence"]) for e in G.edges_from(ov, name="read")]
    check(tgt == [(8, "same_file")], f"py: call attaches to the implementation, not the first @overload stub {tgt}")
    fr = json.loads(run("query", "--graph", os.path.join(root, ".ast-graph", "graph.db"), "find", "read", "--lang", "python", "--kind", "function", "--json"))
    at = run("query", "--graph", os.path.join(root, ".ast-graph", "graph.db"), "symbol", "overloads.py:read@8")
    check("overloads.py:8" in at.splitlines()[0], f"`file:name@line` query form selects that definition: {at.splitlines()[0][:80]}")
    check(fr and fr[0]["line"] == 8, f"find lists the implementation before its @overload stubs (first at line {fr[0]['line'] if fr else None})")
    ctor = run("query", "--graph", os.path.join(root, ".ast-graph", "graph.db"), "trace-deps", "service.py:Service.__init__", "--depth", "1")
    check("is a constructor" in ctor and "python/tests/test_service.py" in ctor, "trace-deps on __init__ includes the class's instantiations")
    pth = run("query", "--graph", os.path.join(root, ".ast-graph", "graph.db"), "path", "PaymentOrchestratorTest", "PaymentGatewayClient")
    check("--calls (typed)--> PaymentGatewayClient.validate" in pth, f"path: text rows end at the target's member: {pth.strip().splitlines()[-1][:80] if pth.strip() else pth!r}")
    pj = json.loads(run("query", "--graph", os.path.join(root, ".ast-graph", "graph.db"), "path", "PaymentOrchestratorTest", "PaymentGatewayClient", "--json"))
    check(pj["found"] and len(pj["hops"]) == len(pth.strip().splitlines()) and pj["hops"][-1]["dst"].endswith("PaymentGatewayClient.validate@4")
          and all(set(h) == {"src", "type", "confidence", "dst", "file", "line"} for h in pj["hops"]),
          f"path --json: one hop per text row with src/type/confidence/dst/file/line ({len(pj['hops'])} hops)")
    pn = json.loads(run("query", "--graph", os.path.join(root, ".ast-graph", "graph.db"), "path", "PaymentGatewayClient", "PaymentOrchestratorTest", "--json"))
    check(pn["found"] is False and pn["hops"] == [], "path --json: no path -> found=false, empty hops")
    pal = G.node("enums.py", "Palette")
    pc = {(G.nodes[e["dst"]]["qname"], e["confidence"]) for e in G.g["edges"] if e["src"].startswith(pal["id"].split("@")[0]) or G.nodes[e["src"]].get("parent") == pal["id"]}
    check(("Color.label", "typed") in pc, f"py: Optional[\"Color\"] field and an enum member both type Color.label {sorted(pc)}")
    pc_calls = [(G.nodes[e["src"]]["qname"], G.nodes[e["dst"]]["qname"], e["confidence"]) for e in G.g["edges"] if e["type"] == "calls" and G.nodes[e["src"]]["qname"] in ("Palette.pick", "Palette.name", "Palette.opts")]
    check(("Palette.pick", "Color.label", "typed") in pc_calls and ("Palette.name", "Color.label", "typed") in pc_calls, f"py: enum member and Optional field calls typed {pc_calls}")
    check(not any(s_ == "Palette.opts" and d_.endswith(".pop") for s_, d_, _ in pc_calls), f"py: **kwargs.pop() leads nowhere {pc_calls}")
    tpal = G.node("palette_user.py", "ThemedPalette")
    ext = [(G.nodes[e["dst"]]["qname"], e["confidence"]) for e in G.g["edges"] if e["src"] == tpal["id"] and e["type"] == "extends"]
    check(ext == [("Palette", "import")], f"py: `from .enums import Palette as BasePalette` resolves the base class {ext}")
    use = G.confs(G.edges_from(G.node("palette_user.py", "use")))
    check(("Palette.name", "typed") in use, f"py: `p = en.Palette()` keeps the module qualifier and types p {use}")
    tpk = G.confs(G.edges_from(G.node("test_palette.py", "test_pick")))
    check(("Palette.pick", "typed") in tpk, f"py: untyped pytest fixture parameter typed from the fixture's return annotation {tpk}")
    for meth, want in (("Board.labels", "Color.label"), ("Board.comp", "Color.label"), ("Board.named", "Color.label"), ("Board.first", "Palette.pick"), ("use_with", "Board.first"), ("Board.first_label", "Color.label"), ("Board.named_swatch", "Color.label")):
        bc = G.confs(G.edges_from(G.node("loops.py", meth)))
        check((want, "typed") in bc, f"py: {meth} -> {want} typed (loop/comprehension/dict.items/walrus/branch field/with-as) {bc}")
    bn = G.confs(G.edges_from(G.node("loops.py", "Board.named")))
    check(not any(q.endswith(".join") for q, _ in bn), f"py: a string-literal receiver never leads into a repo `join` {bn}")
    check(astgraph.is_test_file("core/testing/src/main/kotlin/x/TopicsTestData.kt") is False and astgraph.is_test_file("core/data/src/test/kotlin/x/RepoTest.kt") and astgraph.is_test_file("feature/x/src/androidTest/kotlin/x/ScreenTest.kt"),
          "jvm: src/main under a `testing` module is production; src/test and src/androidTest are tests")

    # --- JavaScript
    jrun = G.node("js/src/service.js", "Service.run")
    c = G.confs(G.edges_from(jrun))
    check(("Repo.save", "typed") in c, f"js: repo.save typed {c}")
    check(("load", "import") in c, "js: load() import")
    check(("fmt", "import") in c, "js: util.fmt() via namespace alias -> import")
    check(not any(q == "readFile" for q, _ in c), "js: fs.readFile (external require) not linked to util.readFile")
    check(("fmt2", "import") in c, f"js: @fx/util workspace package (exports ./dist -> src/index.ts) resolves {c}")
    jimp = sorted(G.nodes[e["dst"]]["id"] for e in G.g["edges"] if e["type"] == "imports" and e["src"] == "js/src/service.js")
    check("js/packages/util/src/index.ts" in jimp and not any(x in ("external:.", "external:@fx/util") for x in jimp), f"js: asset imports produce no external node; workspace import edge present {jimp}")
    check(G.g["stats"].get("asset_imports", 0) == 2, f"js: two asset imports counted ({G.g['stats'].get('asset_imports')})")
    loose = G.node("js/src/service.js", "loose")
    check(G.confs(G.edges_from(loose)) == [("Repo.save", "ambiguous")], "js: x.save unknown receiver -> ambiguous")

    ub = G.node("js/src/barrel_user.js", "useBarrel")
    c = G.confs(G.edges_from(ub))
    check(("Repo.save", "typed") in c and ("fmt", "import") in c and ("load", "import") in c, f"js: barrel `export ... from` re-exports (named, wildcard, aliased) are followed {c}")

    # --- TypeScript
    proc = G.node("ts/src/payment.ts", "Orchestrator.process")
    check(G.confs(G.edges_from(proc)) == [("StripeGateway.charge", "typed")], f"ts: this.gw.charge typed {G.confs(G.edges_from(proc))}")

    # --- Java
    pt = G.node("PaymentOrchestrator.java", "PaymentOrchestrator.processTransaction")
    c = G.confs(G.edges_from(pt))
    check(("PaymentGatewayClient.validate", "typed") in c, f"java: validate typed {c}")
    vl = sorted(e["line"] for e in G.edges_from(pt, name="validate") if e["confidence"] == "typed")
    check(len(vl) == 3, f"java: getGateway().validate and `var g` typed from the return type {vl}")
    check(("AuditLogger.log", "typed") in c, "java: log typed")
    hl = sorted((G.nodes[e["dst"]]["line"], e["line"], e["confidence"]) for e in G.edges_from(pt, name="helper"))
    typed_hl = {(d, l) for d, l, c in hl if c == "typed"}
    check(typed_hl == {(4, 14), (4, 15), (5, 16), (6, 17), (7, 18), (8, 19), (7, 20), (7, 21), (7, 22)},
          f"java: overloads chosen by arity, argument type, call return type and field type {sorted(typed_hl)}")
    amb_hl = sorted((d, l) for d, l, c in hl if c == "ambiguous")
    check(amb_hl and all(l == 23 for d, l in amb_hl) and len(amb_hl) >= 2, f"java: undecidable same-arity overloads become ambiguous leads, not a typed guess {amb_hl}")
    va = G.node("PaymentOrchestrator.java", "PaymentOrchestrator.varargs")
    check([(G.nodes[e["dst"]]["line"], e["confidence"]) for e in G.edges_from(va, name="helper")] == [(8, "typed")], f"java: varargs parameter typed as an array -> helper(Object[]) {[(G.nodes[e['dst']]['line'], e['confidence']) for e in G.edges_from(va, name='helper')]}")
    for cls in ("Square", "Circle"):
        node = G.node(f"{cls}.java", cls)
        imp = [(G.nodes[e["dst"]]["file"], G.nodes[e["dst"]]["qname"]) for e in G.edges_from(node, "implements")]
        check(imp == [("java/src/main/java/com/acme/Shape.java", "Shape")], f"java: {cls} implements the top-level Shape interface, not its nested Shape {imp}")
    check(not any(q.endswith(".add") for q, _ in c), "java: l.add (List, external) not linked to AuditLogger.add")

    # --- Rust
    rr = G.node("rust/src/lib.rs", "run")
    c = G.confs(G.edges_from(rr))
    check(("Store.get", "typed") in c and ("Store.new", "typed") in c, f"rust: typed calls from run {c}")
    check(sum(1 for q, _ in c if q.endswith("get")) == 1, "rust: m.get (HashMap) not linked to Store.get")
    check(("helper", "import") in c, f"rust: helper() from the workspace crate resolves with import confidence {c}")
    rimp = sorted(G.nodes[e["dst"]]["id"] for e in G.g["edges"] if e["type"] == "imports" and e["src"] == "rust/src/lib.rs")
    check("rust/crates/util/src/lib.rs" in rimp and "rust/src/store.rs" in rimp and not any(x.startswith("external:crate") for x in rimp), f"rust: crate:: and workspace crate imports resolve {rimp}")
    ovr = json.loads(run("query", "--graph", os.path.join(root, ".ast-graph", "graph.db"), "overview", "--json", "--no-tests", "--lang", "rust"))
    rscore = {G.nodes[i]["qname"]: c for i, c in ovr["hub_symbols"]}
    ovr_all = json.loads(run("query", "--graph", os.path.join(root, ".ast-graph", "graph.db"), "overview", "--json", "--lang", "rust"))
    rscore_all = {G.nodes[i]["qname"]: c for i, c in ovr_all["hub_symbols"]}
    check(rscore.get("Store", 0) < rscore_all.get("Store", 0) and not any(q.startswith("impl") for q in rscore), f"rust: --no-tests drops inline `mod tests` usage and impl blocks roll up to Store ({rscore_all.get('Store')} -> {rscore.get('Store')})")

    hg = G.node("rust/src/sinks.rs", "Holder.go")
    check(G.confs(G.edges_from(hg)) == [("Sinker.emit", "typed")], f"rust: generic-bounded field resolves to the trait method, not the same-file fn {G.confs(G.edges_from(hg))}")
    ub2 = G.node("rust/src/sinks.rs", "use_built")
    gets = sorted((G.nodes[e["dst"]]["qname"], e["confidence"]) for e in G.edges_from(ub2, name="get"))
    check(gets == [("Store.get", "typed"), ("Store.get", "typed")], f"rust: `.unwrap()` and `?` results typed from Result<Store,_> {gets}")
    tp = G.node("ts/src/checkout.ts", "pay")
    check(G.confs(G.edges_from(tp, name="charge")) == [("StripeGateway.charge", "typed")] and len(G.edges_from(tp, name="charge")) == 2,
          f"ts: local and call-segment receivers typed from a function's return type {G.confs(G.edges_from(tp, name='charge'))}")

    # --- Kotlin
    kp = G.node("OrderService.kt", "OrderService.place")
    kc = G.confs(G.edges_from(kp))
    for want in (("OrderRepo.save", "typed"), ("Order.Companion.create", "typed"), ("Sq.double", "typed"), ("Sq.area", "typed"),
                 ("Base.describe", "typed"), ("Registry.lookup", "typed")):
        check(want in kc, f"kotlin: {want[0]} {want[1]} {kc if want not in kc else ''}")
    fm = sorted((G.nodes[e["dst"]]["line"], e["line"]) for e in G.edges_from(kp, name="fmt") if e["confidence"] != "ambiguous")
    check(fm == [(12, 24), (13, 25)], f"kotlin: fmt overloads chosen by literal type (def line, call line) {fm}")
    check(not G.edges_from(kp, name="println"), "kotlin: builtin println not linked")
    check(not G.edges_from(kp, name="first"), "kotlin: List.first() (external) not linked")
    area_lines = sorted(e["line"] for e in G.edges_from(kp, name="area"))
    check(area_lines == [23, 29, 49], f"kotlin: field chain, safe-call chain and cast+elvis local all reach Sq.area {area_lines}")
    chain_lines = sorted((e["line"], e.get("name")) for e in G.edges_from(kp) if e["line"] == 33 and e["confidence"] != "ambiguous")
    check(chain_lines == [(33, "OrderRepo"), (33, "double"), (33, "save")], f"kotlin: inner calls of a navigation chain are recorded {chain_lines}")
    saves = sorted((e["line"], e["confidence"]) for e in G.edges_from(kp, name="save"))
    check((37, "typed") in saves, f"kotlin: class property `val cached = makeRepo()` typed from the return type {saves}")
    check(("Sq.plus2", "typed") in kc, f"kotlin: infix call `a plus2 b` typed {kc}")
    check(not [e for e in G.edges_from(kp) if e.get("name") in ("let", "forEach")], "kotlin: stdlib scope/collection functions produce no lead edges")
    sq_card = run("query", "--graph", os.path.join(root, ".ast-graph", "graph.db"), "symbol", "Order.kt:Sq")
    check("double" in sq_card and "[extension, " in sq_card, "kotlin: extension functions appear on the receiver's card (tagged with their file)")
    lk = sorted(e["line"] for e in G.edges_from(kp, name="lookup") if e["confidence"] == "typed")
    check(39 in lk, f"kotlin: `val reg = Registry` local typed as the object {lk}")
    tk = sorted((e.get("name"), G.nodes[e["dst"]]["line"], e["confidence"]) for e in G.edges_from(kp) if e.get("name") in ("take", "take2"))
    check(tk == [("take", 16, "import"), ("take2", 18, "import")], f"kotlin: subtype-aware overloads: Shape beats Any and a generic T {tk}")
    ta = G.node("OrderServiceTest.kt", "OrderServiceTest.testAnonymousObject")
    tac = G.confs(G.edges_from(ta))
    check(("Base.describe", "typed") in tac and ("Sq.area", "typed") in tac, f"kotlin: anonymous `object : Base(...) {{}}` local is typed and its members resolve {tac}")
    ka = G.node("OrderService.kt", "OrderService.area")
    check(G.confs(G.edges_from(ka)) == [("Shape.area", "typed")], f"kotlin: generic-bounded constructor property resolves through Shape {G.confs(G.edges_from(ka))}")
    kt = G.node("OrderServiceTest.kt", "OrderServiceTest.testPlace")
    check(("OrderService.place", "typed") in G.confs(G.edges_from(kt)), f"kotlin: src/test reaches src/main by declared package {G.confs(G.edges_from(kt))}")
    os_node = G.node("OrderService.kt", "OrderService")
    ext = [(G.nodes[e["dst"]]["file"].split("/")[-1], G.nodes[e["dst"]]["qname"], e["confidence"]) for e in G.edges_from(os_node, "extends")]
    check(ext == [("Base.kt", "Base", "import")], f"kotlin: `: Base<Sq>(...)` extends the imported class {ext}")
    imp_edges = [(G.nodes[e["dst"]]["file"].split("/")[-1]) for e in G.g["edges"] if e["type"] == "imports" and e["src"].endswith("OrderService.kt")]
    check("Base.kt" in imp_edges and imp_edges.count("Base.kt") >= 2, f"kotlin: imports of a class, a function and a wildcard all resolve to Base.kt {imp_edges}")
    check(astgraph.is_test_file("kotlin/src/main/kotlin/x/FooTest.kt") and not astgraph.is_test_file("kotlin/src/main/kotlin/x/Latest.kt"), "kotlin: *Test.kt is a test file, Latest.kt is not")
    fields = sorted(n["name"] for n in G.g["nodes"] if n["kind"] == "field" and n["file"].endswith("Order.kt"))
    check(fields == ["LARGE", "SMALL", "all", "cached", "h", "id", "id", "items", "lazyOrder", "runner", "side", "sq", "svc", "svc"], f"kotlin: primary-constructor val/var parameters, class properties and enum entries are fields {fields}")
    card = run("query", "--graph", os.path.join(root, ".ast-graph", "graph.db"), "symbol", "Order.kt:Order")
    check("create" in card and "Members" in card, "kotlin: class card lists companion members")
    dt = G.confs(G.edges_from(G.node("Order.kt", "Sq.describeTwice")))
    check(("Sq.double", "typed") in dt, f"kotlin: `this`/`this@label` inside an extension function is the receiver type {dt}")
    kc = G.confs(G.edges_from(kp))
    check(("FunctionProvider.charLength", "typed") in kc, f"kotlin: imported top-level `val currentDialect: Dialect` types a property chain {kc}")
    cd = G.node("Base.kt", "currentDialect")
    check(cd["kind"] == "variable" and cd["extra"].get("type") == "Dialect", f"kotlin: top-level property is a typed variable node {cd['kind']} {cd['extra']}")
    oc = run("query", "--graph", os.path.join(root, ".ast-graph", "graph.db"), "symbol", "Base.kt:FunctionProvider.charLength")
    check("Overridden by (1): SqliteProvider.charLength" in oc, f"kotlin: method card lists overriding methods: {[l for l in oc.splitlines() if 'Overrid' in l]}")
    ja = run("query", "--graph", os.path.join(root, ".ast-graph", "graph.db"), "symbol", "Shape.java:Shape.area")
    check("Overridden by" in ja and "Circle.area" in ja and "Square.area" in ja, f"java: interface method card lists implementers' methods: {[l for l in ja.splitlines() if 'Overrid' in l]}")
    jc = run("query", "--graph", os.path.join(root, ".ast-graph", "graph.db"), "symbol", "Circle.java:Circle.area")
    check("Overrides: Shape.area" in jc, f"java: implementing method card names the interface method: {[l for l in jc.splitlines() if 'Overrid' in l]}")
    kc2 = [(G.nodes[e["dst"]]["qname"], e["line"], e["confidence"]) for e in G.edges_from(kp)]
    for want in ("Cart.grow", "Kind.code", "Order.Builder.build" if False else "Cart.Builder.build", "Cart.plus"):
        hits = [(l, c) for q, l, c in kc2 if q == want]
        check(any(c == "typed" for _, c in hits), f"kotlin: {want} typed ({hits})")
    check(sum(1 for q, l, c in kc2 if q == "Cart.grow" and c == "typed") >= 2, f"kotlin: DSL receiver lambda and `it` of also both reach Cart.grow {[x for x in kc2 if x[0] == 'Cart.grow']}")
    check(("Sq.area", kc2 and max(l for _, l, _ in kc2), "typed") in kc2 or any(q == "Sq.area" and c == "typed" and l > 45 for q, l, c in kc2), f"kotlin: `(o as Order) ?: o` initializer types the local {[x for x in kc2 if x[0] == 'Sq.area']}")
    fa = G.confs(G.edges_from(G.node("Order.kt", "Cart.firstArea")))
    check(("Sq.area", "typed") in fa, f"kotlin: `val first = items[0]` on a MutableList<Sq> property types the local {fa}")
    ct = G.confs(G.edges_from(G.node("Order.kt", "Cart.total")))
    check(("Sq.area", "typed") in ct, f"kotlin: `it` in sumOf on a MutableList<Sq> literal is Sq {ct}")
    an = G.confs(G.edges_from(G.node("Order.kt", "Cart.Audit.note")))
    check(("Cart.total", "typed") in an, f"kotlin: inner class calls the outer member {an}")
    kn = G.node("Order.kt", "Kind.SMALL")
    check(kn["kind"] == "field" and kn["extra"].get("type") == "Kind", f"kotlin: enum entries are fields typed as the enum {kn['extra']}")
    rc = G.confs(G.edges_from(G.node("OrderService.kt", "runChain")))
    check(("Provider.Chain.proceed", "typed") in rc, f"kotlin: parameter typed `Provider.Chain` (Provider imported) resolves member calls {rc}")
    mc = [(G.nodes[e["dst"]]["qname"], e["confidence"]) for e in G.g["edges"] if e["src"] == G.node("OrderService.kt", "MyChain")["id"] and e["type"] == "implements"]
    check(mc == [("Provider.Chain", "import")], f"kotlin: `: Provider.Chain` implements the nested interface of an imported type {mc}")
    bc = run("query", "--graph", os.path.join(root, ".ast-graph", "graph.db"), "symbol", "Order.kt:Cart.Builder.kind")
    check("fun kind(k: Kind): Builder" in bc, f"kotlin: `= apply {{ }}` setter gets the receiver as return type: {bc.splitlines()[0]}")
    wr = G.node("Order.kt", "Widget.react")
    rc2 = [(G.nodes[e["dst"]]["qname"], e["line"], e["confidence"]) for e in G.edges_from(wr)]
    check(sum(1 for q, _, c in rc2 if q == "Ev.Click.describe" and c == "typed") == 2, f"kotlin: smart casts in `if (e is T)` and `when (e) {{ is T -> }}` type e {rc2}")
    wcls = G.node("Order.kt", "Widget")
    wc = [(G.nodes[e["dst"]]["qname"], e["confidence"]) for e in G.g["edges"] if e["src"] == wcls["id"] and e["type"] == "calls"]
    check(("Handler", "same_file") in wc and ("Widget.react", "typed") in wc and ("OrderService.place", "typed") in wc,
          f"kotlin: SAM constructor, lambda -> outer member, and a property initialiser through a plain constructor parameter {wc}")
    lz = G.node("Order.kt", "Widget.lazyOrder")
    check(lz["extra"].get("type") == "Order", f"kotlin: `by lazy {{ Order(..) }}` types the property {lz['extra']}")
    sz = G.confs(G.edges_from(G.node("Order.kt", "Widget.sizes")))
    check(("Sq.describeTwice", "typed") in sz, f"kotlin: callable reference `Sq::describeTwice` is a typed call {sz}")
    ag = G.confs(G.edges_from(G.node("Order.kt", "Widget.again")))
    check(not any(q.endswith(".copy") for q, _ in ag), f"kotlin: synthetic `copy` produces no member edge {ag}")
    cd2 = G.confs(G.edges_from(G.node("Order.kt", "Widget.code")))
    check(("Kind.code", "typed") in cd2, f"kotlin: `Kind.valueOf(..).code()` typed through the enum {cd2}")
    go = G.confs(G.edges_from(G.node("Order.kt", "Consumer.go")))
    check(("Runner.invoke", "typed") in go, f"kotlin: `runner(\"1\")` on a typed property resolves `operator fun invoke` {go}")
    comp = G.g["files"]["kotlin/src/main/kotlin/com/acme/shop/Compose.kt"]
    check(not comp["file_node"]["extra"].get("has_errors") and any(n["name"] == "Row" for n in comp["nodes"]) and any(n["name"] == "Screen" for n in comp["nodes"]),
          "kotlin: `@Composable` on a function type no longer breaks the file (grammar workaround)")
    sc = G.confs(G.edges_from(G.node("Compose.kt", "Screen")))
    check(("Row", "same_file") in sc, f"kotlin: composable calling composable resolved {sc}")
    ft = run("query", "--graph", os.path.join(root, ".ast-graph", "graph.db"), "find", "helper", "--no-tests", "--json")
    check(all(not n["file"].split("/")[-1].startswith("test") and "/tests/" not in n["file"] for n in json.loads(ft)), "find --no-tests hides test-file symbols")

    # --- Terraform / Kubernetes
    types = {(e["type"], e["confidence"]) for e in G.g["edges"]}
    check(("uses_module", "exact") in types, "hcl: module call resolved")
    sg_refs = [r["name"] for r in G.g["files"]["infra/main.tf"]["refs"]]
    check(not any(r.startswith(("node_config.", "log_cfg.")) for r in sg_refs), f"hcl: dynamic-block iterators (label and `iterator =`) are not references {sg_refs}")
    mv = [n for n in G.g["nodes"] if n["kind"] == "moved"]
    mv_edges = [(G.nodes[e["dst"]]["qname"], e["type"], e["confidence"]) for e in G.g["edges"] if mv and e["src"] == mv[0]["id"]]
    check(len(mv) == 1 and mv[0]["signature"] == "moved aws_instance.old -> aws_instance.app" and mv_edges == [("aws_instance.app", "references", "exact")],
          f"hcl: `moved` block is a node referencing its `to` address {mv_edges}")
    td = run("query", "--graph", os.path.join(root, ".ast-graph", "graph.db"), "trace-deps", "infra/main.tf:aws_instance.app", "--depth", "1")
    check("moved.aws_instance.app" in td and "references -> aws_instance.app | exact" in td, f"hcl: blast radius of a resource lists the moved block: {[l for l in td.splitlines() if 'moved' in l]}")
    reg = [(G.nodes[e["dst"]]["id"], e["confidence"]) for e in G.edges_from(G.node("main.tf", "module.vpc_from_registry"), typ="uses_module")]
    check(("terraform_module:infra/modules/vpc", "ambiguous") in reg and any(c == "external" for _, c in reg),
          f"hcl: registry source with a local //subdir gets an ambiguous lead next to the external edge {reg}")
    src_lines = open(os.path.join(root, "infra/main.tf")).read().splitlines()
    vpc_line = next(i + 1 for i, l in enumerate(src_lines) if "module.vpc.vpc_id" in l and "reference on a later line" in l)
    sg = G.node("infra/main.tf", "aws_security_group.app")
    ref_lines = sorted(e["line"] for e in G.g["edges"] if e["src"] == sg["id"] and e["type"] == "references" and G.nodes[e["dst"]]["qname"] == "module.vpc")
    check(vpc_line in ref_lines, f"hcl: a reference inside a multi-line expression is recorded at its own line ({ref_lines}, expected {vpc_line})")
    tfq = run("query", "--graph", os.path.join(root, ".ast-graph", "graph.db"), "symbol", "main.tf:terraform")
    check(tfq.splitlines()[0].endswith("(main.tf:1)"), f"query: `file:name` prefers the exact root path over suffix matches: {tfq.splitlines()[0]}")
    ovh = run("query", "--graph", os.path.join(root, ".ast-graph", "graph.db"), "overview", "--lang", "hcl", "--top", "20")
    check('variable "cidr"' in ovh and "(+1 same-named copy in other directories)" in ovh, "overview: same-named symbols in several directories collapse to one row")
    st = json.loads(run("query", "--root", root, "stats"))
    check(st.get("files", 0) > 0, "query --root reads <root>/.ast-graph/graph.db")
    check(("selects", "exact") in types, "k8s: Service selects Deployment")
    svc = [n for n in G.g["nodes"] if n["kind"] == "k8s_object" and n["name"] == "Service/web"][0]
    check(any(G.nodes[e["dst"]]["name"] == "Deployment/web" for e in G.g["edges"] if e["src"] == svc["id"] and e["type"] == "selects"), "k8s: Service/web -> Deployment/web")

    # --- overview flags
    ovl = json.loads(run("query", "--graph", os.path.join(root, ".ast-graph", "graph.db"), "overview", "--json", "--lang", "go"))
    files_go = {G.nodes[i]["file"] for i, _ in ovl["hub_symbols"]}
    check(files_go and all(f.endswith(".go") for f in files_go), f"overview --lang go ranks only Go symbols {sorted(files_go)}")
    ovt = json.loads(run("query", "--graph", os.path.join(root, ".ast-graph", "graph.db"), "overview", "--json", "--no-tests", "--top", "80"))
    scored = {G.nodes[i]["qname"]: c for i, c in ovt["hub_symbols"]}
    check("PaymentRequest" in scored and scored["PaymentRequest"] < {G.nodes[i]["qname"]: c for i, c in json.loads(run("query", "--graph", os.path.join(root, ".ast-graph", "graph.db"), "overview", "--json", "--top", "80"))["hub_symbols"]}["PaymentRequest"],
          "overview --no-tests drops the usage coming from PaymentOrchestratorTest")
    fj = json.loads(run("query", "--graph", os.path.join(root, ".ast-graph", "graph.db"), "file", "python/pkg/ops.py", "--json"))
    check("python/app/consumer.py" in fj.get("imported_by", []) and any(n["name"] == "convert" for n in fj["nodes"]), f"query file --json lists nodes and importers {fj.get('imported_by')}")

    # --- hub-sized output modes
    gp = os.path.join(root, ".ast-graph", "graph.db")
    summ = run("query", "--graph", gp, "trace-deps", "PaymentRequest", "--summary")
    check("| Directory |" in summ and "Most connected dependents" in summ and "Files affected:" in summ and "| Dependent file |" not in summ, "trace-deps --summary: directory table, dependents, no per-edge rows")
    fo = run("query", "--graph", gp, "trace-deps", "PaymentRequest", "--files-only")
    check("hop 1 (" in fo and "PaymentGatewayClient.java" in fo and "|" not in fo.split("Files affected")[0], "trace-deps --files-only lists files by hop")
    small = run("query", "--graph", gp, "trace-deps", "PaymentRequest", "--max-rows", "1")
    check("exceed --max-rows 1" in small and "| Directory |" in small, "trace-deps degrades to summary above --max-rows")
    card = run("query", "--graph", gp, "symbol", "Repo.save" if False else "models.py:Repo.save", "--limit", "1")
    check("showing 1" in card and "more" in card, "symbol --limit caps the card and summarises the rest")
    full = run("query", "--graph", gp, "symbol", "models.py:Repo.save", "--all")
    check("showing" not in full, "symbol --all removes caps")
    for target in ("handler.go:Handler", "store.rs:Store", "payment.ts:Gateway", "PaymentOrchestrator.java:PaymentOrchestrator", "models.py:Repo"):
        p = subprocess.run([sys.executable, "-B", ENGINE, "query", "--graph", gp, "symbol", target], capture_output=True, text=True)
        check(p.returncode == 0 and "Members" in p.stdout and "Traceback" not in p.stderr, f"symbol card renders for container {target}")

    # --- callers/callees output modes, find ranking
    cs = run("query", "--graph", gp, "callers", "PaymentRequest", "--summary")
    check("| Directory |" in cs and "Most frequent callers" in cs and "Files:" in cs and "<-" not in cs, "callers --summary: directory table, frequent callers, no rows")
    cf = run("query", "--graph", gp, "callers", "PaymentRequest", "--files-only")
    check("hop 1 (" in cf and "PaymentGatewayClient.java" in cf, "callers --files-only lists files by hop")
    cm = run("query", "--graph", gp, "callers", "PaymentRequest", "--max-rows", "1")
    check("exceed --max-rows 1" in cm and "| Directory |" in cm, "callers degrades to summary above --max-rows")
    ce = run("query", "--graph", gp, "callees", "PaymentOrchestrator.processTransaction", "--summary")
    check("Most frequent callees" in ce, "callees --summary works")
    fs = run("query", "--graph", gp, "find", "Store", "--json")
    first = json.loads(fs)[0]
    check(first["name"] == "Store" and first["file"].endswith("rust/src/store.rs"), f"find ranks the exact-name match first ({first['qname']} in {first['file']})")
    fl = json.loads(run("query", "--graph", gp, "find", "Store", "--lang", "go", "--json"))
    check(fl and all(n["file"].endswith(".go") for n in fl), f"find --lang go filters to Go files {[n['file'] for n in fl][:3]}")

    # --- overview hub sanity: the mock must not outrank real code
    ov = json.loads(run("query", "--graph", os.path.join(root, ".ast-graph", "graph.db"), "overview", "--json", "--top", "500"))
    score = {G.nodes[i]["qname"]: c for i, c in ov["hub_symbols"]}
    # the mock's hub score is just its legitimate uses: one instantiation + one typed m.Get call
    check(score.get("ResourceDataMock", 0) == 2 and score.get("MemStore", 0) >= score.get("ResourceDataMock", 0),
          f"overview: ResourceDataMock scores only its real uses {score.get('ResourceDataMock')}, MemStore {score.get('MemStore')}")
    return G


def test_lean_queries(root):
    """Query output that replaces calls instead of adding them: `source`
    answers definition + callers + callees + body in one call, several names per call, hidden
    ambiguous edges are announced, test usages can be left out, and the graph is found from a
    subdirectory."""
    print("# lean queries: source, several names, ambiguous note, --no-tests, subdirectories")
    gp = os.path.join(root, ".ast-graph", "graph.db")
    src = run("query", "--graph", gp, "source", "models.py:Repo.save")
    first = src.splitlines()[0] if src else ""
    check(re.search(r"python/app/models\.py:\d+-\d+ \(\d+ lines\)", first), f"source: header gives file and exact line range: {first}")
    check("called by (" in src and "Service.run" in src, f"source: names the callers in the same call: {src.splitlines()[1:2]}")
    check("helper" in src and "?" in src, "source: an ambiguous caller is listed and marked ?")
    body = [ln for ln in src.splitlines() if re.match(r"^\s*\d+ ", ln)]
    check(body and "def save" in body[0], f"source: body lines carry their line numbers: {body[:1]}")
    cut = run("query", "--graph", gp, "source", "service.py:Service", "--max-lines", "2")
    check("members listed instead of the body" in cut and "[L" in cut, "source: a class over --max-lines prints its member outline with ranges")
    two = run("query", "--graph", gp, "source", "models.py:Repo.save", "service.py:helper", "--no-refs")
    check(two.count(" lines)") == 2 and "called by" not in two, "source: several names in one call; --no-refs drops the reference lines")
    p = subprocess.run([sys.executable, "-B", ENGINE, "query", "--graph", gp, "source", "python/app/models.py"], capture_output=True, text=True)
    check(p.returncode != 0 and "query file" in p.stderr, "source on a file points at `query file`")

    multi = run("query", "--graph", gp, "symbol", "models.py:Repo", "no_such_symbol_xyz")
    check("Members" in multi and "no_such_symbol_xyz: no symbol" in multi, "symbol: several names; a missing one is reported inline, the rest answered")
    card = run("query", "--graph", gp, "symbol", "models.py:Repo")
    check("calls:" not in card and "--calls" in card, "symbol: member call lists are off by default and the flag is named")
    check("calls:" in run("query", "--graph", gp, "symbol", "service.py:Service", "--calls"), "symbol --calls lists member calls")
    jo = json.loads(run("query", "--graph", gp, "symbol", "Shape.java:Shape.area", "--json"))
    check({o["qname"] for o in jo.get("overridden_by", [])} >= {"Circle.area", "Square.area"}, f"symbol --json carries overridden_by {jo.get('overridden_by')}")

    cr = run("query", "--graph", gp, "callers", "models.py:Repo.save")
    check("ambiguous edge(s) not shown" in cr and "helper" not in cr, f"callers: hidden ambiguous edges are counted, not silently dropped: {cr.splitlines()[-1]}")
    check("<- Service.run" in cr and "Repo.save <-" not in cr, "callers: compact rows start at the arrow (target is in the header)")
    check("helper" in run("query", "--graph", gp, "callers", "models.py:Repo.save", "--include-ambiguous"), "--include-ambiguous lists them")
    allc = run("query", "--graph", gp, "callers", "service.py:Service")
    noc = run("query", "--graph", gp, "callers", "service.py:Service", "--no-tests")
    check("python/tests/" in allc and "python/tests/" not in noc and "(no tests)" in noc, "callers --no-tests drops callers in test files")
    td = run("query", "--graph", gp, "trace-deps", "service.py:Service", "--no-tests", "--files-only")
    check("python/tests/" not in td and "test files excluded" in td, "trace-deps --no-tests drops dependents in test files")

    tf = run("query", "--graph", gp, "tests-for", "service.py:Service")
    check("python/tests/test_service.py" in tf and "test functions in" in tf, f"tests-for: groups the tests that reach a symbol by file: {tf.splitlines()[1:2]}")
    tn = run("query", "--graph", gp, "tests-for", "models.py:Repo.save", "--no-ambiguous")
    check("?" not in tn.split("\n", 1)[1], "tests-for --no-ambiguous lists only firm paths")
    big = run("query", "--graph", gp, "source", "service.py:Service", "service.py:helper", "models.py:Repo.save", "--max-lines", "1")
    check(len(re.findall(r"\.py:\d+-\d+ \(\d+ lines\)", big)) == 3, "source: several names share one call")
    loop = run("query", "--graph", gp, "source", "service.py:Factory.run", "--max-lines", "1")
    check("members listed instead" not in loop and "not shown" in loop, "source: a long function is cut with the remaining range, never outlined")

    sub = os.path.join(root, "python")
    s1 = run("query", "symbol", "models.py:Repo", cwd=sub)
    check("Members" in s1, "query from a subdirectory finds the graph above it")
    s2 = run("query", "file", "app/models.py", cwd=sub)
    check("class Repo" in s2, "a path relative to that subdirectory resolves to the repo path")


def test_py_value_typing(G):
    """Python fields and locals typed through `x or Ctor()` / `A() if c else B()`, and properties read
    as fields (Django's `self._query = query or sql.Query(model)` + `@property def query`)."""
    print("# python: `or` / conditional values and properties")
    run_ = G.node("lazy.py", "LazyHolder.run")
    saves = sorted((e["line"], e["confidence"]) for e in G.edges_from(run_, name="put"))
    check(saves == [(21, "typed"), (22, "typed"), (23, "typed"), (24, "typed"), (26, "typed")],
          f"py: property returning `_vault`, `vault or Vault()`, conditional, annotated property and an `or` local all type the receiver {saves}")
    prop = G.node("lazy.py", "LazyHolder.vault")
    check(prop["extra"].get("returns_field") == "_vault", f"py: unannotated property records the field it returns {prop['extra']}")


def test_tests_detection(root, G):
    print("# test-file detection")
    for p, want in [("go/handler/attestor.go", False), ("go/handler/handler_test.go", True),
                    ("python/app/latest.py", False), ("python/tests/test_service.py", True),
                    ("js/src/inspect.js", False), ("js/src/service.spec.js", True), ("ts/src/payment.test.ts", True),
                    ("java/src/main/java/com/acme/LatestAttestor.java", False),
                    ("java/src/test/java/com/acme/PaymentOrchestratorTest.java", True),
                    ("rust/tests/integration.rs", True), ("google/services/x/resource_x_connectivity_test_resource.go", False),
                    ("a/inspect_template.go", False), ("a/latest_version.go", False)]:
        check(astgraph.is_test_file(p) is want, f"is_test_file({p}) == {want}")
    out = run("query", "--graph", os.path.join(root, ".ast-graph", "graph.db"), "trace-deps", "PaymentRequest")
    line = next((l for l in out.splitlines() if l.startswith("Tests reached")), "")
    check("PaymentOrchestratorTest.java" in line and "LatestAttestor" not in line, f"trace-deps PaymentRequest tests: {line}")


def test_keep_dir_and_parse_errors(tmp):
    root = os.path.join(tmp, "fixture-keep")
    shutil.copytree(FIXTURE, root)
    os.makedirs(os.path.join(root, "build"))
    with open(os.path.join(root, "build", "extra.tf"), "w") as f:
        f.write('variable "from_build_dir" {\n  type = string\n}\n')
    build(root)
    check("build/extra.tf" not in load(root)["files"], "build/ is excluded by default")
    build(root, "--keep-dir", "build")
    check("build/extra.tf" in load(root)["files"], "--keep-dir build indexes the directory")
    broken = os.path.join(tmp, "broken.py")
    with open(broken, "w") as f:
        f.write("def ok():\n    return 1\ndef broken(:\n    pass\n")
    head = run("skeleton", broken).splitlines()[0]
    check("parse errors (first at L3)" in head, f"skeleton reports the first parse-error line: {head}")


def test_cpp_extraction(root):
    """C/C++ phase 1: structure, namespaces, inheritance, includes, calls.

    The load-bearing assertion is the last one: an out-of-line `MatMulOp::Compute` defined in
    the .cc must carry the SAME qname as its declaration in the .h. Without that the two are
    separate symbols and every caller links to whichever the linker happened to see first.
    """
    print("# C/C++ extraction")
    hdr = run("skeleton", os.path.join(root, "cpp", "kernel.h"), "--json")
    doc = json.loads(hdr)[0]
    q = {n["qname"]: n for n in doc["nodes"]}
    for want, kind in [("demo", "module"), ("demo.ops", "module"),
                       ("demo.ops.MatMulOp", "class"),
                       ("demo.ops.MatMulOp.MatMulOp", "constructor"),
                       ("demo.ops.MatMulOp.Compute", "method"),
                       ("demo.ops.MatMulOp.rank_", "field"),
                       ("demo.ops.Point", "struct"), ("demo.ops.Point.x", "field"),
                       ("demo.ops.Mode", "enum"), ("demo.ops.Mode.FAST", "field")]:
        check(want in q and q[want]["kind"] == kind,
              f"header: {kind} {want}" + ("" if want in q else " MISSING"))
    check(any(n["qname"] == "demo.ops.MatMulOp.~MatMulOp" and n["kind"] == "destructor"
              for n in doc["nodes"]), "header: destructor recognised")
    refs = {(r["kind"], r["name"]) for r in doc["refs"]}
    check(("extends", "OpKernel") in refs, "header: base class -> extends ref")
    check(("import", "vector") in refs and ("import", "fixture/base.h") in refs,
          "header: both #include forms become imports")

    impl = json.loads(run("skeleton", os.path.join(root, "cpp", "kernel.cc"), "--json"))[0]
    iq = {n["qname"]: n["kind"] for n in impl["nodes"]}
    check(iq.get("demo.ops.MatMulOp.Compute") == "method",
          f"impl: out-of-line definition keeps the class qname (got {iq.get('demo.ops.MatMulOp.Compute')})")
    check(iq.get("demo.ops.MatMulOp.MatMulOp") == "constructor",
          "impl: out-of-line constructor is a constructor, not a method")
    check(iq.get("demo.ops.MatMulOp.~MatMulOp") == "destructor",
          "impl: out-of-line destructor is a destructor")
    irefs = {(r["kind"], r.get("hint"), r["name"]) for r in impl["refs"]}
    check(("call", "ctx", "input") in irefs, "impl: ptr->method() call with receiver hint")
    check(("call", "errors", "InvalidArgument") in irefs, "impl: ns::fn() call with namespace hint")
    check(("instantiates", None, "Point") in irefs, "impl: `new Point()` -> instantiates")
    check(q["demo.ops.MatMulOp.Compute"]["qname"] == "demo.ops.MatMulOp.Compute"
          and "demo.ops.MatMulOp.Compute" in iq,
          "header declaration and .cc definition agree on the qname")

    G = Graph(load(root))
    # Phase 2: the .h declaration and the .cc definition are paired.
    decl = [n for n in G.g["nodes"] if n["file"].endswith("cpp/kernel.h")
            and n["qname"] == "demo.ops.MatMulOp.Compute"]
    defn = [n for n in G.g["nodes"] if n["file"].endswith("cpp/kernel.cc")
            and n["qname"] == "demo.ops.MatMulOp.Compute"]
    check(len(decl) == 1 and len(defn) == 1, "one declaration in the .h and one definition in the .cc")
    if decl and defn:
        check(decl[0]["extra"].get("is_declaration") is True, "the header node is marked a declaration")
        check(defn[0]["extra"].get("has_body") is True, "the .cc node carries the body")
        pairs = [e for e in G.g["edges"] if e["type"] == "defines"
                 and e["src"] == defn[0]["id"] and e["dst"] == decl[0]["id"]]
        check(len(pairs) == 1, f"a `defines` edge pairs the body to its prototype (got {len(pairs)})")
        check(decl[0]["extra"].get("defined_at", "").endswith("kernel.cc:10"),
              f"the declaration records where it is defined ({decl[0]['extra'].get('defined_at')})")
    # `#include "kernel.h"` is a real file->file edge, not a dangling name.
    inc = [e for e in G.g["edges"] if e["type"] == "imports"
           and e["src"].endswith("cpp/kernel.cc") and e["dst"].endswith("cpp/kernel.h")]
    check(len(inc) == 1, f'#include "kernel.h" resolves to the header file node (got {len(inc)})')
    # A system include must stay external -- never matched to a repo file that happens to share a name.
    sysinc = [e for e in G.g["edges"] if e["type"] == "imports" and e["src"].endswith("cpp/kernel.h")
              and not str(e["dst"]).startswith("external:")]
    check(all(G.nodes[e["dst"]]["file"].endswith(".h") for e in sysinc if e["dst"] in G.nodes),
          "<vector> does not resolve to a repo file")

    c = json.loads(run("skeleton", os.path.join(root, "cpp", "plain.c"), "--json"))[0]
    cq = {n["qname"]: n["kind"] for n in c["nodes"]}
    check(cq.get("Buffer") == "struct" and cq.get("Buffer.size") == "field", "plain C: struct and field")
    check(cq.get("buffer_len") == "function" and cq.get("main") == "function", "plain C: functions")


def test_cross_language_bridge(root):
    """Phase 3: Python <-> C++ across pybind11 and TensorFlow-style op registration.

    This is the gap the TensorFlow audit found: a Python symbol whose implementation is a C++
    kernel had its dependency chain cut at the language boundary, so blast radius reported no
    dependents for something that has many. A confidently incomplete answer is worse than an
    obviously missing one, which is why these edges carry their own `binding` confidence.
    """
    print("# cross-language bridge (pybind11 / REGISTER_OP)")
    G = Graph(load(root))

    mod = [n for n in G.g["nodes"] if n["kind"] == "py_module"]
    check(len(mod) == 1 and mod[0]["name"] == "_pywrap_demo",
          f"PYBIND11_MODULE declares the extension module ({[m['name'] for m in mod]})")
    binds = {n["name"]: n for n in G.g["nodes"] if n["kind"] == "py_binding"}
    check("DemoExecute" in binds and "DemoOther" in binds,
          f"m.def(...) exports are recorded ({sorted(binds)})")

    # The load-bearing edge: Python -> C++.
    caller = G.node("python/bridge/caller.py", "run_it")
    bridged = [e for e in G.edges_from(caller, name="DemoExecute")]
    check(len(bridged) == 1 and G.nodes[bridged[0]["dst"]]["file"].endswith("cpp/bindings.cc"),
          f"a Python call reaches the C++ binding {[(G.nodes[e['dst']]['file'], e['confidence']) for e in bridged]}")
    check(bridged and bridged[0]["confidence"] == "binding",
          "the cross-language edge is labelled `binding`, not passed off as a normal call")

    # And back again: this is what blast radius needs.
    dependents = [e for e in G.edges_to(binds["DemoExecute"]) if e["type"] == "calls"]
    check(any(G.nodes[e["src"]]["file"].endswith("caller.py") for e in dependents),
          "blast radius from the C++ binding reaches its Python dependents")

    # Never claim a name Python defines itself.
    local = G.node("python/bridge/caller.py", "not_a_binding")
    check(not [e for e in G.edges_from(local) if G.nodes[e["dst"]]["kind"] == "py_binding"],
          "a call with a Python definition in scope is not diverted to a binding")

    # The hop that makes the chain useful: the binding must reach the C++ body it exports,
    # otherwise a Python caller stops at the binding and blast radius still understates.
    impl_edges = [e for e in G.edges_from(binds["DemoExecute"], name="RealCompute")]
    check(len(impl_edges) == 1 and G.nodes[impl_edges[0]["dst"]]["name"] == "RealCompute",
          f"a binding reaches the C++ function it exports {[G.nodes[e['dst']]['qname'] for e in impl_edges]}")
    ptr_edges = [e for e in G.edges_from(binds["DemoOther"], name="RealCompute")]
    check(len(ptr_edges) == 1, "`m.def(\"X\", &Y)` links the binding to Y as well as a lambda body")
    real = G.node("cpp/bindings.cc", "RealCompute")
    py_deps = [e for e in G.edges_to(real)]
    check(any(G.nodes[e["src"]]["kind"] == "py_binding" for e in py_deps),
          "the C++ implementation has the binding among its dependents (so blast radius crosses back)")

    # C++ local typing, and where a cross-file member call lands.
    uses = G.node("cpp/bindings.cc", "UsesPoint")
    comp = [e for e in G.edges_from(uses, name="Compute")]
    check(len(comp) == 1 and comp[0]["confidence"] == "typed",
          f"a call through a typed C++ local resolves ({[(G.nodes[e['dst']]['qname'], e['confidence']) for e in comp]})")
    check(comp and G.nodes[comp[0]["dst"]]["file"].endswith("kernel.cc"),
          f"the call edge lands on the .cc definition, not the .h prototype "
          f"(got {G.nodes[comp[0]['dst']]['file'] if comp else None})")

    # REGISTER_OP / REGISTER_KERNEL_BUILDER.
    ops = {n["name"]: n for n in G.g["nodes"] if n["kind"] == "op_def"}
    check("DemoMatMul" in ops, f"REGISTER_OP becomes an op_def node ({sorted(ops)})")
    if "DemoMatMul" in ops:
        impl = [e for e in G.edges_to(ops["DemoMatMul"]) if e["type"] == "implements"]
        check(len(impl) == 1 and G.nodes[impl[0]["src"]]["name"] == "DemoMatMulOp",
              f"REGISTER_KERNEL_BUILDER links the kernel class to the op "
              f"{[(G.nodes[e['src']]['name'], e['confidence']) for e in impl]}")


def test_literal_receivers(root):
    """A call on a literal receiver belongs to the literal's TYPE, not to its text.

    Found auditing TensorFlow: `", ".join(xs)` was recorded with the string's own text as the
    receiver, so skeletons printed whole string literals inside `calls:` lines (1.06% of all
    call refs there) and the linker was offered a receiver that could match a same-named repo
    method and produce a false edge.
    """
    print("# literal call receivers")
    out = run("skeleton", os.path.join(root, "python", "app", "loops.py"), "--no-stats")
    body = [ln for ln in out.splitlines() if "format_report" in ln or "calls:" in ln]
    joined = "\n".join(body)
    for want in ("str.join", "str.format", "list.count", "dict.get", "tuple.index", "str.upper"):
        check(want in joined, f"literal receiver normalized to {want}")
    check('"' not in joined and "'" not in joined,
          f"no raw string literal leaks into a calls: line\n{joined}")
    check("across two source lines" not in out,
          "a literal split across source lines does not leak either")


def test_output_budget(tmp):
    """The skeleton's output contract: never emit more than the file it summarizes, and state the
    recovery path for whatever it hides."""
    print("# skeleton output budget")
    tiny = os.path.join(tmp, "tiny.py")
    with open(tiny, "w") as f:
        f.write("x = 1\n")
    out = run("skeleton", tiny)
    check("shown in full" in out and "x = 1" in out,
          "never_worse: a file smaller than its skeleton is shown as source")
    check("too small to summarize" in out and "% less" not in out,
          f"a run that saves nothing says so instead of printing a negative percentage: {out.splitlines()[-1]}")

    # A symbol with more calls than the cap must report the overflow and name the flag that lifts it.
    busy = os.path.join(tmp, "busy.py")
    with open(busy, "w") as f:
        f.write("def busy():\n" + "".join(f"    callee_{i}()\n" for i in range(20)))
    out = run("skeleton", busy)
    check(f"(+{20 - 8} more)" in out, f"calls beyond the cap are reported as (+N more): {out}")
    check("-- elided:" in out and "--max-calls" in out,
          "the elision notice names the flag that raises the cap")
    check(out.count("--max-calls") == 1,
          "the recovery path is stated once, not on every elided line")

    raised = run("skeleton", busy, "--max-calls", "20")
    check("more)" not in raised and "-- elided:" not in raised,
          "--max-calls raises the cap and clears the notice")
    check("callee_19" in run("skeleton", busy, "--max-calls", "0"), "--max-calls 0 means no cap")

    many = os.path.join(tmp, "many.py")
    with open(many, "w") as f:
        f.write("".join(f"import mod_{i}\n" for i in range(20)) + "def f():\n    pass\n")
    out = run("skeleton", many)
    check(f"(+{20 - 12} more)" in out and "--max-imports" in out,
          f"imports beyond the cap are reported and recoverable: {out}")
    check("mod_19" in run("skeleton", many, "--max-imports", "0"), "--max-imports 0 means no cap")

    # Flag interactions. `big` is large enough that the never_worse guard stays out of the way.
    big = os.path.join(tmp, "big.py")
    with open(big, "w") as f:
        f.write("".join(f"def busy_{c}():\n" + "".join(f"    dep_{k}.call_{k}()\n" for k in range(14))
                        for c in range(25)))
    out = run("skeleton", big, "--no-stats")
    check("-- elided:" in out and "% less" not in out,
          "--no-stats drops the token line but keeps the recovery notice")
    check("-- elided:" not in run("skeleton", big, "--no-calls"),
          "--no-calls reports no call elisions -- the calls were never promised")
    check("more)" not in run("skeleton", big, "--max-calls", "-5"), "a negative cap means no cap")

    js = run("skeleton", big, "--json")
    check("-- elided:" not in js, "--json emits no notice (it would corrupt the payload)")
    try:
        json.loads(js)
        check(True, "--json stays parseable when something was elided")
    except ValueError as e:
        check(False, f"--json stays parseable when something was elided: {e}")

    # A signature cut has no flag to raise -- its recovery is the line range printed beside it.
    sig = os.path.join(tmp, "sig.py")
    with open(sig, "w") as f:
        f.write("def f(\n  a: int,\n  b: str,\n  c: float,\n) -> dict:\n    return {}\n" + "# pad\n" * 200)
    out = run("skeleton", sig)
    check("signature lines" in out and "read the line ranges above" in out,
          f"a signature-only elision points at the line range, not a flag: {out.splitlines()[-2]}")


def test_concurrent_and_legacy(tmp):
    """Two robustness cases that only show up outside a single-process run."""
    print("# concurrency and legacy artifacts")
    root = os.path.join(tmp, "conc")
    shutil.copytree(FIXTURE, root)

    # Several builds on one root. They used to share `graph.db.tmp`, so whichever renamed first
    # pulled the file away from the others and they died with FileNotFoundError.
    procs = [subprocess.Popen([sys.executable, "-B", ENGINE, "build", "--root", root, "--full", "--quiet"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(4)]
    results = [(p.wait(), p.communicate()[1].decode()[-160:]) for p in procs]
    bad = [(rc, err) for rc, err in results if rc != 0]
    check(not bad, f"4 concurrent builds on one root all succeed ({bad[:1]})")
    leftovers = [f for f in os.listdir(os.path.join(root, ".ast-graph")) if f.endswith(".tmp")]
    check(not leftovers, f"no temp databases left behind ({leftovers})")
    check(json.loads(run("query", "--root", root, "stats"))["files"] > 0,
          "the graph is usable after concurrent builds")

    # Readers must survive the file being swapped under them (POSIX: the open fd keeps the old
    # inode, so a query sees a consistent older graph rather than a torn one).
    readers = [subprocess.Popen([sys.executable, "-B", ENGINE, "query", "--root", root, "stats"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(6)]
    subprocess.run([sys.executable, "-B", ENGINE, "build", "--root", root, "--full", "--quiet"],
                   capture_output=True)
    rok = sum(1 for p in readers if p.wait() == 0)
    check(rok == 6, f"6 readers survive a rebuild swapping the database ({rok}/6)")

    # A pre-SQLite graph.json left over from an older version must be called out, not ignored.
    legacy = os.path.join(root, ".ast-graph", "graph.json")
    with open(legacy, "w") as f:
        f.write('{"nodes":[],"edges":[]}')
    out = subprocess.run([sys.executable, "-B", ENGINE, "build", "--root", root, "--full"],
                         capture_output=True, text=True)
    check("older version" in out.stderr and "can be deleted" in out.stderr,
          f"build reports a leftover pre-SQLite graph.json ({out.stderr.strip()[:110]})")
    os.remove(os.path.join(root, ".ast-graph", "graph.db"))
    q = subprocess.run([sys.executable, "-B", ENGINE, "query", "--root", root, "stats"],
                       capture_output=True, text=True)
    check(q.returncode != 0 and "older version" in (q.stdout + q.stderr),
          "a query with only a legacy graph.json says so instead of just 'not found'")


def test_empty_root(tmp):
    print("# empty root")
    empty = os.path.join(tmp, "empty")
    os.makedirs(empty)
    p = subprocess.run([sys.executable, "-B", ENGINE, "build", "--root", empty], capture_output=True, text=True)
    check(p.returncode == 0 and "0 files" in p.stdout and "no supported source files" in p.stderr, "build on an empty root succeeds and prints a hint on stderr")


def test_determinism(root):
    print("# determinism across hash seeds")
    outs = []
    for seed in ("1", "2"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        out = os.path.join(root, f"seed{seed}.db")
        subprocess.run([sys.executable, "-B", ENGINE, "build", "--root", root, "--full", "--quiet", "--out", out], env=env, check=True)
        st = astgraph.Store(out)
        try:
            outs.append(({n["id"] for n in st.iter_nodes()},
                         {(e["src"], e["dst"], e["type"], e.get("line"), e["confidence"]) for e in st.iter_edges()}))
        finally:
            st.close()
    check(outs[0][0] == outs[1][0] and outs[0][1] == outs[1][1], f"graph identical under PYTHONHASHSEED=1 and =2 (edge diff {len(outs[0][1] ^ outs[1][1])})")


def test_parallel_parse(root):
    print("# parallel parse and link == serial, and the serial fallbacks")
    import multiprocessing

    def snapshot(out):
        st = astgraph.Store(out)
        try:
            return (sorted(json.dumps(n, sort_keys=True) for n in st.iter_nodes()),
                    sorted(json.dumps(e, sort_keys=True) for e in st.iter_edges()))
        finally:
            st.close()

    saved_min, saved_link_min, saved_ctx = astgraph.PARALLEL_MIN_FILES, astgraph.PARALLEL_LINK_MIN_FILES, multiprocessing.get_context
    astgraph.PARALLEL_MIN_FILES = 1   # the fixture is far below the real thresholds; force both pools
    astgraph.PARALLEL_LINK_MIN_FILES = 1
    try:
        outs = {}
        for jobs in (1, 4):
            outs[jobs] = os.path.join(root, f"jobs{jobs}.db")
            astgraph.build_graph(root, outs[jobs], [], [], full=True, quiet=True, jobs=jobs)
        serial = snapshot(outs[1])
        check(serial == snapshot(outs[4]), f"--jobs 4 (parallel parse + link) graph identical to --jobs 1 ({len(serial[0])} nodes, {len(serial[1])} edges)")

        def no_pool(*a, **k):   # what a sandbox without a writable /dev/shm does to Pool()
            raise OSError("[Errno 38] Function not implemented")
        multiprocessing.get_context = no_pool
        fb = os.path.join(root, "fallback.db")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            astgraph.build_graph(root, fb, [], [], full=True, quiet=True, jobs=4)
        check(snapshot(fb) == serial, "pool start failure falls back to a serial parse with the same graph")
        check("parsing serially" in err.getvalue() and "resolving serially" in err.getvalue(),
              "both fallbacks are reported on stderr")
    finally:
        astgraph.PARALLEL_MIN_FILES, astgraph.PARALLEL_LINK_MIN_FILES, multiprocessing.get_context = saved_min, saved_link_min, saved_ctx


def test_file_used_by(root):
    print("# query file --used-by")
    build(root)
    out = run("query", "--root", root, "file", "python/app/models.py", "--no-calls", "--used-by")
    # User <- nested.py (2 refs), service.py (1), tests/test_service.py (1); same-file uses never count
    check("3 files      4 refs  class User" in out, "--used-by counts distinct other files and refs (User: 3 files, 4 refs)")
    out = run("query", "--root", root, "file", "python/app/models.py", "--no-calls", "--used-by", "--no-tests")
    check("2 files      3 refs  class User" in out and "non-test files" in out, "--no-tests drops usage from test files")
    out = run("query", "--root", root, "file", "python/app/models.py", "--no-calls", "--used-by", "--within", "python/tests")
    check("1 files      1 refs  class User" in out and "under python/tests" in out, "--within DIR counts only users under DIR")
    out = run("query", "--root", root, "file", "python/app/models.py", "--no-calls", "--used-by", "--within", "python/app/n*.py")
    check("1 files      2 refs  class User" in out, "--within GLOB counts only matching files (nested.py: 2 refs)")
    j = json.loads(run("query", "--root", root, "file", "python/app/models.py", "--json", "--used-by", "1"))
    check(j.get("used_by") == [{"id": "python/app/models.py::User@1", "files": 3, "refs": 4}], f"--json --used-by 1: {j.get('used_by')}")


def test_incremental(root):
    print("# incremental == full")
    build(root, "--full")
    a = os.path.join(root, "go/handler/attestor.go")
    with open(a, "a") as f:
        f.write("\n// edited\nfunc Extra() {}\n")
    out = build(root)
    check("(1 parsed" in out, f"incremental re-parsed exactly one file: {out.strip().splitlines()[0]}")
    inc = load(root)
    build(root, "--full")
    full = load(root)
    ni = {n["id"] for n in inc["nodes"]}; nf = {n["id"] for n in full["nodes"]}
    ei = {(e["src"], e["dst"], e["type"], e.get("line")) for e in inc["edges"]}
    ef = {(e["src"], e["dst"], e["type"], e.get("line")) for e in full["edges"]}
    check(ni == nf and ei == ef, f"incremental graph identical to full (nodes {len(ni)}/{len(nf)}, edges {len(ei)}/{len(ef)})")
    check(any(n["qname"] == "Extra" for n in inc["nodes"]), "edited file's new symbol present")


def test_incremental_relink(tmp):
    print("# incremental linking: only affected files re-linked, same graph as --full")
    root = os.path.join(tmp, "relink")
    os.makedirs(root)
    srcs = {
        "r1.py": "class R1:\n    def ping(self):\n        return 1\n",
        "r2.py": "class R2:\n    def ping(self):\n        return 2\n",
        "base.py": "from r1 import R1\nfrom r2 import R2\n\n\nclass Base(R1):\n    pass\n",
        "sub.py": "from base import Base\n\n\nclass Sub(Base):\n    pass\n",
        # use.py never names Base, R1 or R2: only the recorded inheritance lookup ties it to base.py
        "use.py": "from sub import Sub\n\n\ndef go():\n    s = Sub()\n    return s.ping()\n",
        "other.py": "def helper():\n    return 3\n\n\ndef caller():\n    return helper()\n",
        "kernel.h": "class K {\n public:\n  void Compute();\n};\n",
        "kernel.cc": "#include \"kernel.h\"\nvoid K::Compute() {}\n",
    }
    for name, body in srcs.items():
        with open(os.path.join(root, name), "w") as f:
            f.write(body)

    def edges(db):
        st = astgraph.Store(db)
        try:
            return sorted(json.dumps(e, sort_keys=True) for e in st.iter_edges()), \
                   sorted(json.dumps(n, sort_keys=True) for n in st.iter_nodes())
        finally:
            st.close()
    build(root, "--full")
    with open(os.path.join(root, "base.py"), "w") as f:
        f.write(srcs["base.py"].replace("class Base(R1)", "class Base(R2)"))
    out = build(root)
    m = re.search(r"(\d+) re-linked", out)
    check(m and 0 < int(m.group(1)) < len(srcs), f"one edit re-links a subset of files ({m.group(0) if m else out.strip()})")
    inc = edges(os.path.join(root, ".ast-graph", "graph.db"))
    full_db = os.path.join(root, "full.db")
    build(root, "--full", "--out", full_db)
    check(inc == edges(full_db), "incremental graph (edges and nodes) identical to --full after a base-class change")
    check(any('"dst": "r2.py::R2.ping@2"' in e and '"src": "use.py::go@4"' in e for e in inc[0]),
          "caller two inheritance hops from the edit now reaches R2.ping")
    # a body-only edit (no definition, signature or line moves) re-links the edited file alone
    with open(os.path.join(root, "other.py"), "w") as f:
        f.write(srcs["other.py"].replace("return 3", "return 4"))
    out = build(root)
    check("(1 parsed" in out and "1 re-linked" in out, f"body-only edit re-links only that file ({out.strip()[:120]})")
    # a renamed C++ definition must not leave its header declaration marked as defined
    with open(os.path.join(root, "kernel.cc"), "w") as f:
        f.write(srcs["kernel.cc"].replace("K::Compute", "K::Run"))
    build(root)
    inc = edges(os.path.join(root, ".ast-graph", "graph.db"))
    decl = [n for n in inc[1] if '"id": "kernel.h::K.Compute@3"' in n]
    check(decl and "defined_at" not in decl[0], "header declaration loses its pairing mark when the definition is renamed")
    build(root, "--full", "--out", full_db)
    check(inc == edges(full_db), "incremental graph identical to --full after the C++ rename")


def test_fast_path(root):
    print("# git stamp fast path")
    def git(*a):
        subprocess.run(["git", "-C", root, *a], check=True, capture_output=True)
    git("init", "-q"); git("config", "user.email", "t@t"); git("config", "user.name", "t")
    git("add", "-A"); git("commit", "-qm", "init")
    out1 = build(root, "--full")
    check("parsed" in out1, "first build parses")
    check(os.path.exists(os.path.join(root, ".ast-graph", "graph.db.stamp")), "stamp sidecar written")
    out2 = build(root)
    check("up to date" in out2, f"unchanged tree, .ast-graph/ untracked (no .gitignore) -> fast path: {out2.strip()}")
    with open(os.path.join(root, "README.md"), "w") as f:
        f.write("docs only\n")
    check("up to date" in build(root), "new non-source file -> still fast path")
    with open(os.path.join(root, ".gitignore"), "w") as f:
        f.write(".ast-graph/\n")
    git("add", "-A"); git("commit", "-qm", "ignore")
    check("(0 parsed" in build(root), "HEAD moved -> rebuild via hash cache")
    check("up to date" in build(root), "fast path with .ast-graph/ ignored")
    with open(os.path.join(root, "python/app/latest.py"), "a") as f:
        f.write("\ndef newer():\n    pass\n")
    out3 = build(root)
    check("(1 parsed" in out3, f"dirty tracked file -> rebuild, 1 parsed: {out3.strip().splitlines()[0]}")
    out4 = build(root)
    check("up to date" in out4, "same dirty content again -> fast path")
    with open(os.path.join(root, "python/app/latest.py"), "a") as f:
        f.write("\ndef newest():\n    pass\n")
    check("(1 parsed" in build(root), "second edit of the same dirty file is seen (content hashed)")
    with open(os.path.join(root, "python/app/untracked.py"), "w") as f:
        f.write("def u():\n    pass\n")
    check("(1 parsed" in build(root), "new untracked file -> rebuild")
    check("up to date" in build(root), "fast path again")
    os.remove(os.path.join(root, "python/app/untracked.py"))
    check("parsed" in build(root) and "up to date" not in build(root, "--full"), "deleted file -> rebuild; --full bypasses the fast path")
    check("up to date" in build(root), "fast path after --full")
    git("commit", "-qam", "edit")
    out = build(root)
    check("(0 parsed" in out and "up to date" not in out, f"HEAD moved -> stamp miss but hash cache still reuses every file: {out.strip().splitlines()[0]}")
    # an engine change must invalidate the stamp even at the same GRAPH_VERSION
    check("up to date" in build(root), "fast path before engine edit")
    eng = os.path.abspath(ENGINE)
    with open(eng) as f:
        src = f.read()
    with open(eng, "w") as f:
        f.write(src + "\n# stamp-test\n")
    try:
        out_e = build(root)
        check("parsed, 0 unchanged" in out_e and "up to date" not in out_e, f"edited engine file -> cached parses discarded, full re-parse: {out_e.strip().splitlines()[0][:80]}")
    finally:
        with open(eng, "w") as f:
            f.write(src)
    check("up to date" not in build(root) and "up to date" in build(root), "engine restored -> one re-link, then fast path again")

    # Queries refresh a stale graph themselves (no separate `build` call per question), with the
    # options the build recorded; --no-refresh / ASTGRAPH_NO_REFRESH opt out.
    stamp = json.load(open(os.path.join(root, ".ast-graph", "graph.db.stamp")))
    check(stamp.get("options", {}).get("root") == os.path.abspath(root), f"stamp records the build options {stamp.get('options')}")
    with open(os.path.join(root, "python/app/latest.py"), "a") as f:
        f.write("\ndef fresh_after_edit():\n    pass\n")
    stale = subprocess.run([sys.executable, "-B", ENGINE, "query", "--root", root, "--no-refresh", "find", "fresh_after_edit"],
                           capture_output=True, text=True)
    check("fresh_after_edit" not in stale.stdout and "refreshed" not in stale.stderr, "--no-refresh answers from the graph as built")
    q = subprocess.run([sys.executable, "-B", ENGINE, "query", "--root", root, "find", "fresh_after_edit"], capture_output=True, text=True)
    check("graph refreshed" in q.stderr and "fresh_after_edit" in q.stdout, f"a query on a stale graph refreshes it first: {q.stderr.strip()[:90]}")
    q2 = subprocess.run([sys.executable, "-B", ENGINE, "query", "--root", root, "find", "fresh_after_edit"], capture_output=True, text=True)
    check("refreshed" not in q2.stderr, "the next query finds it current (no second rebuild)")
    # A graph from before stamps recorded options cannot be refreshed safely; it must say so, not go stale silently.
    sp = os.path.join(root, ".ast-graph", "graph.db.stamp")
    legacy = {k: v for k, v in json.load(open(sp)).items() if k != "options"}
    json.dump(legacy, open(sp, "w"))
    con = sqlite3.connect(os.path.join(root, ".ast-graph", "graph.db"))
    con.execute("UPDATE meta SET value=? WHERE key='engine'", (json.dumps("an-older-engine"),))
    con.commit(); con.close()
    q3 = subprocess.run([sys.executable, "-B", ENGINE, "query", "--root", root, "stats"], capture_output=True, text=True)
    check("older engine" in q3.stderr and "run `build` once" in q3.stderr, f"a graph built before options were recorded warns instead of going stale: {q3.stderr.strip()[:80]}")


def main():
    tmp = tempfile.mkdtemp(prefix="astgraph-fixture-")
    try:
        root = os.path.join(tmp, "fixture")
        shutil.copytree(FIXTURE, root)
        G = test_resolution(root)
        test_lean_queries(root)
        test_py_value_typing(G)
        test_tests_detection(root, G)
        test_determinism(root)
        test_concurrent_and_legacy(tmp)
        test_empty_root(tmp)
        test_keep_dir_and_parse_errors(tmp)
        test_cpp_extraction(root)
        test_cross_language_bridge(root)
        test_literal_receivers(root)
        test_output_budget(tmp)
        test_parallel_parse(root)
        test_file_used_by(root)
        test_incremental(root)
        test_incremental_relink(tmp)
        # fresh copy for the git test
        root2 = os.path.join(tmp, "fixture-git")
        shutil.copytree(FIXTURE, root2)
        test_fast_path(root2)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{len(FAILS)} failure(s)")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
