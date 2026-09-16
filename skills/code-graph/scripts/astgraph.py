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
import subprocess
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
    ".kt": "kotlin", ".kts": "kotlin",
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
QUAL_TYPE_RE = re.compile(r"((?:\b[a-z_][A-Za-z0-9_]*\.)+)?\b([A-Z][A-Za-z0-9_]*)\b")  # (qualifier., Name)
COMMON_TYPE_WORDS = {"String", "Integer", "Long", "Boolean", "Double", "Float", "Object", "List", "Map", "Set",
                     "Optional", "Promise", "Array", "Record", "Partial", "Readonly", "Dict", "Any", "Self",
                     "Vec", "Option", "Result", "Box", "Rc", "Arc", "Void", "None", "True", "False", "Number",
                     "Date", "Error", "Exception", "Iterable", "Iterator", "Callable", "Tuple", "Union"}
# Signature keywords: when a signature already starts with one, the skeleton omits the kind label.
SIG_KEYWORDS = ("def ", "func ", "fn ", "fun ", "object ", "class ", "interface ", "struct ", "type ", "enum ", "trait ", "impl ",
                "mod ", "function ", "const ", "variable ", "output ", "resource ", "data ", "module ",
                "provider ", "local.", "terraform", "namespace ", "record ", "annotation ")
# Test-file detection: exact directory segments and per-language basename conventions. A plain
# substring match ("test" in path) misfires on names like attestor, latest, inspect, aspect.
TEST_DIR_SEGMENTS = {"test", "tests", "__tests__", "spec", "specs", "testing"}
TEST_FILE_RES = tuple(re.compile(p) for p in (
    r"_test\.go$",                       # Go
    r"^test_.*\.py$", r"_tests?\.py$", r"^conftest\.py$",   # Python
    r"\.(test|spec)\.[cm]?[jt]sx?$",     # JS/TS
    r"(Test|Tests|TestCase|IT)\.java$", r"^Test[A-Z].*\.java$",  # Java
    r"(Test|Tests|Spec|IT)\.kt$",                                # Kotlin
    r"_test\.rs$",                       # Rust (plus the tests/ directory)
))


def is_test_file(path):
    parts = path.replace(os.sep, "/").split("/")
    if any(p.lower() in TEST_DIR_SEGMENTS for p in parts[:-1]):
        return True
    return any(r.search(parts[-1]) for r in TEST_FILE_RES)


GRAPH_VERSION = 4  # 4: call argument counts (overloads), JS export-from and Rust use names (re-exports), asset imports

# Unqualified calls to these names are language builtins; never linked to a repo symbol of the same name.
BUILTIN_CALLS = {
    "python": {"len", "type", "any", "all", "range", "print", "isinstance", "list", "dict", "set", "str", "int", "float",
               "bool", "tuple", "sorted", "enumerate", "zip", "map", "filter", "max", "min", "sum", "abs", "iter", "next",
               "getattr", "setattr", "hasattr", "delattr", "repr", "hash", "id", "open", "super", "format", "round",
               "reversed", "slice", "object", "property", "staticmethod", "classmethod", "vars", "dir", "callable",
               "issubclass", "bytes", "bytearray", "frozenset", "input", "ord", "chr", "divmod", "pow", "memoryview",
               "complex", "exec", "eval", "compile", "globals", "locals", "hex", "oct", "bin", "ascii", "breakpoint",
               "Exception", "ValueError", "TypeError", "KeyError", "IndexError", "RuntimeError", "AttributeError",
               "NotImplementedError", "StopIteration", "OSError", "IOError", "AssertionError", "ImportError",
               "UnicodeDecodeError", "ZeroDivisionError", "OverflowError", "LookupError", "Warning",
               "DeprecationWarning", "UserWarning", "FutureWarning", "RuntimeWarning"},
    "javascript": {"require", "parseInt", "parseFloat", "String", "Number", "Boolean", "Array", "Object", "Promise",
                   "Map", "Set", "WeakMap", "Error", "TypeError", "RangeError", "Date", "RegExp", "Symbol", "BigInt",
                   "setTimeout", "setInterval", "clearTimeout", "clearInterval", "fetch", "isNaN", "isFinite",
                   "encodeURIComponent", "decodeURIComponent", "structuredClone", "queueMicrotask", "alert"},
    "go": {"len", "cap", "make", "new", "append", "copy", "delete", "panic", "recover", "print", "println", "close",
           "min", "max", "clear", "complex", "real", "imag", "error", "string", "int", "int64", "int32", "uint",
           "float64", "float32", "byte", "rune", "bool"},
    "rust": {"Some", "Ok", "Err", "Box", "Vec", "String", "drop", "panic", "Default", "From", "Into", "Rc", "Arc"},
    "java": set(),
    "kotlin": {"println", "print", "listOf", "mutableListOf", "arrayListOf", "mapOf", "mutableMapOf", "hashMapOf", "setOf",
               "mutableSetOf", "hashSetOf", "arrayOf", "intArrayOf", "emptyList", "emptyMap", "emptySet", "sequenceOf",
               "buildList", "buildString", "buildMap", "require", "requireNotNull", "check", "checkNotNull", "error", "TODO",
               "lazy", "with", "run", "let", "apply", "also", "repeat", "synchronized", "assert", "Pair", "Triple",
               "Regex", "StringBuilder", "Exception", "IllegalArgumentException", "IllegalStateException",
               "RuntimeException", "UnsupportedOperationException", "Thread", "runCatching", "takeIf", "takeUnless"},
}
BUILTIN_CALLS["typescript"] = BUILTIN_CALLS["tsx"] = BUILTIN_CALLS["javascript"]
# Literal node types -> type names, for overload resolution by argument type
LITERAL_TYPES = {
    "java": {"decimal_integer_literal": "int", "hex_integer_literal": "int", "decimal_floating_point_literal": "double",
             "string_literal": "String", "character_literal": "char", "true": "boolean", "false": "boolean", "null_literal": "null"},
    "python": {"integer": "int", "float": "float", "string": "str", "true": "bool", "false": "bool", "none": "None"},
    "typescript": {"number": "number", "string": "string", "template_string": "string", "true": "boolean", "false": "boolean"},
    "javascript": {"number": "number", "string": "string", "template_string": "string", "true": "boolean", "false": "boolean"},
    "go": {"int_literal": "int", "float_literal": "float64", "interpreted_string_literal": "string", "raw_string_literal": "string", "true": "bool", "false": "bool"},
    "rust": {"integer_literal": "i32", "float_literal": "f64", "string_literal": "&str", "boolean_literal": "bool"},
    "kotlin": {"integer_literal": "Int", "long_literal": "Long", "real_literal": "Double", "string_literal": "String",
               "character_literal": "Char", "boolean_literal": "Boolean", "null_literal": "null"},
}
LITERAL_TYPES["tsx"] = LITERAL_TYPES["typescript"]
PRIMITIVE_TYPES = {"int", "long", "short", "byte", "char", "boolean", "float", "double", "str", "string", "number", "bool",
                   "Int", "Long", "Short", "Byte", "Char", "Boolean", "Float", "Double",
                   "i8", "i16", "i32", "i64", "u8", "u16", "u32", "u64", "usize", "isize", "f32", "f64", "&str", "float64", "int64", "int32"}

# Receiver-call names that are Kotlin stdlib extensions/scope functions: on an untyped receiver they are
# never leads (they would name-match unrelated repo methods called `apply`, `forEach`, `first`...).
KOTLIN_STDLIB_MEMBERS = {"apply", "let", "also", "run", "with", "takeIf", "takeUnless", "forEach", "forEachIndexed", "map",
                         "mapNotNull", "mapIndexed", "filter", "filterNot", "filterNotNull", "filterIsInstance", "first",
                         "firstOrNull", "last", "lastOrNull", "single", "singleOrNull", "any", "all", "none", "count", "sum",
                         "sumOf", "maxOf", "minOf", "joinToString", "toList", "toSet", "toMap", "toMutableList",
                         "toMutableMap", "toTypedArray", "isEmpty", "isNotEmpty", "isNullOrEmpty", "isNullOrBlank",
                         "isBlank", "isNotBlank", "orEmpty", "getOrNull", "getOrElse", "getOrPut", "getOrDefault",
                         "contains", "containsKey", "plus", "minus", "sorted", "sortedBy", "sortedByDescending", "groupBy",
                         "associate", "associateBy", "associateWith", "flatMap", "flatten", "distinct", "distinctBy", "zip",
                         "reversed", "take", "drop", "split", "trim", "lowercase", "uppercase", "startsWith", "endsWith",
                         "replace", "substringBefore", "substringAfter", "substringAfterLast", "toInt", "toLong",
                         "toDouble", "toBoolean", "toString", "equals", "hashCode", "compareTo", "iterator", "indexOf",
                         "removeIf", "addAll", "removeAll", "add", "remove", "put", "clear", "get", "set", "invoke",
                         "collect", "emit", "launch", "async", "await", "cancel", "use", "lines", "format", "chunked",
                         "windowed", "fold", "reduce", "onEach", "partition", "withIndex", "asSequence", "asIterable",
                         "coerceAtLeast", "coerceAtMost", "coerceIn", "ifEmpty", "ifBlank", "also", "print", "println"}

LANG_FAMILY = {"python": "py", "javascript": "js", "typescript": "js", "tsx": "js", "go": "go", "java": "jvm",
               "kotlin": "jvm", "rust": "rust", "hcl": "hcl", "yaml": "yaml"}
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


def strip_type_decor(t):
    """`*pkg.Foo<Bar>[]` / `Foo?` / `Foo!!` -> `pkg.Foo` (keeps the package/module qualifier)."""
    return t.strip().lstrip("*&!?").split("<")[0].split("[")[0].rstrip("?!").strip()


def split_qualifier(t):
    """`pkg.Foo` -> ("pkg", "Foo"); `a::b::Foo` -> ("a", "Foo"); `Foo` -> (None, "Foo")."""
    t = strip_type_decor(t)
    if "::" in t:
        parts = t.split("::")
        return parts[0], parts[-1]
    if "." in t:
        parts = t.split(".")
        return parts[0], parts[-1]
    return None, t


def base_type_name(t):
    """`Foo<Bar>` -> Foo, `*pkg.Foo` -> Foo, `a.b.C` -> C, `Foo[]` -> Foo."""
    return split_qualifier(t)[1]


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

    def declare_call(self, name, expr, unwrap=False):
        """`x = a.b(...)`: remember the callee so the linker can type x from the callee's return type.
        The receiver root's declared type (if in scope) rides along, since scope is gone at link time."""
        expr = expr.strip()
        root = re.split(r"[.(:]", expr, 1)[0].strip()
        root_type = self.lookup(root) if root and not root[0].isupper() else None
        if self.scopes and name:
            self.scopes[-1][name] = ("<call?>" if unwrap else "<call>") + expr + "|" + (root_type or "")

    def declare(self, name, type_text):
        if self.scopes and name and type_text:
            t = strip_type_decor(type_text)  # keeps `pkg.` so the linker can spot external types
            if type_text.strip().endswith("]") and "[" in type_text:
                t += "[]"                     # keep array-ness for overload resolution (`String[] tags`)
            self.scopes[-1][name] = t

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
            for m in QUAL_TYPE_RE.finditer(type_text):
                q, t = m.group(1), m.group(2)
                if t != name and t not in seen and t not in COMMON_TYPE_WORDS:
                    seen.add(t)
                    # hint = root of the qualifier (`schema` in `*schema.ResourceData`) so the linker can
                    # skip types that belong to an external package.
                    self.refs.append({"kind": "uses_type", "name": t, "src": node["id"], "line": line,
                                      "hint": q.rstrip(".").split(".")[0] if q else None})
        return node

    def add_ref(self, kind, name, tsnode, hint=None, **extra):
        if not name:
            return
        ref = {"kind": kind, "name": name, "src": self.top()["id"],
               "line": tsnode.start_point[0] + 1, "hint": hint}
        if kind == "call" and "argc" not in extra:
            # argument count and, where knowable, argument types for overload resolution
            args = tsnode.child_by_field_name("arguments") if hasattr(tsnode, "child_by_field_name") else None
            if args is not None:
                named = [c for c in args.named_children if c.type != "comment"]
                argc, types = self.arg_info(named)
                ref["argc"] = argc
                if types:
                    ref["arg_types"] = types
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

    def arg_info(self, named):
        """(argc, arg_types) for a list of argument nodes; argc None when a spread/splat is present."""
        spread = any(c.type in ("spread_element", "list_splat", "dictionary_splat", "variadic_argument", "spread_argument")
                     or self.text(c).lstrip().startswith(("*", "...")) for c in named)
        if spread:
            return None, None
        types = []
        if True:
            if True:
                if True:   # (indentation kept from the original inline loop)
                    for c in named:
                        t = None
                        ctext = self.text(c)
                        if c.type == "identifier":
                            t = self.lookup(ctext) or ("<field>" + ctext)   # not in scope: maybe a field via implicit this
                        elif c.type in LITERAL_TYPES.get(self.lang, {}):
                            t = LITERAL_TYPES[self.lang][c.type]
                        elif c.type in ("cast_expression", "as_expression") and c.child_by_field_name("type") is not None:
                            t = self.text(c.child_by_field_name("type"))
                        elif c.type in ("object_creation_expression", "new_expression"):
                            tn = c.child_by_field_name("type") or c.child_by_field_name("constructor")
                            t = self.text(tn) if tn is not None else None
                        elif c.type in ("array_access", "subscript_expression", "index_expression", "subscript") and c.named_children:
                            arr = c.named_children[0]
                            base = self.lookup(self.text(arr)) if arr.type == "identifier" else None
                            if base and base.endswith("[]"):
                                t = base[:-2]
                            elif base and base.startswith("[]"):
                                t = base[2:]
                        elif c.type in ("call", "call_expression", "method_invocation") and "(" in ctext and ctext.split("(", 1)[0].strip()[:1].isupper() and "." not in ctext.split("(", 1)[0]:
                            t = ctext.split("(", 1)[0].strip()   # `Order("2")`: a constructor call
                        elif c.type in ("call", "call_expression", "method_invocation") and "(" in ctext:
                            callee = ctext.split("(", 1)[0].strip()
                            root = re.split(r"[.(:]", callee, 1)[0].strip()
                            root_type = self.lookup(root) if root and not root[0].isupper() else None
                            t = "<call>" + callee + "|" + (root_type or "")
                        elif c.type in ("attribute", "member_expression", "field_access", "field_expression", "navigation_expression", "selector_expression") \
                                and "(" not in ctext and "[" not in ctext:
                            parts = ctext.replace("?.", ".").replace("!!", "").split(".")
                            if len(parts) == 2 and parts[0] in ("self", "this"):
                                t = "<field>" + parts[1]
                            elif len(parts) >= 2:
                                root_type = self.lookup(parts[0]) if not parts[0][:1].isupper() else None
                                t = "<chain>" + ".".join(parts) + "|" + (root_type or "")   # `saved.sq`: follow the field chain
                        types.append(t)
        return len(named), (types if any(types) else None)

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


def py_assignment(stmt):
    """Return the assignment node of a statement. Depending on the tree-sitter-python version the
    block child is `expression_statement > assignment` or the `assignment` itself."""
    if stmt.type == "assignment":
        return stmt
    if stmt.type == "expression_statement" and stmt.named_children and stmt.named_children[0].type == "assignment":
        return stmt.named_children[0]
    return None


def py_class(ctx, n):
    name = ctx.text(n.child_by_field_name("name"))
    node = ctx.add_node("class", name, n, signature=f"class {name}")
    ctx.push(node)
    sup = n.child_by_field_name("superclasses")
    if sup is not None:
        for c in sup.named_children:
            if c.type in ("identifier", "attribute", "subscript"):
                ctx.add_ref("extends", base_type_name(ctx.text(c)), c, hint=split_qualifier(ctx.text(c))[0], hint_full=strip_type_decor(ctx.text(c)))
    body = n.child_by_field_name("body")
    if body is not None:
        for stmt in body.named_children:
            a = py_assignment(stmt)
            if a is not None:
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
            a = py_assignment(stmt)
            if a is not None:
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
                                fn_text = ctx.text(right.child_by_field_name("function"))
                                cand = base_type_name(fn_text)
                                ftype = cand if cand[:1].isupper() else "<call>" + fn_text + "|"
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
                ctx.add_ref("import", ctx.text(c.child_by_field_name("name")), n, names=[],
                            alias=ctx.text(c.child_by_field_name("alias")))
    else:  # import_from_statement
        mod = n.child_by_field_name("module_name")
        names, alias_map = [], {}
        for c in n.named_children:
            if c is mod:
                continue
            if c.type == "dotted_name":
                names.append(ctx.text(c))
            elif c.type == "aliased_import":
                orig = ctx.text(c.child_by_field_name("name"))
                names.append(orig)
                alias_map[orig] = ctx.text(c.child_by_field_name("alias"))  # local binding is the alias
            elif c.type == "wildcard_import":
                names.append("*")
        ctx.add_ref("import", ctx.text(mod), n, names=names, alias_map=alias_map or None)
    return True


def py_expr_stmt(ctx, n):
    a = py_assignment(n)   # `expression_statement > assignment` or a bare `assignment`, depending on grammar version
    if a is not None:
        left, typ, right = a.child_by_field_name("left"), a.child_by_field_name("type"), a.child_by_field_name("right")
        if left is not None and left.type == "identifier":
            if ctx.top()["kind"] == "file":
                ctx.add_node("variable", ctx.text(left), n,
                             signature=f"{ctx.text(left)}: {ctx.text(typ)}" if typ is not None else ctx.text(left))
            elif typ is not None:
                ctx.declare(ctx.text(left), ctx.text(typ))
            elif right is not None and right.type in ("call", "await"):
                unwrap = right.type == "await"
                if unwrap and right.named_children:
                    right = right.named_children[0]
                fn = right.child_by_field_name("function") if right.type == "call" else None
                t = base_type_name(ctx.text(fn)) if fn is not None else ""
                if t[:1].isupper():
                    ctx.declare(ctx.text(left), t)
                elif fn is not None:
                    ctx.declare_call(ctx.text(left), ctx.text(fn), unwrap)
    return False


PY_HANDLERS = {
    "decorated_definition": py_decorated,
    "class_definition": py_class,
    "function_definition": py_function,
    "call": py_call,
    "import_statement": py_import,
    "import_from_statement": py_import,
    "expression_statement": py_expr_stmt, "assignment": py_expr_stmt,
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
                    ctx.add_ref("extends", base_type_name(ctx.text(v)), v, hint=split_qualifier(ctx.text(v))[0], hint_full=strip_type_decor(ctx.text(v)))
        elif h.type == "implements_clause":
            for v in h.named_children:
                ctx.add_ref("implements", base_type_name(ctx.text(v)), v, hint=split_qualifier(ctx.text(v))[0], hint_full=strip_type_decor(ctx.text(v)))
        elif h.type in ("identifier", "member_expression", "call_expression"):
            ctx.add_ref("extends", base_type_name(ctx.text(h)), h, hint=split_qualifier(ctx.text(h))[0], hint_full=strip_type_decor(ctx.text(h)))


def js_class(ctx, n):
    name_n = n.child_by_field_name("name")
    name = ctx.text(name_n) if name_n is not None else "<anonymous>"
    tp = n.child_by_field_name("type_parameters")
    node = ctx.add_node("class", name, n, signature=f"class {name}{ctx.text(tp) if tp is not None else ''}")
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
                ctx.add_ref("extends", base_type_name(ctx.text(v)), v, hint=split_qualifier(ctx.text(v))[0], hint_full=strip_type_decor(ctx.text(v)))
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
        elif val is not None and val.type in ("call_expression", "await_expression"):
            unwrap = val.type == "await_expression"
            inner = val.named_children[0] if unwrap and val.named_children else val
            if inner is not None and inner.type == "call_expression" and inner.child_by_field_name("function") is not None:
                fn = inner.child_by_field_name("function")
                if fn.type != "identifier" or ctx.text(fn) != "require":
                    ctx.declare_call(name, ctx.text(fn), unwrap)
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
                    # `const fs = require("fs")` / `const {a, b} = require("./x")`: record the local bindings
                    alias, names = None, []
                    par = n.parent
                    if par is not None and par.type == "variable_declarator":
                        nm = par.child_by_field_name("name")
                        if nm is not None and nm.type == "identifier":
                            alias = ctx.text(nm)
                        elif nm is not None and nm.type == "object_pattern":
                            names = [ctx.text(c).split(":")[-1].strip() for c in nm.named_children
                                     if c.type in ("shorthand_property_identifier_pattern", "pair_pattern")]
                    ctx.add_ref("import", strip_quotes(ctx.text(args.named_children[0])), n, names=names, alias=alias)
                    return True
            ctx.add_ref("call", name, n)
        elif fn.type == "member_expression":
            ctx.add_ref("call", ctx.text(fn.child_by_field_name("property")), n, hint=ctx.text(fn.child_by_field_name("object")))
    return False


def js_new(ctx, n):
    c = n.child_by_field_name("constructor")
    if c is not None:
        ctx.add_ref("instantiates", base_type_name(ctx.text(c)), n, hint=split_qualifier(ctx.text(c))[0], hint_full=strip_type_decor(ctx.text(c)))
    return False


def js_import(ctx, n):
    src = n.child_by_field_name("source")
    if src is None:
        return True
    names, alias, alias_map = [], None, {}
    for c in n.named_children:
        if c.type == "import_clause":
            for x in c.named_children:
                if x.type == "identifier":
                    names.append(ctx.text(x))
                elif x.type == "named_imports":
                    for s in x.named_children:
                        if s.type == "import_specifier":
                            orig = ctx.text(s.child_by_field_name("name"))
                            names.append(orig)
                            al = s.child_by_field_name("alias")
                            if al is not None:
                                alias_map[orig] = ctx.text(al)
                elif x.type == "namespace_import":
                    names.append("*")
                    alias = ctx.text(x.named_children[0]) if x.named_children else None
    ctx.add_ref("import", strip_quotes(ctx.text(src)), n, names=names, alias=alias, alias_map=alias_map or None)
    return True


def js_export_from(ctx, n):
    """`export { a, b as c } from './x'`, `export * from './x'`, `export * as ns from './x'`: a barrel
    re-export is an import for resolution purposes. Plain `export function/class ...` is walked normally."""
    src = n.child_by_field_name("source")
    if src is None:
        return False
    names, alias, alias_map = [], None, {}
    for c in n.named_children:
        if c.type == "export_clause":
            for sp in c.named_children:
                if sp.type == "export_specifier":
                    orig = ctx.text(sp.child_by_field_name("name"))
                    names.append(orig)
                    al = sp.child_by_field_name("alias")
                    if al is not None:
                        alias_map[orig] = ctx.text(al)
        elif c.type == "namespace_export":
            names.append("*")
            alias = ctx.text(c.named_children[0]) if c.named_children else None
    if not names and "*" in ctx.text(n).split("from")[0]:
        names.append("*")
    ctx.add_ref("import", strip_quotes(ctx.text(src)), n, names=names, alias=alias, alias_map=alias_map or None, reexport=True)
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
    "import_statement": js_import, "export_statement": js_export_from,
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
                ctx.declare(ctx.text(nm), ("[]" if p.type == "variadic_parameter_declaration" else "") + ctx.text(typ))


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
        elif v.type == "call_expression" and v.child_by_field_name("function") is not None:
            ctx.declare_call(nm, ctx.text(v.child_by_field_name("function")))
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
                        ctx.add_ref("extends", base_type_name(ctx.text(ftype)), fd, hint=split_qualifier(ctx.text(ftype))[0], hint_full=strip_type_decor(ctx.text(ftype)))
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
                    ctx.add_ref("extends", base_type_name(ctx.text(el)), el, hint=split_qualifier(ctx.text(el))[0], hint_full=strip_type_decor(ctx.text(el)))
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
            nm = x.child_by_field_name("name")
            alias = ctx.text(nm) if nm is not None else None
            ctx.add_ref("import", strip_quotes(ctx.text(p)), x, names=[], alias=alias if alias not in (None, "_", ".") else None)
        else:
            stack.extend(x.named_children)
    return True


def go_composite(ctx, n):
    t = n.child_by_field_name("type")
    if t is not None and t.type in ("type_identifier", "qualified_type"):
        ctx.add_ref("instantiates", base_type_name(ctx.text(t)), n, hint=split_qualifier(ctx.text(t))[0], hint_full=strip_type_decor(ctx.text(t)))
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
                ctx.add_ref("extends", base_type_name(ctx.text(t)), t, hint=split_qualifier(ctx.text(t))[0], hint_full=strip_type_decor(ctx.text(t)))
        itf = n.child_by_field_name("interfaces")
        if itf is not None:
            for tl in itf.named_children:
                for t in (tl.named_children if tl.type == "type_list" else [tl]):
                    ctx.add_ref("implements", base_type_name(ctx.text(t)), t, hint=split_qualifier(ctx.text(t))[0], hint_full=strip_type_decor(ctx.text(t)))
        for c in n.children:
            if c.type == "extends_interfaces":
                for tl in c.named_children:
                    for t in (tl.named_children if tl.type == "type_list" else [tl]):
                        ctx.add_ref("extends", base_type_name(ctx.text(t)), t, hint=split_qualifier(ctx.text(t))[0], hint_full=strip_type_decor(ctx.text(t)))
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
        if p.type == "formal_parameter":
            ctx.declare(ctx.text(p.child_by_field_name("name")), ctx.text(p.child_by_field_name("type")))
        elif p.type == "spread_parameter":   # `Object... values` is an Object[] inside the method
            typ = next((c for c in p.named_children if c.type not in ("variable_declarator", "modifiers")), None)
            decl = next((c for c in p.named_children if c.type == "variable_declarator"), None)
            if typ is not None and decl is not None:
                ctx.declare(ctx.text(decl.child_by_field_name("name")), ctx.text(typ) + "[]")
    ctx.walk_children(n.child_by_field_name("body"))
    ctx.pop_scope()
    ctx.pop()
    return True


def java_local_var(ctx, n):
    typ = n.child_by_field_name("type")
    for d in n.named_children:
        if d.type == "variable_declarator":
            val = d.child_by_field_name("value")
            if ctx.text(typ) == "var" and val is not None and val.type == "method_invocation":
                obj = val.child_by_field_name("object")
                ctx.declare_call(ctx.text(d.child_by_field_name("name")), (ctx.text(obj) + "." if obj is not None else "") + ctx.text(val.child_by_field_name("name")))
            else:
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
        ctx.add_ref("instantiates", base_type_name(ctx.text(t)), n, hint=split_qualifier(ctx.text(t))[0], hint_full=strip_type_decor(ctx.text(t)))
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
    else:
        unwrap = False
        v = val
        while v is not None and v.type in ("try_expression", "await_expression", "unary_expression", "reference_expression", "parenthesized_expression"):
            unwrap = unwrap or v.type == "try_expression"
            v = v.named_children[0] if v.named_children else None
        # `.unwrap()` / `.expect(..)` / `?` on a call: the binding is the inner type
        if v is not None and v.type == "call_expression":
            fn = v.child_by_field_name("function")
            if fn is not None and fn.type == "field_expression" and ctx.text(fn.child_by_field_name("field")) in ("unwrap", "expect", "unwrap_or_default"):
                unwrap = True
                v = fn.child_by_field_name("value")
        if v is not None and v.type == "call_expression":
            fn = v.child_by_field_name("function")
            if fn is not None and fn.type == "scoped_identifier" and fn.child_by_field_name("path") is not None \
                    and ctx.text(fn.child_by_field_name("name")) in ("new", "default", "from", "builder"):
                ctx.declare(ctx.text(pat), base_type_name(ctx.text(fn.child_by_field_name("path"))))
            elif fn is not None:
                ctx.declare_call(ctx.text(pat), ctx.text(fn).replace("::", "."), unwrap)
    return False


def rs_impl(ctx, n):
    typ = base_type_name(ctx.text(n.child_by_field_name("type")))
    trait = n.child_by_field_name("trait")
    tp = n.child_by_field_name("type_parameters")
    where = next((c for c in n.named_children if c.type == "where_clause"), None)
    gen = ctx.text(tp) if tp is not None else ""
    sig = f"impl{gen} {ctx.text(trait)} for {typ}" if trait is not None else f"impl{gen} {typ}"
    if where is not None:
        sig += " " + " ".join(ctx.text(where).split())
    node = ctx.add_node("impl", f"impl {typ}", n, signature=sig, qname=typ)
    ctx.push(node)
    if trait is not None:
        ctx.add_ref("implements", base_type_name(ctx.text(trait)), trait, hint=split_qualifier(ctx.text(trait))[0], hint_full=strip_type_decor(ctx.text(trait)))
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
        tp = n.child_by_field_name("type_parameters")
        node = ctx.add_node(kind, name, n, signature=f"{word} {name}{ctx.text(tp) if tp is not None else ''}")
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


# ----------------------------------------------------------------------------------------------
# Kotlin
# ----------------------------------------------------------------------------------------------
def kt_annotations(ctx, n):
    out = []
    for m in n.named_children:
        if m.type == "modifiers":
            for a in m.named_children:
                if a.type == "annotation":
                    out.append(ctx.text(a).lstrip("@").split("(")[0].strip())
    return out


def kt_type_text(node):
    """Type text of a Kotlin parameter/property child (user_type, nullable_type, function_type...)."""
    return node


def kt_package(ctx, n):
    ident = next((c for c in n.named_children if c.type == "identifier"), None)
    ctx.file_extra["package"] = ctx.text(ident) if ident is not None else ""
    return True


def kt_import(ctx, n):
    ident = next((c for c in n.named_children if c.type == "identifier"), None)
    if ident is None:
        return True
    path = ctx.text(ident)
    if any(c.type == "wildcard_import" for c in n.named_children):
        ctx.add_ref("import", path, n, names=["*"])
        return True
    leaf = path.split(".")[-1]
    alias = next((ctx.text(c.named_children[0]) for c in n.named_children if c.type == "import_alias" and c.named_children), None)
    ctx.add_ref("import", path, n, names=[leaf], alias_map=({leaf: alias} if alias else None))
    return True


def kt_supertypes(ctx, n):
    for d in n.named_children:
        if d.type != "delegation_specifier":
            continue
        inv = next((c for c in d.named_children if c.type == "constructor_invocation"), None)
        ut = next((c for c in (inv.named_children if inv is not None else d.named_children) if c.type == "user_type"), None)
        if ut is None:
            continue
        txt = ctx.text(ut)
        ctx.add_ref("extends" if inv is not None else "implements", base_type_name(txt), d,
                    hint=split_qualifier(txt)[0], hint_full=strip_type_decor(txt))


def kt_ctor_fields(ctx, n):
    """Primary-constructor `val`/`var` parameters are properties of the class."""
    pc = next((c for c in n.named_children if c.type == "primary_constructor"), None)
    if pc is None:
        return
    for p in pc.named_children:
        if p.type != "class_parameter":
            continue
        if not any(c.type == "binding_pattern_kind" for c in p.named_children):
            continue
        name = next((ctx.text(c) for c in p.named_children if c.type == "simple_identifier"), None)
        typ = next((c for c in p.named_children if c.type in ("user_type", "nullable_type", "function_type", "parenthesized_type")), None)
        if name:
            ttxt = ctx.text(typ) if typ is not None else None
            ctx.add_node("field", name, p, signature=f"{name}: {ttxt}" if ttxt else name, type_text=ttxt, extra={"type": ttxt})


def kt_class(ctx, n):
    name = next((ctx.text(c) for c in n.named_children if c.type == "type_identifier"), None)
    if not name:
        return False
    kw = [c.type for c in n.children if not c.is_named]
    kind = "interface" if "interface" in kw else "enum" if "enum" in kw else "class"
    tp = next((c for c in n.named_children if c.type == "type_parameters"), None)
    mods = " ".join(ctx.text(m) for m in n.named_children if m.type == "modifiers" and ctx.text(m) in ("data", "sealed", "abstract", "open", "enum", "annotation", "value", "inner"))
    sig = f"{kind if kind != 'enum' else 'enum class'} {name}{ctx.text(tp) if tp is not None else ''}"
    node = ctx.add_node(kind, name, n, signature=sig, annotations=kt_annotations(ctx, n))
    ctx.push(node)
    kt_supertypes(ctx, n)
    kt_ctor_fields(ctx, n)
    for body in (c for c in n.named_children if c.type in ("class_body", "enum_class_body")):
        ctx.walk_children(body)
    ctx.pop()
    return True


def kt_object(ctx, n):
    name = next((ctx.text(c) for c in n.named_children if c.type == "type_identifier"), None)
    companion = n.type == "companion_object"
    if not name:
        name = "Companion" if companion else "object"
    node = ctx.add_node("class", name, n, signature=f"{'companion object' if companion else 'object'} {name}",
                        annotations=kt_annotations(ctx, n), extra={"companion": True} if companion else {})
    ctx.push(node)
    kt_supertypes(ctx, n)
    for body in (c for c in n.named_children if c.type == "class_body"):
        ctx.walk_children(body)
    ctx.pop()
    return True


def kt_function(ctx, n):
    name = next((ctx.text(c) for c in n.named_children if c.type == "simple_identifier"), None)
    if not name:
        return False
    recv = n.child_by_field_name("receiver")
    rtype = base_type_name(ctx.text(recv)) if recv is not None else None
    params_n = next((c for c in n.named_children if c.type == "function_value_parameters"), None)
    # parameters: `vararg` modifiers precede their parameter as a sibling
    parts, declared, pending_vararg = [], [], False
    for c in (params_n.named_children if params_n is not None else []):
        if c.type == "parameter_modifiers":
            pending_vararg = "vararg" in ctx.text(c)
            continue
        if c.type == "parameter":
            pname = next((ctx.text(x) for x in c.named_children if x.type == "simple_identifier"), "")
            ptype = next((ctx.text(x) for x in c.named_children if x.type in ("user_type", "nullable_type", "function_type", "parenthesized_type")), "")
            parts.append(("vararg " if pending_vararg else "") + f"{pname}: {ptype}".rstrip(": "))
            declared.append((pname, ptype + ("[]" if pending_vararg else "")))
            pending_vararg = False
    seen_params = False
    ret = None
    for c in n.named_children:
        if c.type == "function_value_parameters":
            seen_params = True
        elif seen_params and c.type in ("user_type", "nullable_type", "function_type", "parenthesized_type"):
            ret = ctx.text(c)
            break
    tp = next((c for c in n.named_children if c.type == "type_parameters"), None)
    sig = f"fun{' ' + ctx.text(tp) if tp is not None else ''} {rtype + '.' if rtype else ''}{name}({', '.join(parts)})" + (f": {ret}" if ret else "")
    kind = "method" if (rtype or ctx.top()["kind"] in ("class", "interface", "enum")) else "function"
    node = ctx.add_node(kind, name, n, signature=sig, annotations=kt_annotations(ctx, n),
                        qname=f"{rtype}.{name}" if rtype and ctx.top()["kind"] == "file" else None,
                        extra={"receiver_type": rtype} if rtype else {},
                        type_text=", ".join(t for _, t in declared) + (" " + ret if ret else ""))
    ctx.push(node)
    ctx.push_scope()
    for pname, ptype in declared:
        if pname and ptype:
            ctx.declare(pname, ptype)
    body = next((c for c in n.named_children if c.type == "function_body"), None)
    if body is not None:
        ctx.walk_children(body)
    ctx.pop_scope()
    ctx.pop()
    return True


def kt_initializer(ctx, n):
    """The expression after `=` in a property declaration, or None."""
    seen_eq = False
    for c in n.children:
        if not c.is_named and c.type == "=":
            seen_eq = True
            continue
        if seen_eq and c.is_named:
            return c
    return None


def kt_property(ctx, n):
    vd = next((c for c in n.named_children if c.type == "variable_declaration"), None)
    if vd is None:
        return False
    name = next((ctx.text(c) for c in vd.named_children if c.type == "simple_identifier"), None)
    typ = next((c for c in vd.named_children if c.type in ("user_type", "nullable_type", "function_type", "parenthesized_type")), None)
    init = kt_initializer(ctx, n)
    ttxt = ctx.text(typ) if typ is not None else None
    callee = None
    if init is not None and init.type == "call_expression" and init.named_children:
        head = init.named_children[0]
        callee = ctx.text(head) if head.type in ("simple_identifier", "navigation_expression") else None
    elif init is not None and init.type == "simple_identifier" and ctx.text(init)[:1].isupper() and ttxt is None:
        ttxt = ctx.text(init)   # `val reg = Registry`: a reference to an object/class
    if ttxt is None and callee and callee[:1].isupper() and "." not in callee:
        ttxt = callee   # `val o = Order(...)`: a constructor call
    if ttxt is None and init is not None and init.type == "object_literal" and name:
        # `val users = object : IntIdTable("users") { val name = varchar(...) }`: a class of its own,
        # named after the property, so the local and its members resolve like any other type
        anon = ctx.add_node("class", f"{name}$object", init, signature=f"object {name}$object", extra={"anonymous": True})
        ctx.push(anon)
        kt_supertypes(ctx, init)
        for body in (c for c in init.named_children if c.type == "class_body"):
            ctx.walk_children(body)
        ctx.pop()
        ttxt = anon["name"]
        init = None
    if ctx.top()["kind"] in ("class", "interface", "enum"):
        ftype = ttxt or (("<call>" + callee.replace("?.", ".") + "|") if callee else None)   # `val name = varchar(...)`
        ctx.add_node("field", name, n, signature=f"{name}: {ttxt}" if ttxt else name, type_text=ttxt,
                     extra={"type": ftype, "inferred": typ is None} if ftype else {"type": None})
    elif name:
        if ttxt:
            ctx.declare(name, ttxt)
        elif callee:
            ctx.declare_call(name, callee.replace("?.", "."))
    if init is not None:
        ctx.walk(init)
    return True


def kt_call(ctx, n):
    head = n.named_children[0] if n.named_children else None
    if head is None:
        return False
    suffix = next((c for c in n.named_children if c.type == "call_suffix"), None)
    named = []
    if suffix is not None:
        va = next((c for c in suffix.named_children if c.type == "value_arguments"), None)
        if va is not None:
            for a in va.named_children:
                if a.type == "value_argument" and a.named_children:
                    named.append(a.named_children[-1])
        if any(c.type == "annotated_lambda" for c in suffix.named_children):
            named.append(next(c for c in suffix.named_children if c.type == "annotated_lambda"))
    argc, types = ctx.arg_info(named)
    extra = {"argc": argc}
    if types:
        extra["arg_types"] = types
    if head.type == "simple_identifier":
        ctx.add_ref("call", ctx.text(head), n, **extra)
    elif head.type == "navigation_expression" and head.named_children:
        recv, suf = head.named_children[0], head.named_children[-1]
        member = next((ctx.text(c) for c in suf.named_children if c.type == "simple_identifier"), None) if suf.type == "navigation_suffix" else None
        if member:
            hint = ctx.text(recv).replace("?.", ".").replace("!!", "")
            ref = ctx.add_ref("call", member, n, hint=hint, **extra)
            # `(x as Foo).bar()`: the cast tells us the receiver type
            if recv.type == "parenthesized_expression" and recv.named_children and recv.named_children[0].type == "as_expression":
                ae = recv.named_children[0]
                ref["hint_type"] = ctx.text(ae.named_children[-1])
                ref["chain"] = []
    for c in n.named_children[1:]:
        ctx.walk(c)   # arguments and trailing lambdas may contain calls
    if head.type != "simple_identifier":
        ctx.walk(head)   # `a.b().c()`: the receiver chain holds further calls (and chained constructors)
    return True


def kt_infix(ctx, n):
    """`a eq b` / `a and b`: an infix call of `eq` on `a` with one argument."""
    kids = n.named_children
    if len(kids) == 3 and kids[1].type == "simple_identifier":
        op = ctx.text(kids[1])
        argc, types = ctx.arg_info([kids[2]])
        extra = {"argc": argc}
        if types:
            extra["arg_types"] = types
        ctx.add_ref("call", op, n, hint=ctx.text(kids[0]).replace("?.", ".").replace("!!", ""), **extra)
        ctx.walk(kids[0])
        ctx.walk(kids[2])
        return True
    return False


def rs_expand_use(txt):
    """`a::{b, c::{d, e as f}}` -> ["a::b", "a::c::d", "a::c::e as f"]; plain paths pass through."""
    txt = " ".join(txt.split())
    i = txt.find("{")
    if i < 0:
        return [txt.strip()]
    prefix = txt[:i]
    depth, j = 0, i
    while j < len(txt):
        if txt[j] == "{":
            depth += 1
        elif txt[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    inner, suffix = txt[i + 1:j], txt[j + 1:]
    parts, cur, depth = [], "", 0
    for ch in inner:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    out = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        for leaf in rs_expand_use(p):
            leaf = leaf.strip()
            if leaf == "self":
                out.append(prefix.rstrip(": "))          # `a::{self, b}`: `self` is the module a itself
            else:
                out.append(prefix + leaf + suffix)
    return out


def rs_use(ctx, n):
    arg = n.child_by_field_name("argument")
    is_pub = ctx.text(n).lstrip().startswith("pub ")
    for path in rs_expand_use(ctx.text(arg)):
        leaf = path.split("::")[-1].strip()
        names, alias_map = [], {}
        if " as " in leaf:
            orig, al = [x.strip() for x in leaf.split(" as ", 1)]
            path = path.rsplit("::", 1)[0] + "::" + orig if "::" in path else orig
            names.append(orig)
            alias_map[orig] = al
        elif leaf:
            names.append(leaf)   # includes "*" for glob imports
        ctx.add_ref("import", path, n, names=names, alias_map=alias_map or None, reexport=is_pub)
    return True


def rs_struct_expr(ctx, n):
    ctx.add_ref("instantiates", base_type_name(ctx.text(n.child_by_field_name("name"))), n,
                hint=split_qualifier(ctx.text(n.child_by_field_name("name")))[0])
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


KOTLIN_HANDLERS = {
    "package_header": kt_package, "import_header": kt_import,
    "class_declaration": kt_class, "object_declaration": kt_object, "companion_object": kt_object,
    "function_declaration": kt_function, "property_declaration": kt_property, "call_expression": kt_call,
    "infix_expression": kt_infix,
}

HANDLERS = {
    "kotlin": KOTLIN_HANDLERS,
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
            sig_lines = n["signature"].split("\n")
            sig = n["signature"] if len(sig_lines) <= 3 else "\n".join(sig_lines[:2]) + f"\n{'  ' * depth}    ... (+{len(sig_lines) - 2} signature lines)"
            lines.append(f"{'  ' * depth}{kind_label(n)}{sig}{rng(n)}{ann}")
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


_DIR_CACHE = {}
_TS_PATHS = {}
_WORKSPACES = {}
ASSET_EXTS = {".css", ".scss", ".sass", ".less", ".styl", ".sss", ".pcss", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp",
              ".ico", ".json", ".json5", ".wasm", ".vue", ".svelte", ".astro", ".html", ".md", ".mdx", ".txt", ".woff", ".woff2",
              ".ttf", ".mp3", ".mp4", ".webm", ".glsl", ".graphql", ".gql", ".yaml", ".yml", ".toml", ".xml", ".csv"}


def _read_json_loose(p):
    try:
        txt = open(p, encoding="utf-8", errors="replace").read()
        txt = re.sub(r"/\*.*?\*/", "", txt, flags=re.S)
        txt = re.sub(r"^\s*//.*$", "", txt, flags=re.M)
        txt = re.sub(r",\s*([}\]])", r"\1", txt)
        return json.loads(txt)
    except (OSError, ValueError):
        return None


def workspaces(root_dir, files):
    """Per-repo package indexes, built once:
    js: {package name: entry file} from every package.json (workspaces/pnpm-workspace or any nested one),
        entry = exports['.'] / module / main / types mapped dist->src and to an indexed file, else src/index.*;
    rust: {crate name (underscored): crate dir} from every Cargo.toml [package]."""
    key = (root_dir, id(files))
    hit = _WORKSPACES.get(key)
    if hit is not None and hit[0] is files:
        return hit[1]
    js, rust = {}, {}
    for dirpath, dirnames, filenames in os.walk(root_dir):
        dirnames[:] = [d for d in dirnames if d not in DEFAULT_EXCLUDE_DIRS]
        rel_dir = os.path.relpath(dirpath, root_dir).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        if "package.json" in filenames:
            pj = _read_json_loose(os.path.join(dirpath, "package.json")) or {}
            name = pj.get("name")
            if isinstance(name, str) and name:
                cands = []
                ex = pj.get("exports")
                dot = ex.get(".") if isinstance(ex, dict) else ex
                while isinstance(dot, dict):
                    dot = dot.get("import") or dot.get("default") or dot.get("types") or dot.get("require") or next(iter(dot.values()), None)
                for v in (dot, pj.get("module"), pj.get("main"), pj.get("types")):
                    if isinstance(v, str):
                        cands.append(v)
                        if "dist/" in v:
                            cands.append(v.replace("dist/", "src/"))
                cands += ["src/index.ts", "src/index.tsx", "src/index.js", "src/index.mjs", "index.ts", "index.js", "src/main.ts"]
                for c in cands:
                    base = os.path.normpath(os.path.join(rel_dir, c)).replace(os.sep, "/").lstrip("./")
                    stem = re.sub(r"\.(js|jsx|mjs|cjs|d\.ts)$", "", base)
                    for cand in (base, stem + ".ts", stem + ".tsx", stem + ".js", stem + ".mjs", stem + "/index.ts", stem + "/index.js"):
                        if cand in files:
                            js.setdefault(name, cand)
                            break
                    if name in js:
                        break
        if "Cargo.toml" in filenames:
            try:
                txt = open(os.path.join(dirpath, "Cargo.toml"), encoding="utf-8", errors="replace").read()
            except OSError:
                txt = ""
            m = re.search(r"^\[package\][^\[]*?^\s*name\s*=\s*\"([^\"]+)\"", txt, flags=re.M | re.S)
            if m:
                rust[m.group(1).replace("-", "_")] = rel_dir
            # explicit crate roots: [lib] path = "..." / [[bin]] path = "crates/core/main.rs"
            for pm in re.finditer(r"^\s*path\s*=\s*\"([^\"]+\.rs)\"", txt, flags=re.M):
                src_dir = os.path.dirname(os.path.normpath(os.path.join(rel_dir, pm.group(1)))).replace(os.sep, "/")
                rust.setdefault("__roots__", set()).add(src_dir)
    out = {"js": js, "rust": rust}
    _WORKSPACES[key] = (files, out)
    return out


def ts_paths(root_dir, file_dir):
    """{pattern: [target patterns]} from the nearest tsconfig.json / jsconfig.json at or above `file_dir`
    (JSONC tolerated), with baseUrl applied and targets made repo-relative. Cached per directory."""
    key = (root_dir, file_dir)
    if key in _TS_PATHS:
        return _TS_PATHS[key]
    out = {}
    cfg_dir, found = file_dir or ".", None
    while True:
        for fn in ("tsconfig.json", "jsconfig.json"):
            p = os.path.join(root_dir, cfg_dir, fn)
            if os.path.exists(p):
                found = p
                break
        if found or cfg_dir in ("", "."):
            break
        cfg_dir = os.path.dirname(cfg_dir)
    def load(p, depth=0):
        """compilerOptions of a tsconfig with its `extends` chain applied (child wins); baseUrl and
        paths are made relative to the config that declares them, as TypeScript does."""
        try:
            txt = open(p, encoding="utf-8", errors="replace").read()
            txt = re.sub(r"/\*.*?\*/", "", txt, flags=re.S)
            txt = re.sub(r"^\s*//.*$", "", txt, flags=re.M)
            txt = re.sub(r",\s*([}\]])", r"\1", txt)
            cfg = json.loads(txt)
        except (OSError, ValueError):
            return {}
        merged = {}
        ext = cfg.get("extends")
        if ext and depth < 5:
            for e in (ext if isinstance(ext, list) else [ext]):
                if e.startswith((".", "/")):
                    bp = os.path.normpath(os.path.join(os.path.dirname(p), e))
                    if not os.path.exists(bp) and os.path.exists(bp + ".json"):
                        bp = bp + ".json"
                    merged.update(load(bp, depth + 1))
                # bare names (`@tsconfig/node18`) live in node_modules and are not indexed: skipped
        co = cfg.get("compilerOptions") or {}
        here = os.path.dirname(p)
        if "baseUrl" in co:
            merged["baseUrl"] = os.path.join(here, co["baseUrl"])
        if "paths" in co:
            merged["paths"] = co["paths"]
            merged["pathsBase"] = merged.get("baseUrl") or here
        return merged

    if found:
        co = load(os.path.join(root_dir, found) if not os.path.isabs(found) else found)
        base = co.get("pathsBase") or co.get("baseUrl") or os.path.join(root_dir, cfg_dir)
        base = os.path.relpath(base, root_dir) if os.path.isabs(base) else base
        for pat, targets in (co.get("paths") or {}).items():
            out[pat] = [os.path.join(base, t) for t in (targets if isinstance(targets, list) else [targets])]
    _TS_PATHS[key] = out
    return out


def dirs_of(files):
    """Set of every directory (all prefixes) that contains an indexed file; cached per file set."""
    key = id(files)
    hit = _DIR_CACHE.get(key)
    if hit is not None and hit[0] is files:
        return hit[1]
    out = {"."}
    for fp in files:
        d = os.path.dirname(fp)
        while d and d not in out:
            out.add(d)
            d = os.path.dirname(d)
    _DIR_CACHE[key] = (files, out)
    return out


_PY_ROOTS = {}


def py_source_roots(files):
    """Directories that act as Python import roots: the parent of every top-level package (a dir with
    __init__.py whose own parent has none), shortest first. Deterministic, unlike scanning a set."""
    key = id(files)
    hit = _PY_ROOTS.get(key)
    if hit is not None and hit[0] is files:
        return hit[1]
    roots = {"."}
    for fp in files:
        if fp.endswith("__init__.py"):
            pkg = os.path.dirname(fp)
            parent = os.path.dirname(pkg)
            if pkg and ((parent + "/__init__.py") if parent else "__init__.py") not in files:
                roots.add(parent or ".")
    out = sorted(roots, key=lambda r: (len(r), r))
    _PY_ROOTS[key] = (files, out)
    return out


def resolve_import(ref, file_path, lang, files, root_dir, go_module=None, ctx=None):
    """Return a repo-relative file path (or directory for packages) an import points at, else None.
    `ctx` carries precomputed indexes from link_graph (Java declared packages); resolution never
    depends on set iteration order."""
    name = ref["name"]
    d = os.path.dirname(file_path)
    ctx = ctx or {}
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
            # import roots: repo root, src/, the parent of every top-level package, then the importing
            # file's own directory (scripts run from that directory). `import io` must never resolve
            # to a repo package that merely happens to be named io somewhere in the tree.
            cands = [mod, f"src/{mod}"] + [os.path.join(r, mod) if r != "." else mod for r in py_source_roots(files)] + [os.path.normpath(os.path.join(d, mod))]
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
        plain = name.split("?")[0]
        if os.path.splitext(plain)[1].lower() in ASSET_EXTS or ("?" in name and not name.startswith("@")):
            return "asset"   # `./style.css`, `./logo.svg?url`, `./worker?worker`: not code; not an external module either
        ws = workspaces(root_dir, files)["js"]
        if not name.startswith((".", "/")):
            pkg = name if name.startswith("@") else name.split("/")[0]
            if name.startswith("@"):
                pkg = "/".join(name.split("/")[:2])
            if pkg in ws:
                if name == pkg:
                    return ws[pkg]
                # deep import inside a workspace package: `pkg/sub/path` -> <pkgdir>/(src/)?sub/path
                pdir = os.path.dirname(ws[pkg])
                rest = name[len(pkg) + 1:]
                for base in (os.path.join(pdir, rest), os.path.join(os.path.dirname(pdir), rest)):
                    base = os.path.normpath(base).replace(os.sep, "/")
                    for suf in ("", ".ts", ".tsx", ".js", ".mjs", "/index.ts", "/index.js"):
                        if base + suf in files:
                            return base + suf
                return ws[pkg]
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
        # tsconfig.json / jsconfig.json `compilerOptions.paths` (+ baseUrl): "@app/*" -> ["src/app/*"]
        for pattern, targets in ts_paths(root_dir, d).items():
            if pattern.endswith("*"):
                pre = pattern[:-1]
                if not name.startswith(pre):
                    continue
                rest = name[len(pre):]
                cands = [t.replace("*", rest) for t in targets]
            elif name == pattern:
                cands = list(targets)
            else:
                continue
            for base in cands:
                base = os.path.normpath(base).replace(os.sep, "/").lstrip("./")
                for suf in ("", ".ts", ".tsx", ".js", ".jsx", ".mjs", "/index.ts", "/index.tsx", "/index.js"):
                    if base + suf in files:
                        return base + suf
        return None
    if lang == "go":
        if go_module and name.startswith(go_module):
            rel = name[len(go_module):].lstrip("/") or "."
            return ("dir:" + rel) if rel in dirs_of(files) else None
        return None
    if lang == "kotlin":
        pkgs = ctx.get("java_pkgs") or {}
        kt_top = ctx.get("kt_top") or {}
        if ref.get("names") == ["*"]:
            fs = pkgs.get(name)
            return ("dir:" + os.path.dirname(fs[0])) if fs else None
        pkg, _, leaf = name.rpartition(".")
        for f in pkgs.get(pkg, ()):
            if leaf in kt_top.get(f, ()) or os.path.basename(f) == leaf + ".kt" or os.path.basename(f) == leaf + ".java":
                return f
        # `import a.b.Outer.Inner` / member imports: the enclosing class's file
        pkg2, _, cls2 = pkg.rpartition(".")
        for f in pkgs.get(pkg2, ()):
            if cls2 in kt_top.get(f, ()):
                return f
        return None
    if lang == "java":
        pkgs = ctx.get("java_pkgs")   # {declared package: sorted files}; built by link_graph
        if pkgs is not None:
            if ref.get("names") == ["*"]:
                fs = pkgs.get(name)
                return ("dir:" + os.path.dirname(fs[0])) if fs else None
            pkg, _, cls = name.rpartition(".")
            for f in pkgs.get(pkg, ()):
                if os.path.basename(f) == cls + ".java":
                    return f
            pkg2, _, cls2 = pkg.rpartition(".")   # static import of a member: strip last segment
            for f in pkgs.get(pkg2, ()):
                if os.path.basename(f) == cls2 + ".java":
                    return f
            return None
        suffix = name.replace(".", "/")
        ordered = sorted(files)
        if ref.get("names") == ["*"]:
            for fp in ordered:
                if fp.endswith(".java") and os.path.dirname(fp).endswith(suffix):
                    return "dir:" + os.path.dirname(fp)
            return None
        for fp in ordered:
            if fp.endswith(suffix + ".java"):
                return fp
        parent = "/".join(suffix.split("/")[:-1])
        for fp in ordered:
            if parent and fp.endswith(parent + ".java"):
                return fp
        return None
    if lang == "rust":
        parts = [p for p in re.split(r"::|\{|\}|,|\s", name) if p and p != "*"]
        if not parts:
            return None
        crates = workspaces(root_dir, files)["rust"]
        # crate root: an explicit [lib]/[[bin]] source dir containing this file, else <nearest Cargo.toml dir>/src
        crate_src = None
        for rd in sorted(crates.get("__roots__", ()), key=len, reverse=True):
            if d == rd or d.startswith(rd + "/"):
                crate_src = rd
                break
        if crate_src is None:
            crate_dir = None
            probe = d
            while True:
                if os.path.exists(os.path.join(root_dir, probe, "Cargo.toml")):
                    crate_dir = probe
                    break
                if probe in ("", "."):
                    break
                probe = os.path.dirname(probe)
            crate_src = os.path.normpath(os.path.join(crate_dir or "", "src")).replace(os.sep, "/").lstrip("./") or "src"
        inline_module = ref.get("src") not in (None, file_path)   # `use` inside `mod tests { ... }`: super/self are this file
        fname = os.path.basename(file_path)
        # the module directory a file's `self::` refers to: foo.rs -> foo/, mod.rs|lib.rs|main.rs -> its own dir
        own_dir = d if fname in ("mod.rs", "lib.rs", "main.rs") else os.path.join(d, fname[:-3])
        head = parts[0]
        bases = []
        if head == "crate":
            bases, rest = [crate_src], parts[1:]
        elif head == "self":
            bases, rest = [own_dir, d], parts[1:]
        elif head == "super":
            parent_dir = os.path.dirname(own_dir)
            bases, rest = [parent_dir, os.path.dirname(parent_dir)], parts[1:]
        elif head in crates and head != "__roots__":
            crate_root_src = os.path.normpath(os.path.join(crates[head], "src")).replace(os.sep, "/").lstrip("./")
            bases, rest = [crate_root_src], parts[1:]
            for k in range(len(rest), 0, -1):
                c = os.path.normpath(os.path.join(crate_root_src, *rest[:k])).replace(os.sep, "/").lstrip("./")
                for suf in (".rs", "/mod.rs"):
                    if c + suf in files:
                        return c + suf
            for suf in ("/lib.rs", "/main.rs"):   # `use other_crate::Item`: the item lives at the crate root
                if crate_root_src + suf in files:
                    return crate_root_src + suf
            return None
        else:
            bases, rest = [own_dir, d, crate_src], parts   # bare path: a sibling module or a crate-level module
        if head in ("super", "self") and inline_module:
            return file_path   # the enclosing module is this file (an inline `mod`): bind the names here
        for base in bases:
            for k in range(len(rest), 0, -1):
                c = os.path.normpath(os.path.join(base, *rest[:k])).replace(os.sep, "/").lstrip("./")
                for suf in (".rs", "/mod.rs"):
                    if c + suf in files:
                        return c + suf
            if not rest:
                for suf in ("/mod.rs", ".rs"):
                    c = os.path.normpath(base).replace(os.sep, "/").lstrip("./") + suf
                    if c in files:
                        return c
        if head == "super":
            # super of a top-level module file (src/foo.rs) is the crate root
            for suf in ("/lib.rs", "/main.rs"):
                if crate_src + suf in files:
                    return crate_src + suf
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


def git_stamp(root_dir, includes, excludes, out_path=None):
    """Fingerprint of the git working tree under root_dir: HEAD, porcelain status (tracked changes and
    untracked files) and the content of every listed source file. None when root_dir is not in a git
    repo. The graph's own directory and files the graph does not index (unsupported extensions) are
    ignored. Limit: edits to git-ignored source files are invisible to the stamp (use --full)."""
    out_dir = os.path.dirname(os.path.abspath(out_path)) if out_path else None
    STAMP_EXTRA = {"go.mod", "modules.json"}   # non-source files that change how the graph links
    def git(*a):
        return subprocess.run(["git", "-C", root_dir, *a], capture_output=True, text=True, check=True).stdout
    try:
        top = git("rev-parse", "--show-toplevel").strip()
        head = git("rev-parse", "HEAD").strip()
        status = git("-c", "core.quotepath=off", "status", "--porcelain", "--untracked-files=all", "--no-renames", "--", ".")
    except (OSError, subprocess.CalledProcessError):
        return None
    h = hashlib.sha1()
    try:
        with open(os.path.abspath(__file__), "rb") as f:
            engine = hashlib.sha1(f.read()).hexdigest()   # a changed engine must re-link even at the same GRAPH_VERSION
    except OSError:
        engine = ""
    h.update(f"{GRAPH_VERSION}|{engine}|{head}|{sorted(includes or [])}|{sorted(excludes or [])}|{root_dir}\n".encode())
    for line in status.splitlines():
        path = line[3:]
        if path.startswith('"') and path.endswith('"'):
            path = path[1:-1]
        ap = os.path.abspath(os.path.join(top, path))
        if out_dir and (ap == out_dir or ap.startswith(out_dir + os.sep)):
            continue   # the graph and its stamp live here; they change on every build
        if os.path.splitext(path)[1].lower() not in EXT_LANG and os.path.basename(path) not in STAMP_EXTRA:
            continue   # not indexed, cannot change the graph
        h.update(line.encode() + b"\n")
        try:
            with open(ap, "rb") as f:
                h.update(hashlib.sha1(f.read()).digest())
        except OSError:
            h.update(b"-")
    return h.hexdigest()


def build_graph(root_dir, out_path, includes, excludes, full=False, quiet=False):
    t0 = time.time()
    root_dir = os.path.abspath(root_dir)
    # Fast path: if the git working tree is byte-identical to the one the graph was built from, the
    # graph is current and even the (always full) link pass can be skipped.
    stamp = git_stamp(root_dir, includes, excludes, out_path)
    stamp_path = out_path + ".stamp"
    if stamp and not full and os.path.exists(out_path) and os.path.exists(stamp_path):
        try:
            with open(stamp_path) as f:
                prev = json.load(f)
        except (OSError, ValueError):
            prev = None
        if prev and prev.get("stamp") == stamp and prev.get("version") == GRAPH_VERSION:
            if not quiet:
                s = prev.get("stats", {})
                print(f"graph at {out_path} is up to date (git working tree unchanged since {prev.get('built_at')}): "
                      f"{s.get('files')} files, {s.get('nodes')} nodes, {s.get('edges')} edges, {time.time() - t0:.1f}s")
            return None
    old = None
    try:
        with open(os.path.abspath(__file__), "rb") as f:
            engine_hash = hashlib.sha1(f.read()).hexdigest()
    except OSError:
        engine_hash = ""
    if not full and os.path.exists(out_path):
        try:
            old = load_graph(out_path)
            # cached parses are only reusable if the engine that produced them is byte-identical:
            # extractors change what a parse records (argument types, re-export names, ...)
            if old.get("version") != GRAPH_VERSION or old.get("engine") != engine_hash:
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
    graph["engine"] = engine_hash
    graph["root"] = root_dir
    graph["built_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    graph["files"] = files
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(graph, f, separators=(",", ":"))
    if stamp:
        with open(stamp_path, "w") as f:
            json.dump({"version": GRAPH_VERSION, "stamp": stamp, "stats": graph["stats"], "built_at": graph["built_at"]}, f)
    elif os.path.exists(stamp_path):
        os.remove(stamp_path)
    if not quiet:
        s = graph["stats"]
        print(f"graph written to {out_path}: {s['files']} files ({changed} parsed, {reused} unchanged), "
              f"{s['nodes']} nodes, {s['edges']} edges, {s['unresolved_refs']} unresolved refs, {time.time() - t0:.1f}s")
        for lang, c in sorted(s["languages"].items()):
            print(f"  {lang}: {c} files")
        if s["files"] == 0:
            print("  no supported source files found under this root. Supported extensions: "
                  + ", ".join(sorted(EXT_LANG)) + "; directories skipped by default: "
                  + ", ".join(sorted(DEFAULT_EXCLUDE_DIRS)) + ". Check --root, --include and --exclude.", file=sys.stderr)
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

    # Go receiver methods belong to their struct/interface, not to the file. Do this before any
    # reference is resolved so that a fresh parse and a cached (already re-parented) node behave the
    # same: a method's reference to its own type is never an edge, and typed matching sees the owner.
    drop_contains = set()   # (old parent, method id) pairs whose file->method `contains` edge is replaced
    for n in nodes:
        if n["kind"] == "method" and n["extra"].get("receiver_type"):
            d = os.path.dirname(n["file"]) or "."
            owners = [c for c in by_name.get(n["extra"]["receiver_type"], []) if c["kind"] in ("struct", "interface", "type") and (os.path.dirname(c["file"]) or ".") == d]
            if owners and n["parent"] != owners[0]["id"]:
                drop_contains.add((n["parent"], n["id"]))
                n["parent"] = owners[0]["id"]
                edges.append({"src": owners[0]["id"], "dst": n["id"], "type": "contains", "line": n["line"], "confidence": "exact"})
    if drop_contains:   # one filtered pass (a per-method rebuild of the edge list was quadratic: 12s on a 3.6k-file Go repo)
        edges[:] = [e for e in edges if not (e["type"] == "contains" and (e["src"], e["dst"]) in drop_contains)]

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

    java_pkgs = defaultdict(list)
    kt_top = {}
    for rel, info in files.items():
        lang_ = info["file_node"]["extra"].get("language")
        if lang_ in ("java", "kotlin"):
            java_pkgs[info["file_node"]["extra"].get("package") or ""].append(rel)
        if lang_ == "kotlin":   # a Kotlin file may define several top-level classes/functions
            kt_top[rel] = {n["name"] for n in info["nodes"] if n["parent"] == rel}
    res_ctx = {"java_pkgs": {k: sorted(v) for k, v in java_pkgs.items()}, "kt_top": kt_top}
    imports_of = defaultdict(set)   # file -> set of files it imports (resolved)
    ns_repo = defaultdict(dict)     # file -> {local name: set(files it is bound to)} for repo imports
    ns_ext = defaultdict(set)       # file -> local names bound to external packages/modules
    import_links = defaultdict(list)  # file -> [(names, alias_map, target files)] for re-export following
    ext_nodes = {}
    unresolved = 0
    assets = 0
    stats_conf = defaultdict(int)
    dir_files = defaultdict(list)
    for fp in files:
        dir_files[os.path.dirname(fp) or "."].append(fp)

    def go_pkg_guess(path):
        """`github.com/x/go-version/v2` -> version, `gopkg.in/yaml.v3` -> yaml."""
        parts = path.rstrip("/").split("/")
        last = parts[-1]
        if re.fullmatch(r"v\d+", last) and len(parts) > 1:
            last = parts[-2]
        last = re.sub(r"\.v\d+$", "", last)
        if last.startswith("go-"):
            last = last[3:]
        return last.replace("-", "_")

    def external_bindings(r, lang):
        """Local names an unresolved import binds (alias, imported names, guessed package name)."""
        b = {x for x in r.get("names", []) if x != "*"}
        am = r.get("alias_map") or {}
        b = {am.get(x, x) for x in b}
        if r.get("alias"):
            b.add(r["alias"])
        elif lang == "go":
            b.add(go_pkg_guess(r["name"]))
        elif lang == "python" and not b:
            b.add(r["name"].lstrip(".").split(".")[0])
        elif lang == "rust":
            txt = r["name"].replace("{", " ").replace("}", " ").replace("*", " ")
            for part in txt.split(","):
                seg = part.strip().split(" as ")[-1].strip().split("::")[-1].strip()
                if seg and seg != "self":
                    b.add(seg)
            b.add(r["name"].split("::")[0].strip())
        return b

    def repo_bindings(r, lang, target):
        """{local name: set(files)} an import of a repo target binds. `*` marks a wildcard import."""
        tfiles = set(dir_files[target[4:]]) if target.startswith("dir:") else {target}
        if lang in ("java", "kotlin") and target.startswith("dir:"):
            tfiles = set(res_ctx["java_pkgs"].get(r["name"], tfiles))   # a package may span src/main and src/test
        out = {}
        am = r.get("alias_map") or {}
        names = r.get("names", [])
        if r.get("alias"):
            out[r["alias"]] = set(tfiles)
        elif lang == "go":
            pkg = dir_nodes.get(("package", target[4:]), {}).get("extra", {}).get("package") if target.startswith("dir:") else None
            out[pkg or go_pkg_guess(r["name"])] = set(tfiles)
        elif lang == "python" and not names:
            # `import a.b.c`: bind every dotted prefix that is a package/module so `a.b.c.f()` and `a.f()` both resolve
            mod = r["name"]
            parts = mod.lstrip(".").split(".")
            for k in range(1, len(parts) + 1):
                pref = ".".join(parts[:k])
                t = target if k == len(parts) else resolve_import({"name": pref, "names": []}, r["_file"], lang, file_set, root_dir, go_module, res_ctx)
                if t and not t.startswith("dir:"):
                    out.setdefault(pref, set()).add(t)
        elif lang == "rust":
            for nm in external_bindings(r, lang):
                out.setdefault(nm, set()).update(tfiles)
        for nm in names:
            if nm == "*":
                out.setdefault("*", set()).update(tfiles)
                continue
            bound = set()
            if lang == "python":
                # `from pkg import mod`: bind to the module file when it exists, not to pkg/__init__.py
                for t in tfiles:
                    base = os.path.dirname(t) if os.path.basename(t) == "__init__.py" else None
                    if base is not None:
                        for cand in (f"{base}/{nm}.py", f"{base}/{nm}/__init__.py"):
                            cand = cand.lstrip("./")
                            if cand in file_set:
                                bound.add(cand)
            out.setdefault(am.get(nm, nm), set()).update(bound or tfiles)
        return out

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
                r = dict(r, _file=rel)
                target = resolve_import(r, rel, lang, file_set, root_dir, go_module, res_ctx)
                if target == "asset":
                    assets += 1
                    continue
                if target is None:
                    ns_ext[rel].update(external_bindings(r, lang))
                    if lang in ("javascript", "typescript", "tsx", "go", "python", "java", "rust"):
                        add_edge(r["src"], external(external_name(r["name"], lang))["id"], "imports", r["line"], "external", names=r.get("names", []))
                    else:
                        unresolved += 1
                    continue
                bound = repo_bindings(r, lang, target)
                for nm, fset in bound.items():
                    ns_repo[rel].setdefault(nm, set()).update(fset)
                import_links[rel].append((set(r.get("names", [])), r.get("alias_map") or {}, set().union(*bound.values()) if bound else set()))
                if target.startswith("dir:"):
                    dd = target[4:]
                    key = ("package", dd)
                    dst = dir_nodes[key]["id"] if key in dir_nodes else None
                    if dst is None:
                        # Java package dir: link to each file in it
                        for fp in dir_files[dd]:
                            imports_of[rel].add(fp)
                            add_edge(r["src"], fp, "imports", r["line"], "exact", names=r.get("names", []))
                        continue
                    imports_of[rel].update(dir_files[dd])
                    add_edge(r["src"], dst, "imports", r["line"], "exact", names=r.get("names", []))
                elif target == rel:
                    pass   # `use super::*` inside an inline module: names bound (above), no self-import edge
                else:
                    imports_of[rel].add(target)
                    add_edge(r["src"], target, "imports", r["line"], "exact", names=r.get("names", []))
                    # `from pkg import mod` also depends on pkg/mod.py itself
                    for fset in bound.values():
                        for extra_t in fset - {target}:
                            imports_of[rel].add(extra_t)
                            add_edge(r["src"], extra_t, "imports", r["line"], "exact", names=r.get("names", []))
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

    # Names defined at the top level of each file (for re-export following)
    top_defs = defaultdict(dict)   # file -> {name: [nodes]}
    for n in nodes:
        if n.get("file") and n.get("parent") == n["file"] and n["kind"] not in ("file",):
            top_defs[n["file"]].setdefault(n["name"], []).append(n)

    _fd_cache = {}

    def files_defining(name, fset, depth=3, seen=None):
        """Files among `fset` that define `name` at top level, following `from x import name` re-exports
        (and wildcard imports) up to `depth` levels. Memoized on (name, files) for the top-level call."""
        if seen is None:
            key = (name, frozenset(fset))
            hit = _fd_cache.get(key)
            if hit is None:
                hit = _fd_cache[key] = files_defining(name, fset, depth, {})
            return hit
        out = set()
        for f in sorted(fset):   # sorted: the result must not depend on set iteration order
            if seen.get((f, name), -1) >= depth:
                continue         # already explored from here with at least this much depth left
            seen[(f, name)] = depth
            if name in top_defs.get(f, {}):
                out.add(f)
                continue
            if depth <= 0:
                continue
            for names, am, targets in import_links.get(f, ()):
                local = {am.get(x, x) for x in names}
                if name in local or "*" in names:
                    orig = next((x for x in sorted(names) if am.get(x, x) == name), name)
                    out |= files_defining(orig, targets, depth - 1, seen)
        return out

    # Field type lookup for typed call resolution: class node id -> {field name: type}
    field_types = defaultdict(dict)
    field_qual = defaultdict(dict)   # class node id -> {field name: package qualifier of its type or None}
    for n in nodes:
        if n["kind"] == "field" and n["parent"] and n["extra"].get("type"):
            if n["extra"]["type"].startswith("<call"):
                field_types[n["parent"]][n["name"]] = n["extra"]["type"]   # resolved from the callee's return type on use
                field_qual[n["parent"]][n["name"]] = None
                continue
            q, base = split_qualifier(n["extra"]["type"])
            field_types[n["parent"]][n["name"]] = base
            field_qual[n["parent"]][n["name"]] = q

    def is_within(node, ancestor_id):
        """True when `node` is `ancestor_id` or nested anywhere inside it."""
        n = node
        hops = 0
        while n is not None and hops < 20:
            if n["id"] == ancestor_id:
                return True
            n = node_by_id.get(n.get("parent")) if n.get("parent") else None
            hops += 1
        return False

    def container_of(node_id):
        n = node_by_id.get(node_id)
        while n is not None and n["kind"] not in CONTAINER_KINDS and n["parent"]:
            n = node_by_id.get(n["parent"])
        return n if n is not None and n["kind"] in CONTAINER_KINDS else None

    overload_stub = set()   # ids of @overload-annotated definitions that have a real implementation in their file
    for name_, group in by_name.items():
        stubs = [c for c in group if c["kind"] in ("function", "method") and any(a.split(".")[-1].startswith("overload") for a in c.get("annotations", []))]
        for st in stubs:
            if any(c is not st and c["kind"] == st["kind"] and c["file"] == st["file"] and c.get("parent") == st.get("parent")
                   and not any(a.split(".")[-1].startswith("overload") for a in c.get("annotations", [])) for c in group):
                overload_stub.add(st["id"])

    def candidates(name, kinds):
        return [c for c in by_name.get(name, []) if c["kind"] in kinds and c["id"] not in overload_stub]

    _arity_cache = {}
    _params_cache = {}

    def params_of(node):
        """Parameter strings of a callable from its signature text (self/cls/receiver removed), or None."""
        if node["id"] in _params_cache:
            return _params_cache[node["id"]]
        sig = node.get("signature") or ""
        lang = lang_of(node["file"]) if node.get("file") in files else None
        res = None
        idx = sig.find(node["name"] + "(")
        if idx < 0:
            idx = sig.find("(")
        else:
            idx += len(node["name"])
        if 0 <= idx < len(sig) and sig[idx] == "(":
            depth, j = 0, idx
            while j < len(sig):
                ch = sig[j]
                if ch in "([{<":
                    depth += 1
                elif ch in ")]}>":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            inner = sig[idx + 1:j]
            params, cur, depth = [], "", 0
            for ch in inner:
                if ch in "([{<":
                    depth += 1
                elif ch in ")]}>":
                    depth -= 1
                if ch == "," and depth == 0:
                    params.append(cur)
                    cur = ""
                else:
                    cur += ch
            params.append(cur)
            params = [p.strip() for p in params if p.strip()]
            if lang == "python" and node["kind"] == "method" and params and params[0].split(":")[0].split("=")[0].strip() in ("self", "cls"):
                params = params[1:]
            if lang == "rust":
                params = [p for p in params if p.replace("&", "").replace("mut ", "").strip() != "self"]
            if lang == "python":
                params = [p for p in params if p not in ("/", "*")]
            res = params
        _params_cache[node["id"]] = res
        return res

    def param_type(p, lang):
        """Type text of one parameter string, per language conventions; '' when unknown."""
        p = p.strip()
        if lang in ("python", "typescript", "tsx", "rust", "kotlin"):
            if ":" in p:
                t = p.split(":", 1)[1].split("=")[0].strip()
                return t + "..." if p.startswith("vararg ") else t
            return ""
        if lang == "go":
            parts = p.split(None, 1)
            return parts[1].strip() if len(parts) == 2 else p   # `x []string` or bare type in grouped params
        if lang == "java":
            toks = [t for t in p.replace("final ", "").split() if t]
            if len(toks) >= 2:
                return " ".join(toks[:-1]) if not toks[-1].startswith("...") else " ".join(toks[:-1]) + "..."
            return p
        return ""

    def norm_type(t):
        t = t.replace("final ", "").replace("mut ", "").replace("&", "").replace(" ", "")
        t = re.sub(r"<[^<>]*>", "", t)
        while "<" in t and ">" in t:
            t = re.sub(r"<[^<>]*>", "", t)
        return t.split(".")[-1] if "..." not in t else t.split(".")[-4] + "..." if t.count(".") > 3 else t

    _sub_cache = {}

    def is_subtype(sub_name, sup_name):
        """True when a repo type named sub_name extends/implements (transitively) one named sup_name."""
        key = (sub_name, sup_name)
        if key in _sub_cache:
            return _sub_cache[key]
        res = False
        for start in candidates(sub_name, TYPE_LIKE_KINDS):
            seen, frontier = set(), [start]
            while frontier and not res:
                x = frontier.pop()
                if x["id"] in seen:
                    continue
                seen.add(x["id"])
                for pn in parents_of.get(x["id"], []):
                    if pn["name"] == sup_name:
                        res = True
                        break
                    frontier.append(pn)
            if res:
                break
        _sub_cache[key] = res
        return res

    def compat_score(param, arg):
        """0 = clearly incompatible; 4 exact, 3 subtype, 2 `Object/Any`, 1 generic/unknown."""
        if not compatible(param, arg):
            return 0
        p, a = norm_type(param), norm_type(arg)
        if not p or not a:
            return 1
        pb, ab = p.rstrip("[].").lstrip("[]"), a.rstrip("[].").lstrip("[]")
        if pb == ab:
            return 4                         # exact
        if len(pb) == 1 and pb.isupper():
            return 1                         # generic parameter
        if pb in ("Object", "Any"):
            return 2 if ab not in PRIMITIVE_TYPES else 0   # accepts anything: least specific match
        if candidates(pb, TYPE_LIKE_KINDS):
            # a repo type: a primitive/String argument cannot be one; a repo subtype is a real match
            if ab in PRIMITIVE_TYPES or ab == "String":
                return 0
            return 3 if is_subtype(ab, pb) else 1
        return 1

    def compatible(param, arg):
        """False only when both types are known and clearly incompatible (primitive mismatch, array-ness)."""
        p, a = norm_type(param), norm_type(arg)
        if not p or not a:
            return True
        if p == a:
            return True
        if a == "null":
            return p not in PRIMITIVE_TYPES
        p_var = p.endswith("...")
        p_arr = p.endswith("[]") or p.startswith("[]") or p_var
        a_arr = a.endswith("[]") or a.startswith("[]") or a.endswith("...")
        if p_var and not a_arr:
            return compatible(p[:-3], a)   # a single element passed to varargs
        if p_arr != a_arr:
            return False
        pb, ab = p.rstrip("[].").lstrip("[]"), a.rstrip("[].").lstrip("[]")
        if pb == ab:
            return True
        if len(pb) == 1 and pb.isupper():
            return True   # generic parameter
        if pb == "Object":
            return ab not in PRIMITIVE_TYPES
        if pb in PRIMITIVE_TYPES or ab in PRIMITIVE_TYPES:
            return False
        return True   # two reference types: subtyping unknown, keep

    def arity(node):
        """(min, max) parameter counts from the signature text; max None for varargs; None if unknown."""
        if node["id"] in _arity_cache:
            return _arity_cache[node["id"]]
        sig = node.get("signature") or ""
        lang = lang_of(node["file"]) if node.get("file") in files else None
        res = None
        # the parameter list is the first "(...)" after the symbol's name
        idx = sig.find(node["name"] + "(")
        if idx < 0:
            idx = sig.find("(")
        else:
            idx += len(node["name"])
        if idx >= 0 and idx < len(sig) and sig[idx] == "(":
            depth, j = 0, idx
            while j < len(sig):
                ch = sig[j]
                if ch in "([{<":
                    depth += 1
                elif ch in ")]}>":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            inner = sig[idx + 1:j]
            params, cur, depth = [], "", 0
            for ch in inner:
                if ch in "([{<":
                    depth += 1
                elif ch in ")]}>":
                    depth -= 1
                if ch == "," and depth == 0:
                    params.append(cur)
                    cur = ""
                else:
                    cur += ch
            params.append(cur)
            params = [p.strip() for p in params if p.strip()]
            if lang == "python" and node["kind"] == "method" and params and params[0].split(":")[0].split("=")[0].strip() in ("self", "cls"):
                params = params[1:]
            if lang == "rust":
                params = [p for p in params if p.replace("&", "").replace("mut ", "").strip() != "self"]
            if lang == "python":
                params = [p for p in params if p not in ("/", "*")]
            varargs = any(p.startswith(("*", "...", "vararg ")) or "..." in p.split("=")[0] for p in params)
            required = [p for p in params if "=" not in p and "?:" not in p and not p.startswith(("*", "...", "vararg "))]
            res = (len(required), None if varargs else len(params))
        _arity_cache[node["id"]] = res
        return res

    _cur = {"argc": None, "arg_types": None, "rel": None, "cont": None}   # the call being resolved

    def arg_type_text(a):
        """Materialize an argument type recorded as `<call>expr|root`, `<field>name` or `<chain>a.b|root`
        into type text. Nested resolution runs with the argument state cleared (no re-entry)."""
        if a is None or not a.startswith("<"):
            return a
        saved = dict(_cur)
        _cur.update({"argc": None, "arg_types": None})
        try:
            return _arg_type_text(a)
        finally:
            _cur.clear()
            _cur.update(saved)

    def _arg_type_text(a):
        rel, cont = _cur.get("rel"), _cur.get("cont")
        if a.startswith("<field>"):
            fname = a[len("<field>"):]
            if cont is not None:
                for oid in owner_ids(cont):
                    if fname in field_types.get(oid, {}):
                        q = field_qual[oid].get(fname)
                        return (q + "." if q else "") + field_types[oid][fname]
            return None
        if a.startswith("<chain>"):
            body = a[len("<chain>"):]
            expr, _, root_type = body.partition("|")
            parts = expr.split(".")
            tn = None
            if root_type:
                kind, tn = resolve_type_ref(root_type, rel, cont)
                rest = parts[1:]
            elif parts[0][:1].isupper():
                tn = type_node(parts[0], rel)
                rest = parts[1:]
            elif cont is not None and any(parts[0] in field_types.get(oid, {}) for oid in owner_ids(cont)):
                tn, rest = cont, parts
            else:
                return None
            if tn is None:
                return None
            t = follow_chain(tn, rest)
            return t["name"] if t and t != "" else None
        body = a.split(">", 1)[1]
        expr, _, root_type = body.partition("|")
        hint, name = (expr.rsplit(".", 1) if "." in expr else (None, expr))
        fake = {"kind": "call", "name": name.strip(), "hint": hint, "src": cont["id"] if cont else rel, "line": 0, "argc": None}
        if hint and root_type:
            fake["hint_type"] = root_type
            fake["chain"] = [seg.split("(")[0].strip("*&!? ") for seg in hint.split(".")][1:]
        saved = dict(_cur)
        try:
            targets, conf, mode = resolve_call(fake, rel, lang_of(rel), cont)
        finally:
            _cur.update(saved)
        if not targets:
            return None
        return return_type(targets[0])

    def by_arity(cands):
        """Among same-named callables, keep those whose parameter count admits the call's argument count,
        then those whose parameter types are compatible with the known argument types."""
        argc = _cur["argc"]
        if argc is None or len(cands) <= 1 or not any(c["kind"] in ("function", "method", "constructor") for c in cands):
            return cands   # not an overload choice (types, packages): argument state does not apply
        ok = []
        for c in cands:
            if c["kind"] not in ("function", "method", "constructor"):
                ok.append(c)
                continue
            ar = arity(c)
            if ar is None or (ar[0] <= argc and (ar[1] is None or argc <= ar[1])):
                ok.append(c)
        ok = ok or cands
        at = _cur.get("arg_types")
        if len(ok) > 1 and at:
            at = [arg_type_text(a) for a in at]
            if not any(at):
                at = None
        if len(ok) > 1 and at:
            scored = []
            for c in ok:
                ps = params_of(c) if c["kind"] in ("function", "method", "constructor") else None
                if not ps:
                    scored.append((1, c))
                    continue
                lang = lang_of(c["file"]) if c.get("file") in files else None
                total, good = 0, True
                for i, a in enumerate(at):
                    if a is None:
                        continue
                    if i < len(ps):
                        pt = param_type(ps[i], lang)
                    elif ps and param_type(ps[-1], lang).endswith("..."):
                        pt = param_type(ps[-1], lang)
                    else:
                        continue
                    sc = compat_score(pt, a)
                    if sc == 0:
                        good = False
                        break
                    total += sc
                if good:
                    scored.append((total, c))
            if scored:
                best = max(t for t, _ in scored)
                ok = [c for t, c in scored if t == best]
        if len(ok) > 1 and all(c["qname"] == ok[0]["qname"] and c["file"] == ok[0]["file"] for c in ok):
            _cur["overload_undecided"] = True   # same-arity overloads the arguments cannot separate
        return ok

    def lang_of(rel):
        return files[rel]["file_node"]["extra"].get("language")

    def same_package(a, b):
        """Go: same directory. Java: same declared package (src/main and src/test share packages)."""
        if lang_of(a) in ("java", "kotlin") and lang_of(b) in ("java", "kotlin"):
            pa = files[a]["file_node"]["extra"].get("package")
            pb = files[b]["file_node"]["extra"].get("package")
            if pa and pb:
                return pa == pb
        return os.path.dirname(a) == os.path.dirname(b)

    def same_family(cands, rel):
        fam = LANG_FAMILY.get(lang_of(rel))
        return [c for c in cands if LANG_FAMILY.get(lang_of(c["file"])) == fam] if fam else cands

    def pick(cands, rel):
        """Free-name choice: same_file > package > import > unique > ambiguous (types and free calls)."""
        if not cands:
            return [], None
        same = [c for c in cands if c["file"] == rel]
        if same:
            return by_arity(same)[:1], "same_file"
        lang = lang_of(rel)
        if lang in ("java", "kotlin", "go"):  # same package, no import needed
            pkg = [c for c in cands if same_package(c["file"], rel)]
            if pkg:
                return by_arity(pkg)[:1], "package"
        imp = [c for c in cands if c["file"] in imports_of.get(rel, ())]
        if imp:
            return by_arity(imp)[:1], "import"
        if len({c["file"] for c in cands}) == 1:
            # one definition site: a struct plus its impl blocks, or a class and an overload, count as unique
            primary = [c for c in cands if c["kind"] != "impl"] or cands
            return primary[:1], "unique"
        return cands[:5], "ambiguous"

    _defs_cache = {}

    def defs_in(name, fset, depth=3, seen=None):
        """Top-level definitions of `name` reachable from `fset`, following `from x import name`,
        `export { orig as name } from`, `pub use` and wildcard re-exports up to `depth` levels. Unlike
        files_defining this returns the definition nodes, so an aliased re-export resolves to the
        definition under its original name."""
        if seen is None:
            key = (name, frozenset(fset))
            hit = _defs_cache.get(key)
            if hit is None:
                hit = _defs_cache[key] = defs_in(name, fset, depth, {})
            return hit
        out = []
        for f in sorted(fset):
            if seen.get((f, name), -1) >= depth:
                continue
            seen[(f, name)] = depth
            if name in top_defs.get(f, {}):
                out.extend(top_defs[f][name])
                continue
            if depth <= 0:
                continue
            for names, am, targets in import_links.get(f, ()):
                local = {am.get(x, x) for x in names}
                if name in local or "*" in names:
                    orig = next((x for x in sorted(names) if am.get(x, x) == name), name)
                    out.extend(defs_in(orig, targets, depth - 1, seen))
        return out

    def bound_pick(cands, name, fset, kinds=None):
        """Definitions of `name` in (or re-exported through) the files an import bound it to."""
        kinds = kinds if kinds is not None else ({c["kind"] for c in cands} or {"function", "class", "struct", "constructor", "method"})
        nodes_ = [d for d in defs_in(name, fset) if d["kind"] in kinds and d["id"] not in overload_stub]
        return (by_arity(nodes_)[:1], "import") if nodes_ else ([], None)

    def lead_pick(cands, rel):
        """Receiver of unknown type: a same-file definition is plausible; otherwise a few same-language leads."""
        cands = same_family(cands, rel)
        if not cands:
            return [], None
        same = [c for c in cands if c["file"] == rel]
        if same:
            return same[:1], "same_file"
        if len(cands) > 3:
            return [], None   # 3 of 40 `copy` methods is noise, not a lead
        return cands, "ambiguous"

    def type_node(type_name, rel, fset=None):
        """Container node for a type name in the context of `rel` (or of an import binding). None when ambiguous."""
        cands = candidates(type_name, TYPE_LIKE_KINDS | {"impl"})
        if fset is not None:
            targets, conf = bound_pick(cands, type_name, fset, TYPE_LIKE_KINDS | {"impl"})
        else:
            targets, conf = pick(cands, rel)
        if not targets or conf == "ambiguous":
            return None
        return targets[0]

    def owner_ids(tn):
        """Node ids whose children are members of type `tn`: the node itself plus, for Rust, the `impl`
        blocks of that name in its file. Ordered (the type first, impls by line) so nothing depends on
        set iteration order; two unrelated same-named classes in one file are never merged."""
        ids = [tn["id"]]
        for alt in sorted((a for a in by_name.get(tn["name"], []) if a["file"] == tn["file"] and a["kind"] == "impl" and a["id"] != tn["id"]),
                          key=lambda a: a["line"]):
            ids.append(alt["id"])
        for kid in by_file.get(tn["file"], []):   # Kotlin: companion object members are reachable as Outer.member()
            if kid.get("parent") == tn["id"] and kid["kind"] == "class" and kid["extra"].get("companion"):
                ids.append(kid["id"])
        return ids

    def typed_match(cands, tn):
        ids = owner_ids(tn)
        d = os.path.dirname(tn["file"])
        return [c for c in cands if c["parent"] in ids
                or (c["qname"].startswith(tn["name"] + ".") and os.path.dirname(c["file"]) == d and lang_of(c["file"]) in ("go", "rust"))
                or (c["extra"].get("receiver_type") == tn["name"] and lang_of(c["file"]) == "kotlin")]

    def return_type(node):
        """Return type text of a callable from its signature, or None (void, unknown, unit)."""
        if node["kind"] not in ("function", "method", "constructor"):
            return None
        sig = node.get("signature") or ""
        idx = sig.find(node["name"] + "(")
        if idx < 0:
            return None
        j, depth = idx + len(node["name"]), 0
        while j < len(sig):
            if sig[j] in "([{<":
                depth += 1
            elif sig[j] in ")]}>":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        tail = re.sub(r"\bwhere\b.*$", "", sig[j + 1:]).strip()
        if tail.startswith("->"):
            tail = tail[2:].strip()
        elif tail.startswith(":"):
            tail = tail[1:].strip()
        elif tail.startswith("=>"):
            return None
        if tail.startswith("("):   # Go multiple / named returns: first one
            tail = tail[1:].split(",")[0].strip()
            tail = tail.split()[-1] if " " in tail else tail
        tail = tail.rstrip(":;{ ").strip()
        if not tail or tail in ("void", "None", "()", "!", "never", "undefined"):
            return None
        return tail

    def unwrap_generic(t):
        """Result<T, E> / Option<T> / Promise<T> / Optional<T> -> T (first generic argument)."""
        m = re.match(r"^(?:[\w:.]+(?:::|\.))?(Result|Option|Promise|Optional|Task|Future|Awaitable)\s*<(.+)>$", t.strip())
        if not m:
            return t
        inner, depth, out = m.group(2), 0, ""
        for ch in inner:
            if ch in "<([":
                depth += 1
            elif ch in ">)]":
                depth -= 1
            if ch == "," and depth == 0:
                break
            out += ch
        return out.strip()

    _bounds_cache = {}

    def generic_bounds(tn):
        """{type parameter: first bound or None} from a container's `<...>`/`where` text and, for Rust,
        from the impl blocks of that type in the same file."""
        if tn["id"] in _bounds_cache:
            return _bounds_cache[tn["id"]]
        texts = [tn.get("signature") or ""]
        for alt in by_name.get(tn["name"], []):
            if alt["kind"] == "impl" and alt["file"] == tn["file"]:
                texts.append(alt.get("signature") or "")
        out = {}
        for txt in texts:
            i = txt.find("<")
            if i >= 0:
                depth, j = 0, i
                while j < len(txt):
                    if txt[j] == "<":
                        depth += 1
                    elif txt[j] == ">":
                        depth -= 1
                        if depth == 0:
                            break
                    j += 1
                items, cur, depth = [], "", 0
                for ch in txt[i + 1:j]:
                    if ch in "<([":
                        depth += 1
                    elif ch in ">)]":
                        depth -= 1
                    if ch == "," and depth == 0:
                        items.append(cur)
                        cur = ""
                    else:
                        cur += ch
                items.append(cur)
                for it in items:
                    it = it.strip()
                    if not it or it.startswith("'") or it.startswith("const "):
                        continue
                    m = re.match(r"^([A-Za-z_]\w*)\s*(?::|\bextends\b|\bimplements\b)\s*([^+&]+)", it)
                    if m:
                        out.setdefault(m.group(1), m.group(2).strip())
                    else:
                        out.setdefault(re.split(r"[\s:=]", it, 1)[0], None)
            w = re.search(r"\bwhere\b(.*)$", txt)
            if w:
                for it in w.group(1).split(","):
                    m = re.match(r"^\s*([A-Za-z_]\w*)\s*:\s*([^+]+)", it)
                    if m:
                        out[m.group(1)] = m.group(2).strip()
        _bounds_cache[tn["id"]] = out
        return out

    _rt_depth = [0]

    def resolve_type_ref(type_text, rel, cont=None, depth=0):
        """Declared/field type text -> ("ext", None) | ("node", tn) | ("none", None).
        `<call>expr|roottype` (a local bound to a call result) is typed from the callee's return type."""
        if _rt_depth[0] > 8:
            return "none", None   # pathological chains (x = f(x)): give up rather than recurse
        _rt_depth[0] += 1
        try:
            return _resolve_type_ref(type_text, rel, cont, depth)
        finally:
            _rt_depth[0] -= 1

    def _resolve_type_ref(type_text, rel, cont=None, depth=0):
        if type_text.startswith("<call"):
            if depth >= 3:
                return "none", None
            unwrap = type_text.startswith("<call?>")
            body = type_text.split(">", 1)[1]
            expr, _, root_type = body.partition("|")   # the root type may itself be a nested marker
            expr = expr.strip()
            if "." in expr:
                hint, name = expr.rsplit(".", 1)
            else:
                hint, name = None, expr
            fake = {"kind": "call", "name": name.split("(")[0].strip(), "hint": hint, "src": cont["id"] if cont else rel,
                    "line": 0, "argc": None}
            if hint and root_type:
                fake["hint_type"] = root_type
                fake["chain"] = [seg.split("(")[0].strip("*&!? ") for seg in hint.split(".")][1:]
            targets, conf, mode = resolve_call(fake, rel, lang_of(rel), cont)
            if not targets:
                return "unknown", None   # callee not in the graph: the receiver stays an unknown value (lead)
            if targets[0]["kind"] in TYPE_LIKE_KINDS:
                return "node", targets[0]   # `Foo(...)` / `Foo::new`-style constructor call: the value is a Foo
            rt = return_type(targets[0])
            if not rt:
                return "unknown", None
            if unwrap:
                rt = unwrap_generic(rt)
            return resolve_type_ref(rt, targets[0]["file"], container_of(targets[0]["id"]), depth + 1)
        q, base = split_qualifier(type_text)
        if q:
            if q in ns_ext.get(rel, ()):
                return "ext", None
            if q in ns_repo.get(rel, {}):
                tn = type_node(base, rel, ns_repo[rel][q])
                return ("node", tn) if tn else ("none", None)
        if base in ns_repo.get(rel, {}):
            tn = type_node(base, rel, ns_repo[rel][base])
            if tn:
                return "node", tn
        tn = type_node(base, rel)
        return ("node", tn) if tn else ("none", None)

    def field_owner(tn, seg):
        """(owner node, field type, qualifier) for field `seg` on tn or one of its ancestors, else None."""
        seen, frontier = set(), [tn]
        while frontier:
            x = frontier.pop(0)
            if x["id"] in seen:
                continue
            seen.add(x["id"])
            for oid in owner_ids(x):
                if seg in field_types.get(oid, {}):
                    return x, field_types[oid][seg], field_qual[oid].get(seg)
            if len(seen) < 12:
                frontier.extend(parents_of.get(x["id"], []))
        return None

    def follow_chain(tn, chain, flags=None):
        """Walk `a.b.c` / `a.b().c` through field types and method return types from container node tn
        (fields may be inherited). Returns the final container node, "" when the chain provably leaves
        the repo (a field of an external type, an unbounded generic), or None."""
        for i, seg in enumerate(chain):
            if flags and i < len(flags) and flags[i]:
                # method-call segment: the type of `x.m()` is m's return type
                targets, conf = typed_pick(candidates(seg, {"method", "function"}), tn)
                if not targets:
                    return None
                rt = return_type(targets[0])
                if not rt:
                    return None
                kind, nxt = resolve_type_ref(rt, targets[0]["file"], container_of(targets[0]["id"]))
                if kind == "ext":
                    return ""
                if nxt is None:
                    return None
                tn = nxt
                continue
            found = field_owner(tn, seg)
            if found is None:
                return None
            owner, ft, q = found
            if ft.startswith("<call"):
                kind, nxt = resolve_type_ref(ft, owner["file"], owner)
                if kind == "ext":
                    return ""
                if nxt is None:
                    return None
                tn = nxt
                continue
            bounds = generic_bounds(owner)
            if not q and ft in bounds:
                # field typed by a generic parameter: the bound (a trait/interface) is all we know
                if not bounds[ft]:
                    return ""
                kind, nxt = resolve_type_ref(bounds[ft], owner["file"])
            else:
                kind, nxt = resolve_type_ref((q + "." if q else "") + ft, owner["file"])
            if kind == "ext":
                return ""
            if nxt is None:
                return None
            tn = nxt
        return tn

    # Pass 2a: extends, implements, instantiates, signature type uses (types must be linked before calls)
    for rel, info in files.items():
        lang = lang_of(rel)
        for r in info["refs"]:
            k = r["kind"]
            if k in ("extends", "implements", "instantiates", "uses_type"):
                hint = r.get("hint")
                name = r["name"]
                cands = candidates(name, TYPE_LIKE_KINDS)
                if k in ("extends", "implements"):
                    # a class's own nested members are not in scope in its extends/implements clause
                    cands = [c for c in cands if not is_within(c, r["src"])]
                qual = None
                hf = r.get("hint_full")
                if hf and ("." in hf or "::" in hf):
                    qual = hf.rsplit("::", 1)[0] if "::" in hf else hf.rsplit(".", 1)[0]
                if qual and (hint not in ns_repo.get(rel, {}) and hint not in ns_ext.get(rel, ())) and "." in qual:
                    # fully-qualified reference (`org.apache.commons.lang3.builder.Builder<T>`, `pkg.sub.Class`)
                    fq = None
                    if lang == "java" and qual in res_ctx["java_pkgs"]:
                        fq = set(res_ctx["java_pkgs"][qual])
                    elif lang == "python":
                        t = resolve_import({"name": qual, "names": [name]}, rel, lang, file_set, root_dir, go_module, res_ctx)
                        if t and t != "asset":
                            fq = set(dir_files[t[4:]]) if t.startswith("dir:") else {t}
                    if fq:
                        targets, conf = bound_pick(cands, name, fq, TYPE_LIKE_KINDS)
                        if targets:
                            if k == "uses_type":
                                if targets[0]["id"] != r["src"] and targets[0]["id"] != node_by_id.get(r["src"], {}).get("parent"):
                                    add_edge(r["src"], targets[0]["id"], "references", r["line"], conf, name=name, via="signature")
                                continue
                            for t_ in targets:
                                if t_["id"] != r["src"]:
                                    add_edge(r["src"], t_["id"], k, r["line"], conf, name=name)
                            continue
                if hint and hint in ns_ext.get(rel, ()):
                    targets, conf = [], None            # `schema.Resource{}`, `collections_abc.Iterable`
                elif hint and hint in ns_repo.get(rel, {}):
                    targets, conf = bound_pick(cands, name, ns_repo[rel][hint], TYPE_LIKE_KINDS)
                elif hint is None and name in ns_repo.get(rel, {}):
                    targets, conf = bound_pick(cands, name, ns_repo[rel][name], TYPE_LIKE_KINDS)   # from-imported type name
                    if not targets:
                        targets, conf = pick(cands, rel)
                elif hint and hint[:1].isupper():
                    targets, conf = pick(cands, rel)     # `Outer.Inner`: qualifier is a type, not a module
                elif hint:
                    targets, conf = [], None             # qualifier is an unknown value/module: not a type reference we can trust
                else:
                    targets, conf = pick(cands, rel)
                    if not targets and "*" in ns_repo.get(rel, {}):
                        targets, conf = bound_pick(cands, name, ns_repo[rel]["*"], TYPE_LIKE_KINDS)
                if conf == "unique" and lang != "rust":
                    # A type that is neither imported nor in scope is not ours (a type alias, a builtin, an
                    # external class); guessing the one same-named repo class (often in a test) is wrong.
                    targets, conf = [], None
                if k == "uses_type":
                    if not targets or conf == "ambiguous":
                        continue  # unknown/builtin type names are expected; never counted as unresolved
                    if targets[0]["id"] == r["src"] or targets[0]["id"] == node_by_id.get(r["src"], {}).get("parent"):
                        continue
                    add_edge(r["src"], targets[0]["id"], "references", r["line"], conf, name=name, via="signature")
                    continue
                if not targets:
                    unresolved += 1
                    continue
                for t in targets:
                    if t["id"] == r["src"]:
                        continue   # a class cannot extend itself; the qualified base was mis-resolved
                    add_edge(r["src"], t["id"], k, r["line"], conf, name=name)

    # Inheritance index from the resolved edges: class node id -> parent type nodes
    parents_of = defaultdict(list)
    for e in edges:
        if e["type"] in ("extends", "implements") and e["confidence"] != "ambiguous" and e["dst"] in node_by_id:
            parents_of[e["src"]].append(node_by_id[e["dst"]])

    def typed_pick(cands, tn, depth=0):
        """Member of type node `tn` or of one of its resolved ancestors, else nothing."""
        typed = typed_match(cands, tn)
        if typed:
            return by_arity(typed)[:1], "typed"
        if depth >= 3:
            return [], None
        for pn in parents_of.get(tn["id"], []):
            t2, c2 = typed_pick(cands, pn, depth + 1)
            if t2:
                return t2, c2
        return [], None

    def receiver_mode(r, rel, lang, cont):
        """Classify the receiver of a call: how much do we know about `x` in `x.y.f()`?
        Returns (mode, payload):
          free       - no receiver
          typed      - receiver type is a repo container node (payload = node): only its members/ancestors
          namespace  - receiver is an import binding to repo files (payload = file set)
          rust_path  - Rust `a::b::f` path we do not map to files
          external   - receiver type/namespace belongs to an external package or builtin: never name-matched
          unknown    - receiver is a value of unknown type: same-file definition at most, else a lead"""
        hint = r.get("hint") or ""
        if not hint:
            return "free", None
        raw = hint.split(".")
        flags = ["(" in seg for seg in raw]
        chain = [seg.split("(")[0].strip("*&!? ") for seg in raw]
        if chain[0].startswith("new "):
            chain[0] = chain[0][4:].strip()   # `new Foo(...).bar()`: the receiver is a Foo
            flags[0] = False
        first = chain[0]
        ext = ns_ext.get(rel, ())
        repo = ns_repo.get(rel, {})

        def from_node(tn, rest, rest_flags=None):
            t = follow_chain(tn, rest, rest_flags)
            if t == "":
                return "external", None
            return ("typed", t) if t else ("unknown", None)

        if r.get("hint_type"):  # receiver root typed by a local declaration/parameter (maybe a call result)
            kind, tn = resolve_type_ref(r["hint_type"], rel, cont)
            if kind == "unknown":
                return "unknown", None    # bound to a call the graph cannot resolve
            if kind == "ext" or tn is None:
                return "external", None   # external package type, builtin (string, error, List) or unresolvable
            return from_node(tn, r.get("chain") or [], flags[1:])
        if flags[0] and first not in ("self", "this", "cls", "Self"):
            # receiver is a call: `getStyle().x()` / `make().save()` -> the callee's return type
            if cont is not None and any(first in field_types.get(oid, {}) for oid in owner_ids(cont)):
                return from_node(cont, chain, flags)
            kind, tn = resolve_type_ref("<call>" + first + "|", rel, cont)
            if kind == "ext" or (kind == "none" and tn is None):
                return "external", None
            if tn is None:
                return "unknown", None
            return from_node(tn, chain[1:], flags[1:])
        if first in ("self", "this", "cls", "Self") and cont is not None:
            return from_node(cont, chain[1:], flags[1:])
        if cont is not None and any(first in field_types.get(oid, {}) for oid in owner_ids(cont)):
            return from_node(cont, chain, flags)
        if cont is not None and not flags[0]:
            # a property inherited from an ancestor, used without `this.` (Kotlin/Java/Python)
            anc, seen_anc, frontier = None, set(), list(parents_of.get(cont["id"], []))
            while frontier and anc is None:
                pn = frontier.pop(0)
                if pn["id"] in seen_anc:
                    continue
                seen_anc.add(pn["id"])
                if any(first in field_types.get(oid, {}) for oid in owner_ids(pn)):
                    anc = pn
                    break
                frontier.extend(parents_of.get(pn["id"], []))
            if anc is not None:
                return from_node(anc, chain, flags)
        if lang != "rust" and "::" not in hint:
            # longest dotted prefix bound by an import: `a.b.c.f()` -> `a.b.c`, `ops.f()` -> `ops`
            for k in range(len(chain), 0, -1):
                pref = ".".join(chain[:k])
                if pref in repo:
                    fset = repo[pref]
                    rest = chain[k:]
                    if pref[:1].isupper() or rest:
                        # the binding may be a type (`from x import Foo; Foo.make()`), or a module path continues
                        tn = type_node(chain[k - 1], rel, fset) if k == len(chain) or rest else None
                        if tn is not None:
                            return from_node(tn, rest, flags[k:])
                        if rest:
                            tn = type_node(rest[0], rel, fset) if rest[0][:1].isupper() else None
                            if tn is not None:
                                return from_node(tn, rest[1:], flags[k + 1:])
                            return "unknown", None
                    return "namespace", fset
                if pref in ext:
                    return "external", None
        if first and first[0].isupper() and candidates(first, CONTAINER_KINDS):
            tn = type_node(first, rel)
            if tn is None:
                return "unknown", None
            return from_node(tn, chain[1:], flags[1:])
        if lang == "rust" and "::" in hint:
            return ("external" if hint.split("::")[0] in ("std", "core", "alloc") else "rust_path"), None
        if first in ext:
            return "external", None
        return "unknown", None

    def resolve_call(r, rel, lang, cont):
        """Resolve one call ref -> (targets, confidence, mode); mode 'external'/'builtin' mean no edge.
        When several same-arity overloads remain undecided, they are all returned as `ambiguous`."""
        saved = dict(_cur)   # re-entrant: typing an argument or receiver may resolve nested calls
        _cur.update({"argc": r.get("argc"), "arg_types": r.get("arg_types"), "rel": rel, "cont": cont, "overload_undecided": False})
        try:
            targets, conf, mode = _resolve_call(r, rel, lang, cont)
            if targets and _cur.get("overload_undecided") and conf in ("typed", "same_file", "package", "import"):
                group = [c for c in by_name.get(r["name"], []) if c["qname"] == targets[0]["qname"] and c["file"] == targets[0]["file"]
                         and c["kind"] == targets[0]["kind"] and c["id"] not in overload_stub]
                group = by_arity(group)
                if len(group) > 1:
                    return group[:5], "ambiguous", mode
            return targets, conf, mode
        finally:
            _cur.clear()
            _cur.update(saved)

    def _resolve_call(r, rel, lang, cont):
        name = r["name"]
        mode, payload = receiver_mode(r, rel, lang, cont)
        if mode == "external":
            return [], None, "external"
        if lang == "kotlin" and mode in ("unknown", "typed") and name in KOTLIN_STDLIB_MEMBERS:
            # `x.apply { }`, `list.forEach { }`, `s.isEmpty()`: stdlib unless the receiver's own type defines it
            if mode == "unknown" or not typed_match(candidates(name, {"method"}), payload):
                return [], None, "builtin"
        if mode == "free":
            # Unqualified call: a function/class in scope. Never a method (except Java's implicit `this`),
            # never a language builtin.
            if name in BUILTIN_CALLS.get(lang, ()) and name not in top_defs.get(rel, {}):
                return [], None, "builtin"
            targets, conf = [], None
            if lang in ("java", "kotlin") and cont is not None:
                targets, conf = typed_pick(candidates(name, {"method", "constructor"}), cont)
            if not targets:
                cands = candidates(name, {"function", "class", "struct", "constructor"})
                if name in ns_repo.get(rel, {}):
                    targets, conf = bound_pick(cands, name, ns_repo[rel][name], {"function", "class", "struct", "constructor"})   # from-import wins over same-file shadowing
                if not targets:
                    same = [c for c in cands if c["file"] == rel]
                    if same:
                        targets, conf = by_arity(same)[:1], "same_file"
                    elif lang in ("java", "kotlin", "go"):
                        pkg = [c for c in cands if same_package(c["file"], rel)]
                        if pkg:
                            targets, conf = by_arity(pkg)[:1], "package"
                if not targets and "*" in ns_repo.get(rel, {}):
                    targets, conf = bound_pick(cands, name, ns_repo[rel]["*"], {"function", "class", "struct", "constructor"})
                if not targets and lang == "rust":
                    targets, conf = pick(same_family(cands, rel), rel)
        elif mode == "namespace":
            targets, conf = bound_pick(candidates(name, {"function", "class", "struct", "constructor", "method"}), name, payload, {"function", "class", "struct", "constructor", "method"})
        elif mode == "rust_path":
            targets, conf = pick(same_family(candidates(name, {"function", "method", "struct", "constructor"}), rel), rel)
        elif mode == "typed":
            targets, conf = typed_pick(candidates(name, {"function", "method", "class", "struct", "constructor"}), payload)
            if not targets:  # our type but no such member (embedded struct, macro, dynamic attr): keep as a lead
                targets, conf = lead_pick(candidates(name, {"method"}), rel)
        else:  # unknown receiver
            targets, conf = lead_pick(candidates(name, {"method"}), rel)
        return targets, conf, mode

    # Pass 2b: calls, then HCL/K8s references
    for rel, info in files.items():
        lang = lang_of(rel)
        d = os.path.dirname(rel) or "."
        for r in info["refs"]:
            k = r["kind"]
            if k == "call":
                cont = container_of(r["src"])
                name = r["name"]
                targets, conf, mode = resolve_call(r, rel, lang, cont)
                if mode == "external":
                    unresolved += 1
                    continue
                if mode == "builtin":
                    continue
                if not targets:
                    unresolved += 1
                    continue
                for t in targets:
                    add_edge(r["src"], t["id"], "calls", r["line"], conf, name=name)
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

    # Pass 3: k8s selectors -> workloads
    workloads = [n for n in nodes if n["kind"] == "k8s_object" and n["extra"].get("template_labels")]
    for n in nodes:
        if n["kind"] == "k8s_object" and n["extra"].get("selector") and n["name"].split("/")[0] in ("Service", "PodDisruptionBudget", "NetworkPolicy"):
            sel = n["extra"]["selector"]
            for w in workloads:
                tl = w["extra"]["template_labels"]
                if all(tl.get(kk) == vv for kk, vv in sel.items()) and (w["extra"].get("namespace") == n["extra"].get("namespace")):
                    add_edge(n["id"], w["id"], "selects", n["line"], "exact")

    # Deduplicate edges
    seen, uniq = set(), []
    for e in edges:
        key = (e["src"], e["dst"], e["type"], e.get("line"))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(e)
    stats = {"files": len(files), "nodes": len(nodes), "edges": len(uniq), "unresolved_refs": unresolved,
             "asset_imports": assets, "languages": dict(langs), "edge_confidence": dict(stats_conf)}
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
            line = None
            if "@" in npart and npart.rsplit("@", 1)[1].isdigit():   # `readers.py:read_csv@1283`
                npart, line = npart.rsplit("@", 1)
                line = int(line)
            return list({n["id"]: n for n in self.by_name.get(npart.lower(), [])
                         if (n["file"] == fpart or n["file"].endswith("/" + fpart)) and (line is None or n["line"] == line)}.values())
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
    sig = n["signature"].split("\n")
    sig = sig[0] + (" ..." if len(sig) > 1 else "")   # multi-line signatures: first line only
    return f"{kind_label(n)}{sig}{ann}" + (f"  ({loc})" if with_file and loc else "")


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
    langs = set(getattr(args, "lang", None) or [])

    def lang_of(n):
        return g.g.get("files", {}).get(n["file"], {}).get("file_node", {}).get("extra", {}).get("language") if n.get("file") else None

    res = [n for n in g.g["nodes"] if (ql in n["name"].lower() or ql in n["qname"].lower() or ql in n["file"].lower())
           and (not args.kind or n["kind"] in args.kind) and n["kind"] != "file" or (n["kind"] == "file" and ql in n["file"].lower() and (not args.kind or "file" in args.kind))]
    if langs:
        res = [n for n in res if lang_of(n) in langs]

    def rank(n):
        # exact name (case-sensitive, then case-insensitive) < exact qname < name starts with query < substring
        # in name < match only in the path; symbols before package/file/external nodes; non-test first; shorter path
        nm, qn = n["name"].lower(), n["qname"].lower()
        if n["name"] == args.name:
            r = 0
        elif nm == ql:
            r = 1
        elif qn == ql or qn.endswith("." + ql):
            r = 2
        elif nm.startswith(ql):
            r = 3
        elif ql in nm:
            r = 4
        else:
            r = 5
        structural = n["kind"] in ("package", "terraform_module", "file", "external", "external_module")
        stub = any(a.split(".")[-1].startswith("overload") for a in n.get("annotations", []))   # implementation before @overload stubs
        return (r, structural, stub, is_test_file(n["file"]) if n.get("file") else False, len(n["file"]), n["line"])

    res.sort(key=rank)
    total = len(res)
    res = res[:args.limit]
    if args.json:
        print(json.dumps(res))
        return
    for n in res:
        print(f"{n['id']}\n    {fmt_node(n)}")
    if not res:
        print("no matches")
    elif total > len(res):
        print(f"... {total - len(res)} more matches (exact-name matches are listed first; narrow with --kind, --lang or a longer query)")


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
    cap = None if getattr(args, "all", False) else getattr(args, "limit", 40)

    def collapse(edge_list, key_node):
        """Identical (type, target, confidence) rows from several lines become one row with a count."""
        groups = {}
        for e in edge_list:
            k = (e["type"], key_node(e), e["confidence"])
            if k in groups:
                groups[k][1] += 1
            else:
                groups[k] = [e, 1]
        return list(groups.values())

    def by_file_summary(edge_nodes):
        c = defaultdict(int)
        for x in edge_nodes:
            c[x["file"] or x["name"]] += 1
        top = sorted(c.items(), key=lambda kv: -kv[1])[:5]
        return f"{len(c)} files; top: " + ", ".join(f"{f} ({v})" for f, v in top)

    kids = g.children(n["id"])
    if n["kind"] in ("struct", "enum", "trait"):
        for alt in g.by_name.get(n["qname"].lower(), []):   # Rust: methods live under `impl X` blocks
            if alt["kind"] == "impl" and alt["file"] == n["file"] and alt["id"] != n["id"]:
                kids += g.children(alt["id"])
        kids.sort(key=lambda k: k["line"])
    if n["kind"] in CONTAINER_KINDS:
        # Kotlin extension functions `fun X.f()` belong on X's card even though they live at file level
        own = {k["id"] for k in kids}
        exts = [m for m in g.g["nodes"] if m["kind"] == "method" and m.get("extra", {}).get("receiver_type") == n["name"]
                and m["id"] not in own and m.get("parent") != n["id"] and m["file"].endswith((".kt", ".kts"))]
        if exts:
            kids = kids + [dict(m, signature=m["signature"] + "   [extension]") for m in sorted(exts, key=lambda m: (m["file"], m["line"]))]
    if kids:
        print("├── Members" + (f" ({len(kids)}, showing {cap})" if cap and len(kids) > cap else ""))
        for k in kids[:cap]:
            print(f"│   ├── {fmt_node(k, with_file=False)}  [L{k['line']}]")
            calls = collapse(g.out_edges(k["id"], {"calls"}), lambda e: e["dst"])
            per_member = None if cap is None else 8
            for e, cnt in calls[:per_member]:
                t = g.nodes.get(e["dst"])
                if t:
                    print(f"│   │     calls: {t['qname']}  ({t['file']}:{t['line']}, {e['confidence']})" + (f"  x{cnt}" if cnt > 1 else ""))
            if per_member and len(calls) > per_member:
                print(f"│   │     ... {len(calls) - per_member} more calls")
    outs = collapse([e for e in g.out.get(n["id"], []) if e["type"] != "contains"], lambda e: e["dst"])
    if outs:
        print("├── Depends on (outgoing)" + (f" ({len(outs)}, showing {cap})" if cap and len(outs) > cap else ""))
        for e, cnt in sorted(outs, key=lambda ec: (ec[0]["type"], ec[0].get("line") or 0))[:cap]:
            t = g.nodes.get(e["dst"])
            times = f"  x{cnt}" if cnt > 1 else ""
            if t:
                print(f"│   ├── {e['type']}: {t['qname']}  ({t['file']}:{t['line']}, {e['confidence']}){times}" if t["file"] else f"│   ├── {e['type']}: {t['name']}  ({t['kind']}){times}")
        if cap and len(outs) > cap:
            print(f"│   └── ... {len(outs) - cap} more; " + by_file_summary([g.nodes[e["dst"]] for e, _ in outs if e["dst"] in g.nodes]))
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
        total_in = len(ins) + len(member_ins)
        print("└── Used by (incoming)" + (f" ({total_in}, showing {cap}; `--all` for everything, `query callers` to traverse)" if cap and total_in > cap else ""))
        ins_sorted = sorted(collapse(ins, lambda e: e["src"]), key=lambda ec: (ec[0]["confidence"] == "ambiguous", ec[0]["type"], ec[0].get("line") or 0))
        for e, cnt in ins_sorted[:cap]:
            s = g.nodes.get(e["src"])
            if s:
                print(f"    ├── {e['type']} from {s['qname']}  ({s['file']}:{e.get('line') or s['line']}, {e['confidence']})" + (f"  x{cnt}" if cnt > 1 else ""))
        if cap and len(ins) > cap:
            print(f"    ├── ... {len(ins) - cap} more; " + by_file_summary([g.nodes[e["src"]] for e in ins if e["src"] in g.nodes]))
        room = None if cap is None else max(0, cap - min(len(ins), cap))
        for k, e in member_ins[:room]:
            s = g.nodes.get(e["src"])
            if s:
                print(f"    ├── {e['type']} {k['name']} from {s['qname']}  ({s['file']}:{e.get('line') or s['line']}, {e['confidence']})")
        if room is not None and len(member_ins) > room:
            print(f"    └── ... {len(member_ins) - room} more member usages; " + by_file_summary([g.nodes[e["src"]] for _, e in member_ins if e["src"] in g.nodes]))


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


CONSTRUCTOR_NAMES = {"__init__", "__new__", "constructor", "new"}


def with_owner_if_constructor(g, n):
    """Instantiation edges target the class, so a constructor's dependents are the class's."""
    if n["kind"] in ("method", "constructor") and n["name"] in CONSTRUCTOR_NAMES and n.get("parent") in g.nodes \
            and g.nodes[n["parent"]]["kind"] in CONTAINER_KINDS:
        owner = g.nodes[n["parent"]]
        print(f"(note: {n['qname']} is a constructor; instantiations are recorded against {owner['qname']}, included below)")
        return [n["id"], owner["id"]]
    return [n["id"]]


def q_callers(g, args, direction="in"):
    n = ensure_one(g, args.name)
    targets = with_owner_if_constructor(g, n) if direction == "in" else [n["id"]]
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
        return
    max_rows = getattr(args, "max_rows", 200)
    mode = "summary" if getattr(args, "summary", False) else "files" if getattr(args, "files_only", False) else "rows"
    if mode == "rows" and len(hops) > max_rows:
        mode = "summary"
        print(f"  ({len(hops)} rows exceed --max-rows {max_rows}; showing the summary. Use --files-only for the file list, --max-rows N for all rows.)")
    arrow = "<-" if direction == "in" else "->"
    if mode == "rows":
        for a, e, b, lvl in hops:
            other = g.nodes[b]
            this = g.nodes[a]
            if direction == "in":
                loc = f"{other['file']}:{e.get('line') or other['line']}"                  # caller file, call-site line
            else:
                loc = f"defined at {other['file']}:{other['line']}, called at line {e.get('line') or '?'}"
            print(f"  {'  ' * (lvl - 1)}{this['qname']} {arrow} {other['qname']}  [{e['type']}, {e['confidence']}]  ({loc})")
        return
    per_file = defaultdict(lambda: {"level": 99, "rows": 0})
    per_sym = defaultdict(lambda: [0, 99, None])
    mix = defaultdict(int)
    for a, e, b, lvl in hops:
        other = g.nodes[b]
        f = other["file"] or other["name"]
        per_file[f]["level"] = min(per_file[f]["level"], lvl)
        per_file[f]["rows"] += 1
        r = per_sym[b]
        r[0] += 1
        r[1] = min(r[1], lvl)
        r[2] = other
        mix[(e["type"], e["confidence"])] += 1
    direct = [f for f, i in per_file.items() if i["level"] == 1]
    tests = [f for f in per_file if is_test_file(f)]
    if mode == "files":
        by_level = defaultdict(list)
        for f, i in per_file.items():
            by_level[i["level"]].append(f)
        for lvl in sorted(by_level):
            print(f"  hop {lvl} ({len(by_level[lvl])} files):")
            for f in sorted(by_level[lvl]):
                print(f"    {f}")
    else:
        top = getattr(args, "top", 15)
        by_dir = defaultdict(lambda: [0, 0, 0])
        for f, i in per_file.items():
            dd = os.path.dirname(f) or "."
            by_dir[dd][0 if i["level"] == 1 else 1] += 1
            by_dir[dd][2] += i["rows"]
        print("  | Directory | Hop-1 files | Deeper files | Rows |")
        print("  |---|---|---|---|")
        for dd, (x, y, z) in sorted(by_dir.items(), key=lambda kv: (-kv[1][0], -kv[1][1], kv[0]))[:top]:
            print(f"  | {dd} | {x} | {y} | {z} |")
        if len(by_dir) > top:
            print(f"  | ... {len(by_dir) - top} more directories | | | |")
        who = "Most frequent callers" if direction == "in" else "Most frequent callees"
        print(f"  {who} (rows, hop):")
        for _, (cnt, lvl, other) in sorted(per_sym.items(), key=lambda kv: (-kv[1][0], kv[1][1]))[:top]:
            print(f"    {cnt:4d}  hop {lvl}  {other['qname']}  (defined at {other['file']}:{other['line']})")
        print("  Relationship mix: " + ", ".join(f"{t}/{c}={v}" for (t, c), v in sorted(mix.items(), key=lambda kv: -kv[1])))
    print(f"  Files: {len(per_file)} (hop 1: {len(direct)}); symbols: {len(per_sym)}; rows: {len(hops)}; test files: {len(tests)}")


def q_trace_deps(g, args):
    """Blast radius: everything that (transitively) depends on the target."""
    n = ensure_one(g, args.target)
    start = with_owner_if_constructor(g, n)
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
    direct = sorted(f for f, i in per_file.items() if i["level"] == 1)
    tests = sorted(f for f in per_file if is_test_file(f))
    total_rows = sum(len(i["items"]) for i in per_file.values())
    mode = "summary" if getattr(args, "summary", False) else "files" if getattr(args, "files_only", False) else "table"
    max_rows = getattr(args, "max_rows", 200)
    if mode == "table" and total_rows > max_rows:
        mode = "summary"
        print(f"({total_rows} dependency rows across {len(per_file)} files exceed --max-rows {max_rows}; showing the summary. "
              f"Use --files-only for the file list, --max-rows N for the full table.)")
    print()
    if mode == "table":
        print("| Dependent file | Hop | Dependent symbol | Relationship -> target | Confidence |")
        print("|---|---|---|---|---|")
        for f, info in sorted(per_file.items(), key=lambda kv: (kv[1]["level"], kv[0])):
            rows = sorted(info["items"], key=lambda t: (t[0], t[1]["line"]))
            shown = rows[: args.per_file]
            for lvl, dep, e, tgt in shown:
                print(f"| {f} | {lvl} | {dep['qname']} (L{e.get('line') or dep['line']}) | {e['type']} -> {tgt['qname']} | {e['confidence']} |")
            if len(rows) > len(shown):
                print(f"| {f} |  | ... {len(rows) - len(shown)} more | | |")
    elif mode == "files":
        by_level = defaultdict(list)
        for f, i in per_file.items():
            by_level[i["level"]].append(f)
        for lvl in sorted(by_level):
            print(f"hop {lvl} ({len(by_level[lvl])} files):")
            for f in sorted(by_level[lvl]):
                print(f"  {f}")
    else:
        # Directories by number of dependent files, then the most-connected dependent symbols and the
        # relationship/confidence mix: enough to judge the change without a per-edge table.
        by_dir = defaultdict(lambda: [0, 0, 0])
        for f, i in per_file.items():
            dd = os.path.dirname(f) or "."
            by_dir[dd][0 if i["level"] == 1 else 1] += 1
            by_dir[dd][2] += len(i["items"])
        print("| Directory | Direct files | Transitive files | Dependency rows |")
        print("|---|---|---|---|")
        for dd, (a, b, c) in sorted(by_dir.items(), key=lambda kv: (-kv[1][0], -kv[1][1], kv[0]))[: args.top]:
            print(f"| {dd} | {a} | {b} | {c} |")
        if len(by_dir) > args.top:
            print(f"| ... {len(by_dir) - args.top} more directories | | | |")
        sym_rows = defaultdict(lambda: [0, 99, None])
        for i in per_file.values():
            for lvl, dep, e, tgt in i["items"]:
                r = sym_rows[dep["id"]]
                r[0] += 1
                r[1] = min(r[1], lvl)
                r[2] = dep
        print()
        print("Most connected dependents (rows -> target, hop):")
        for _, (cnt, lvl, dep) in sorted(sym_rows.items(), key=lambda kv: (-kv[1][0], kv[1][1]))[: args.top]:
            print(f"  {cnt:4d}  hop {lvl}  {dep['qname']}  (defined at {dep['file']}:{dep['line']})")
        mix = defaultdict(int)
        for i in per_file.values():
            for lvl, dep, e, tgt in i["items"]:
                mix[(e["type"], e["confidence"])] += 1
        print("Relationship mix: " + ", ".join(f"{t}/{c}={v}" for (t, c), v in sorted(mix.items(), key=lambda kv: -kv[1])))
    print()
    print(f"Files affected: {len(per_file)} (direct: {len(direct)}, transitive: {len(per_file) - len(direct)}); dependency rows: {total_rows}")
    if tests:
        shown_t = tests[: args.top * 2]
        print("Tests reached through resolved edges (a lower bound; tests that reach the target through fixtures or untyped receivers are not linked): "
              + ", ".join(shown_t) + (f" (+{len(tests) - len(shown_t)} more; --files-only lists all)" if len(tests) > len(shown_t) else ""))
    amb = sum(1 for e in g.g["edges"] if e["confidence"] == "ambiguous" and e["dst"] in start_set)
    if amb and not args.include_ambiguous:
        print(f"Note: {amb} ambiguous edge(s) to the target were excluded; re-run with --include-ambiguous to see them.")


def q_overview(g, args):
    """Centrality overview: hub symbols, hub files, and directory summary."""
    indeg, outdeg = defaultdict(int), defaultdict(int)
    test_files = {f for f in g.g.get("files", {}) if is_test_file(f)} if getattr(args, "no_tests", False) else set()

    def in_test_module(nid):
        """Rust `mod tests` / `#[cfg(test)]` modules and similar inline test containers."""
        n = g.nodes.get(nid)
        hops = 0
        while n is not None and hops < 12:
            if n["kind"] == "module" and (n["name"] in ("tests", "test") or any(a.startswith("cfg(test") for a in n.get("annotations", []))):
                return True
            n = g.nodes.get(n.get("parent")) if n.get("parent") else None
            hops += 1
        return False
    langs_ok = set(getattr(args, "lang", None) or [])
    lang_files = {f for f, i in g.g.get("files", {}).items() if i["file_node"]["extra"].get("language") in langs_ok} if langs_ok else None
    for e in g.g["edges"]:
        if e["type"] == "contains" or e["confidence"] == "ambiguous":
            continue
        sf = g.nodes.get(e["src"], {}).get("file")
        if test_files and (sf in test_files or in_test_module(e["src"])):
            continue   # --no-tests: usage from test files or inline test modules does not make a symbol a hub
        if lang_files is not None and (sf not in lang_files or g.nodes.get(e["dst"], {}).get("file") not in lang_files):
            continue   # --lang: rank only within the requested language(s)
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
        if p is not None and p["kind"] == "impl":
            # a Rust impl block is not a symbol of its own: credit the struct/enum of that name in the same file
            owner = next((x for x in g.by_name.get(p["qname"].lower(), []) if x["file"] == p["file"] and x["kind"] in ("struct", "enum", "trait")), None)
            if owner is not None:
                p = owner
        if p is not None and p["kind"] in CONTAINER_KINDS:
            sym_in[p["id"]] += c
        elif n["kind"] not in ("file", "external", "external_module"):   # externals have their own section
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
    shown_dirs = sorted(dirs.items())[: args.top * 2]
    for d, langs in shown_dirs:
        print(f"  {d}: " + ", ".join(f"{k}={v}" for k, v in sorted(langs.items())))
    if len(dirs) > len(shown_dirs):
        print(f"  ... {len(dirs) - len(shown_dirs)} more directories (query stats / query file for any of them)")
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
    entry = [n for n in g.g["nodes"] if n["kind"] == "function" and n["name"] in ("main", "lambda_handler", "cli")
             and indeg.get(n["id"], 0) == 0 and not (test_files and n["file"] in test_files) and not is_test_file(n["file"])]
    if entry:
        print("\nLikely entry points: " + ", ".join(f"{n['qname']} ({n['file']})" for n in entry[: args.top]))


def q_file(g, args):
    n = ensure_one(g, args.path, kinds={"file"})
    info = g.g["files"].get(n["file"])
    if not info:
        sys.exit("file not in graph")
    ins = [e for e in g.inc.get(n["id"], []) if e["type"] == "imports"]
    if args.json:
        print(json.dumps({"file": info["file_node"], "nodes": info["nodes"], "refs": info["refs"],
                          "imported_by": sorted({g.nodes[e["src"]]["file"] for e in ins if e["src"] in g.nodes})}))
        return
    print(render_skeleton(info["file_node"], info["nodes"], info["refs"], show_calls=not args.no_calls))
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
    chain = []
    cur = found
    while cur is not None and prev.get(cur):
        x, e = prev[cur]
        chain.append((x, e, cur))
        cur = x
    chain.reverse()
    if getattr(args, "json", False):
        # Same shape as the text rows: one hop per edge, src -> dst, with the dst location.
        print(json.dumps({"src": a["id"], "dst": b["id"], "found": found is not None,
                          "hops": [{"src": x, "type": e["type"], "confidence": e["confidence"], "dst": y,
                                    "file": g.nodes[y]["file"], "line": g.nodes[y]["line"]} for x, e, y in chain]}))
        return
    if found is None:
        print(f"no dependency path from {a['qname']} to {b['qname']}")
        return
    for x, e, y in chain:
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

    x = qs.add_parser("find", help="search symbols/files by name (exact-name matches first)")
    x.add_argument("name"); x.add_argument("--kind", action="append"); x.add_argument("--limit", type=int, default=50); x.add_argument("--json", action="store_true")
    x.add_argument("--lang", action="append", help="only symbols from files of this language (repeatable)")
    x.set_defaults(qfn=q_find)
    x = qs.add_parser("symbol", help="architecture card for one symbol: members, dependencies, dependents")
    x.add_argument("name"); x.add_argument("--json", action="store_true")
    x.add_argument("--limit", type=int, default=40, help="max rows per section (default 40; hubs get a per-file summary for the rest)")
    x.add_argument("--all", action="store_true", help="no caps")
    x.set_defaults(qfn=q_symbol)
    for cmd, direction, helptext in (("callers", "in", "who calls/extends/instantiates this symbol (transitive)"),
                                     ("callees", "out", "what this symbol calls/instantiates (transitive)")):
        x = qs.add_parser(cmd, help=helptext)
        x.add_argument("name"); x.add_argument("--depth", type=int, default=2); x.add_argument("--include-ambiguous", action="store_true"); x.add_argument("--json", action="store_true")
        x.add_argument("--summary", action="store_true", help="directories, most frequent symbols and relationship mix instead of rows")
        x.add_argument("--files-only", action="store_true", help="only the files, grouped by hop")
        x.add_argument("--max-rows", type=int, default=200, help="above this many rows the listing degrades to --summary (default 200)")
        x.add_argument("--top", type=int, default=15, help="rows per section in --summary")
        x.set_defaults(qfn=(lambda d: (lambda g, a: q_callers(g, a, d)))(direction))
    x = qs.add_parser("trace-deps", help="blast radius: every file/symbol that depends on a target")
    x.add_argument("target", help="file path, symbol name, qualified name, or node id")
    x.add_argument("--depth", type=int, default=3); x.add_argument("--per-file", type=int, default=6)
    x.add_argument("--include-ambiguous", action="store_true"); x.add_argument("--json", action="store_true")
    x.add_argument("--summary", action="store_true", help="directories, most-connected dependents and relationship mix instead of the per-edge table")
    x.add_argument("--files-only", action="store_true", help="only the affected files, grouped by hop")
    x.add_argument("--max-rows", type=int, default=200, help="above this many dependency rows the table degrades to --summary (default 200)")
    x.add_argument("--top", type=int, default=15, help="rows per section in --summary")
    x.set_defaults(qfn=q_trace_deps)
    x = qs.add_parser("overview", help="centrality ranking: hub symbols, hub files, directories, externals")
    x.add_argument("--top", type=int, default=15); x.add_argument("--json", action="store_true")
    x.add_argument("--no-tests", action="store_true", help="ignore usage coming from test files when ranking hubs")
    x.add_argument("--lang", action="append", help="rank only symbols/files of this language (repeatable, e.g. --lang python)")
    x.set_defaults(qfn=q_overview)
    x = qs.add_parser("file", help="skeleton of a file from the graph, plus who imports it")
    x.add_argument("path"); x.add_argument("--no-calls", action="store_true"); x.add_argument("--json", action="store_true")
    x.set_defaults(qfn=q_file)
    x = qs.add_parser("path", help="shortest dependency path from one symbol/file to another")
    x.add_argument("src"); x.add_argument("dst"); x.add_argument("--json", action="store_true")
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
