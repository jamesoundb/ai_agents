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
    check(area_lines == [23, 29], f"kotlin: field chain and safe-call chain both reach Sq.area {area_lines}")
    chain_lines = sorted((e["line"], e.get("name")) for e in G.edges_from(kp) if e["line"] == 33 and e["confidence"] != "ambiguous")
    check(chain_lines == [(33, "OrderRepo"), (33, "double"), (33, "save")], f"kotlin: inner calls of a navigation chain are recorded {chain_lines}")
    saves = sorted((e["line"], e["confidence"]) for e in G.edges_from(kp, name="save"))
    check((37, "typed") in saves, f"kotlin: class property `val cached = makeRepo()` typed from the return type {saves}")
    check(("Sq.plus2", "typed") in kc, f"kotlin: infix call `a plus2 b` typed {kc}")
    check(not [e for e in G.edges_from(kp) if e.get("name") in ("let", "forEach")], "kotlin: stdlib scope/collection functions produce no lead edges")
    sq_card = run("query", "--graph", os.path.join(root, ".ast-graph", "graph.json"), "symbol", "Order.kt:Sq")
    check("double" in sq_card and "[extension]" in sq_card, "kotlin: extension functions appear on the receiver's card")
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
    check(fields == ["id", "side", "sq"], f"kotlin: primary-constructor val/var parameters are fields {fields}")
    card = run("query", "--graph", os.path.join(root, ".ast-graph", "graph.json"), "symbol", "Order.kt:Order")
    check("create" in card and "Members" in card, "kotlin: class card lists companion members")

    # --- Terraform / Kubernetes
    types = {(e["type"], e["confidence"]) for e in G.g["edges"]}
    check(("uses_module", "exact") in types, "hcl: module call resolved")
    check(("selects", "exact") in types, "k8s: Service selects Deployment")
    svc = [n for n in G.g["nodes"] if n["kind"] == "k8s_object" and n["name"] == "Service/web"][0]
    check(any(G.nodes[e["dst"]]["name"] == "Deployment/web" for e in G.g["edges"] if e["src"] == svc["id"] and e["type"] == "selects"), "k8s: Service/web -> Deployment/web")

    # --- overview flags
    ovl = json.loads(run("query", "--graph", os.path.join(root, ".ast-graph", "graph.json"), "overview", "--json", "--lang", "go"))
    files_go = {G.nodes[i]["file"] for i, _ in ovl["hub_symbols"]}
    check(files_go and all(f.endswith(".go") for f in files_go), f"overview --lang go ranks only Go symbols {sorted(files_go)}")
    ovt = json.loads(run("query", "--graph", os.path.join(root, ".ast-graph", "graph.json"), "overview", "--json", "--no-tests"))
    scored = {G.nodes[i]["qname"]: c for i, c in ovt["hub_symbols"]}
    check("PaymentRequest" in scored and scored["PaymentRequest"] < {G.nodes[i]["qname"]: c for i, c in json.loads(run("query", "--graph", os.path.join(root, ".ast-graph", "graph.json"), "overview", "--json"))["hub_symbols"]}["PaymentRequest"],
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
