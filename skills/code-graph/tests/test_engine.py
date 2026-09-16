#!/usr/bin/env python3
"""Regression tests for astgraph.py against tests/fixture.

Covers: language coverage, receiver-aware call resolution (no name-only edges onto external or
unknown receivers, typed edges through fields/locals/inheritance, namespace calls into repo
packages), test-file detection, Terraform/Kubernetes edges, incremental == full, and the git
stamp fast path. Run via run_tests.sh (needs tree-sitter). Exit code 1 on any failure.
"""
import json
import os
import shutil
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
    with open(os.path.join(root, ".ast-graph", "graph.json")) as f:
        return json.load(f)


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
    out = build(root, "--full")
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
    fr = json.loads(run("query", "--graph", os.path.join(root, ".ast-graph", "graph.json"), "find", "read", "--lang", "python", "--kind", "function", "--json"))
    at = run("query", "--graph", os.path.join(root, ".ast-graph", "graph.json"), "symbol", "overloads.py:read@8")
    check("overloads.py:8" in at.splitlines()[0], f"`file:name@line` query form selects that definition: {at.splitlines()[0][:80]}")
    check(fr and fr[0]["line"] == 8, f"find lists the implementation before its @overload stubs (first at line {fr[0]['line'] if fr else None})")
    ctor = run("query", "--graph", os.path.join(root, ".ast-graph", "graph.json"), "trace-deps", "service.py:Service.__init__", "--depth", "1")
    check("is a constructor" in ctor and "python/tests/test_service.py" in ctor, "trace-deps on __init__ includes the class's instantiations")
    pth = run("query", "--graph", os.path.join(root, ".ast-graph", "graph.json"), "path", "PaymentOrchestratorTest", "PaymentGatewayClient")
    check("--calls (typed)--> PaymentGatewayClient.validate" in pth, f"path: text rows end at the target's member: {pth.strip().splitlines()[-1][:80] if pth.strip() else pth!r}")
    pj = json.loads(run("query", "--graph", os.path.join(root, ".ast-graph", "graph.json"), "path", "PaymentOrchestratorTest", "PaymentGatewayClient", "--json"))
    check(pj["found"] and len(pj["hops"]) == len(pth.strip().splitlines()) and pj["hops"][-1]["dst"].endswith("PaymentGatewayClient.validate@4")
          and all(set(h) == {"src", "type", "confidence", "dst", "file", "line"} for h in pj["hops"]),
          f"path --json: one hop per text row with src/type/confidence/dst/file/line ({len(pj['hops'])} hops)")
    pn = json.loads(run("query", "--graph", os.path.join(root, ".ast-graph", "graph.json"), "path", "PaymentGatewayClient", "PaymentOrchestratorTest", "--json"))
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
    ovr = json.loads(run("query", "--graph", os.path.join(root, ".ast-graph", "graph.json"), "overview", "--json", "--no-tests", "--lang", "rust"))
    rscore = {G.nodes[i]["qname"]: c for i, c in ovr["hub_symbols"]}
    ovr_all = json.loads(run("query", "--graph", os.path.join(root, ".ast-graph", "graph.json"), "overview", "--json", "--lang", "rust"))
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
    sq_card = run("query", "--graph", os.path.join(root, ".ast-graph", "graph.json"), "symbol", "Order.kt:Sq")
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
    card = run("query", "--graph", os.path.join(root, ".ast-graph", "graph.json"), "symbol", "Order.kt:Order")
    check("create" in card and "Members" in card, "kotlin: class card lists companion members")
    dt = G.confs(G.edges_from(G.node("Order.kt", "Sq.describeTwice")))
    check(("Sq.double", "typed") in dt, f"kotlin: `this`/`this@label` inside an extension function is the receiver type {dt}")
    kc = G.confs(G.edges_from(kp))
    check(("FunctionProvider.charLength", "typed") in kc, f"kotlin: imported top-level `val currentDialect: Dialect` types a property chain {kc}")
    cd = G.node("Base.kt", "currentDialect")
    check(cd["kind"] == "variable" and cd["extra"].get("type") == "Dialect", f"kotlin: top-level property is a typed variable node {cd['kind']} {cd['extra']}")
    oc = run("query", "--graph", os.path.join(root, ".ast-graph", "graph.json"), "symbol", "Base.kt:FunctionProvider.charLength")
    check("Overridden by (1): SqliteProvider.charLength" in oc, f"kotlin: method card lists overriding methods: {[l for l in oc.splitlines() if 'Overrid' in l]}")
    ja = run("query", "--graph", os.path.join(root, ".ast-graph", "graph.json"), "symbol", "Shape.java:Shape.area")
    check("Overridden by" in ja and "Circle.area" in ja and "Square.area" in ja, f"java: interface method card lists implementers' methods: {[l for l in ja.splitlines() if 'Overrid' in l]}")
    jc = run("query", "--graph", os.path.join(root, ".ast-graph", "graph.json"), "symbol", "Circle.java:Circle.area")
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
    bc = run("query", "--graph", os.path.join(root, ".ast-graph", "graph.json"), "symbol", "Order.kt:Cart.Builder.kind")
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
    ft = run("query", "--graph", os.path.join(root, ".ast-graph", "graph.json"), "find", "helper", "--no-tests", "--json")
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
    td = run("query", "--graph", os.path.join(root, ".ast-graph", "graph.json"), "trace-deps", "infra/main.tf:aws_instance.app", "--depth", "1")
    check("moved.aws_instance.app" in td and "references -> aws_instance.app | exact" in td, f"hcl: blast radius of a resource lists the moved block: {[l for l in td.splitlines() if 'moved' in l]}")
    reg = [(G.nodes[e["dst"]]["id"], e["confidence"]) for e in G.edges_from(G.node("main.tf", "module.vpc_from_registry"), typ="uses_module")]
    check(("terraform_module:infra/modules/vpc", "ambiguous") in reg and any(c == "external" for _, c in reg),
          f"hcl: registry source with a local //subdir gets an ambiguous lead next to the external edge {reg}")
    src_lines = open(os.path.join(root, "infra/main.tf")).read().splitlines()
    vpc_line = next(i + 1 for i, l in enumerate(src_lines) if "module.vpc.vpc_id" in l and "reference on a later line" in l)
    sg = G.node("infra/main.tf", "aws_security_group.app")
    ref_lines = sorted(e["line"] for e in G.g["edges"] if e["src"] == sg["id"] and e["type"] == "references" and G.nodes[e["dst"]]["qname"] == "module.vpc")
    check(vpc_line in ref_lines, f"hcl: a reference inside a multi-line expression is recorded at its own line ({ref_lines}, expected {vpc_line})")
    tfq = run("query", "--graph", os.path.join(root, ".ast-graph", "graph.json"), "symbol", "main.tf:terraform")
    check(tfq.splitlines()[0].endswith("(main.tf:1)"), f"query: `file:name` prefers the exact root path over suffix matches: {tfq.splitlines()[0]}")
    ovh = run("query", "--graph", os.path.join(root, ".ast-graph", "graph.json"), "overview", "--lang", "hcl", "--top", "20")
    check('variable "cidr"' in ovh and "(+1 same-named copy in other directories)" in ovh, "overview: same-named symbols in several directories collapse to one row")
    st = json.loads(run("query", "--root", root, "stats"))
    check(st.get("files", 0) > 0, "query --root reads <root>/.ast-graph/graph.json")
    check(("selects", "exact") in types, "k8s: Service selects Deployment")
    svc = [n for n in G.g["nodes"] if n["kind"] == "k8s_object" and n["name"] == "Service/web"][0]
    check(any(G.nodes[e["dst"]]["name"] == "Deployment/web" for e in G.g["edges"] if e["src"] == svc["id"] and e["type"] == "selects"), "k8s: Service/web -> Deployment/web")

    # --- overview flags
    ovl = json.loads(run("query", "--graph", os.path.join(root, ".ast-graph", "graph.json"), "overview", "--json", "--lang", "go"))
    files_go = {G.nodes[i]["file"] for i, _ in ovl["hub_symbols"]}
    check(files_go and all(f.endswith(".go") for f in files_go), f"overview --lang go ranks only Go symbols {sorted(files_go)}")
    ovt = json.loads(run("query", "--graph", os.path.join(root, ".ast-graph", "graph.json"), "overview", "--json", "--no-tests", "--top", "80"))
    scored = {G.nodes[i]["qname"]: c for i, c in ovt["hub_symbols"]}
    check("PaymentRequest" in scored and scored["PaymentRequest"] < {G.nodes[i]["qname"]: c for i, c in json.loads(run("query", "--graph", os.path.join(root, ".ast-graph", "graph.json"), "overview", "--json", "--top", "80"))["hub_symbols"]}["PaymentRequest"],
          "overview --no-tests drops the usage coming from PaymentOrchestratorTest")
    fj = json.loads(run("query", "--graph", os.path.join(root, ".ast-graph", "graph.json"), "file", "python/pkg/ops.py", "--json"))
    check("python/app/consumer.py" in fj.get("imported_by", []) and any(n["name"] == "convert" for n in fj["nodes"]), f"query file --json lists nodes and importers {fj.get('imported_by')}")

    # --- hub-sized output modes
    gp = os.path.join(root, ".ast-graph", "graph.json")
    summ = run("query", "--graph", gp, "trace-deps", "PaymentRequest", "--summary")
    check("| Directory |" in summ and "Most connected dependents" in summ and "Files affected:" in summ and "| Dependent file |" not in summ, "trace-deps --summary: directory table, dependents, no per-edge rows")
    fo = run("query", "--graph", gp, "trace-deps", "PaymentRequest", "--files-only")
    check("hop 1 (" in fo and "PaymentGatewayClient.java" in fo and "|" not in fo.split("Files affected")[0], "trace-deps --files-only lists files by hop")
    small = run("query", "--graph", gp, "trace-deps", "PaymentRequest", "--max-rows", "1")
    check("exceed --max-rows 1" in small and "| Directory |" in small, "trace-deps degrades to summary above --max-rows")
    card = run("query", "--graph", gp, "symbol", "Repo.save" if False else "models.py:Repo.save", "--limit", "1")
    check("showing 1" in card and "more" in card, f"symbol --limit caps the card and summarises the rest")
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
    ov = json.loads(run("query", "--graph", os.path.join(root, ".ast-graph", "graph.json"), "overview", "--json", "--top", "500"))
    score = {G.nodes[i]["qname"]: c for i, c in ov["hub_symbols"]}
    # the mock's hub score is just its legitimate uses: one instantiation + one typed m.Get call
    check(score.get("ResourceDataMock", 0) == 2 and score.get("MemStore", 0) >= score.get("ResourceDataMock", 0),
          f"overview: ResourceDataMock scores only its real uses {score.get('ResourceDataMock')}, MemStore {score.get('MemStore')}")
    return G


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
    out = run("query", "--graph", os.path.join(root, ".ast-graph", "graph.json"), "trace-deps", "PaymentRequest")
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
        out = os.path.join(root, f"seed{seed}.json")
        subprocess.run([sys.executable, "-B", ENGINE, "build", "--root", root, "--full", "--quiet", "--out", out], env=env, check=True)
        with open(out) as f:
            g = json.load(f)
        outs.append(({n["id"] for n in g["nodes"]}, {(e["src"], e["dst"], e["type"], e.get("line"), e["confidence"]) for e in g["edges"]}))
    check(outs[0][0] == outs[1][0] and outs[0][1] == outs[1][1], f"graph identical under PYTHONHASHSEED=1 and =2 (edge diff {len(outs[0][1] ^ outs[1][1])})")


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


def test_fast_path(root):
    print("# git stamp fast path")
    def git(*a):
        subprocess.run(["git", "-C", root, *a], check=True, capture_output=True)
    git("init", "-q"); git("config", "user.email", "t@t"); git("config", "user.name", "t")
    git("add", "-A"); git("commit", "-qm", "init")
    out1 = build(root, "--full")
    check("parsed" in out1, "first build parses")
    check(os.path.exists(os.path.join(root, ".ast-graph", "graph.json.stamp")), "stamp sidecar written")
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


def main():
    tmp = tempfile.mkdtemp(prefix="astgraph-fixture-")
    try:
        root = os.path.join(tmp, "fixture")
        shutil.copytree(FIXTURE, root)
        G = test_resolution(root)
        test_tests_detection(root, G)
        test_determinism(root)
        test_empty_root(tmp)
        test_keep_dir_and_parse_errors(tmp)
        test_incremental(root)
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
