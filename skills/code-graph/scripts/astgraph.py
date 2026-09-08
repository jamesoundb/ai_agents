#!/usr/bin/env python3
"""
astgraph.py — Tree-sitter powered semantic skeletons and relationship graph for AI coding agents.

Design goals (from "Stop Dumping Raw Code into LLMs", HN 47367129, Maki, Copilot OSS analysis):
  * Deterministic extraction before any LLM call. The model queries a map; it never parses code.
  * Skeleton = signatures + members + calls with exact line ranges (read-before-cat).
  * Graph = nodes (files, classes, functions, fields, resources, k8s objects ...) and typed edges
    (contains, calls, imports, extends, implements, instantiates, references, uses_module, selects,
    depends_on). Every cross-file edge carries a confidence label because tree-sitter is syntactic.
  * Works for application code AND platform code (Terraform HCL, Kubernetes YAML).

Subcommands:
  skeleton PATH...                 print a token-light skeleton of files/dirs (no graph needed)
  build [--root DIR] [--out FILE]  build/refresh the graph incrementally (by file hash)
  query [--graph FILE] <find|symbol|callers|callees|trace-deps|overview|file|path|stats> ...

Dependencies: pip install tree-sitter tree-sitter-language-pack
"""
import argparse
import fnmatch
import hashlib
import json
import os
import re
import sys
import time
from collections import defaultdict, deque

try:
    from tree_sitter_language_pack import get_parser
except ImportError:  # pragma: no cover
    sys.stderr.write("astgraph: missing dependencies. Run: pip install tree-sitter tree-sitter-language-pack\n")
    sys.exit(2)

# ----------------------------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------------------------
EXT_LANG = {
    ".py": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "tsx",
    ".go": "go",
    ".java": "java",
    ".rs": "rust",
    ".tf": "hcl", ".hcl": "hcl", ".tfvars": "hcl",
    ".yaml": "yaml", ".yml": "yaml",
}
DEFAULT_EXCLUDE_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", "target",
    ".terraform", ".ast-graph", "vendor", ".idea", ".vscode", ".mypy_cache", ".pytest_cache",
    "coverage", ".next", ".tox",
}
# Edge types that mean "src depends on dst" (used for blast radius / reverse dependencies).
DEP_EDGE_TYPES = {
    "calls", "imports", "extends", "implements", "instantiates", "references",
    "uses_module", "selects", "depends_on",
}
CONTAINER_KINDS = {"class", "interface", "struct", "enum", "trait", "impl", "record", "module"}
TYPE_LIKE_KINDS = {"class", "interface", "struct", "enum", "trait", "type", "record", "annotation"}
TYPE_NAME_RE = re.compile(r"\b[A-Z][A-Za-z0-9_]*\b")
COMMON_TYPE_WORDS = {"String", "Integer", "Long", "Boolean", "Double", "Float", "Object", "List", "Map", "Set",
                     "Optional", "Promise", "Array", "Record", "Partial", "Readonly", "Dict", "Any", "Self",
                     "Vec", "Option", "Result", "Box", "Rc", "Arc", "Void", "None", "True", "False", "Number",
                     "Date", "Error", "Exception", "Iterable", "Iterator", "Callable", "Tuple", "Union"}
# Signature keywords: when a signature already starts with one, the skeleton omits the kind label.
SIG_KEYWORDS = ("def ", "func ", "fn ", "class ", "interface ", "struct ", "type ", "enum ", "trait ", "impl ",
                "mod ", "function ", "const ", "variable ", "output ", "resource ", "data ", "module ",
                "provider ", "local.", "terraform", "namespace ", "record ", "annotation ")
TEST_PATTERNS = ("test", "spec", "__tests__")
GRAPH_VERSION = 1
DEFAULT_GRAPH = ".ast-graph/graph.json"

_PARSERS = {}


def parser_for(lang):
    if lang not in _PARSERS:
        _PARSERS[lang] = get_parser(lang)
    return _PARSERS[lang]


def approx_tokens(text):
    """Cheap token estimate (~4 chars/token) used for savings reporting."""
    return max(1, len(text) // 4)


def strip_quotes(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'`":
        return s[1:-1]
    return s


def base_type_name(t):
    """`Foo<Bar>` -> Foo, `*pkg.Foo` -> Foo, `a.b.C` -> C, `Foo[]` -> Foo."""
    t = t.strip().lstrip("*&").split("<")[0].split("[")[0].strip()
    if "." in t:
        t = t.split(".")[-1]
    if "::" in t:
        t = t.split("::")[-1]
    return t


# ----------------------------------------------------------------------------------------------
# Extraction context (shared by all language handlers)
# ----------------------------------------------------------------------------------------------
class Ctx:
    """Holds per-file extraction state: the node stack, produced nodes and unresolved refs."""

    def __init__(self, path, lang, src, root_dir):
        self.path = path
        self.lang = lang
        self.src = src
        self.root_dir = root_dir
        self.nodes = []
        self.refs = []
        self.pending_annotations = []
        self.file_extra = {}
        self.scopes = []  # stack of {local var name: type name} for typed call resolution
        self.file_node = {
            "id": path, "kind": "file", "name": os.path.basename(path), "qname": path,
            "file": path, "line": 1, "end_line": src.count(b"\n") + 1, "signature": path,
            "annotations": [], "parent": None, "extra": {},
        }
        self.stack = [self.file_node]

    # -- helpers ---------------------------------------------------------------------------
    def text(self, n):
        if n is None:
            return ""
        return self.src[n.start_byte:n.end_byte].decode("utf-8", "replace")

    def top(self):
        return self.stack[-1]

    def push(self, node):
        self.stack.append(node)

    def pop(self):
        self.stack.pop()

    def take_annotations(self):
        a, self.pending_annotations = self.pending_annotations, []
        return a

    # -- local variable scopes (for `obj.method()` resolution) ------------------------------
    def push_scope(self):
        self.scopes.append({})

    def pop_scope(self):
        if self.scopes:
            self.scopes.pop()

    def declare(self, name, type_text):
        if self.scopes and name and type_text:
            self.scopes[-1][name] = base_type_name(type_text)

    def lookup(self, name):
        for sc in reversed(self.scopes):
            if name in sc:
                return sc[name]
        return None

    def add_node(self, kind, name, tsnode, signature=None, annotations=None, extra=None, qname=None, type_text=None):
        parent = self.top()
        if qname is None:
            qname = name if parent["kind"] == "file" else f"{parent['qname']}.{name}"
        line = tsnode.start_point[0] + 1
        node = {
            "id": f"{self.path}::{qname}@{line}",
            "kind": kind, "name": name, "qname": qname, "file": self.path,
            "line": line, "end_line": tsnode.end_point[0] + 1,
            "signature": (signature or name).strip(),
            "annotations": annotations if annotations is not None else self.take_annotations(),
            "parent": parent["id"], "extra": extra or {},
        }
        self.nodes.append(node)
        # Types named in a signature (params, return type, field type) become `uses_type` refs so
        # that changing a DTO surfaces every API contract that mentions it.
        if type_text:
            seen = set()
            for m in TYPE_NAME_RE.finditer(type_text):
                t = m.group(0).split(".")[-1]
                if t != name and t not in seen and t not in COMMON_TYPE_WORDS:
                    seen.add(t)
                    self.refs.append({"kind": "uses_type", "name": t, "src": node["id"], "line": line, "hint": None})
        return node

    def add_ref(self, kind, name, tsnode, hint=None, **extra):
        if not name:
            return
        ref = {"kind": kind, "name": name, "src": self.top()["id"],
               "line": tsnode.start_point[0] + 1, "hint": hint}
        if kind == "call" and hint:
            # Resolve the receiver's root locally when we can: `o.process()` with `Foo o` in scope.
            chain = [seg.split("(")[0].strip("*&!? ") for seg in hint.split(".")]
            root = chain[0]
            root_type = self.lookup(root)
            if root_type:
                ref["hint_type"] = root_type
                ref["chain"] = chain[1:]
        ref.update(extra)
        self.refs.append(ref)
        return ref

    # -- generic walk ----------------------------------------------------------------------
    def walk(self, n):
        handler = HANDLERS[self.lang].get(n.type)
        if handler and handler(self, n):
            return
        for c in n.children:
            self.walk(c)

    def walk_children(self, n):
        if n is None:
            return
        for c in n.children:
            self.walk(c)


# ----------------------------------------------------------------------------------------------
# Python
# ----------------------------------------------------------------------------------------------
def py_decorated(ctx, n):
    for c in n.children:
        if c.type == "decorator":
            ctx.pending_annotations.append(ctx.text(c).lstrip("@").strip())
        else:
            ctx.walk(c)
    ctx.pending_annotations = []
    return True


def py_class(ctx, n):
    name = ctx.text(n.child_by_field_name("name"))
    node = ctx.add_node("class", name, n, signature=f"class {name}")
    ctx.push(node)
    sup = n.child_by_field_name("superclasses")
    if sup is not None:
        for c in sup.named_children:
            if c.type in ("identifier", "attribute", "subscript"):
                ctx.add_ref("extends", base_type_name(ctx.text(c)), c)
    body = n.child_by_field_name("body")
    if body is not None:
        for stmt in body.named_children:
            if stmt.type == "expression_statement" and stmt.named_children and stmt.named_children[0].type == "assignment":
                a = stmt.named_children[0]
                left, typ = a.child_by_field_name("left"), a.child_by_field_name("type")
                if left is not None and left.type == "identifier":
                    sig = f"{ctx.text(left)}: {ctx.text(typ)}" if typ is not None else ctx.text(left)
                    ctx.add_node("field", ctx.text(left), stmt, signature=sig, type_text=ctx.text(typ) if typ is not None else None,
                                 extra={"type": ctx.text(typ) if typ is not None else None})
        ctx.walk_children(body)
    ctx.pop()
    return True


def py_function(ctx, n):
    name = ctx.text(n.child_by_field_name("name"))
    params = ctx.text(n.child_by_field_name("parameters"))
    ret = n.child_by_field_name("return_type")
    kind = "method" if ctx.top()["kind"] == "class" else "function"
    sig = f"def {name}{params}" + (f" -> {ctx.text(ret)}" if ret is not None else "")
    node = ctx.add_node(kind, name, n, signature=sig, type_text=params + (ctx.text(ret) if ret is not None else ""))
    ctx.push(node)
    ctx.push_scope()
    pn = n.child_by_field_name("parameters")
    for p in (pn.named_children if pn is not None else []):
        if p.type == "typed_parameter" and p.named_children:
            ctx.declare(ctx.text(p.named_children[0]), ctx.text(p.child_by_field_name("type")))
        elif p.type == "typed_default_parameter":
            ctx.declare(ctx.text(p.child_by_field_name("name")), ctx.text(p.child_by_field_name("type")))
    # self.x: T = ... inside __init__ become fields of the class
    if kind == "method":
        body = n.child_by_field_name("body")
        for stmt in (body.named_children if body is not None else []):
            if stmt.type == "expression_statement" and stmt.named_children and stmt.named_children[0].type == "assignment":
                a = stmt.named_children[0]
                left, typ = a.child_by_field_name("left"), a.child_by_field_name("type")
                if left is not None and left.type == "attribute" and ctx.text(left).startswith("self."):
                    fname = ctx.text(left)[5:]
                    cls = ctx.stack[-2]
                    if "." not in fname and not any(x["kind"] == "field" and x["name"] == fname and x["parent"] == cls["id"] for x in ctx.nodes):
                        ctx.stack.append(cls)
                        ftype = ctx.text(typ) if typ is not None else None
                        if ftype is None:  # self.x = SomeClass(...)  or  self.x = param (typed parameter)
                            right = a.child_by_field_name("right")
                            if right is not None and right.type == "call" and right.child_by_field_name("function") is not None:
                                cand = base_type_name(ctx.text(right.child_by_field_name("function")))
                                ftype = cand if cand[:1].isupper() else None
                            elif right is not None and right.type == "identifier":
                                ftype = ctx.lookup(ctx.text(right))
                        ctx.add_node("field", fname, stmt, signature=f"{fname}: {ftype}" if ftype else fname, type_text=ftype,
                                     extra={"type": ftype, "inferred": True})
                        ctx.stack.pop()
    ctx.walk_children(n.child_by_field_name("body"))
    ctx.pop_scope()
    ctx.pop()
    return True


def py_call(ctx, n):
    fn = n.child_by_field_name("function")
    if fn is not None:
        if fn.type == "identifier":
            ctx.add_ref("call", ctx.text(fn), n)
        elif fn.type == "attribute":
            ctx.add_ref("call", ctx.text(fn.child_by_field_name("attribute")), n, hint=ctx.text(fn.child_by_field_name("object")))
    return False


def py_import(ctx, n):
    if n.type == "import_statement":
        for c in n.named_children:
            if c.type == "dotted_name":
                ctx.add_ref("import", ctx.text(c), n, names=[])
            elif c.type == "aliased_import":
                ctx.add_ref("import", ctx.text(c.child_by_field_name("name")), n, names=[])
    else:  # import_from_statement
        mod = n.child_by_field_name("module_name")
        names = []
        for c in n.named_children:
            if c is mod:
                continue
            if c.type == "dotted_name":
                names.append(ctx.text(c))
            elif c.type == "aliased_import":
                names.append(ctx.text(c.child_by_field_name("name")))
            elif c.type == "wildcard_import":
                names.append("*")
        ctx.add_ref("import", ctx.text(mod), n, names=names)
    return True


def py_expr_stmt(ctx, n):
    if n.named_children and n.named_children[0].type == "assignment":
        a = n.named_children[0]
        left, typ, right = a.child_by_field_name("left"), a.child_by_field_name("type"), a.child_by_field_name("right")
        if left is not None and left.type == "identifier":
            if ctx.top()["kind"] == "file":
                ctx.add_node("variable", ctx.text(left), n,
                             signature=f"{ctx.text(left)}: {ctx.text(typ)}" if typ is not None else ctx.text(left))
            elif typ is not None:
                ctx.declare(ctx.text(left), ctx.text(typ))
            elif right is not None and right.type == "call":
                fn = right.child_by_field_name("function")
                t = base_type_name(ctx.text(fn)) if fn is not None else ""
                if t[:1].isupper():
                    ctx.declare(ctx.text(left), t)
    return False


PY_HANDLERS = {
    "decorated_definition": py_decorated,
    "class_definition": py_class,
    "function_definition": py_function,
    "call": py_call,
    "import_statement": py_import,
    "import_from_statement": py_import,
    "expression_statement": py_expr_stmt,
}


# ----------------------------------------------------------------------------------------------
# JavaScript / TypeScript / TSX
# ----------------------------------------------------------------------------------------------
def js_decorator(ctx, n):
    ctx.pending_annotations.append(ctx.text(n).lstrip("@").strip())
    return True


def js_heritage(ctx, heritage):
    for h in heritage.named_children:
        if h.type == "extends_clause":
            for v in h.named_children:
                if v.type != "type_arguments":
                    ctx.add_ref("extends", base_type_name(ctx.text(v)), v)
        elif h.type == "implements_clause":
            for v in h.named_children:
                ctx.add_ref("implements", base_type_name(ctx.text(v)), v)
        elif h.type in ("identifier", "member_expression", "call_expression"):
            ctx.add_ref("extends", base_type_name(ctx.text(h)), h)


def js_class(ctx, n):
    name_n = n.child_by_field_name("name")
    name = ctx.text(name_n) if name_n is not None else "<anonymous>"
    node = ctx.add_node("class", name, n, signature=f"class {name}")
    ctx.push(node)
    for c in n.children:
        if c.type == "class_heritage":
            js_heritage(ctx, c)
    ctx.walk_children(n.child_by_field_name("body"))
    ctx.pop()
    return True


def js_interface(ctx, n):
    name = ctx.text(n.child_by_field_name("name"))
    node = ctx.add_node("interface", name, n, signature=f"interface {name}")
    ctx.push(node)
    for c in n.children:
        if c.type == "extends_type_clause":
            for v in c.named_children:
                ctx.add_ref("extends", base_type_name(ctx.text(v)), v)
    ctx.walk_children(n.child_by_field_name("body"))
    ctx.pop()
    return True


def js_declare_params(ctx, params_node, as_fields=False):
    """Declare typed parameters into scope; TS constructor parameter properties become fields."""
    for p in (params_node.named_children if params_node is not None else []):
        if p.type not in ("required_parameter", "optional_parameter"):
            continue
        pat, typ = p.child_by_field_name("pattern"), p.child_by_field_name("type")
        if pat is None or pat.type != "identifier":
            continue
        ttxt = ctx.text(typ)[1:].strip() if typ is not None else None
        ctx.declare(ctx.text(pat), ttxt)
        if as_fields and any(c.type == "accessibility_modifier" or ctx.text(c) in ("readonly",) for c in p.children):
            cls = ctx.stack[-2]
            ctx.stack.append(cls)
            ctx.add_node("field", ctx.text(pat), p, signature=f"{ctx.text(pat)}: {ttxt}" if ttxt else ctx.text(pat),
                         type_text=ttxt, extra={"type": ttxt, "parameter_property": True})
            ctx.stack.pop()


def js_method(ctx, n):
    name = ctx.text(n.child_by_field_name("name"))
    params_n = n.child_by_field_name("parameters")
    params = ctx.text(params_n)
    ret = n.child_by_field_name("return_type")
    sig = f"{name}{params}" + (ctx.text(ret) if ret is not None else "")
    node = ctx.add_node("method", name, n, signature=sig, type_text=params + (ctx.text(ret) if ret is not None else ""))
    ctx.push(node)
    ctx.push_scope()
    js_declare_params(ctx, params_n, as_fields=(name == "constructor"))
    ctx.walk_children(n.child_by_field_name("body"))
    ctx.pop_scope()
    ctx.pop()
    return True


def js_field(ctx, n):
    name = ctx.text(n.child_by_field_name("name"))
    typ = n.child_by_field_name("type")
    val = n.child_by_field_name("value")
    if val is not None and val.type in ("arrow_function", "function", "function_expression"):
        params = ctx.text(val.child_by_field_name("parameters")) or "()"
        node = ctx.add_node("method", name, n, signature=f"{name} = {params} =>")
        ctx.push(node)
        ctx.walk_children(val.child_by_field_name("body"))
        ctx.pop()
        return True
    sig = f"{name}{ctx.text(typ)}" if typ is not None else name
    ttxt = ctx.text(typ)[1:].strip() if typ is not None else (base_type_name(ctx.text(val.child_by_field_name("constructor"))) if val is not None and val.type == "new_expression" else None)
    ctx.add_node("field", name, n, signature=sig, type_text=ttxt, extra={"type": ttxt})
    return False


def js_function(ctx, n):
    name = ctx.text(n.child_by_field_name("name"))
    params_n = n.child_by_field_name("parameters")
    params = ctx.text(params_n)
    ret = n.child_by_field_name("return_type")
    node = ctx.add_node("function", name, n, signature=f"function {name}{params}" + (ctx.text(ret) if ret is not None else ""),
                        type_text=params + (ctx.text(ret) if ret is not None else ""))
    ctx.push(node)
    ctx.push_scope()
    js_declare_params(ctx, params_n)
    ctx.walk_children(n.child_by_field_name("body"))
    ctx.pop_scope()
    ctx.pop()
    return True


def js_variable_decl(ctx, n):
    handled = False
    for d in n.named_children:
        if d.type != "variable_declarator":
            continue
        name_n, val = d.child_by_field_name("name"), d.child_by_field_name("value")
        if name_n is None:
            continue
        if name_n.type != "identifier":  # destructuring: const { a } = require('x') / = obj
            if val is not None:
                ctx.walk(val)
            handled = True
            continue
        name = ctx.text(name_n)
        typ = d.child_by_field_name("type")
        if typ is not None:
            ctx.declare(name, ctx.text(typ)[1:].strip())
        elif val is not None and val.type == "new_expression" and val.child_by_field_name("constructor") is not None:
            ctx.declare(name, base_type_name(ctx.text(val.child_by_field_name("constructor"))))
        if val is not None and val.type in ("arrow_function", "function", "function_expression", "generator_function"):
            params_n = val.child_by_field_name("parameters")
            params = ctx.text(params_n) or "()"
            ret = val.child_by_field_name("return_type")
            node = ctx.add_node("function", name, n, signature=f"const {name} = {params}" + (ctx.text(ret) if ret is not None else "") + " =>",
                                type_text=params + (ctx.text(ret) if ret is not None else ""))
            ctx.push(node)
            ctx.push_scope()
            js_declare_params(ctx, params_n)
            ctx.walk_children(val.child_by_field_name("body"))
            ctx.pop_scope()
            ctx.pop()
            handled = True
        elif ctx.top()["kind"] == "file":
            typ = d.child_by_field_name("type")
            ctx.add_node("variable", name, n, signature=f"{name}{ctx.text(typ)}" if typ is not None else name)
            if val is not None:
                ctx.walk(val)
            handled = True
    return handled


def js_call(ctx, n):
    fn = n.child_by_field_name("function")
    if fn is not None:
        if fn.type == "identifier":
            name = ctx.text(fn)
            if name == "require":
                args = n.child_by_field_name("arguments")
                if args is not None and args.named_children and args.named_children[0].type == "string":
                    ctx.add_ref("import", strip_quotes(ctx.text(args.named_children[0])), n, names=[])
                    return True
            ctx.add_ref("call", name, n)
        elif fn.type == "member_expression":
            ctx.add_ref("call", ctx.text(fn.child_by_field_name("property")), n, hint=ctx.text(fn.child_by_field_name("object")))
    return False


def js_new(ctx, n):
    c = n.child_by_field_name("constructor")
    if c is not None:
        ctx.add_ref("instantiates", base_type_name(ctx.text(c)), n)
    return False


def js_import(ctx, n):
    src = n.child_by_field_name("source")
    if src is None:
        return True
    names = []
    for c in n.named_children:
        if c.type == "import_clause":
            for x in c.named_children:
                if x.type == "identifier":
                    names.append(ctx.text(x))
                elif x.type == "named_imports":
                    for s in x.named_children:
                        if s.type == "import_specifier":
                            names.append(ctx.text(s.child_by_field_name("name")))
                elif x.type == "namespace_import":
                    names.append("*")
    ctx.add_ref("import", strip_quotes(ctx.text(src)), n, names=names)
    return True


def js_simple(kind, sigword):
    def h(ctx, n):
        name = ctx.text(n.child_by_field_name("name"))
        node = ctx.add_node(kind, name, n, signature=f"{sigword} {name}")
        ctx.push(node)
        ctx.walk_children(n.child_by_field_name("body"))
        ctx.pop()
        return True
    return h


def ts_signature(ctx, n):
    name = ctx.text(n.child_by_field_name("name"))
    if n.type in ("method_signature", "abstract_method_signature"):
        ret = n.child_by_field_name("return_type")
        ptxt = ctx.text(n.child_by_field_name("parameters"))
        ctx.add_node("method", name, n, signature=f"{name}{ptxt}" + (ctx.text(ret) if ret is not None else ""),
                     type_text=ptxt + (ctx.text(ret) if ret is not None else ""))
    else:
        typ = n.child_by_field_name("type")
        ttxt = ctx.text(typ)[1:].strip() if typ is not None else None
        ctx.add_node("field", name, n, signature=f"{name}{ctx.text(typ)}" if typ is not None else name,
                     type_text=ttxt, extra={"type": ttxt})
    return True


JS_HANDLERS = {
    "decorator": js_decorator,
    "class_declaration": js_class, "class": js_class, "abstract_class_declaration": js_class,
    "interface_declaration": js_interface,
    "method_definition": js_method,
    "public_field_definition": js_field, "field_definition": js_field,
    "property_signature": ts_signature, "method_signature": ts_signature, "abstract_method_signature": ts_signature,
    "function_declaration": js_function, "generator_function_declaration": js_function,
    "lexical_declaration": js_variable_decl, "variable_declaration": js_variable_decl,
    "call_expression": js_call,
    "new_expression": js_new,
    "import_statement": js_import,
    "type_alias_declaration": js_simple("type", "type"),
    "enum_declaration": js_simple("enum", "enum"),
    "module": js_simple("module", "namespace"), "internal_module": js_simple("module", "namespace"),
}


# ----------------------------------------------------------------------------------------------
# Go
# ----------------------------------------------------------------------------------------------
GO_RECEIVER_RE = re.compile(r"\*?\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:\[[^\]]*\])?\s*\)\s*$")


def go_declare_params(ctx, params_node):
    for p in (params_node.named_children if params_node is not None else []):
        if p.type in ("parameter_declaration", "variadic_parameter_declaration"):
            typ = p.child_by_field_name("type")
            for nm in p.children_by_field_name("name"):
                ctx.declare(ctx.text(nm), ctx.text(typ))


def go_function(ctx, n):
    name = ctx.text(n.child_by_field_name("name"))
    params_n = n.child_by_field_name("parameters")
    params = ctx.text(params_n)
    res = n.child_by_field_name("result")
    sig = f"func {name}{params}" + (f" {ctx.text(res)}" if res is not None else "")
    node = ctx.add_node("function", name, n, signature=sig, type_text=params + (ctx.text(res) if res is not None else ""))
    ctx.push(node)
    ctx.push_scope()
    go_declare_params(ctx, params_n)
    ctx.walk_children(n.child_by_field_name("body"))
    ctx.pop_scope()
    ctx.pop()
    return True


def go_method(ctx, n):
    name = ctx.text(n.child_by_field_name("name"))
    recv = ctx.text(n.child_by_field_name("receiver"))
    m = GO_RECEIVER_RE.search(recv)
    rtype = m.group(1) if m else None
    params_n = n.child_by_field_name("parameters")
    params = ctx.text(params_n)
    res = n.child_by_field_name("result")
    sig = f"func {recv} {name}{params}" + (f" {ctx.text(res)}" if res is not None else "")
    node = ctx.add_node("method", name, n, signature=sig, qname=f"{rtype}.{name}" if rtype else name,
                        extra={"receiver_type": rtype}, type_text=params + (ctx.text(res) if res is not None else ""))
    ctx.push(node)
    ctx.push_scope()
    go_declare_params(ctx, n.child_by_field_name("receiver"))
    go_declare_params(ctx, params_n)
    ctx.walk_children(n.child_by_field_name("body"))
    ctx.pop_scope()
    ctx.pop()
    return True


def go_short_var(ctx, n):
    """x := T{...} / x := &T{...} / var x T  -> declare x as T for call resolution."""
    left, right = n.child_by_field_name("left"), n.child_by_field_name("right")
    if left is None or right is None:
        return False
    names = [ctx.text(c) for c in left.named_children if c.type == "identifier"]
    vals = list(right.named_children)
    for nm, v in zip(names, vals):
        if v.type == "unary_expression" and v.named_children:
            v = v.named_children[0]
        if v.type == "composite_literal" and v.child_by_field_name("type") is not None:
            ctx.declare(nm, ctx.text(v.child_by_field_name("type")))
    return False


def go_type_decl(ctx, n):
    for spec in n.named_children:
        if spec.type != "type_spec":
            continue
        name = ctx.text(spec.child_by_field_name("name"))
        typ = spec.child_by_field_name("type")
        if typ is None:
            continue
        if typ.type == "struct_type":
            node = ctx.add_node("struct", name, spec, signature=f"type {name} struct")
            ctx.push(node)
            for fl in typ.named_children:
                if fl.type != "field_declaration_list":
                    continue
                for fd in fl.named_children:
                    if fd.type != "field_declaration":
                        continue
                    ftype = fd.child_by_field_name("type")
                    names = [ctx.text(c) for c in fd.children_by_field_name("name")]
                    if not names:  # embedded struct/interface
                        ctx.add_ref("extends", base_type_name(ctx.text(ftype)), fd)
                        continue
                    for fname in names:
                        ctx.add_node("field", fname, fd, signature=f"{fname} {ctx.text(ftype)}", type_text=ctx.text(ftype), extra={"type": ctx.text(ftype)})
            ctx.pop()
        elif typ.type == "interface_type":
            node = ctx.add_node("interface", name, spec, signature=f"type {name} interface")
            ctx.push(node)
            for el in typ.named_children:
                if el.type in ("method_elem", "method_spec"):
                    mname = ctx.text(el.child_by_field_name("name"))
                    res = el.child_by_field_name("result")
                    ptxt = ctx.text(el.child_by_field_name("parameters"))
                    ctx.add_node("method", mname, el, signature=f"{mname}{ptxt}" + (f" {ctx.text(res)}" if res is not None else ""),
                                 type_text=ptxt + (ctx.text(res) if res is not None else ""))
                elif el.type in ("type_identifier", "qualified_type", "type_elem"):
                    ctx.add_ref("extends", base_type_name(ctx.text(el)), el)
            ctx.pop()
        else:
            ctx.add_node("type", name, spec, signature=f"type {name} {ctx.text(typ)[:60]}")
    return True


def go_call(ctx, n):
    fn = n.child_by_field_name("function")
    if fn is not None:
        if fn.type == "identifier":
            ctx.add_ref("call", ctx.text(fn), n)
        elif fn.type == "selector_expression":
            ctx.add_ref("call", ctx.text(fn.child_by_field_name("field")), n, hint=ctx.text(fn.child_by_field_name("operand")))
    return False


def go_import(ctx, n):
    stack = [n]
    while stack:
        x = stack.pop()
        if x.type == "import_spec":
            p = x.child_by_field_name("path")
            ctx.add_ref("import", strip_quotes(ctx.text(p)), x, names=[])
        else:
            stack.extend(x.named_children)
    return True


def go_composite(ctx, n):
    t = n.child_by_field_name("type")
    if t is not None and t.type in ("type_identifier", "qualified_type"):
        ctx.add_ref("instantiates", base_type_name(ctx.text(t)), n)
    return False


def go_package(ctx, n):
    ctx.file_extra["package"] = ctx.text(n.named_children[0]) if n.named_children else ""
    return True


def go_var_decl(ctx, n):
    if ctx.top()["kind"] != "file":
        return False
    for spec in n.named_children:
        if spec.type in ("var_spec", "const_spec"):
            for nm in spec.children_by_field_name("name"):
                ctx.add_node("variable", ctx.text(nm), spec, signature=ctx.text(spec)[:80].split("\n")[0])
    return False


def go_var_in_func(ctx, n):
    if ctx.top()["kind"] == "file":
        return go_var_decl(ctx, n)
    for spec in n.named_children:
        if spec.type == "var_spec" and spec.child_by_field_name("type") is not None:
            for nm in spec.children_by_field_name("name"):
                ctx.declare(ctx.text(nm), ctx.text(spec.child_by_field_name("type")))
    return False


GO_HANDLERS = {
    "function_declaration": go_function,
    "method_declaration": go_method,
    "type_declaration": go_type_decl,
    "call_expression": go_call,
    "import_declaration": go_import,
    "composite_literal": go_composite,
    "package_clause": go_package,
    "var_declaration": go_var_in_func, "const_declaration": go_var_decl,
    "short_var_declaration": go_short_var,
}


# ----------------------------------------------------------------------------------------------
# Java
# ----------------------------------------------------------------------------------------------
def java_annotations(ctx, n):
    out = []
    for c in n.children:
        if c.type == "modifiers":
            for m in c.children:
                if m.type in ("marker_annotation", "annotation"):
                    out.append(ctx.text(m).lstrip("@"))
    return out


def java_type_decl(kind):
    def h(ctx, n):
        name = ctx.text(n.child_by_field_name("name"))
        node = ctx.add_node(kind, name, n, signature=f"{kind} {name}", annotations=java_annotations(ctx, n))
        ctx.push(node)
        sc = n.child_by_field_name("superclass")
        if sc is not None:
            for t in sc.named_children:
                ctx.add_ref("extends", base_type_name(ctx.text(t)), t)
        itf = n.child_by_field_name("interfaces")
        if itf is not None:
            for tl in itf.named_children:
                for t in (tl.named_children if tl.type == "type_list" else [tl]):
                    ctx.add_ref("implements", base_type_name(ctx.text(t)), t)
        for c in n.children:
            if c.type == "extends_interfaces":
                for tl in c.named_children:
                    for t in (tl.named_children if tl.type == "type_list" else [tl]):
                        ctx.add_ref("extends", base_type_name(ctx.text(t)), t)
        if kind == "record":
            params = n.child_by_field_name("parameters")
            for p in (params.named_children if params is not None else []):
                if p.type == "formal_parameter":
                    ctx.add_node("field", ctx.text(p.child_by_field_name("name")), p, signature=ctx.text(p),
                                 type_text=ctx.text(p.child_by_field_name("type")), extra={"type": ctx.text(p.child_by_field_name("type"))})
        ctx.walk_children(n.child_by_field_name("body"))
        ctx.pop()
        return True
    return h


def java_method(ctx, n):
    name = ctx.text(n.child_by_field_name("name"))
    params_n = n.child_by_field_name("parameters")
    params = ctx.text(params_n)
    ret = n.child_by_field_name("type")
    kind = "constructor" if n.type == "constructor_declaration" else "method"
    sig = f"{name}{params}" + (f" -> {ctx.text(ret)}" if ret is not None else "")
    node = ctx.add_node(kind, name, n, signature=sig, annotations=java_annotations(ctx, n),
                        type_text=params + (ctx.text(ret) if ret is not None else ""))
    ctx.push(node)
    ctx.push_scope()
    for p in (params_n.named_children if params_n is not None else []):
        if p.type in ("formal_parameter", "spread_parameter"):
            ctx.declare(ctx.text(p.child_by_field_name("name")), ctx.text(p.child_by_field_name("type")))
    ctx.walk_children(n.child_by_field_name("body"))
    ctx.pop_scope()
    ctx.pop()
    return True


def java_local_var(ctx, n):
    typ = n.child_by_field_name("type")
    for d in n.named_children:
        if d.type == "variable_declarator":
            ctx.declare(ctx.text(d.child_by_field_name("name")), ctx.text(typ))
    return False


def java_enhanced_for(ctx, n):
    ctx.declare(ctx.text(n.child_by_field_name("name")), ctx.text(n.child_by_field_name("type")))
    return False


def java_field(ctx, n):
    typ = ctx.text(n.child_by_field_name("type"))
    ann = java_annotations(ctx, n)
    for d in n.named_children:
        if d.type == "variable_declarator":
            name = ctx.text(d.child_by_field_name("name"))
            ctx.add_node("field", name, n, signature=f"{name}: {typ}", annotations=ann, type_text=typ, extra={"type": typ})
    return False


def java_invocation(ctx, n):
    obj = n.child_by_field_name("object")
    ctx.add_ref("call", ctx.text(n.child_by_field_name("name")), n, hint=ctx.text(obj) if obj is not None else None)
    return False


def java_new(ctx, n):
    t = n.child_by_field_name("type")
    if t is not None:
        ctx.add_ref("instantiates", base_type_name(ctx.text(t)), n)
    return False


def java_import(ctx, n):
    txt = ctx.text(n).strip().rstrip(";")
    txt = re.sub(r"^import\s+(static\s+)?", "", txt).strip()
    wildcard = txt.endswith(".*")
    ctx.add_ref("import", txt[:-2] if wildcard else txt, n, names=["*"] if wildcard else [txt.split(".")[-1]])
    return True


def java_package(ctx, n):
    ctx.file_extra["package"] = ctx.text(n).replace("package", "").strip().rstrip(";")
    return True


JAVA_HANDLERS = {
    "class_declaration": java_type_decl("class"),
    "interface_declaration": java_type_decl("interface"),
    "enum_declaration": java_type_decl("enum"),
    "record_declaration": java_type_decl("record"),
    "annotation_type_declaration": java_type_decl("annotation"),
    "method_declaration": java_method, "constructor_declaration": java_method,
    "field_declaration": java_field,
    "local_variable_declaration": java_local_var,
    "enhanced_for_statement": java_enhanced_for,
    "method_invocation": java_invocation,
    "object_creation_expression": java_new,
    "import_declaration": java_import,
    "package_declaration": java_package,
}


# ----------------------------------------------------------------------------------------------
# Rust
# ----------------------------------------------------------------------------------------------
def rs_attribute(ctx, n):
    ctx.pending_annotations.append(ctx.text(n).strip("#[]"))
    return True


def rs_function(ctx, n):
    name = ctx.text(n.child_by_field_name("name"))
    params = ctx.text(n.child_by_field_name("parameters"))
    ret = n.child_by_field_name("return_type")
    kind = "method" if ctx.top()["kind"] in ("impl", "trait") else "function"
    node = ctx.add_node(kind, name, n, signature=f"fn {name}{params}" + (f" -> {ctx.text(ret)}" if ret is not None else ""),
                        type_text=params + (ctx.text(ret) if ret is not None else ""))
    ctx.push(node)
    ctx.push_scope()
    pn = n.child_by_field_name("parameters")
    for p in (pn.named_children if pn is not None else []):
        if p.type == "parameter" and p.child_by_field_name("pattern") is not None:
            ctx.declare(ctx.text(p.child_by_field_name("pattern")), ctx.text(p.child_by_field_name("type")))
        elif p.type == "self_parameter" and kind == "method":
            ctx.declare("self", ctx.stack[-2]["qname"])
    ctx.walk_children(n.child_by_field_name("body"))
    ctx.pop_scope()
    ctx.pop()
    return True


def rs_let(ctx, n):
    pat, typ, val = n.child_by_field_name("pattern"), n.child_by_field_name("type"), n.child_by_field_name("value")
    if pat is None or pat.type != "identifier":
        return False
    if typ is not None:
        ctx.declare(ctx.text(pat), ctx.text(typ))
    elif val is not None and val.type == "struct_expression":
        ctx.declare(ctx.text(pat), ctx.text(val.child_by_field_name("name")))
    elif val is not None and val.type == "call_expression":
        fn = val.child_by_field_name("function")
        if fn is not None and fn.type == "scoped_identifier" and fn.child_by_field_name("path") is not None:
            ctx.declare(ctx.text(pat), base_type_name(ctx.text(fn.child_by_field_name("path"))))
    return False


def rs_impl(ctx, n):
    typ = base_type_name(ctx.text(n.child_by_field_name("type")))
    trait = n.child_by_field_name("trait")
    sig = f"impl {ctx.text(trait)} for {typ}" if trait is not None else f"impl {typ}"
    node = ctx.add_node("impl", sig, n, signature=sig, qname=typ)
    ctx.push(node)
    if trait is not None:
        ctx.add_ref("implements", base_type_name(ctx.text(trait)), trait)
    ctx.walk_children(n.child_by_field_name("body"))
    ctx.pop()
    return True


def rs_struct(ctx, n):
    name = ctx.text(n.child_by_field_name("name"))
    node = ctx.add_node("struct", name, n, signature=f"struct {name}")
    ctx.push(node)
    body = n.child_by_field_name("body")
    for fd in (body.named_children if body is not None else []):
        if fd.type == "field_declaration":
            fname = ctx.text(fd.child_by_field_name("name"))
            ftype = ctx.text(fd.child_by_field_name("type"))
            ctx.add_node("field", fname, fd, signature=f"{fname}: {ftype}", type_text=ftype, extra={"type": ftype})
    ctx.pop()
    return True


def rs_named(kind, word):
    def h(ctx, n):
        name = ctx.text(n.child_by_field_name("name"))
        node = ctx.add_node(kind, name, n, signature=f"{word} {name}")
        ctx.push(node)
        ctx.walk_children(n.child_by_field_name("body"))
        ctx.pop()
        return True
    return h


def rs_call(ctx, n):
    fn = n.child_by_field_name("function")
    if fn is None:
        return False
    if fn.type == "identifier":
        ctx.add_ref("call", ctx.text(fn), n)
    elif fn.type == "scoped_identifier":
        ctx.add_ref("call", ctx.text(fn.child_by_field_name("name")), n, hint=ctx.text(fn.child_by_field_name("path")))
    elif fn.type == "field_expression":
        ctx.add_ref("call", ctx.text(fn.child_by_field_name("field")), n, hint=ctx.text(fn.child_by_field_name("value")))
    elif fn.type == "generic_function":
        inner = fn.child_by_field_name("function")
        ctx.add_ref("call", base_type_name(ctx.text(inner)), n)
    return False


def rs_use(ctx, n):
    arg = n.child_by_field_name("argument")
    ctx.add_ref("import", ctx.text(arg), n, names=[])
    return True


def rs_struct_expr(ctx, n):
    ctx.add_ref("instantiates", base_type_name(ctx.text(n.child_by_field_name("name"))), n)
    return False


def rs_macro(ctx, n):
    ctx.add_ref("call", ctx.text(n.child_by_field_name("macro")) + "!", n)
    return False


RS_HANDLERS = {
    "attribute_item": rs_attribute,
    "function_item": rs_function, "function_signature_item": rs_function,
    "impl_item": rs_impl,
    "struct_item": rs_struct,
    "enum_item": rs_named("enum", "enum"),
    "trait_item": rs_named("trait", "trait"),
    "mod_item": rs_named("module", "mod"),
    "type_item": rs_named("type", "type"),
    "call_expression": rs_call,
    "let_declaration": rs_let,
    "use_declaration": rs_use,
    "struct_expression": rs_struct_expr,
    "macro_invocation": rs_macro,
}


# ----------------------------------------------------------------------------------------------
# Terraform / HCL
# ----------------------------------------------------------------------------------------------
HCL_REF_RE = re.compile(
    r"\b(?:(var|local|module|data)\.([\w\-]+)(?:\.([\w\-]+))?"
    r"|([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\.([\w\-]+))"
)
HCL_SKIP_PREFIX = {"path", "terraform", "each", "count", "self"}


def hcl_refs_from_text(ctx, txt, tsnode, kind="references"):
    seen = set()
    for m in HCL_REF_RE.finditer(txt):
        if m.group(1):
            p, a, b = m.group(1), m.group(2), m.group(3)
            name = f"data.{a}.{b}" if p == "data" and b else f"{p}.{a}"
            attr = None if p == "data" else b
        else:
            name, attr = f"{m.group(4)}.{m.group(5)}", None
        key = (name, attr)
        if key in seen:
            continue
        seen.add(key)
        ctx.add_ref(kind, name, tsnode, attr=attr)


def hcl_attrs(ctx, body):
    """Yield (key, expression_node) for attributes directly in a body."""
    for c in (body.named_children if body is not None else []):
        if c.type == "attribute":
            key = ctx.text(c.named_children[0]) if c.named_children else ""
            expr = c.named_children[1] if len(c.named_children) > 1 else None
            yield key, expr


def hcl_walk_body_refs(ctx, body):
    for c in (body.named_children if body is not None else []):
        if c.type == "attribute":
            key = ctx.text(c.named_children[0]) if c.named_children else ""
            expr_txt = ctx.text(c.named_children[1]) if len(c.named_children) > 1 else ""
            if key == "depends_on":
                hcl_refs_from_text(ctx, expr_txt, c, kind="depends_on")
            else:
                hcl_refs_from_text(ctx, expr_txt, c)
        elif c.type == "block":
            for b in c.named_children:
                if b.type == "body":
                    hcl_walk_body_refs(ctx, b)


def hcl_block(ctx, n):
    if ctx.top()["kind"] != "file":
        return False
    btype, labels, body = None, [], None
    for c in n.named_children:
        if c.type == "identifier" and btype is None:
            btype = ctx.text(c)
        elif c.type == "string_lit":
            labels.append(strip_quotes(ctx.text(c)))
        elif c.type == "body":
            body = c
    attrs = {k: ctx.text(e) for k, e in hcl_attrs(ctx, body)}
    sig = " ".join([btype] + [f'"{l}"' for l in labels])
    if btype == "resource" and len(labels) >= 2:
        node = ctx.add_node("resource", f"{labels[0]}.{labels[1]}", n, signature=sig, extra={"type": labels[0]})
    elif btype == "data" and len(labels) >= 2:
        node = ctx.add_node("data", f"data.{labels[0]}.{labels[1]}", n, signature=sig, extra={"type": labels[0]})
    elif btype == "module" and labels:
        node = ctx.add_node("module_call", f"module.{labels[0]}", n, signature=sig,
                            extra={"source": strip_quotes(attrs.get("source", "")), "version": strip_quotes(attrs.get("version", ""))})
        ctx.push(node)
        ctx.add_ref("uses_module", strip_quotes(attrs.get("source", "")), n, module_name=labels[0])
        ctx.pop()
    elif btype == "variable" and labels:
        node = ctx.add_node("variable", f"var.{labels[0]}", n, signature=f'variable "{labels[0]}"' + (f" ({attrs['type'].strip()})" if "type" in attrs else ""),
                            extra={"type": attrs.get("type", "").strip(), "description": strip_quotes(attrs.get("description", ""))[:120]})
    elif btype == "output" and labels:
        node = ctx.add_node("output", f"output.{labels[0]}", n, signature=sig,
                            extra={"description": strip_quotes(attrs.get("description", ""))[:120]})
    elif btype == "provider" and labels:
        node = ctx.add_node("provider", f"provider.{labels[0]}", n, signature=sig, extra={"alias": strip_quotes(attrs.get("alias", ""))})
    elif btype == "locals":
        for k, e in hcl_attrs(ctx, body):
            node = ctx.add_node("local", f"local.{k}", n if e is None else e.parent, signature=f"local.{k}")
            ctx.push(node)
            hcl_refs_from_text(ctx, ctx.text(e), e.parent if e is not None else n)
            ctx.pop()
        return True
    elif btype == "terraform":
        rp = []
        for c in (body.named_children if body is not None else []):
            if c.type == "block" and c.named_children and ctx.text(c.named_children[0]) == "required_providers":
                for k, _ in hcl_attrs(ctx, next((b for b in c.named_children if b.type == "body"), None)):
                    rp.append(k)
            if c.type == "block" and c.named_children and ctx.text(c.named_children[0]) == "backend":
                ctx.file_extra["backend"] = strip_quotes(ctx.text(c.named_children[1])) if len(c.named_children) > 1 else ""
        ctx.add_node("terraform", "terraform", n, signature="terraform", extra={"required_providers": rp, "required_version": strip_quotes(attrs.get("required_version", ""))})
        return True
    else:
        return False
    ctx.push(node)
    hcl_walk_body_refs(ctx, body)
    ctx.pop()
    return True


HCL_HANDLERS = {"block": hcl_block}


# ----------------------------------------------------------------------------------------------
# YAML (Kubernetes manifests, kustomization, Helm values)
# ----------------------------------------------------------------------------------------------
def yaml_to_py(ctx, n):
    """Convert a tree-sitter YAML subtree into Python data (scalars become strings)."""
    t = n.type
    if t in ("stream", "document", "block_node", "flow_node"):
        vals = [yaml_to_py(ctx, c) for c in n.named_children if c.type not in ("comment", "anchor", "tag")]
        vals = [v for v in vals if v is not None]
        if t == "stream":
            return vals
        return vals[0] if vals else None
    if t in ("block_mapping", "flow_mapping"):
        out = {}
        for p in n.named_children:
            if p.type in ("block_mapping_pair", "flow_pair"):
                k = p.child_by_field_name("key")
                v = p.child_by_field_name("value")
                key = yaml_to_py(ctx, k) if k is not None else None
                if isinstance(key, str):
                    out[key] = yaml_to_py(ctx, v) if v is not None else None
        return out
    if t in ("block_sequence", "flow_sequence"):
        out = []
        for it in n.named_children:
            if it.type == "block_sequence_item":
                out.append(yaml_to_py(ctx, it.named_children[0]) if it.named_children else None)
            elif it.type not in ("comment",):
                out.append(yaml_to_py(ctx, it))
        return out
    if t in ("plain_scalar", "single_quote_scalar", "double_quote_scalar", "block_scalar"):
        txt = ctx.text(n)
        if t == "block_scalar":
            return txt.split("\n", 1)[1] if "\n" in txt else ""
        return strip_quotes(txt)
    if t in ("string_scalar", "integer_scalar", "float_scalar", "boolean_scalar", "null_scalar"):
        return strip_quotes(ctx.text(n))
    if t == "alias":
        return ctx.text(n)
    if n.named_children:
        vals = [yaml_to_py(ctx, c) for c in n.named_children if c.type != "comment"]
        vals = [v for v in vals if v is not None]
        return vals[0] if vals else None
    return ctx.text(n) if n.is_named else None


K8S_WORKLOADS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob", "ReplicaSet", "Pod"}
K8S_NAME_PARENT_KIND = {
    "configMapRef": "ConfigMap", "configMapKeyRef": "ConfigMap", "configMap": "ConfigMap",
    "secretRef": "Secret", "secretKeyRef": "Secret", "secret": "Secret",
    "service": "Service", "persistentVolumeClaim": "PersistentVolumeClaim",
    "serviceAccount": "ServiceAccount",
}
K8S_KEY_KIND = {
    "serviceAccountName": "ServiceAccount", "secretName": "Secret", "claimName": "PersistentVolumeClaim",
    "serviceName": "Service", "storageClassName": "StorageClass", "ingressClassName": "IngressClass",
    "priorityClassName": "PriorityClass", "runtimeClassName": "RuntimeClass",
}


def k8s_collect(ctx, data, doc_node, out, parent_key=None):
    """Walk a manifest and collect references, images, and template labels."""
    if isinstance(data, dict):
        if parent_key is not None and "kind" in data and "name" in data and isinstance(data.get("kind"), str):
            out["refs"].append((f"{data['kind']}/{data['name']}", parent_key))
        for k, v in data.items():
            if k == "name" and parent_key in K8S_NAME_PARENT_KIND and isinstance(v, str):
                out["refs"].append((f"{K8S_NAME_PARENT_KIND[parent_key]}/{v}", parent_key))
            elif k in K8S_KEY_KIND and isinstance(v, str):
                out["refs"].append((f"{K8S_KEY_KIND[k]}/{v}", k))
            elif k == "image" and isinstance(v, str):
                out["images"].append(v)
            k8s_collect(ctx, v, doc_node, out, parent_key=k)
    elif isinstance(data, list):
        for it in data:
            k8s_collect(ctx, it, doc_node, out, parent_key=parent_key)


def yaml_file(ctx, root):
    if b"{{" in ctx.src:  # Helm/Go template: not valid YAML, index best-effort by regex
        kinds = re.findall(r"^kind:\s*([A-Za-z]+)", ctx.src.decode("utf-8", "replace"), flags=re.M)
        ctx.add_node("helm_template", os.path.basename(ctx.path), root, signature=f"helm template ({', '.join(dict.fromkeys(kinds)) or 'unknown kind'})",
                     extra={"kinds": list(dict.fromkeys(kinds))})
        return
    docs = [c for c in root.named_children if c.type == "document"]
    is_values = os.path.basename(ctx.path).startswith("values") and ctx.path.endswith((".yaml", ".yml"))
    for d in docs:
        data = yaml_to_py(ctx, d)
        items = data.get("items") if isinstance(data, dict) and data.get("kind") == "List" else [data]
        for obj in (items or []):
            if not isinstance(obj, dict):
                continue
            kind, api = obj.get("kind"), obj.get("apiVersion")
            meta = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
            if kind and api and isinstance(kind, str):
                name = meta.get("name") or (os.path.dirname(ctx.path) or "." if kind == "Kustomization" else "<unnamed>")
                ns = meta.get("namespace")
                spec = obj.get("spec") if isinstance(obj.get("spec"), dict) else {}
                out = {"refs": [], "images": []}
                k8s_collect(ctx, {k: v for k, v in obj.items() if k not in ("apiVersion", "kind", "metadata")}, d, out)
                selector = None
                if kind == "Service" and isinstance(spec.get("selector"), dict):
                    selector = spec["selector"]
                elif isinstance(spec.get("selector"), dict) and isinstance(spec["selector"].get("matchLabels"), dict):
                    selector = spec["selector"]["matchLabels"]
                tmpl_labels = None
                tmpl = spec.get("template") if isinstance(spec.get("template"), dict) else None
                if kind == "CronJob":
                    jt = spec.get("jobTemplate", {}).get("spec", {}) if isinstance(spec.get("jobTemplate"), dict) else {}
                    tmpl = jt.get("template") if isinstance(jt, dict) else None
                if isinstance(tmpl, dict) and isinstance(tmpl.get("metadata"), dict):
                    tmpl_labels = tmpl["metadata"].get("labels")
                labels = meta.get("labels") if isinstance(meta.get("labels"), dict) else {}
                if kind == "Pod":
                    tmpl_labels = labels
                node = ctx.add_node("k8s_object", f"{kind}/{name}", d, signature=f"{kind} {name}" + (f" (ns: {ns})" if ns else ""),
                                    extra={"apiVersion": api, "namespace": ns, "labels": labels,
                                           "images": list(dict.fromkeys(out["images"])), "selector": selector,
                                           "template_labels": tmpl_labels if isinstance(tmpl_labels, dict) else None})
                ctx.push(node)
                for target, via in dict.fromkeys(out["refs"]):
                    ctx.add_ref("references", target, d, via=via, namespace=ns)
                if kind == "Kustomization":
                    for key in ("resources", "bases", "components", "crds"):
                        for r in (obj.get(key) or []):
                            if isinstance(r, str):
                                ctx.add_ref("import", r, d, names=[])
                    for p in (obj.get("patchesStrategicMerge") or []):
                        if isinstance(p, str):
                            ctx.add_ref("import", p, d, names=[])
                ctx.pop()
            elif is_values:
                for k, v in obj.items():
                    ctx.add_node("value", k, d, signature=f"{k}: {type(v).__name__ if not isinstance(v, str) else v[:40]}")
            elif isinstance(obj, dict) and ("resources" in obj or "bases" in obj) and os.path.basename(ctx.path).startswith("kustomization"):
                node = ctx.add_node("k8s_object", "Kustomization/" + os.path.dirname(ctx.path), d, signature="Kustomization")
                ctx.push(node)
                for key in ("resources", "bases", "components"):
                    for r in (obj.get(key) or []):
                        if isinstance(r, str):
                            ctx.add_ref("import", r, d, names=[])
                ctx.pop()


HANDLERS = {
    "python": PY_HANDLERS,
    "javascript": JS_HANDLERS, "typescript": JS_HANDLERS, "tsx": JS_HANDLERS,
    "go": GO_HANDLERS,
    "java": JAVA_HANDLERS,
    "rust": RS_HANDLERS,
    "hcl": HCL_HANDLERS,
    "yaml": {},
}


# ----------------------------------------------------------------------------------------------
# File-level extraction
# ----------------------------------------------------------------------------------------------
def extract_file(path, root_dir, src=None):
    """Parse one file and return (file_node, nodes, refs, meta)."""
    lang = EXT_LANG.get(os.path.splitext(path)[1].lower())
    if not lang:
        return None
    if src is None:
        with open(os.path.join(root_dir, path), "rb") as f:
            src = f.read()
    ctx = Ctx(path, lang, src, root_dir)
    tree = parser_for(lang).parse(src)
    if lang == "yaml":
        yaml_file(ctx, tree.root_node)
    else:
        ctx.walk(tree.root_node)
    ctx.file_node["extra"] = dict(ctx.file_extra, language=lang, has_errors=tree.root_node.has_error)
    return ctx.file_node, ctx.nodes, ctx.refs


# ----------------------------------------------------------------------------------------------
# Skeleton rendering
# ----------------------------------------------------------------------------------------------
def collapse_calls(hint):
    """`DataService(DataRepository()).convert` -> `DataService().convert` (drop argument text)."""
    out, depth = [], 0
    for ch in hint:
        if ch == "(":
            if depth == 0:
                out.append("()")
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def kind_label(n):
    """Kind prefix for display, omitted when the signature already starts with a keyword."""
    sig = n["signature"]
    if sig.startswith(SIG_KEYWORDS) or sig.startswith(n["kind"] + " "):
        return ""
    return n["kind"] + " "


def render_skeleton(file_node, nodes, refs, show_calls=True, max_calls=8, show_lines=True):
    by_parent = defaultdict(list)
    for n in nodes:
        by_parent[n["parent"]].append(n)
    calls_by_src = defaultdict(list)
    for r in refs:
        if r["kind"] == "call":
            hint = collapse_calls(r.get("hint") or "")
            label = f"{hint}.{r['name']}" if hint else r["name"]
            if label not in calls_by_src[r["src"]]:
                calls_by_src[r["src"]].append(label)
    imports = [r["name"] for r in refs if r["kind"] == "import"]
    lines = []
    extra = file_node.get("extra", {})
    head = f"{file_node['file']}  ({extra.get('language', '?')}, {file_node['end_line']} lines"
    if extra.get("package"):
        head += f", package {extra['package']}"
    if extra.get("has_errors"):
        head += ", parse errors"
    lines.append(head + ")")
    if imports:
        lines.append("  imports: " + ", ".join(imports[:12]) + (f" (+{len(imports) - 12} more)" if len(imports) > 12 else ""))

    def rng(n):
        if not show_lines:
            return ""
        return f"  [L{n['line']}]" if n["line"] == n["end_line"] else f"  [L{n['line']}-{n['end_line']}]"

    def emit(parent_id, depth):
        for n in sorted(by_parent.get(parent_id, []), key=lambda x: x["line"]):
            ann = f"  @{' @'.join(a.split('(')[0] for a in n['annotations'])}" if n["annotations"] else ""
            lines.append(f"{'  ' * depth}{kind_label(n)}{n['signature']}{rng(n)}{ann}")
            if show_calls and calls_by_src.get(n["id"]):
                cl = calls_by_src[n["id"]]
                lines.append(f"{'  ' * (depth + 1)}calls: " + ", ".join(cl[:max_calls]) + (f" (+{len(cl) - max_calls} more)" if len(cl) > max_calls else ""))
            emit(n["id"], depth + 1)

    emit(file_node["id"], 1)
    if show_calls and calls_by_src.get(file_node["id"]):
        cl = calls_by_src[file_node["id"]]
        lines.append("  module-level calls: " + ", ".join(cl[:max_calls]))
    return "\n".join(lines)


def iter_source_files(root_dir, includes=None, excludes=None, extra_dirs=None):
    """Yield repo-relative paths of supported files, honoring exclude dirs and glob filters."""
    excl_dirs = set(DEFAULT_EXCLUDE_DIRS)
    excl_globs = list(excludes or [])
    seen = set()
    roots = [root_dir] + [os.path.join(root_dir, d) for d in (extra_dirs or [])]
    for base in roots:
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in excl_dirs and not any(fnmatch.fnmatch(d, g) for g in excl_globs))
            for fn in sorted(filenames):
                if os.path.splitext(fn)[1].lower() not in EXT_LANG:
                    continue
                rel = os.path.relpath(os.path.join(dirpath, fn), root_dir).replace(os.sep, "/")
                if rel in seen:
                    continue
                if includes and not any(fnmatch.fnmatch(rel, g) for g in includes):
                    continue
                if any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(fn, g) for g in excl_globs):
                    continue
                seen.add(rel)
                yield rel


def cmd_skeleton(args):
    root = os.path.abspath(args.root)
    paths = []
    for p in args.paths:
        ap = os.path.abspath(p)
        if os.path.isdir(ap):
            for rel in iter_source_files(ap, args.include, args.exclude):
                paths.append(os.path.join(ap, rel))
        else:
            paths.append(ap)
    out, raw_tok, skel_tok, payload = [], 0, 0, []
    for ap in paths:
        rel = os.path.relpath(ap, root).replace(os.sep, "/")
        try:
            with open(ap, "rb") as f:
                src = f.read()
        except OSError as e:
            out.append(f"{rel}: cannot read ({e})")
            continue
        res = extract_file(rel, root, src)
        if res is None:
            out.append(f"{rel}: unsupported extension (skipped)")
            continue
        fnode, nodes, refs = res
        txt = render_skeleton(fnode, nodes, refs, show_calls=not args.no_calls, show_lines=not args.no_lines)
        raw_tok += approx_tokens(src.decode("utf-8", "replace"))
        skel_tok += approx_tokens(txt)
        out.append(txt)
        payload.append({"file": fnode, "nodes": nodes, "refs": refs})
    if args.json:
        print(json.dumps(payload, indent=None))
        return
    print("\n\n".join(out))
    if len(paths) and not args.no_stats:
        pct = 100 - (skel_tok * 100 // raw_tok) if raw_tok else 0
        print(f"\n-- {len(paths)} file(s): raw ≈ {raw_tok:,} tokens, skeleton ≈ {skel_tok:,} tokens ({pct}% saved)")


# ----------------------------------------------------------------------------------------------
# Graph build + linking
# ----------------------------------------------------------------------------------------------
def file_hash(data):
    return hashlib.sha1(data).hexdigest()


def load_graph(path):
    with open(path) as f:
        return json.load(f)


def terraform_module_map(root_dir):
    """Map module keys -> directories from every .terraform/modules/modules.json found."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root_dir):
        dirnames[:] = [d for d in dirnames if d not in (DEFAULT_EXCLUDE_DIRS - {".terraform"})]
        if os.path.basename(dirpath) == ".terraform":
            mj = os.path.join(dirpath, "modules", "modules.json")
            if os.path.exists(mj):
                try:
                    data = json.load(open(mj))
                except (OSError, ValueError):
                    continue
                caller_dir = os.path.relpath(os.path.dirname(dirpath), root_dir).replace(os.sep, "/")
                for m in data.get("Modules", []):
                    key, d, source = m.get("Key", ""), m.get("Dir", ""), m.get("Source", "")
                    if not key or not d:
                        continue
                    rel = os.path.normpath(os.path.join(os.path.dirname(dirpath), d)).replace(os.sep, "/")
                    rel = os.path.relpath(rel, root_dir).replace(os.sep, "/")
                    out[(caller_dir if caller_dir != "." else "", key)] = {"dir": rel, "source": source}
            dirnames[:] = []
    return out


def resolve_import(ref, file_path, lang, files, root_dir, go_module=None):
    """Return a repo-relative file path (or directory for packages) an import points at, else None."""
    name = ref["name"]
    d = os.path.dirname(file_path)
    if lang == "python":
        if name.startswith("."):
            dots = len(name) - len(name.lstrip("."))
            base = d
            for _ in range(dots - 1):
                base = os.path.dirname(base)
            mod = name.lstrip(".").replace(".", "/")
            cands = [os.path.normpath(os.path.join(base, mod)) if mod else base]
        else:
            mod = name.replace(".", "/")
            cands = [mod, f"src/{mod}", os.path.normpath(os.path.join(d, mod))]
            # also look for a source root: any file dir that ends with the first package segment
            first = mod.split("/")[0]
            for fp in files:
                parts = fp.split("/")
                if first in parts[:-1]:
                    idx = parts.index(first)
                    cands.append("/".join(parts[:idx] + [mod]))
                    break
        for c in cands:
            c = c.replace(os.sep, "/").lstrip("./") or "."
            for suffix in (".py", "/__init__.py"):
                if c + suffix in files:
                    return c + suffix
        # `from pkg.mod import name` where name is itself a module
        for nm in ref.get("names", []):
            for c in cands:
                for suffix in (f"/{nm}.py", f"/{nm}/__init__.py"):
                    if (c.lstrip("./") + suffix) in files:
                        return c.lstrip("./") + suffix
        return None
    if lang in ("javascript", "typescript", "tsx"):
        if name.startswith((".", "/")):
            base = os.path.normpath(os.path.join(d, name)).replace(os.sep, "/")
            base = re.sub(r"\.(js|jsx|mjs|cjs)$", "", base) if not base.endswith((".ts", ".tsx")) else base
            for suf in ("", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", "/index.ts", "/index.tsx", "/index.js", "/index.jsx"):
                if base + suf in files:
                    return base + suf
            return None
        if name.startswith("@/") or name.startswith("~/"):
            for prefix in ("src/", ""):
                base = prefix + name[2:]
                for suf in ("", ".ts", ".tsx", ".js", ".jsx", "/index.ts", "/index.js"):
                    if base + suf in files:
                        return base + suf
        return None
    if lang == "go":
        if go_module and name.startswith(go_module):
            rel = name[len(go_module):].lstrip("/") or "."
            return ("dir:" + rel) if any(fp.startswith(rel + "/") if rel != "." else True for fp in files) else None
        return None
    if lang == "java":
        suffix = name.replace(".", "/")
        if ref.get("names") == ["*"]:
            for fp in files:
                if fp.endswith(".java") and os.path.dirname(fp).endswith(suffix):
                    return "dir:" + os.path.dirname(fp)
            return None
        for fp in files:
            if fp.endswith(suffix + ".java"):
                return fp
        # static import of a member: strip last segment
        parent = "/".join(suffix.split("/")[:-1])
        for fp in files:
            if parent and fp.endswith(parent + ".java"):
                return fp
        return None
    if lang == "rust":
        parts = [p for p in re.split(r"::|\{|\}|,|\s", name) if p]
        if not parts:
            return None
        if parts[0] in ("crate", "super", "self"):
            base = "src" if parts[0] == "crate" else d
            if parts[0] == "super":
                base = os.path.dirname(d)
            rest = parts[1:]
        else:
            base, rest = "src", parts
        for k in range(len(rest), 0, -1):
            c = os.path.normpath(os.path.join(base, *rest[:k])).replace(os.sep, "/")
            for suf in (".rs", "/mod.rs"):
                if c + suf in files:
                    return c + suf
        return None
    if lang == "yaml":  # kustomization resources
        c = os.path.normpath(os.path.join(d, name)).replace(os.sep, "/")
        if c in files:
            return c
        for suf in ("/kustomization.yaml", "/kustomization.yml"):
            if c + suf in files:
                return c + suf
        return None
    return None


def build_graph(root_dir, out_path, includes, excludes, full=False, quiet=False):
    t0 = time.time()
    root_dir = os.path.abspath(root_dir)
    old = None
    if not full and os.path.exists(out_path):
        try:
            old = load_graph(out_path)
            if old.get("version") != GRAPH_VERSION:
                old = None
        except (OSError, ValueError):
            old = None
    tf_modules = terraform_module_map(root_dir)
    extra_dirs = sorted({v["dir"] for v in tf_modules.values() if os.path.isdir(os.path.join(root_dir, v["dir"]))})
    files = {}
    changed, reused = 0, 0
    for rel in iter_source_files(root_dir, includes, excludes, extra_dirs=extra_dirs):
        try:
            with open(os.path.join(root_dir, rel), "rb") as f:
                src = f.read()
        except OSError:
            continue
        h = file_hash(src)
        prev = (old or {}).get("files", {}).get(rel)
        if prev and prev.get("hash") == h:
            files[rel] = prev
            reused += 1
            continue
        res = extract_file(rel, root_dir, src)
        if res is None:
            continue
        fnode, nodes, refs = res
        files[rel] = {"hash": h, "file_node": fnode, "nodes": nodes, "refs": refs}
        changed += 1
    graph = link_graph(root_dir, files, tf_modules)
    graph["version"] = GRAPH_VERSION
    graph["root"] = root_dir
    graph["built_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    graph["files"] = files
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(graph, f)
    if not quiet:
        s = graph["stats"]
        print(f"graph written to {out_path}: {s['files']} files ({changed} parsed, {reused} unchanged), "
              f"{s['nodes']} nodes, {s['edges']} edges, {s['unresolved_refs']} unresolved refs, {time.time() - t0:.1f}s")
        for lang, c in sorted(s["languages"].items()):
            print(f"  {lang}: {c} files")
    return graph


def external_name(name, lang):
    """Normalize an unresolved import to a dependency name: npm package, Go path, Java class, crate."""
    if lang in ("javascript", "typescript", "tsx"):
        parts = name.split("/")
        return "/".join(parts[:2]) if name.startswith("@") else parts[0]
    if lang == "python":
        return name.split(".")[0]
    if lang == "rust":
        return re.split(r"::|\{", name)[0]
    return name


def link_graph(root_dir, files, tf_modules):
    """Turn per-file nodes + raw refs into a global node list with resolved, confidence-labelled edges."""
    nodes, edges = [], []
    node_by_id = {}
    by_name = defaultdict(list)        # simple name -> nodes (definitions only)
    by_file = defaultdict(list)
    go_module = None
    gm = os.path.join(root_dir, "go.mod")
    if os.path.exists(gm):
        m = re.search(r"^module\s+(\S+)", open(gm).read(), flags=re.M)
        go_module = m.group(1) if m else None
    langs = defaultdict(int)
    file_set = set(files)

    def add_node(n):
        nodes.append(n)
        node_by_id[n["id"]] = n

    for rel, info in files.items():
        fn = info["file_node"]
        langs[fn["extra"].get("language", "?")] += 1
        add_node(fn)
        for n in info["nodes"]:
            add_node(n)
            by_file[rel].append(n)
            if n["kind"] not in ("variable", "value"):
                by_name[n["name"]].append(n)
                if n["qname"] != n["name"]:
                    by_name[n["qname"]].append(n)
            edges.append({"src": n["parent"], "dst": n["id"], "type": "contains", "line": n["line"], "confidence": "exact"})

    # Directory-level nodes: Go packages and Terraform modules
    dir_nodes = {}
    for rel, info in files.items():
        lang = info["file_node"]["extra"].get("language")
        if lang in ("go", "hcl"):
            d = os.path.dirname(rel) or "."
            key = ("package" if lang == "go" else "terraform_module", d)
            if key not in dir_nodes:
                nid = f"{key[0]}:{d}"
                dn = {"id": nid, "kind": key[0], "name": d, "qname": d, "file": d, "line": 0, "end_line": 0,
                      "signature": f"{key[0]} {d}", "annotations": [], "parent": None, "extra": {}}
                if lang == "go":
                    dn["extra"]["package"] = info["file_node"]["extra"].get("package")
                add_node(dn)
                dir_nodes[key] = dn
            edges.append({"src": dir_nodes[key]["id"], "dst": rel, "type": "contains", "line": 0, "confidence": "exact"})

    # Per-directory HCL index and k8s index
    hcl_index = {}
    k8s_index = defaultdict(list)
    for n in nodes:
        if n["kind"] in ("resource", "data", "module_call", "variable", "output", "provider", "local") and files.get(n["file"], {}).get("file_node", {}).get("extra", {}).get("language") == "hcl":
            hcl_index[(os.path.dirname(n["file"]) or ".", n["qname"])] = n
        if n["kind"] == "k8s_object":
            k8s_index[n["name"]].append(n)

    imports_of = defaultdict(set)   # file -> set of files it imports (resolved)
    ext_nodes = {}
    unresolved = 0
    stats_conf = defaultdict(int)

    def external(name, kind="external"):
        nid = f"{kind}:{name}"
        if nid not in ext_nodes:
            en = {"id": nid, "kind": kind, "name": name, "qname": name, "file": "", "line": 0, "end_line": 0,
                  "signature": name, "annotations": [], "parent": None, "extra": {}}
            ext_nodes[nid] = en
            add_node(en)
        return ext_nodes[nid]

    def add_edge(src, dst, typ, line, conf, **extra):
        e = {"src": src, "dst": dst, "type": typ, "line": line, "confidence": conf}
        e.update(extra)
        edges.append(e)
        stats_conf[conf] += 1

    # Pass 1: imports, module usage (needed before call resolution)
    for rel, info in files.items():
        lang = info["file_node"]["extra"].get("language")
        d = os.path.dirname(rel) or "."
        for r in info["refs"]:
            if r["kind"] == "import":
                target = resolve_import(r, rel, lang, file_set, root_dir, go_module)
                if target is None:
                    if lang in ("javascript", "typescript", "tsx", "go", "python", "java", "rust"):
                        add_edge(r["src"], external(external_name(r["name"], lang))["id"], "imports", r["line"], "external", names=r.get("names", []))
                    else:
                        unresolved += 1
                    continue
                if target.startswith("dir:"):
                    dd = target[4:]
                    key = ("package", dd)
                    dst = dir_nodes[key]["id"] if key in dir_nodes else None
                    if dst is None:
                        # Java package dir: link to each file in it
                        for fp in files:
                            if os.path.dirname(fp) == dd:
                                imports_of[rel].add(fp)
                                add_edge(r["src"], fp, "imports", r["line"], "exact", names=r.get("names", []))
                        continue
                    for fp in files:
                        if (os.path.dirname(fp) or ".") == dd:
                            imports_of[rel].add(fp)
                    add_edge(r["src"], dst, "imports", r["line"], "exact", names=r.get("names", []))
                else:
                    imports_of[rel].add(target)
                    add_edge(r["src"], target, "imports", r["line"], "exact", names=r.get("names", []))
            elif r["kind"] == "uses_module":
                src_str = r["name"]
                target_dir = None
                if src_str.startswith((".", "/")):
                    target_dir = os.path.normpath(os.path.join(d, src_str)).replace(os.sep, "/")
                    if target_dir == "":
                        target_dir = "."
                else:
                    mm = tf_modules.get((d if d != "." else "", r.get("module_name")))
                    if mm:
                        target_dir = mm["dir"]
                key = ("terraform_module", target_dir)
                if target_dir and key in dir_nodes:
                    node_by_id[r["src"]]["extra"]["resolved_dir"] = target_dir
                    add_edge(r["src"], dir_nodes[key]["id"], "uses_module", r["line"], "exact")
                else:
                    add_edge(r["src"], external(src_str, "external_module")["id"], "uses_module", r["line"], "external")

    # Field type lookup for typed call resolution: class node id -> {field name: type}
    field_types = defaultdict(dict)
    for n in nodes:
        if n["kind"] == "field" and n["parent"] and n["extra"].get("type"):
            field_types[n["parent"]][n["name"]] = base_type_name(n["extra"]["type"])

    def container_of(node_id):
        n = node_by_id.get(node_id)
        while n is not None and n["kind"] not in CONTAINER_KINDS and n["parent"]:
            n = node_by_id.get(n["parent"])
        return n if n is not None and n["kind"] in CONTAINER_KINDS else None

    def candidates(name, kinds):
        return [c for c in by_name.get(name, []) if c["kind"] in kinds]

    def pick(cands, rel, hint_type=None):
        """Choose targets + confidence: typed > same_file > package > import > unique > ambiguous."""
        if not cands:
            return [], None
        if hint_type:
            typed = [c for c in cands if c["qname"].startswith(hint_type + ".") or node_by_id.get(c["parent"], {}).get("name") == hint_type]
            if typed:
                return typed[:1], "typed"
        same = [c for c in cands if c["file"] == rel]
        if same:
            return same[:1], "same_file"
        lang = files[rel]["file_node"]["extra"].get("language")
        if lang in ("java", "go"):  # same directory == same package, no import needed
            pkg = [c for c in cands if os.path.dirname(c["file"]) == os.path.dirname(rel)]
            if pkg:
                return pkg[:1], "package"
        imp = [c for c in cands if c["file"] in imports_of.get(rel, ())]
        if imp:
            return imp[:1], "import"
        if len(cands) == 1:
            return cands, "unique"
        return cands[:5], "ambiguous"

    def type_node(type_name, rel):
        """Find the container node for a type name, preferring same file / package / imports."""
        cands = candidates(type_name, TYPE_LIKE_KINDS | {"impl"})
        targets, _ = pick(cands, rel)
        return targets[0] if targets else None

    def follow_chain(root_type, chain, rel):
        """Walk `a.b.c` through field types: root_type -> type of field b -> type of field c."""
        t = root_type
        for seg in chain:
            tn = type_node(t, rel)
            if tn is None:
                return None
            ft = field_types.get(tn["id"], {}).get(seg)
            if ft is None:
                # Go embedded structs / Rust impl blocks: also look in same-named containers
                for alt in candidates(t, TYPE_LIKE_KINDS | {"impl"}):
                    ft = field_types.get(alt["id"], {}).get(seg)
                    if ft:
                        break
            if ft is None:
                return None
            t = ft
        return t

    # Pass 2: calls, extends, implements, instantiates, references
    for rel, info in files.items():
        lang = info["file_node"]["extra"].get("language")
        d = os.path.dirname(rel) or "."
        for r in info["refs"]:
            k = r["kind"]
            if k == "call":
                hint_type = None
                hint = r.get("hint") or ""
                cont = container_of(r["src"])
                if r.get("hint_type"):  # receiver root typed by a local declaration/parameter
                    hint_type = follow_chain(r["hint_type"], r.get("chain") or [], rel)
                elif hint:
                    chain = [seg.split("(")[0].strip("*&!? ") for seg in hint.split(".")]
                    first = chain[0]
                    if first in ("self", "this", "cls") and cont is not None:
                        hint_type = follow_chain(cont["qname"] if cont["kind"] == "impl" else cont["name"], chain[1:], rel)
                    elif cont is not None and first in field_types.get(cont["id"], {}):
                        hint_type = follow_chain(field_types[cont["id"]][first], chain[1:], rel)
                    elif first and first[0].isupper() and candidates(first, CONTAINER_KINDS):
                        hint_type = follow_chain(first, chain[1:], rel)
                cands = candidates(r["name"], {"function", "method", "class", "struct", "constructor"})
                targets, conf = pick(cands, rel, hint_type)
                if not targets:
                    unresolved += 1
                    continue
                for t in targets:
                    add_edge(r["src"], t["id"], "calls", r["line"], conf, name=r["name"])
            elif k in ("extends", "implements", "instantiates"):
                cands = candidates(r["name"], TYPE_LIKE_KINDS)
                targets, conf = pick(cands, rel)
                if not targets:
                    unresolved += 1
                    continue
                for t in targets:
                    add_edge(r["src"], t["id"], k, r["line"], conf, name=r["name"])
            elif k == "uses_type":
                cands = candidates(r["name"], TYPE_LIKE_KINDS)
                targets, conf = pick(cands, rel)
                if not targets or conf == "ambiguous":
                    continue  # unknown/builtin type names are expected; never counted as unresolved
                if targets[0]["id"] == r["src"] or targets[0]["id"] == node_by_id.get(r["src"], {}).get("parent"):
                    continue
                add_edge(r["src"], targets[0]["id"], "references", r["line"], conf, name=r["name"], via="signature")
            elif k in ("references", "depends_on") and lang == "hcl":
                name = r["name"]
                target = hcl_index.get((d, name))
                if target is None:
                    unresolved += 1
                    continue
                add_edge(r["src"], target["id"], k, r["line"], "exact", name=name)
                # module.x.attr -> output.attr in the module's directory
                if name.startswith("module.") and r.get("attr"):
                    mdir = target["extra"].get("resolved_dir")
                    if mdir:
                        out_node = hcl_index.get((mdir, f"output.{r['attr']}"))
                        if out_node is not None:
                            add_edge(r["src"], out_node["id"], "references", r["line"], "exact", name=f"{name}.{r['attr']}")
            elif k == "references" and lang == "yaml":
                cands = k8s_index.get(r["name"], [])
                ns = r.get("namespace")
                same_ns = [c for c in cands if c["extra"].get("namespace") == ns]
                targets = same_ns or cands
                if not targets:
                    unresolved += 1
                    continue
                conf = "exact" if len(targets) == 1 else "ambiguous"
                for t in targets[:5]:
                    add_edge(r["src"], t["id"], "references", r["line"], conf, name=r["name"], via=r.get("via"))

    # Pass 3: k8s selectors -> workloads; Go receiver methods -> their struct
    workloads = [n for n in nodes if n["kind"] == "k8s_object" and n["extra"].get("template_labels")]
    for n in nodes:
        if n["kind"] == "k8s_object" and n["extra"].get("selector") and n["name"].split("/")[0] in ("Service", "PodDisruptionBudget", "NetworkPolicy"):
            sel = n["extra"]["selector"]
            for w in workloads:
                tl = w["extra"]["template_labels"]
                if all(tl.get(kk) == vv for kk, vv in sel.items()) and (w["extra"].get("namespace") == n["extra"].get("namespace")):
                    add_edge(n["id"], w["id"], "selects", n["line"], "exact")
    for n in nodes:
        if n["kind"] == "method" and n["extra"].get("receiver_type"):
            d = os.path.dirname(n["file"]) or "."
            owners = [c for c in by_name.get(n["extra"]["receiver_type"], []) if c["kind"] in ("struct", "interface", "type") and (os.path.dirname(c["file"]) or ".") == d]
            if owners and n["parent"] != owners[0]["id"]:
                old_parent = n["parent"]
                n["parent"] = owners[0]["id"]
                edges[:] = [e for e in edges if not (e["type"] == "contains" and e["src"] == old_parent and e["dst"] == n["id"])]
                edges.append({"src": owners[0]["id"], "dst": n["id"], "type": "contains", "line": n["line"], "confidence": "exact"})

    # Deduplicate edges
    seen, uniq = set(), []
    for e in edges:
        key = (e["src"], e["dst"], e["type"], e.get("line"))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(e)
    stats = {"files": len(files), "nodes": len(nodes), "edges": len(uniq), "unresolved_refs": unresolved,
             "languages": dict(langs), "edge_confidence": dict(stats_conf)}
    return {"nodes": nodes, "edges": uniq, "stats": stats}


# ----------------------------------------------------------------------------------------------
# Queries
# ----------------------------------------------------------------------------------------------
class G:
    def __init__(self, graph):
        self.g = graph
        self.nodes = {n["id"]: n for n in graph["nodes"]}
        self.out = defaultdict(list)
        self.inc = defaultdict(list)
        for e in graph["edges"]:
            self.out[e["src"]].append(e)
            self.inc[e["dst"]].append(e)
        self.by_name = defaultdict(list)
        for n in graph["nodes"]:
            self.by_name[n["name"].lower()].append(n)
            self.by_name[n["qname"].lower()].append(n)
            if n["kind"] == "file":
                self.by_name[n["file"].lower()].append(n)

    def children(self, nid):
        return sorted((self.nodes[e["dst"]] for e in self.out.get(nid, []) if e["type"] == "contains"), key=lambda n: n["line"])

    def resolve(self, query, kinds=None):
        """Match a user query to nodes: exact id, exact (q)name, file path, `file:name`, then substring."""
        q = query.strip()
        if q in self.nodes:
            return [self.nodes[q]]
        if ":" in q and "::" not in q:
            fpart, npart = q.split(":", 1)
            return list({n["id"]: n for n in self.by_name.get(npart.lower(), []) if n["file"].endswith(fpart)}.values())
        exact = self.by_name.get(q.lower(), [])
        if kinds:
            exact = [n for n in exact if n["kind"] in kinds]
        if exact:
            return list({n["id"]: n for n in exact}.values())
        ql = q.lower()
        subs = [n for n in self.g["nodes"] if ql in n["qname"].lower() or ql in n["file"].lower()]
        if kinds:
            subs = [n for n in subs if n["kind"] in kinds]
        return subs

    def in_edges(self, nid, types):
        return [e for e in self.inc.get(nid, []) if e["type"] in types]

    def out_edges(self, nid, types):
        return [e for e in self.out.get(nid, []) if e["type"] in types]


def fmt_node(n, with_file=True):
    loc = f"{n['file']}:{n['line']}" if n.get("file") and n.get("line") else n.get("file") or ""
    ann = f" @{' @'.join(a.split('(')[0] for a in n['annotations'])}" if n.get("annotations") else ""
    return f"{kind_label(n)}{n['signature']}{ann}" + (f"  ({loc})" if with_file and loc else "")


def ensure_one(g, query, kinds=None):
    matches = g.resolve(query, kinds)
    if not matches:
        sys.exit(f"no symbol or file matches '{query}'. Try: query find {query}")
    if len(matches) > 1 and not all(m["id"] == matches[0]["id"] for m in matches):
        qn = query.split(":", 1)[1] if ":" in query and "::" not in query else query
        exact = [m for m in matches if m["name"].lower() == qn.lower() or m["qname"].lower() == qn.lower() or m["file"] == query]
        if len(exact) == 1:
            return exact[0]
        preferred = [m for m in exact if m["kind"] in TYPE_LIKE_KINDS | {"k8s_object", "resource", "module_call", "file", "terraform_module", "package"}]
        if len(preferred) == 1:
            return preferred[0]
        print(f"'{query}' is ambiguous ({len(matches)} matches). Re-run with one of these ids or `file:name`:")
        for m in matches[:25]:
            print(f"  {m['id']}    {fmt_node(m)}")
        sys.exit(1)
    return matches[0]


def q_find(g, args):
    ql = args.name.lower()
    res = [n for n in g.g["nodes"] if (ql in n["name"].lower() or ql in n["qname"].lower() or ql in n["file"].lower())
           and (not args.kind or n["kind"] in args.kind) and n["kind"] != "file" or (n["kind"] == "file" and ql in n["file"].lower() and (not args.kind or "file" in args.kind))]
    res = res[:args.limit]
    if args.json:
        print(json.dumps(res))
        return
    for n in res:
        print(f"{n['id']}\n    {fmt_node(n)}")
    if not res:
        print("no matches")


def q_symbol(g, args):
    n = ensure_one(g, args.name)
    if args.json:
        print(json.dumps({"node": n, "children": g.children(n["id"]),
                          "out": g.out.get(n["id"], []), "in": g.inc.get(n["id"], [])}))
        return
    print(fmt_node(n))
    if n["extra"]:
        ex = {k: v for k, v in n["extra"].items() if v not in (None, "", [], {}) and k not in ("language",)}
        if ex:
            print("  extra: " + json.dumps(ex)[:400])
    kids = g.children(n["id"])
    if kids:
        print("├── Members")
        for k in kids:
            print(f"│   ├── {fmt_node(k, with_file=False)}  [L{k['line']}]")
            for e in g.out_edges(k["id"], {"calls"}):
                t = g.nodes.get(e["dst"])
                if t:
                    print(f"│   │     calls: {t['qname']}  ({t['file']}:{t['line']}, {e['confidence']})")
    outs = [e for e in g.out.get(n["id"], []) if e["type"] != "contains"]
    if outs:
        print("├── Depends on (outgoing)")
        for e in sorted(outs, key=lambda e: (e["type"], e.get("line") or 0)):
            t = g.nodes.get(e["dst"])
            if t:
                print(f"│   ├── {e['type']}: {t['qname']}  ({t['file']}:{t['line']}, {e['confidence']})" if t["file"] else f"│   ├── {e['type']}: {t['name']}  ({t['kind']})")
    finfo = g.g.get("files", {}).get(n["file"], {})
    member_ids = {n["id"]} | {k["id"] for k in kids}
    resolved_lines = {(e["src"], e.get("line"), e.get("name")) for e in g.g["edges"]}
    unresolved = []
    for r in finfo.get("refs", []):
        if r["src"] in member_ids and r["kind"] in ("call", "extends", "implements", "instantiates", "import") \
                and (r["src"], r["line"], r["name"]) not in resolved_lines and (r["src"], r["line"], None) not in resolved_lines:
            label = f"{r['kind']} {r['hint'] + '.' if r.get('hint') else ''}{r['name']}"
            if label not in unresolved:
                unresolved.append(label)
    if unresolved:
        print("├── Unresolved (external or not indexed): " + ", ".join(unresolved[:12]) + (f" (+{len(unresolved) - 12})" if len(unresolved) > 12 else ""))
    ins = [e for e in g.inc.get(n["id"], []) if e["type"] != "contains"]
    # also include incoming edges to members (e.g. callers of a class's methods)
    member_ins = []
    for k in kids:
        for e in g.inc.get(k["id"], []):
            if e["type"] != "contains":
                member_ins.append((k, e))
    if ins or member_ins:
        print("└── Used by (incoming)")
        for e in sorted(ins, key=lambda e: e["type"]):
            s = g.nodes.get(e["src"])
            if s:
                print(f"    ├── {e['type']} from {s['qname']}  ({s['file']}:{e.get('line') or s['line']}, {e['confidence']})")
        for k, e in member_ins[:40]:
            s = g.nodes.get(e["src"])
            if s:
                print(f"    ├── {e['type']} {k['name']} from {s['qname']}  ({s['file']}:{e.get('line') or s['line']}, {e['confidence']})")
        if len(member_ins) > 40:
            print(f"    └── ... {len(member_ins) - 40} more member usages")


def bfs(g, start_ids, direction, types, depth, include_ambiguous):
    """Breadth-first over edges. direction 'in' = who depends on start, 'out' = what start depends on."""
    seen = {sid: 0 for sid in start_ids}
    frontier = deque((sid, 0) for sid in start_ids)
    hops = []  # (from_node_id, edge, to_node_id, level)
    while frontier:
        nid, lvl = frontier.popleft()
        if lvl >= depth:
            continue
        edges = g.in_edges(nid, types) if direction == "in" else g.out_edges(nid, types)
        for e in edges:
            if e["confidence"] == "ambiguous" and not include_ambiguous:
                continue
            other = e["src"] if direction == "in" else e["dst"]
            hops.append((nid, e, other, lvl + 1))
            if other not in seen:
                seen[other] = lvl + 1
                frontier.append((other, lvl + 1))
    return seen, hops


def q_callers(g, args, direction="in"):
    n = ensure_one(g, args.name)
    targets = [n["id"]]
    if n["kind"] in CONTAINER_KINDS or n["kind"] == "file":
        targets += [k["id"] for k in g.children(n["id"])]
    # Every dependency relation except file-level imports, so Terraform/K8s references count as "callers".
    seen, hops = bfs(g, targets, direction, DEP_EDGE_TYPES - {"imports"}, args.depth, args.include_ambiguous)
    if args.json:
        print(json.dumps({"root": n["id"], "levels": seen, "hops": [(a, e, b, l) for a, e, b, l in hops]}))
        return
    label = "callers of" if direction == "in" else "callees of"
    print(f"{label} {n['qname']}  ({n['file']}:{n['line']})  depth={args.depth}")
    if not hops:
        print("  none found (no resolved edges)")
    for a, e, b, lvl in hops:
        other = g.nodes[b]
        this = g.nodes[a]
        arrow = "<-" if direction == "in" else "->"
        print(f"  {'  ' * (lvl - 1)}{this['qname']} {arrow} {other['qname']}  [{e['type']}, {e['confidence']}]  ({other['file']}:{e.get('line') or other['line']})")


def q_trace_deps(g, args):
    """Blast radius: everything that (transitively) depends on the target."""
    n = ensure_one(g, args.target)
    start = [n["id"]]
    if n["kind"] == "file" or n["kind"] in CONTAINER_KINDS or n["kind"] in ("package", "terraform_module"):
        stack = [n["id"]]
        while stack:
            x = stack.pop()
            for k in g.children(x):
                start.append(k["id"])
                stack.append(k["id"])
    seen, hops = bfs(g, start, "in", DEP_EDGE_TYPES, args.depth, args.include_ambiguous)
    start_set = set(start)
    if args.json:
        print(json.dumps({"target": n["id"], "levels": seen, "hops": hops}))
        return
    # Group by dependent file
    per_file = defaultdict(lambda: {"level": 99, "items": []})
    for a, e, b, lvl in hops:
        dep, tgt = g.nodes[b], g.nodes[a]
        if b in start_set and dep["file"] == n["file"]:
            continue  # intra-target edges
        f = dep["file"] or dep["name"]
        per_file[f]["level"] = min(per_file[f]["level"], lvl)
        per_file[f]["items"].append((lvl, dep, e, tgt))
    print(f"### Blast Radius & Downstream Impact: {n['kind']} {n['qname']}  ({n['file']}:{n['line']})  depth={args.depth}")
    if not per_file:
        print("no dependents found in the graph (nothing imports, calls, extends, references or selects it).")
        return
    print()
    print("| Dependent file | Hop | Dependent symbol | Relationship -> target | Confidence |")
    print("|---|---|---|---|---|")
    for f, info in sorted(per_file.items(), key=lambda kv: (kv[1]["level"], kv[0])):
        rows = sorted(info["items"], key=lambda t: (t[0], t[1]["line"]))
        shown = rows[: args.per_file]
        for lvl, dep, e, tgt in shown:
            print(f"| {f} | {lvl} | {dep['qname']} (L{e.get('line') or dep['line']}) | {e['type']} -> {tgt['qname']} | {e['confidence']} |")
        if len(rows) > len(shown):
            print(f"| {f} |  | ... {len(rows) - len(shown)} more | | |")
    direct = sorted(f for f, i in per_file.items() if i["level"] == 1)
    tests = sorted(f for f in per_file if any(p in f.lower() for p in TEST_PATTERNS))
    print()
    print(f"Files affected: {len(per_file)} (direct: {len(direct)}, transitive: {len(per_file) - len(direct)})")
    if tests:
        print("Tests likely to exercise this change: " + ", ".join(tests))
    amb = sum(1 for e in g.g["edges"] if e["confidence"] == "ambiguous" and e["dst"] in start_set)
    if amb and not args.include_ambiguous:
        print(f"Note: {amb} ambiguous edge(s) to the target were excluded; re-run with --include-ambiguous to see them.")


def q_overview(g, args):
    """Centrality overview: hub symbols, hub files, and directory summary."""
    indeg, outdeg = defaultdict(int), defaultdict(int)
    for e in g.g["edges"]:
        if e["type"] == "contains" or e["confidence"] == "ambiguous":
            continue
        indeg[e["dst"]] += 1
        outdeg[e["src"]] += 1
    # roll member usage up to the containing symbol and file
    file_in = defaultdict(int)
    sym_in = defaultdict(int)
    for nid, c in indeg.items():
        n = g.nodes.get(nid)
        if not n:
            continue
        if n["file"]:
            file_in[n["file"]] += c
        p = n
        while p is not None and p["kind"] not in CONTAINER_KINDS and p.get("parent"):
            p = g.nodes.get(p["parent"])
        if p is not None and p["kind"] in CONTAINER_KINDS:
            sym_in[p["id"]] += c
        elif n["kind"] not in ("file",):
            sym_in[nid] += c
    s = g.g["stats"]
    if args.json:
        print(json.dumps({"stats": s, "hub_symbols": sorted(sym_in.items(), key=lambda kv: -kv[1])[: args.top],
                          "hub_files": sorted(file_in.items(), key=lambda kv: -kv[1])[: args.top]}))
        return
    print(f"Graph: {s['files']} files, {s['nodes']} nodes, {s['edges']} edges; languages: " + ", ".join(f"{k}={v}" for k, v in sorted(s["languages"].items())))
    dirs = defaultdict(lambda: defaultdict(int))
    for n in g.g["nodes"]:
        if n["kind"] == "file":
            dirs[os.path.dirname(n["file"]) or "."][n["extra"].get("language", "?")] += 1
    print("\nDirectories (files by language):")
    for d, langs in sorted(dirs.items())[: args.top * 2]:
        print(f"  {d}: " + ", ".join(f"{k}={v}" for k, v in sorted(langs.items())))
    print(f"\nMost depended-upon symbols (incoming calls/extends/implements/references, top {args.top}):")
    for nid, c in sorted(sym_in.items(), key=lambda kv: -kv[1])[: args.top]:
        n = g.nodes[nid]
        print(f"  {c:4d}  {fmt_node(n)}")
    print(f"\nMost depended-upon files (top {args.top}):")
    for f, c in sorted(file_in.items(), key=lambda kv: -kv[1])[: args.top]:
        print(f"  {c:4d}  {f}")
    ext = [n for n in g.g["nodes"] if n["kind"] in ("external", "external_module")]
    if ext:
        ext_in = sorted(((indeg.get(n["id"], 0), n["name"]) for n in ext), reverse=True)[: args.top]
        print(f"\nExternal dependencies (by import count, top {args.top}): " + ", ".join(f"{name} ({c})" for c, name in ext_in))
    entry = [n for n in g.g["nodes"] if n["kind"] in ("function", "method") and n["name"] in ("main", "handler", "lambda_handler", "run", "serve") and indeg.get(n["id"], 0) == 0]
    if entry:
        print("\nLikely entry points: " + ", ".join(f"{n['qname']} ({n['file']})" for n in entry[: args.top]))


def q_file(g, args):
    n = ensure_one(g, args.path, kinds={"file"})
    info = g.g["files"].get(n["file"])
    if not info:
        sys.exit("file not in graph")
    print(render_skeleton(info["file_node"], info["nodes"], info["refs"], show_calls=not args.no_calls))
    ins = [e for e in g.inc.get(n["id"], []) if e["type"] == "imports"]
    if ins:
        print("  imported by: " + ", ".join(sorted({g.nodes[e['src']]['file'] for e in ins if e['src'] in g.nodes})[:20]))


def q_path(g, args):
    a, b = ensure_one(g, args.src), ensure_one(g, args.dst)
    a_ids = [a["id"]] + [k["id"] for k in g.children(a["id"])]
    b_ids = {b["id"]} | {k["id"] for k in g.children(b["id"])}
    prev = {x: None for x in a_ids}
    dq = deque(a_ids)
    found = None
    while dq and found is None:
        x = dq.popleft()
        for e in g.out.get(x, []):
            if e["type"] == "contains":
                continue
            y = e["dst"]
            if y not in prev:
                prev[y] = (x, e)
                if y in b_ids:
                    found = y
                    break
                dq.append(y)
    if found is None:
        print(f"no dependency path from {a['qname']} to {b['qname']}")
        return
    chain = []
    cur = found
    while prev.get(cur):
        x, e = prev[cur]
        chain.append((x, e, cur))
        cur = x
    for x, e, y in reversed(chain):
        print(f"{g.nodes[x]['qname']} --{e['type']} ({e['confidence']})--> {g.nodes[y]['qname']}  ({g.nodes[y]['file']}:{g.nodes[y]['line']})")


def q_stats(g, args):
    s = dict(g.g["stats"])
    s["built_at"] = g.g.get("built_at")
    s["root"] = g.g.get("root")
    kinds = defaultdict(int)
    for n in g.g["nodes"]:
        kinds[n["kind"]] += 1
    s["node_kinds"] = dict(sorted(kinds.items()))
    et = defaultdict(int)
    for e in g.g["edges"]:
        et[e["type"]] += 1
    s["edge_types"] = dict(sorted(et.items()))
    print(json.dumps(s, indent=2))


# ----------------------------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(prog="astgraph", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sk = sub.add_parser("skeleton", help="print a token-light skeleton of files or directories")
    sk.add_argument("paths", nargs="+")
    sk.add_argument("--root", default=".", help="repo root used for relative paths")
    sk.add_argument("--include", action="append", help="glob(s) to include when a directory is given")
    sk.add_argument("--exclude", action="append", help="glob(s) to exclude")
    sk.add_argument("--no-calls", action="store_true", help="omit the 'calls:' lines")
    sk.add_argument("--no-lines", action="store_true", help="omit line ranges")
    sk.add_argument("--no-stats", action="store_true")
    sk.add_argument("--json", action="store_true")
    sk.set_defaults(fn=cmd_skeleton)

    b = sub.add_parser("build", help="build or incrementally refresh the relationship graph")
    b.add_argument("--root", default=".")
    b.add_argument("--out", default=None, help=f"graph file (default <root>/{DEFAULT_GRAPH})")
    b.add_argument("--include", action="append")
    b.add_argument("--exclude", action="append")
    b.add_argument("--full", action="store_true", help="ignore the cached graph and re-parse everything")
    b.add_argument("--quiet", action="store_true")
    b.set_defaults(fn=lambda a: build_graph(a.root, a.out or os.path.join(a.root, DEFAULT_GRAPH), a.include, a.exclude, a.full, a.quiet))

    q = sub.add_parser("query", help="query a built graph")
    q.add_argument("--graph", default=DEFAULT_GRAPH)
    qs = q.add_subparsers(dest="qcmd", required=True)

    x = qs.add_parser("find", help="search symbols/files by substring")
    x.add_argument("name"); x.add_argument("--kind", action="append"); x.add_argument("--limit", type=int, default=50); x.add_argument("--json", action="store_true")
    x.set_defaults(qfn=q_find)
    x = qs.add_parser("symbol", help="architecture card for one symbol: members, dependencies, dependents")
    x.add_argument("name"); x.add_argument("--json", action="store_true")
    x.set_defaults(qfn=q_symbol)
    x = qs.add_parser("callers", help="who calls/extends/instantiates this symbol (transitive)")
    x.add_argument("name"); x.add_argument("--depth", type=int, default=2); x.add_argument("--include-ambiguous", action="store_true"); x.add_argument("--json", action="store_true")
    x.set_defaults(qfn=lambda g, a: q_callers(g, a, "in"))
    x = qs.add_parser("callees", help="what this symbol calls/instantiates (transitive)")
    x.add_argument("name"); x.add_argument("--depth", type=int, default=2); x.add_argument("--include-ambiguous", action="store_true"); x.add_argument("--json", action="store_true")
    x.set_defaults(qfn=lambda g, a: q_callers(g, a, "out"))
    x = qs.add_parser("trace-deps", help="blast radius: every file/symbol that depends on a target")
    x.add_argument("target", help="file path, symbol name, qualified name, or node id")
    x.add_argument("--depth", type=int, default=3); x.add_argument("--per-file", type=int, default=6)
    x.add_argument("--include-ambiguous", action="store_true"); x.add_argument("--json", action="store_true")
    x.set_defaults(qfn=q_trace_deps)
    x = qs.add_parser("overview", help="centrality ranking: hub symbols, hub files, directories, externals")
    x.add_argument("--top", type=int, default=15); x.add_argument("--json", action="store_true")
    x.set_defaults(qfn=q_overview)
    x = qs.add_parser("file", help="skeleton of a file from the graph, plus who imports it")
    x.add_argument("path"); x.add_argument("--no-calls", action="store_true")
    x.set_defaults(qfn=q_file)
    x = qs.add_parser("path", help="shortest dependency path from one symbol/file to another")
    x.add_argument("src"); x.add_argument("dst")
    x.set_defaults(qfn=q_path)
    x = qs.add_parser("stats", help="graph statistics")
    x.set_defaults(qfn=q_stats)

    def run_query(a):
        if not os.path.exists(a.graph):
            sys.exit(f"graph not found at {a.graph}. Build it first: astgraph.py build --root <repo>")
        g = G(load_graph(a.graph))
        a.qfn(g, a)
    q.set_defaults(fn=run_query)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
