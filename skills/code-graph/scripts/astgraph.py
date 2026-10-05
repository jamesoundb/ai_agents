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
import sqlite3
import subprocess
import sys
import time
from collections import Counter, defaultdict, deque
from functools import lru_cache

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
    ".c": "c",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".c++": "cpp",
    ".h": "cpp", ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp",   # .h: C++ grammar is a superset
    ".cu": "cpp", ".cuh": "cpp",                                # CUDA parses as C++ here
    ".tf": "hcl", ".hcl": "hcl", ".tfvars": "hcl",
    ".yaml": "yaml", ".yml": "yaml",
}
CPP_EXTS = {".c", ".cc", ".cpp", ".cxx", ".c++", ".h", ".hpp", ".hh", ".hxx", ".cu", ".cuh"}
DEFAULT_EXCLUDE_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", "target",
    ".terraform", ".ast-graph", "vendor", ".idea", ".vscode", ".mypy_cache", ".pytest_cache",
    "coverage", ".next", ".tox",
}
# Output caps. Every elision in the human-readable output comes from one of these, so "how
# aggressive is the output budget" is answerable from one place instead of from a dozen inline
# literals. The rule each cap must satisfy: whatever it hides is recoverable, and the output says
# how -- either a CLI flag that raises the cap, or an exact line range to read instead. An elision
# that states no recovery path is a bug.
CAP_SKELETON_CALLS = 8         # `calls:` per symbol                    -> --max-calls
CAP_SKELETON_IMPORTS = 12      # `imports:` per file                    -> --max-imports
CAP_SKELETON_SIG_LINES = 3     # signature lines kept before eliding    -> the item's [Lstart-Lend]
CAP_FIND = 50                  # `find` rows                            -> --limit
CAP_SYMBOL_SECTION = 40        # symbol-card rows per section           -> --limit / --all
CAP_ROWS = 200                 # rows before a listing degrades to a summary -> --max-rows
CAP_SUMMARY_TOP = 15           # rows per section in --summary          -> --top
CAP_PER_FILE = 6               # trace-deps rows per file               -> --per-file
CAP_OVERRIDES = 8              # `Overrides:` entries on a method card
CAP_OVERRIDDEN_BY = 12         # `Overridden by:` entries on a method card -> symbol --all
CAP_UNRESOLVED = 12            # `Unresolved (external or not indexed)` entries
CAP_IMPORTED_BY = 20           # `imported by:` files on a file card
CAP_SOURCE_LINES = 60          # `source` body lines (a longer class: member outline) -> --max-lines (0 = all)
CAP_SOURCE_TOTAL = 150         # `source` body lines across all names of one call      -> one name per call / --max-lines 0
CAP_SOURCE_REFS = 8            # callers / callees named per `source` symbol         -> --refs / query callers
CAP_TESTS_FILES = 8            # test files listed by `tests-for`                     -> --top
CAP_TESTS_PER_FILE = 3         # test functions named per file by `tests-for`         -> the count shown, `callers`
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
SIG_KEYWORDS = ("def ", "func ", "fn ", "fun ", "object ", "companion ", "val ", "var ", "class ", "interface ", "struct ", "type ", "enum ", "trait ", "impl ",
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


@lru_cache(maxsize=None)   # pure in `path`; the linker asks about the same few thousand files millions of times
def is_test_file(path):
    parts = path.replace(os.sep, "/").split("/")
    if "src" in parts[:-1]:
        # Gradle/Maven source sets: src/main is production even under a `testing` library module;
        # src/test, src/androidTest, src/testDebug ... are tests
        i = parts.index("src")
        if i + 1 < len(parts) - 1:
            ss = parts[i + 1]
            if ss == "main":
                return any(r.search(parts[-1]) for r in TEST_FILE_RES)   # only the basename conventions apply
            if ss.startswith(("test", "androidTest", "integrationTest", "it")) and ss not in ("it",):
                return True
    if any(p.lower() in TEST_DIR_SEGMENTS for p in parts[:-1]):
        return True
    return any(r.search(parts[-1]) for r in TEST_FILE_RES)


GRAPH_VERSION = 8  # 8: literal call receivers normalized to their type (", ".join -> str.join) instead of the literal's text; 7: Python loop/with/walrus typing, nested __init__ fields; Kotlin smart casts, by lazy, callable refs, invoke hints, Compose type annotations (6: lambdas, enums, operators; 5: HCL moved/dynamic)

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
                         "coerceAtLeast", "coerceAtMost", "coerceIn", "ifEmpty", "ifBlank", "print", "println"}

LANG_FAMILY = {"c": "c", "cpp": "c", "python": "py", "javascript": "js", "typescript": "js", "tsx": "js", "go": "go", "java": "jvm",
               "kotlin": "jvm", "rust": "rust", "hcl": "hcl", "yaml": "yaml"}
DEFAULT_GRAPH = ".ast-graph/graph.db"

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
        self.lambda_stack = []   # Kotlin: enclosing trailing-lambda calls, for implicit receivers and `it`
        self.pybind_handles = []  # C++: handle names introduced by an enclosing PYBIND11_MODULE
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
        root = re.split(r"[.(:]", expr, maxsplit=1)[0].strip()
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
        if kind == "call" and hint:
            hint = re.sub(r"\s*\.\s*", ".", hint).strip()   # multi-line builder chains: `Request\n  .Builder()\n  .url(x)`
        ref = {"kind": kind, "name": name, "src": self.top()["id"],
               "line": extra.pop("line", None) or tsnode.start_point[0] + 1, "hint": hint}
        if kind in ("call", "instantiates") and "argc" not in extra:
            # argument count and, where knowable, argument types for overload resolution (constructors
            # included: `new TarArchiveEntry(globalPax, header, encoding, lenient)` picks one of 15)
            args = tsnode.child_by_field_name("arguments") if hasattr(tsnode, "child_by_field_name") else None
            if args is not None:
                named = [c for c in args.named_children if c.type != "comment"]
                argc, types = self.arg_info(named)
                ref["argc"] = argc
                if types:
                    ref["arg_types"] = types
        if kind == "call" and hint:
            # Resolve the receiver's root locally when we can: `o.process()` with `Foo o` in scope.
            chain = [seg.split("(")[0].strip("*&!? ") for seg in split_chain(hint)]
            root = chain[0]
            root_type = self.lookup(root)
            if root_type:
                ref["hint_type"] = root_type
                ref["chain"] = chain[1:]
        ref.update(extra)
        if kind == "call" and self.lambda_stack and (not hint or split_chain(hint)[0] in ("it", "this")):
            ref["lam"] = self.lambda_stack[-1]   # implicit receiver / `it` of the enclosing lambda (resolved at link time)
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
                            root = re.split(r"[.(:]", callee, maxsplit=1)[0].strip()
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
    enum_like = False
    if sup is not None:
        for c in sup.named_children:
            if c.type in ("identifier", "attribute", "subscript"):
                ctx.add_ref("extends", base_type_name(ctx.text(c)), c, hint=split_qualifier(ctx.text(c))[0], hint_full=strip_type_decor(ctx.text(c)))
                if base_type_name(ctx.text(c)) in ("Enum", "IntEnum", "StrEnum", "Flag", "IntFlag"):
                    enum_like = True   # `NEW = 1` members are values of the enum class
    body = n.child_by_field_name("body")
    if body is not None:
        for stmt in body.named_children:
            a = py_assignment(stmt)
            if a is not None:
                left, typ = a.child_by_field_name("left"), a.child_by_field_name("type")
                if left is not None and left.type == "identifier":
                    sig = f"{ctx.text(left)}: {ctx.text(typ)}" if typ is not None else ctx.text(left)
                    ftype = ctx.text(typ) if typ is not None else (name if enum_like and ctx.text(left).isupper() else None)
                    ctx.add_node("field", ctx.text(left), stmt, signature=sig, type_text=ctx.text(typ) if typ is not None else None,
                                 extra={"type": ftype, "inferred": True} if (ftype and typ is None) else {"type": ftype})
        ctx.walk_children(body)
    ctx.pop()
    return True


def py_value_operands(right):
    """The operands that can be the value of `x = <right>`: `a or B()` -> a, B(); `A() if c else B()`
    -> A(), B(); parentheses unwrapped. Anything else is its own single operand. `and` is left alone:
    its value is usually the falsy left side, not a type worth binding."""
    if right is None:
        return []
    if right.type == "parenthesized_expression" and right.named_children:
        return py_value_operands(right.named_children[0])
    if right.type == "boolean_operator":
        op = right.child_by_field_name("operator")
        if op is not None and op.type == "or":
            return py_value_operands(right.child_by_field_name("left")) + py_value_operands(right.child_by_field_name("right"))
        return [right]
    if right.type == "conditional_expression" and len(right.named_children) == 3:
        return py_value_operands(right.named_children[0]) + py_value_operands(right.named_children[2])
    return [right]


def py_field_value_type(ctx, right):
    """Type text for `self.x = <right>` in a method: `SomeClass(...)` / `mod.SomeClass(...)`, a typed
    parameter or local, or `<call>f|` for a call typed later from its return type. For `query or
    sql.Query(model)` (Django's QuerySet) the first operand that names a type wins, so an untyped
    optional parameter no longer leaves the field untyped."""
    fallback = None
    for op in py_value_operands(right):
        t = None
        if op.type == "call" and op.child_by_field_name("function") is not None:
            fn_text = ctx.text(op.child_by_field_name("function"))
            cand = base_type_name(fn_text)
            full = strip_type_decor(fn_text)
            t = (full if re.match(r"^[a-z_]\w*(\.\w+)*\.[A-Z]", full) else cand) if cand[:1].isupper() else "<call>" + fn_text + "|"
        elif op.type == "identifier":
            t = ctx.lookup(ctx.text(op))
        if t and not t.startswith("<"):
            return t
        fallback = fallback or t
    return fallback


def py_returned_self_field(body):
    """`x` when a method body returns `self.x` (first such return outside nested defs), else None:
    how a `@property` that wraps a private field gets that field's type."""
    stack = list(body.named_children) if body is not None else []
    while stack:
        c = stack.pop(0)
        if c.type in ("function_definition", "class_definition", "decorated_definition", "lambda"):
            continue
        if c.type == "return_statement" and c.named_children:
            v = c.named_children[0]
            if v.type == "attribute":
                obj, attr = v.child_by_field_name("object"), v.child_by_field_name("attribute")
                if obj is not None and attr is not None and obj.type == "identifier" and obj.text == b"self":
                    return attr.text.decode("utf-8", "replace")
            continue
        stack.extend(c.named_children)
    return None


PY_PROPERTY_DECORATORS = {"property", "cached_property"}   # also `functools.cached_property`


def py_function(ctx, n):
    name = ctx.text(n.child_by_field_name("name"))
    params = ctx.text(n.child_by_field_name("parameters"))
    ret = n.child_by_field_name("return_type")
    kind = "method" if ctx.top()["kind"] == "class" else "function"
    sig = f"def {name}{params}" + (f" -> {ctx.text(ret)}" if ret is not None else "")
    node = ctx.add_node(kind, name, n, signature=sig, type_text=params + (ctx.text(ret) if ret is not None else ""))
    if kind == "method" and ret is None and any(a.split("(")[0].rsplit(".", 1)[-1] in PY_PROPERTY_DECORATORS for a in node["annotations"]):
        # an unannotated property that returns `self._x` is typed like `_x` at link time (field_types)
        field = py_returned_self_field(n.child_by_field_name("body"))
        if field:
            node["extra"]["returns_field"] = field
    ctx.push(node)
    ctx.push_scope()
    pn = n.child_by_field_name("parameters")
    for p in (pn.named_children if pn is not None else []):
        if p.type == "typed_parameter" and p.named_children:
            pname = ctx.text(p.named_children[0])
            if pname.startswith("*"):
                ctx.declare(pname.lstrip("*"), "dict" if pname.startswith("**") else "tuple")   # `**options: t.Any` is a dict
            else:
                ctx.declare(pname, ctx.text(p.child_by_field_name("type")))
        elif p.type == "typed_default_parameter":
            ctx.declare(ctx.text(p.child_by_field_name("name")), ctx.text(p.child_by_field_name("type")))
        elif p.type in ("list_splat_pattern", "dictionary_splat_pattern"):
            ctx.declare(ctx.text(p).lstrip("*"), "dict" if p.type == "dictionary_splat_pattern" else "tuple")
    # self.x: T = ... inside __init__ become fields of the class
    if kind == "method":
        body = n.child_by_field_name("body")

        def stmts(node):   # statements at any nesting level except inside nested defs/classes/lambdas
            for c in (node.named_children if node is not None else []):
                if c.type in ("function_definition", "class_definition", "decorated_definition", "lambda"):
                    continue
                if c.type in ("expression_statement", "assignment"):
                    yield c
                elif c.type in ("if_statement", "elif_clause", "else_clause", "try_statement", "except_clause", "finally_clause",
                                "with_statement", "for_statement", "while_statement", "block", "match_statement", "case_clause"):
                    yield from stmts(c)
        for stmt in stmts(body):
            a = py_assignment(stmt)
            if a is not None:
                left, typ = a.child_by_field_name("left"), a.child_by_field_name("type")
                if left is not None and left.type == "attribute" and ctx.text(left).startswith("self."):
                    fname = ctx.text(left)[5:]
                    cls = ctx.stack[-2]
                    if "." not in fname and not any(x["kind"] == "field" and x["name"] == fname and x["parent"] == cls["id"] for x in ctx.nodes):
                        ctx.stack.append(cls)
                        ftype = ctx.text(typ) if typ is not None else None
                        if ftype is None:  # self.x = SomeClass(...), self.x = param (typed), self.x = p or SomeClass(...)
                            ftype = py_field_value_type(ctx, a.child_by_field_name("right"))
                        ctx.add_node("field", fname, stmt, signature=f"{fname}: {ftype}" if ftype else fname, type_text=ftype,
                                     extra={"type": ftype, "inferred": True})
                        ctx.stack.pop()
    ctx.walk_children(n.child_by_field_name("body"))
    ctx.pop_scope()
    ctx.pop()
    return True


# Literal receivers, normalized to the type they are. `"a,b".join(xs)` is a call to str.join,
# not to a method on a symbol named `"a,b"`. Recording the literal text was doing two kinds of
# damage: it pasted whole string literals into skeleton `calls:` lines (1.06% of all call refs in
# TensorFlow, some of them 60+ characters), and it offered the linker a receiver that could match
# a same-named repo method and produce a false edge.
PY_LITERAL_TYPES = {
    "string": "str", "concatenated_string": "str", "f_string": "str",
    "integer": "int", "float": "float", "true": "bool", "false": "bool", "none": "None",
    "list": "list", "dictionary": "dict", "set": "set", "tuple": "tuple",
    "list_comprehension": "list", "dictionary_comprehension": "dict", "set_comprehension": "set",
}
JS_LITERAL_TYPES = {
    "string": "String", "template_string": "String", "number": "Number",
    "true": "Boolean", "false": "Boolean", "null": "null", "undefined": "undefined",
    "array": "Array", "object": "Object", "regex": "RegExp",
}


def receiver_hint(ctx, obj, literal_types):
    """Text of a call receiver, with literals collapsed to their type name.

    Unwraps parentheses first: `("a " "b").upper()` parses as a parenthesized_expression around
    the literal, and without this the raw text leaked through (caught by the fixture's
    two-line literal).
    """
    if obj is None:
        return None
    inner = obj
    while inner.type == "parenthesized_expression":
        named = [c for c in inner.named_children if c.type != "comment"]
        if len(named) != 1:
            break
        inner = named[0]
    return literal_types.get(inner.type) or ctx.text(obj)


def py_call(ctx, n):
    fn = n.child_by_field_name("function")
    if fn is not None:
        if fn.type == "identifier":
            ctx.add_ref("call", ctx.text(fn), n)
        elif fn.type == "attribute":
            ctx.add_ref("call", ctx.text(fn.child_by_field_name("attribute")), n,
                        hint=receiver_hint(ctx, fn.child_by_field_name("object"), PY_LITERAL_TYPES))
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


def py_bind_value(ctx, name, right):
    """Type a local from its value: `X(...)` / `mod.X(...)` constructor, else the callee for link-time typing."""
    if right is None:
        return
    ops = py_value_operands(right)
    if len(ops) > 1:   # `q = query or Query(model)`: bind from the first operand that yields a type
        before = ctx.lookup(name)
        for op in ops:
            py_bind_value(ctx, name, op)
            if ctx.lookup(name) != before:
                return
        return
    unwrap = right.type == "await"
    if unwrap and right.named_children:
        right = right.named_children[0]
    if right.type == "subscript" and right.named_children:
        base_t = py_type_of_expr(ctx, right.named_children[0])
        kv = py_mapping_types(base_t)
        elem = kv[1] if kv else py_element_of(base_t)
        if elem:
            ctx.declare(name, elem)   # `s = self.items[0]` / `sw = self.by_name["x"]`
        return
    if right.type != "call":
        return
    fn = right.child_by_field_name("function")
    if fn is None:
        return
    t = base_type_name(ctx.text(fn))
    if t[:1].isupper():
        full = strip_type_decor(ctx.text(fn))
        # `bp = flask.Blueprint(...)`: keep the module qualifier so the type resolves through the import
        ctx.declare(name, full if re.match(r"^[a-z_]\w*(\.\w+)*\.[A-Z]", full) else t)
    else:
        ctx.declare_call(name, ctx.text(fn), unwrap)


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
            else:
                py_bind_value(ctx, ctx.text(left), right)
    return False


def py_named(ctx, n):
    """`(found := d.get(k))`: the walrus binds like an assignment."""
    kids = n.named_children
    if len(kids) == 2 and kids[0].type == "identifier" and ctx.top()["kind"] != "file":
        py_bind_value(ctx, ctx.text(kids[0]), kids[1])
    return False


def py_with(ctx, n):
    """`with httpx.Client() as client:` types `client` as the constructed class (the 99% case of `__enter__`)."""
    for clause in (c for c in n.named_children if c.type == "with_clause"):
        for item in (c for c in clause.named_children if c.type == "with_item"):
            ap = next((c for c in item.named_children if c.type == "as_pattern"), None)
            if ap is None or len(ap.named_children) < 2:
                continue
            value, alias = ap.named_children[0], ap.named_children[-1]
            if alias.type == "as_pattern_target" and alias.named_children:
                alias = alias.named_children[0]
            if alias.type == "identifier" and ctx.top()["kind"] != "file":
                py_bind_value(ctx, ctx.text(alias), value)
    return False


PY_ITERABLES = r"(?:typing\.)?(?:list|List|set|Set|frozenset|FrozenSet|tuple|Tuple|Sequence|MutableSequence|Iterable|Iterator|Collection|deque|Deque|Generator|AsyncIterator|AsyncIterable|AsyncGenerator|KeysView|ValuesView)"
PY_MAPPINGS = r"(?:typing\.|collections\.)?(?:dict|Dict|Mapping|MutableMapping|defaultdict|DefaultDict|OrderedDict)"


def py_element_of(t):
    """`list[Item]` / `Iterable[Item]` / `tuple[Item, ...]` -> `Item`; `dict[K, V]` -> `K`; else None."""
    if not t:
        return None
    t = unwrap_optional(t)
    m = re.match(r"^" + PY_ITERABLES + r"\[(.+)\]$", t)
    if m:
        return split_top(m.group(1))[0].strip().strip("\"'") or None
    kv = py_mapping_types(t)
    return kv[0] if kv else None


def py_mapping_types(t):
    """`dict[str, Item]` -> ("str", "Item"); None when not a mapping with two arguments."""
    if not t:
        return None
    m = re.match(r"^" + PY_MAPPINGS + r"\[(.+)\]$", unwrap_optional(t))
    if not m:
        return None
    parts = [x.strip().strip("\"'") for x in split_top(m.group(1))]
    return (parts[0], parts[1]) if len(parts) == 2 else None


def py_type_of_expr(ctx, node):
    """Declared type text of a simple expression: a scope variable or `self.<field>`; else None."""
    if node is None:
        return None
    if node.type == "identifier":
        t = ctx.lookup(ctx.text(node))
        return t if t and not t.startswith("<") else None
    if node.type == "attribute":
        obj, attr = node.child_by_field_name("object"), node.child_by_field_name("attribute")
        if obj is not None and attr is not None and ctx.text(obj) in ("self", "cls"):
            cls = next((x for x in reversed(ctx.stack) if x["kind"] == "class"), None)
            if cls is not None:
                for f in ctx.nodes:
                    if f["kind"] == "field" and f["parent"] == cls["id"] and f["name"] == ctx.text(attr):
                        t = f["extra"].get("type")
                        return t if t and not t.startswith("<") else None
    return None


def py_declare_loop_vars(ctx, left, right):
    """`for it in self.items` / `for k, v in d.items()` / comprehension clauses: type the loop variables."""
    if left is None or right is None or ctx.top()["kind"] == "file":
        return
    elem, kv = None, None
    if right.type == "call":
        fn = right.child_by_field_name("function")
        if fn is not None and fn.type == "attribute":
            m = ctx.text(fn.child_by_field_name("attribute"))
            base = py_mapping_types(py_type_of_expr(ctx, fn.child_by_field_name("object")))
            if base and m == "items":
                kv = base
            elif base and m == "values":
                elem = base[1]
            elif base and m == "keys":
                elem = base[0]
    else:
        elem = py_element_of(py_type_of_expr(ctx, right))
    names = [ctx.text(left)] if left.type == "identifier" else [ctx.text(c) for c in left.named_children if c.type == "identifier"] if left.type in ("pattern_list", "tuple_pattern") else []
    if kv and len(names) == 2:
        ctx.declare(names[0], kv[0])
        ctx.declare(names[1], kv[1])
    elif elem and len(names) == 1:
        ctx.declare(names[0], elem)


def py_for(ctx, n):
    py_declare_loop_vars(ctx, n.child_by_field_name("left"), n.child_by_field_name("right"))
    return False


def py_comprehension(ctx, n):
    """Clauses are declared before the element expression is walked (`[i.price() for i in self.items]`)."""
    for c in n.named_children:
        if c.type == "for_in_clause":
            py_declare_loop_vars(ctx, c.child_by_field_name("left"), c.child_by_field_name("right"))
    ctx.walk_children(n)
    return True


PY_HANDLERS = {
    "decorated_definition": py_decorated,
    "class_definition": py_class,
    "function_definition": py_function,
    "call": py_call,
    "import_statement": py_import,
    "import_from_statement": py_import,
    "expression_statement": py_expr_stmt, "assignment": py_expr_stmt, "named_expression": py_named,
    "with_statement": py_with, "for_statement": py_for,
    "list_comprehension": py_comprehension, "set_comprehension": py_comprehension,
    "generator_expression": py_comprehension, "dictionary_comprehension": py_comprehension,
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
            ctx.add_ref("call", ctx.text(fn.child_by_field_name("property")), n,
                        hint=receiver_hint(ctx, fn.child_by_field_name("object"), JS_LITERAL_TYPES))
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
    for nm, v in zip(names, vals, strict=False):
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
    static = re.match(r"^import\s+static\s", txt) is not None
    txt = re.sub(r"^import\s+(static\s+)?", "", txt).strip()
    wildcard = txt.endswith(".*")
    # `import static a.b.C.m;` / `import static a.b.C.*;` bring C's static members into scope (resolved per call)
    extra = {"static": True} if static else {}
    ctx.add_ref("import", txt[:-2] if wildcard else txt, n, names=["*"] if wildcard else [txt.split(".")[-1]], **extra)
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
        name = next((ctx.text(c) for c in p.named_children if c.type == "simple_identifier"), None)
        typ = next((c for c in p.named_children if c.type in ("user_type", "nullable_type", "function_type", "parenthesized_type")), None)
        ttxt = ctx.text(typ) if typ is not None else None
        if not any(c.type == "binding_pattern_kind" for c in p.named_children):
            if name and ttxt:
                ctx.declare(name, ttxt)   # a plain constructor parameter is in scope for property initialisers and init blocks
            continue
        if name:
            ctx.add_node("field", name, p, signature=f"{name}: {ttxt}" if ttxt else name, type_text=ttxt, extra={"type": ttxt})


def kt_class(ctx, n):
    name = next((ctx.text(c) for c in n.named_children if c.type == "type_identifier"), None)
    if not name:
        return False
    kw = [c.type for c in n.children if not c.is_named]
    kind = "interface" if "interface" in kw else "enum" if "enum" in kw else "class"
    tp = next((c for c in n.named_children if c.type == "type_parameters"), None)
    sig = f"{kind if kind != 'enum' else 'enum class'} {name}{ctx.text(tp) if tp is not None else ''}"
    node = ctx.add_node(kind, name, n, signature=sig, annotations=kt_annotations(ctx, n))
    ctx.push(node)
    ctx.push_scope()   # constructor parameters live here
    kt_supertypes(ctx, n)
    kt_ctor_fields(ctx, n)
    for body in (c for c in n.named_children if c.type in ("class_body", "enum_class_body")):
        for e in (x for x in body.named_children if x.type == "enum_entry"):
            ename = next((ctx.text(x) for x in e.named_children if x.type == "simple_identifier"), None)
            if ename:
                ctx.add_node("field", ename, e, signature=f"{ename}: {ctx.top()['name']}", extra={"type": ctx.top()["name"], "inferred": True})
        ctx.walk_children(body)
    ctx.pop_scope()
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
    if ret is None and ctx.top()["kind"] in ("class", "interface", "object"):
        body = next((c for c in n.named_children if c.type == "function_body"), None)
        btxt = ctx.text(body).lstrip("= \t") if body is not None else ""
        if re.match(r"^(apply|also)\s*\{", btxt) or btxt.strip() == "this":
            ret = ctx.top()["name"]   # `fun id(v: String) = apply { id = v }`: a builder setter returns the receiver
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


KT_COLLECTION_CTORS = {"listOf": "List", "mutableListOf": "MutableList", "arrayListOf": "ArrayList", "listOfNotNull": "List",
                       "setOf": "Set", "mutableSetOf": "MutableSet", "hashSetOf": "HashSet", "linkedSetOf": "LinkedHashSet",
                       "emptyList": "List", "emptySet": "Set", "sequenceOf": "Sequence", "arrayOf": "Array", "ArrayList": "ArrayList",
                       "HashSet": "HashSet", "ArrayDeque": "ArrayDeque"}


def kt_type_of_expr(ctx, node):
    """Declared type text of a simple Kotlin expression: a scope variable, or a property of the enclosing class."""
    if node is None:
        return None
    name = None
    if node.type == "simple_identifier":
        name = ctx.text(node)
        t = ctx.lookup(name)
        if t and not t.startswith("<"):
            return t
    elif node.type == "navigation_expression" and node.named_children and node.named_children[0].type == "this_expression":
        suf = node.named_children[-1]
        name = next((ctx.text(c) for c in suf.named_children if c.type == "simple_identifier"), None) if suf.type == "navigation_suffix" else None
    if name:
        cls = next((x for x in reversed(ctx.stack) if x["kind"] in ("class", "interface")), None)
        if cls is not None:
            for f in ctx.nodes:
                if f["kind"] == "field" and f["parent"] == cls["id"] and f["name"] == name:
                    t = f["extra"].get("type")
                    return t if t and not t.startswith("<") else None
    return None


def kt_property(ctx, n):
    vd = next((c for c in n.named_children if c.type == "variable_declaration"), None)
    if vd is None:
        return False
    name = next((ctx.text(c) for c in vd.named_children if c.type == "simple_identifier"), None)
    typ = next((c for c in vd.named_children if c.type in ("user_type", "nullable_type", "function_type", "parenthesized_type")), None)
    init = kt_initializer(ctx, n)
    ttxt = ctx.text(typ) if typ is not None else None
    delegate = next((c for c in n.named_children if c.type == "property_delegate"), None)
    if init is None and delegate is not None and delegate.named_children and delegate.named_children[0].type == "call_expression":
        dcall = delegate.named_children[0]
        if dcall.named_children and ctx.text(dcall.named_children[0]) == "lazy":
            lam = next((x for suf in dcall.named_children if suf.type == "call_suffix" for x in suf.named_children if x.type == "annotated_lambda"), None)
            stmts = next((x for lit in (lam.named_children if lam is not None else []) if lit.type == "lambda_literal" for x in lit.named_children if x.type == "statements"), None)
            if stmts is not None and stmts.named_children:
                init = stmts.named_children[-1]   # `by lazy { Repo(Net()) }`: the value is the last expression
    while init is not None and init.type in ("elvis_expression", "parenthesized_expression") and init.named_children:
        init = init.named_children[0]   # `repo.find(id) ?: return` / `(x as T)`: the left operand carries the type
    if init is not None and init.type == "as_expression" and init.named_children and ttxt is None:
        ttxt = ctx.text(init.named_children[-1])   # `val real = chain as RealChain`
    if init is not None and init.type == "indexing_expression" and init.named_children and ttxt is None:
        elem = element_type(kt_type_of_expr(ctx, init.named_children[0]) or "")
        if elem:
            ttxt = elem   # `val interceptor = interceptors[index]` on a List<Interceptor> -> Interceptor
    callee = None
    if init is not None and init.type == "call_expression" and init.named_children:
        head = init.named_children[0]
        callee = ctx.text(head) if head.type in ("simple_identifier", "navigation_expression") else None
        if callee:
            callee = re.sub(r"\s+", "", callee)
        suf = next((c for c in init.named_children if c.type == "call_suffix"), None)
        targs = next((ctx.text(c) for c in (suf.named_children if suf is not None else []) if c.type == "type_arguments"), None)
        if ttxt is None and targs and callee in KT_COLLECTION_CTORS:
            ttxt = KT_COLLECTION_CTORS[callee] + targs   # `mutableListOf<Line>()` -> MutableList<Line>: element type for `it`
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
        if ctx.top()["kind"] == "file":
            # top-level property: a node, so `import pkg.currentDialect` resolves and
            # `currentDialect.functionProvider.f()` can be typed from its declared type
            ctx.add_node("variable", name, n, signature=f"val {name}: {ttxt}" if ttxt else f"val {name}",
                         extra={"type": ttxt or (("<call>" + callee.replace("?.", ".") + "|") if callee else None), "inferred": typ is None})
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
    lam = {"callee": None, "recv": None, "hint_type": None, "chain": None, "argc": argc}
    if head.type == "simple_identifier":
        vt = ctx.lookup(ctx.text(head))
        if vt and not vt.startswith("<"):
            extra["invoke_type"] = vt   # `getFollowableTopics()` on a typed parameter: `operator fun invoke`
        ctx.add_ref("call", ctx.text(head), n, **extra)
        lam["callee"] = ctx.text(head)
    elif head.type == "navigation_expression" and head.named_children:
        recv, suf = head.named_children[0], head.named_children[-1]
        member = next((ctx.text(c) for c in suf.named_children if c.type == "simple_identifier"), None) if suf.type == "navigation_suffix" else None
        if member:
            hint = re.sub(r"\bthis@\w+", "this", ctx.text(recv).replace("?.", ".").replace("!!", ""))   # `this@label.f()` is `this.f()`
            ref = ctx.add_ref("call", member, n, hint=hint, **extra)
            # `(x as Foo).bar()`: the cast tells us the receiver type
            if recv.type == "parenthesized_expression" and recv.named_children and recv.named_children[0].type == "as_expression":
                ae = recv.named_children[0]
                ref["hint_type"] = ctx.text(ae.named_children[-1])
                ref["chain"] = []
            lam.update(callee=member, recv=ref["hint"], hint_type=ref.get("hint_type"), chain=ref.get("chain"))
    for c in n.named_children[1:]:
        if c.type != "call_suffix":
            ctx.walk(c)
            continue
        for sc in c.named_children:
            if sc.type == "annotated_lambda":
                ctx.lambda_stack.append(lam)     # `x.apply { add() }`, `order(id) { add() }`, `xs.forEach { it.f() }`
                ctx.walk(sc)
                ctx.lambda_stack.pop()
            elif sc.type == "value_arguments":
                for a in sc.named_children:
                    if a.type == "value_argument" and a.named_children and a.named_children[-1].type == "lambda_literal":
                        ctx.lambda_stack.append(lam)
                        ctx.walk(a)
                        ctx.lambda_stack.pop()
                    else:
                        ctx.walk(a)
            else:
                ctx.walk(sc)
    if head.type != "simple_identifier":
        ctx.walk(head)   # `a.b().c()`: the receiver chain holds further calls (and chained constructors)
    return True


KT_OPERATOR_FUNS = {"+": "plus", "-": "minus", "*": "times", "/": "div", "%": "rem"}
KT_BUILTIN_TYPES = {"Int", "Long", "Short", "Byte", "Double", "Float", "Char", "Boolean", "String", "Number", "Any", "Unit"}


def kt_arith(ctx, n):
    """`Line(1) + Line(2)` / `a * b` with a repo-typed left operand: a call of the operator function.
    Left operands that are literals, untyped or of builtin types record nothing (no false leads)."""
    kids = n.named_children
    if len(kids) != 2:
        return False
    op = next((ctx.text(c) for c in n.children if not c.is_named and ctx.text(c) in KT_OPERATOR_FUNS), None)
    left = kids[0]
    ltxt = ctx.text(left)
    typed = False
    if left.type == "call_expression" and left.named_children and left.named_children[0].type == "simple_identifier" \
            and ctx.text(left.named_children[0])[:1].isupper():
        typed = True
    elif left.type == "simple_identifier":
        t = ctx.lookup(ltxt)
        typed = bool(t) and not t.startswith("<") and strip_type_decor(t).split(".")[-1] not in KT_BUILTIN_TYPES
    elif left.type == "this_expression":
        typed = True
    if op and typed:
        argc, types = ctx.arg_info([kids[1]])
        extra = {"argc": argc}
        if types:
            extra["arg_types"] = types
        ctx.add_ref("call", KT_OPERATOR_FUNS[op], n, hint=ltxt, **extra)
    ctx.walk(kids[0])
    ctx.walk(kids[1])
    return True


def kt_smart_casts(ctx, cond):
    """[(name, type)] asserted by `x is T` conditions (directly, in parentheses or joined by &&)."""
    out = []
    if cond is None:
        return out
    if cond.type == "check_expression":
        kids = cond.named_children
        txt = ctx.text(cond)
        if len(kids) == 2 and kids[0].type == "simple_identifier" and " is " in txt and "!is" not in txt:
            out.append((ctx.text(kids[0]), ctx.text(kids[1])))
    elif cond.type in ("conjunction_expression", "parenthesized_expression"):
        for c in cond.named_children:
            out += kt_smart_casts(ctx, c)
    return out


def kt_if(ctx, n):
    """`if (e is Click) e.describe()`: inside the then-branch, `e` is a Click."""
    kids = n.named_children
    if not kids:
        return False
    cond = kids[0]
    ctx.walk(cond)
    casts = kt_smart_casts(ctx, cond)
    first_body = True
    for c in kids[1:]:
        if c.type == "control_structure_body" and first_body and casts:
            ctx.push_scope()
            for name, typ in casts:
                ctx.declare(name, typ)
            ctx.walk(c)
            ctx.pop_scope()
        else:
            ctx.walk(c)
        if c.type == "control_structure_body":
            first_body = False
    return True


def kt_when(ctx, n):
    """`when (e) { is Click -> e.describe() }`: inside that entry, `e` is a Click."""
    subj = next((c for c in n.named_children if c.type == "when_subject"), None)
    name = ctx.text(subj.named_children[0]) if subj is not None and subj.named_children and subj.named_children[0].type == "simple_identifier" else None
    for c in n.named_children:
        if c.type != "when_entry":
            ctx.walk(c)
            continue
        typ = None
        body = None
        for x in c.named_children:
            if x.type == "when_condition" and x.named_children and x.named_children[0].type == "type_test":
                t = ctx.text(x.named_children[0]).strip()
                if t.startswith("is ") and typ is None:
                    typ = t[3:].strip()
            if x.type == "control_structure_body":
                body = x
            if x.type != "control_structure_body":
                ctx.walk(x)
        if body is not None and name and typ:
            ctx.push_scope()
            ctx.declare(name, typ)
            ctx.walk(body)
            ctx.pop_scope()
        elif body is not None:
            ctx.walk(body)
    return True


def kt_callable_ref(ctx, n):
    """`Topic::slug` / `::helper`: a reference to a callable is recorded as a call of it."""
    kids = n.named_children
    if not kids:
        return False
    if len(kids) >= 2:
        ctx.add_ref("call", ctx.text(kids[-1]), n, hint=ctx.text(kids[0]), argc=None)
    else:
        ctx.add_ref("call", ctx.text(kids[0]), n, argc=None)
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


def hcl_refs_from_text(ctx, txt, tsnode, kind="references", skip=frozenset()):
    """Record every `var.x` / `local.x` / `module.x` / `data.t.n` / `type.name` reference in an
    expression. `tsnode` is the node whose text is `txt`, so the recorded line is the line of the
    reference itself even inside a multi-line expression. `skip` holds names that are not
    addresses in this scope: the iterators of enclosing `dynamic` blocks (`node_config.value`)."""
    seen = set()
    for m in HCL_REF_RE.finditer(txt):
        if m.group(1):
            p, a, b = m.group(1), m.group(2), m.group(3)
            name = f"data.{a}.{b}" if p == "data" and b else f"{p}.{a}"
            attr = None if p == "data" else b
        else:
            name, attr = f"{m.group(4)}.{m.group(5)}", None
            if m.group(4) in skip:
                continue
        key = (name, attr)
        if key in seen:
            continue
        seen.add(key)
        ctx.add_ref(kind, name, tsnode, attr=attr, line=tsnode.start_point[0] + 1 + txt[:m.start()].count("\n"))


def hcl_attrs(ctx, body):
    """Yield (key, expression_node) for attributes directly in a body."""
    for c in (body.named_children if body is not None else []):
        if c.type == "attribute":
            key = ctx.text(c.named_children[0]) if c.named_children else ""
            expr = c.named_children[1] if len(c.named_children) > 1 else None
            yield key, expr


def hcl_block_parts(ctx, n):
    """(block type, labels, body node) of a `block` node."""
    btype, labels, body = None, [], None
    for c in n.named_children:
        if c.type == "identifier" and btype is None:
            btype = ctx.text(c)
        elif c.type == "string_lit":
            labels.append(strip_quotes(ctx.text(c)))
        elif c.type == "body":
            body = c
    return btype, labels, body


def hcl_walk_body_refs(ctx, body, skip=frozenset()):
    for c in (body.named_children if body is not None else []):
        if c.type == "attribute":
            key = ctx.text(c.named_children[0]) if c.named_children else ""
            expr = c.named_children[1] if len(c.named_children) > 1 else None
            if expr is None:
                continue
            hcl_refs_from_text(ctx, ctx.text(expr), expr, kind="depends_on" if key == "depends_on" else "references", skip=skip)
        elif c.type == "block":
            btype, labels, b = hcl_block_parts(ctx, c)
            inner = skip
            if btype == "dynamic":
                # `dynamic "node_config" { for_each = ...; iterator = nc; content { ... nc.value.x } }`:
                # the iterator (default: the block label) is a loop variable, not a resource address
                it = strip_quotes(dict(hcl_attrs_text(ctx, b)).get("iterator", "")).strip() or (labels[0] if labels else "")
                if it:
                    inner = skip | {it}
            if b is not None:
                hcl_walk_body_refs(ctx, b, inner)


def hcl_attrs_text(ctx, body):
    for k, e in hcl_attrs(ctx, body):
        yield k, (ctx.text(e) if e is not None else "")


def hcl_block(ctx, n):
    if ctx.top()["kind"] != "file":
        return False
    btype, labels, body = hcl_block_parts(ctx, n)
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
    elif btype in ("moved", "import") and "to" in attrs:
        # `moved { from = a.b  to = c.d }` / `import { to = c.d  id = "..." }`: the `to` address is a
        # dependent of the resource (renaming it breaks the block); `from` no longer exists in code.
        to = attrs["to"].strip()
        frm = attrs.get("from", "").strip()
        kind = "moved" if btype == "moved" else "import_block"
        node = ctx.add_node(kind, f"{btype}.{to}", n, signature=f"moved {frm} -> {to}" if btype == "moved" else f"import {to}",
                            extra={"from": frm, "to": to, "id": strip_quotes(attrs.get("id", ""))} if btype == "import" else {"from": frm, "to": to})
        ctx.push(node)
        e = next((e for k, e in hcl_attrs(ctx, body) if k == "to"), None)
        hcl_refs_from_text(ctx, to, e if e is not None else n)
        ctx.pop()
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
    if re.search(rb"(?<!\$)\{\{", ctx.src):  # Helm/Go template (not GitHub Actions `${{ }}`): index best-effort by regex
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
    "infix_expression": kt_infix, "additive_expression": kt_arith, "multiplicative_expression": kt_arith,
    "if_expression": kt_if, "when_expression": kt_when, "callable_reference": kt_callable_ref,
}



# ----------------------------------------------------------------------------------------------
# C / C++
#
# Shapes that matter, from the tree-sitter-cpp grammar:
#   function_definition.declarator -> function_declarator, possibly wrapped in
#     pointer_declarator / reference_declarator ("Tensor* f()"), so unwrap before reading a name.
#   function_declarator.declarator -> identifier (free function) | field_identifier (in-class)
#     | qualified_identifier ("MatMulOp::Compute", an out-of-line definition of a member declared
#     in a header -- CPP_QUALIFIED_DEFS below records the owner so phase 2 can merge the two).
#   call_expression.function -> identifier | field_expression (obj.m / ptr->n / this->h)
#     | qualified_identifier (ns::f / Class::static_m).
#   Members live in field_declaration_list: function_definition (inline body),
#     field_declaration with a function_declarator (a declaration), declaration (constructors),
#     or field_declaration with a plain declarator (a data member).
# ----------------------------------------------------------------------------------------------
CPP_DECL_WRAPPERS = {"pointer_declarator", "reference_declarator", "array_declarator",
                     "parenthesized_declarator", "init_declarator"}


def cpp_unwrap(n):
    """Strip pointer/reference/array wrappers to the declarator that carries the name."""
    seen = 0
    while n is not None and n.type in CPP_DECL_WRAPPERS and seen < 8:
        nxt = n.child_by_field_name("declarator")
        if nxt is None:
            break
        n, seen = nxt, seen + 1
    return n


def cpp_decl_name(ctx, n):
    """(name, owner) for a declarator. `owner` is set for out-of-line `Class::method` definitions."""
    n = cpp_unwrap(n)
    if n is None:
        return "", None
    if n.type == "function_declarator":
        return cpp_decl_name(ctx, n.child_by_field_name("declarator"))
    if n.type == "qualified_identifier":
        txt = ctx.text(n)
        parts = [p for p in txt.split("::") if p]
        if len(parts) >= 2:
            return parts[-1], "::".join(parts[:-1])
        return txt, None
    if n.type in ("identifier", "field_identifier", "type_identifier", "destructor_name",
                  "operator_name", "primitive_type"):
        return ctx.text(n), None
    return ctx.text(n).strip(), None


def cpp_find_declarator(n):
    """The function_declarator inside a definition/declaration, through any wrappers."""
    d = cpp_unwrap(n.child_by_field_name("declarator"))
    if d is not None and d.type == "function_declarator":
        return d
    # `Tensor* Class::f()` can park the pointer_declarator under an ERROR node; look one level in.
    for c in n.named_children:
        if c.type in ("ERROR",) or c.type in CPP_DECL_WRAPPERS:
            cand = cpp_unwrap(c if c.type in CPP_DECL_WRAPPERS else
                              next((g for g in c.named_children if g.type in CPP_DECL_WRAPPERS
                                    or g.type == "function_declarator"), None))
            if cand is not None and cand.type == "function_declarator":
                return cand
    return None


def cpp_member_kind(name, holder):
    """constructor / destructor / method, from the member name and its holding type."""
    if holder and name.lstrip("~") == holder:
        return "destructor" if name.startswith("~") else "constructor"
    return "destructor" if name.startswith("~") else "method"


def cpp_type_name(ctx, node):
    """`const acme::Engine&` / `Engine*` / `std::unique_ptr<Engine>` -> a bare class name, or None."""
    if node is None:
        return None
    txt = ctx.text(node).strip()
    txt = re.sub(r"\b(const|volatile|static|mutable|constexpr|inline|struct|class|typename)\b", " ", txt)
    txt = txt.replace("*", " ").replace("&", " ").strip()
    if not txt:
        return None
    # a smart pointer or container names the interesting type inside its template arguments
    m = re.match(r"^(?:std::)?(?:unique_ptr|shared_ptr|weak_ptr|optional|vector|span)\s*<(.+)>$", txt)
    if m:
        txt = m.group(1).split(",")[0].strip()
    txt = txt.split("<")[0].strip().split("::")[-1]
    return txt or None


def cpp_declare_params(ctx, declarator):
    """Bind parameter names to their types so `ctx->input(0)` resolves through the parameter."""
    params = declarator.child_by_field_name("parameters") if declarator is not None else None
    for prm in (params.named_children if params is not None else []):
        if prm.type != "parameter_declaration":
            continue
        t = cpp_type_name(ctx, prm.child_by_field_name("type"))
        d = cpp_unwrap(prm.child_by_field_name("declarator"))
        if t and d is not None and d.type in ("identifier", "field_identifier"):
            ctx.declare(ctx.text(d), t)


def cpp_include(ctx, n):
    """`#include "a/b.h"` / `#include <vector>` -> an import ref carrying the path."""
    for c in n.named_children:
        if c.type == "system_lib_string":
            ctx.add_ref("import", ctx.text(c).strip("<>"), n, extra_kind="system")
            return True
        if c.type == "string_literal":
            ctx.add_ref("import", strip_quotes(ctx.text(c)), n)
            return True
    return True


def cpp_namespace(ctx, n):
    name = ctx.text(n.child_by_field_name("name")) or "(anonymous)"
    node = ctx.add_node("module", name, n, signature=f"namespace {name}")
    ctx.push(node)
    ctx.walk_children(n.child_by_field_name("body"))
    ctx.pop()
    return True


def cpp_type_decl(kind):
    """class / struct / union / enum."""
    def h(ctx, n):
        nm = n.child_by_field_name("name")
        name = ctx.text(nm) if nm is not None else ""
        if not name:
            return False            # anonymous struct in a typedef: let the walk continue
        body = n.child_by_field_name("body")
        # `class OpKernel;` is a forward declaration, not a definition. TensorFlow forward-declares
        # OpKernel in four headers besides the one that defines it; without this the real class
        # competes with four empty stubs and `query symbol OpKernel` just reports an ambiguity.
        extra = {} if body is not None else {"is_declaration": True, "forward": True}
        node = ctx.add_node(kind, name, n, signature=f"{kind} {name}", extra=extra)
        ctx.push(node)
        for c in n.named_children:
            if c.type == "base_class_clause":
                for t in c.named_children:
                    if t.type in ("type_identifier", "qualified_identifier", "template_type"):
                        full = ctx.text(t)
                        ctx.add_ref("extends", base_type_name(full.split("::")[-1]), t,
                                    hint=full.split("::")[0] if "::" in full else None)
        body = n.child_by_field_name("body")
        for c in (body.named_children if body is not None else []):
            if c.type == "enumerator":
                ctx.add_node("field", ctx.text(c.child_by_field_name("name")), c,
                             signature=ctx.text(c))
        ctx.walk_children(body)
        ctx.pop()
        return True
    return h


def cpp_function(ctx, n):
    """function_definition: a free function, an inline member, or an out-of-line `Class::method`."""
    d = cpp_find_declarator(n)
    if d is None:
        return False
    name, owner = cpp_decl_name(ctx, d)
    if not name:
        return False
    if name in ("PYBIND11_MODULE", "PYBIND11_PLUGIN"):
        return cpp_pybind_module(ctx, n, d)
    params = ctx.text(d.child_by_field_name("parameters"))
    ret = ctx.text(n.child_by_field_name("type"))
    parent_kind = ctx.top()["kind"]
    kind = "method" if (owner or parent_kind in ("class", "struct", "union")) else "function"
    # `MatMulOp::MatMulOp` defined out of line: the enclosing scope is the namespace, so the
    # owner -- not ctx.top() -- is what says whether this is a constructor.
    holder = owner.split("::")[-1] if owner else ctx.top().get("name")
    if owner or parent_kind in ("class", "struct", "union"):
        kind = cpp_member_kind(name, holder)
    sig = " ".join(x for x in (ret, f"{name}{params}") if x).strip()
    extra = {"has_body": n.child_by_field_name("body") is not None}
    if owner:
        extra["owner"] = owner
    qname = f"{owner.replace('::', '.')}.{name}" if owner and parent_kind == "file" else None
    if owner and parent_kind != "file":
        qname = f"{ctx.top()['qname']}.{owner.replace('::', '.')}.{name}"
    node = ctx.add_node(kind, name, n, signature=sig, qname=qname,
                        type_text=f"{ret} {params}", extra=extra)
    if owner:
        # The definition lives away from its class; record the link so `Class::m` resolves.
        ctx.add_ref("defines_member", owner.split("::")[-1], n, hint=None, member=name)
    ctx.push(node)
    ctx.push_scope()
    cpp_declare_params(ctx, d)
    if kind in ("method", "constructor", "destructor"):
        holder_node = ctx.stack[-2]
        ctx.declare("this", owner.split("::")[-1] if owner else holder_node.get("name"))
    ctx.walk_children(n.child_by_field_name("body"))
    ctx.pop_scope()
    ctx.pop()
    return True


def cpp_field(ctx, n):
    """field_declaration: a member function declaration, or a data member."""
    d = cpp_unwrap(n.child_by_field_name("declarator"))
    typ = ctx.text(n.child_by_field_name("type"))
    if d is not None and d.type == "function_declarator":
        name, _ = cpp_decl_name(ctx, d)
        if not name:
            return True
        params = ctx.text(d.child_by_field_name("parameters"))
        kind = cpp_member_kind(name, ctx.top().get("name"))
        ctx.add_node(kind, name, n, signature=" ".join(x for x in (typ, f"{name}{params}") if x).strip(),
                     type_text=f"{typ} {params}")
        return True
    if d is not None and d.type in ("identifier", "field_identifier"):
        name = ctx.text(d)
        if name:
            ctx.add_node("field", name, n, signature=ctx.text(n).rstrip(";"),
                         type_text=typ, extra={"type": typ})
        return True
    return True


def cpp_declaration(ctx, n):
    """Constructor/destructor declarations in a class body, and file-scope variables."""
    d = cpp_unwrap(n.child_by_field_name("declarator"))
    if d is not None and d.type == "function_declarator":
        name, owner = cpp_decl_name(ctx, d)
        if name:
            params = ctx.text(d.child_by_field_name("parameters"))
            typ = ctx.text(n.child_by_field_name("type"))
            # A prototype at file or namespace scope (`void call(client *c, int flags);` in a C header) is
            # a free function: calling it a method kept every C call to a header-declared function unlinked.
            in_type = ctx.top()["kind"] in ("class", "struct", "union")
            kind = cpp_member_kind(name, owner.split("::")[-1] if owner else ctx.top().get("name")) if (owner or in_type) else "function"
            ctx.add_node(kind, name, n,
                         signature=" ".join(x for x in (typ, f"{name}{params}") if x).strip(),
                         type_text=f"{typ} {params}")
        return True
    if ctx.top()["kind"] in ("class", "struct", "union") and d is not None and \
            d.type in ("identifier", "field_identifier"):
        ctx.add_node("field", ctx.text(d), n, signature=ctx.text(n).rstrip(";"),
                     type_text=ctx.text(n.child_by_field_name("type")))
        return True
    # A local: `acme::Engine e(2);` or `Engine* e = ...`. Binding it is what lets `e.Run(x)` and
    # `e->Run(x)` resolve to Engine::Run instead of becoming an ambiguous name-only guess.
    t = cpp_type_name(ctx, n.child_by_field_name("type"))
    if t:
        for c in n.named_children:
            nm = cpp_unwrap(c)
            if nm is not None and nm.type in ("identifier", "field_identifier"):
                ctx.declare(ctx.text(nm), t)
    return False        # keep walking: initialisers hold calls


def cpp_call(ctx, n):
    fn = n.child_by_field_name("function")
    if fn is None:
        return False
    if cpp_registration_call(ctx, n, fn):
        return False      # recorded as a binding; keep walking for nested calls
    if fn.type == "identifier":
        ctx.add_ref("call", ctx.text(fn), n)
    elif fn.type == "field_expression":
        recv = ctx.text(fn.child_by_field_name("argument"))
        ctx.add_ref("call", ctx.text(fn.child_by_field_name("field")), n,
                    hint=None if recv == "this" else recv)
    elif fn.type == "qualified_identifier":
        parts = [p for p in ctx.text(fn).split("::") if p]
        if parts:
            ctx.add_ref("call", parts[-1], n, hint="::".join(parts[:-1]) or None)
    elif fn.type in ("template_function", "parenthesized_expression"):
        inner = fn.child_by_field_name("name")
        if inner is not None:
            ctx.add_ref("call", ctx.text(inner).split("::")[-1], n)
    return False        # arguments can hold further calls


def cpp_new(ctx, n):
    t = n.child_by_field_name("type")
    if t is not None:
        ctx.add_ref("instantiates", base_type_name(ctx.text(t).split("::")[-1]), n)
    return False


def cpp_alias(ctx, n):
    name = ctx.text(n.child_by_field_name("name") or n.child_by_field_name("declarator"))
    if name:
        ctx.add_node("type", name, n, signature=ctx.text(n).rstrip(";"),
                     type_text=ctx.text(n.child_by_field_name("type")))
    return True


CPP_STRING_NODES = ("string_literal", "concatenated_string", "raw_string_literal")


def cpp_first_string_arg(ctx, call_node, deep=False):
    """The first string-literal argument of a call, unquoted. `REGISTER_OP("MatMul")` -> MatMul.

    `deep` searches descendants, which REGISTER_KERNEL_BUILDER needs: its op name is buried in a
    builder chain, `Name("MatMul").Device(DEVICE_CPU)`, not a direct argument."""
    args = call_node.child_by_field_name("arguments")
    if args is None:
        return None
    stack = list(args.named_children)
    while stack:
        a = stack.pop(0)
        if a.type in CPP_STRING_NODES:
            return strip_quotes(ctx.text(a)).strip()
        if deep:
            stack.extend(a.named_children)
    return None


def cpp_pybind_module(ctx, n, declarator):
    """`PYBIND11_MODULE(_pywrap_tfe, m) { ... }` declares the Python module a C++ extension exposes.

    The macro parses as an ordinary function_definition whose "parameters" are the module name and
    the handle, so the module name is the first parameter's text."""
    params = declarator.child_by_field_name("parameters")
    kids = [c for c in (params.named_children if params is not None else []) if c.type != "comment"]
    if not kids:
        return False
    mod = ctx.text(kids[0]).split()[-1].strip("*&")
    handle = ctx.text(kids[1]).split()[-1].strip("*&") if len(kids) > 1 else "m"
    node = ctx.add_node("py_module", mod, n, signature=f"PYBIND11_MODULE {mod}",
                        extra={"handle": handle})
    ctx.push(node)
    ctx.pybind_handles.append(handle)
    ctx.walk_children(n.child_by_field_name("body"))
    ctx.pybind_handles.pop()
    ctx.pop()
    return True


def cpp_registration_call(ctx, n, fn):
    """Names that cross the language boundary: pybind exports and TensorFlow op registrations.

    Returns True when the call was recorded as a binding, so the generic call handler skips it."""
    if fn.type == "field_expression":
        recv = ctx.text(fn.child_by_field_name("argument"))
        field = ctx.text(fn.child_by_field_name("field"))
        # `m.def("name", ...)` / `m.def_submodule(...)` on the handle PYBIND11_MODULE introduced.
        if field in ("def", "def_static", "def_property_readonly") and recv in ctx.pybind_handles:
            name = cpp_first_string_arg(ctx, n)
            if name:
                node = ctx.add_node("py_binding", name, n, signature=f"{recv}.{field}(\"{name}\")")
                # Walk the rest of the call inside the binding's scope, so whatever it exports --
                # `&TFE_Py_Execute`, or the calls inside an exported lambda -- is attributed to the
                # binding. That is the hop that carries a Python caller through to the C++ body;
                # without it the chain stops at the binding and blast radius still lies.
                args = n.child_by_field_name("arguments")
                kids = [a for a in (args.named_children if args is not None else []) if a.type != "comment"]
                ctx.push(node)
                for a in kids[1:]:
                    if a.type in ("pointer_expression", "identifier", "qualified_identifier"):
                        target = ctx.text(a).lstrip("&").split("::")[-1]
                        if target and target != name:
                            ctx.add_ref("call", target, a)
                    else:
                        ctx.walk(a)
                ctx.pop()
                return True
        return False
    if fn.type == "identifier":
        macro = ctx.text(fn)
        if macro in ("REGISTER_OP", "REGISTER_SYSTEM_OP"):
            name = cpp_first_string_arg(ctx, n)
            if name:
                ctx.add_node("op_def", name, n, signature=f'REGISTER_OP("{name}")')
                return True
        if macro in ("REGISTER_KERNEL_BUILDER", "REGISTER_SYSTEM_KERNEL_BUILDER"):
            args = n.child_by_field_name("arguments")
            kids = [a for a in (args.named_children if args is not None else []) if a.type != "comment"]
            if not kids:
                return False
            # first argument is the `Name("Op")...` builder chain, last is the kernel class
            op = cpp_first_string_arg(ctx, n, deep=True)
            kernel = base_type_name(ctx.text(kids[-1]).split("::")[-1]) if len(kids) > 1 else None
            if op:
                # the kernel class implements the registered op: a real edge across the boundary
                ctx.add_ref("registers_op", op, n, hint=kernel, kernel=kernel)
            return True
    return False


CPP_HANDLERS = {
    "preproc_include": cpp_include,
    "namespace_definition": cpp_namespace,
    "class_specifier": cpp_type_decl("class"),
    "struct_specifier": cpp_type_decl("struct"),
    "union_specifier": cpp_type_decl("union"),
    "enum_specifier": cpp_type_decl("enum"),
    "function_definition": cpp_function,
    "field_declaration": cpp_field,
    "declaration": cpp_declaration,
    "call_expression": cpp_call,
    "new_expression": cpp_new,
    "alias_declaration": cpp_alias,
    "type_definition": cpp_alias,
}


HANDLERS = {
    "cpp": CPP_HANDLERS, "c": CPP_HANDLERS,
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
    if lang == "kotlin":
        src = KT_TYPE_ANNOTATION_RE.sub(lambda m: b" " * len(m.group(0)), src)   # same byte offsets, no parse error
        src = KT_SUPERTYPE_ANNOTATION_RE.sub(lambda m: m.group(1) + b" " * len(m.group(2)), src)
    ctx = Ctx(path, lang, src, root_dir)
    tree = parser_for(lang).parse(src)
    if lang == "yaml":
        yaml_file(ctx, tree.root_node)
    else:
        ctx.walk(tree.root_node)
    ctx.file_node["extra"] = dict(ctx.file_extra, language=lang, has_errors=tree.root_node.has_error)
    if tree.root_node.has_error:
        ctx.file_node["extra"]["first_error_line"] = first_error_line(tree.root_node)
    return ctx.file_node, ctx.nodes, ctx.refs


def split_top(txt, sep=","):
    """Split at `sep` outside brackets (the `>` of a `->` function type is not a bracket)."""
    parts, cur, depth = [], "", 0
    for i, ch in enumerate(txt):
        if ch in "<([{":
            depth += 1
        elif ch in ">)]}" and not (ch == ">" and i > 0 and txt[i - 1] == "-"):
            depth -= 1
        if ch == sep and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return parts


def split_chain(hint):
    """Receiver text -> raw segments split at dots outside parentheses/brackets:
    `a.b(x.y).c` -> ["a", "b(x.y)", "c"]."""
    return [seg for seg in split_top(hint, ".")]


def unwrap_optional(t):
    """`Optional["X"]`, `X | None`, `"X"`, `Union[X, None]`, `Final[X]`, `Annotated[X, ..]`, Kotlin `X?` -> `X`."""
    t = t.strip().strip("\"'").strip()
    for _ in range(3):
        m = re.match(r"^(?:typing\.|t\.)?(?:Optional|Final|ClassVar|Annotated)\[(.+)\]$", t)
        if m:
            t = split_top(m.group(1))[0].strip().strip("\"'")
            continue
        m = re.match(r"^(?:typing\.|t\.)?Union\[(.+)\]$", t)
        if m:
            parts = [x.strip().strip("\"'") for x in split_top(m.group(1)) if x.strip() != "None"]
            if len(parts) == 1:
                t = parts[0]
                continue
            break
        if "|" in t:
            parts = [x.strip().strip("\"'") for x in split_top(t, "|") if x.strip() != "None"]
            if len(parts) == 1:
                t = parts[0]
                continue
        break
    return t[:-1] if t.endswith("?") else t


def element_type(t):
    """`List<Line>` / `MutableSet<Order?>` / `Sequence<X>` -> `Line` / `Order` / `X`; None for maps and non-collections."""
    m = re.match(r"^(?:kotlin\.collections\.|java\.util\.)?(?:Mutable)?(?:List|Set|Collection|Iterable|Sequence|Array|ArrayList|HashSet|LinkedHashSet|ArrayDeque|Deque)<(.+)>\??$", (t or "").strip())
    if not m:
        return None
    inner = m.group(1).strip()
    if "," in split_top(inner)[0] if False else len(split_top(inner)) != 1:
        return None
    return inner.rstrip("?").split("<")[0].strip()


KOTLIN_SYNTHETIC_MEMBERS = {"copy", "valueOf", "values", "entries", "ordinal", "name", "hashCode", "equals", "toString", "compareTo"}
KT_RECEIVER_LAMBDAS = {"apply", "run"}
KT_IT_LAMBDAS = {"also", "let", "takeIf", "takeUnless"}
KT_ELEMENT_LAMBDAS = {"forEach", "map", "mapNotNull", "filter", "filterNot", "first", "firstOrNull", "last", "lastOrNull", "find",
                      "any", "all", "none", "count", "sumOf", "flatMap", "onEach", "sortedBy", "sortedByDescending", "groupBy",
                      "associateBy", "associateWith", "maxByOrNull", "minByOrNull", "maxOf", "minOf", "partition", "takeWhile",
                      "dropWhile", "single", "singleOrNull", "indexOfFirst", "distinctBy", "forEachIndexed", "sumBy"}


# `content: @Composable RowScope.() -> Unit` / `x: @Composable () -> Unit`: the grammar rejects an
# annotation in a type position and loses the whole declaration. Blanked before parsing.
# `class K : @Suppress("DEPRECATION") api.MultiRule() {`: an annotated supertype (or any annotated type
# right after `:` / `,`) sends the grammar into error recovery, and the class body ends up at file level --
# every member lost from the class. Blanked before parsing, line and column positions unchanged.
KT_SUPERTYPE_ANNOTATION_RE = re.compile(rb"(?<=[:,])(\s*)(@[A-Za-z_][\w.]*(?:\([^()\n]*\))?)(?=\s+[A-Za-z_][\w.]*(?:<[^>]*>)?\s*[({,<\n])")
KT_TYPE_ANNOTATION_RE = re.compile(rb"@[A-Za-z_][\w.]*(?=[ \t]+(?:\(\)?[ \t]*->|\([^)]*\)[ \t]*->|[A-Za-z_][\w.]*(?:<[^>]*>)?\.\())")


BUILTIN_TYPE_NAMES = {"dict", "list", "set", "tuple", "str", "int", "float", "bool", "bytes", "bytearray", "object", "type",
                      "frozenset", "complex", "Dict", "List", "Set", "Tuple", "Any", "Callable", "Iterable", "Iterator",
                      "Sequence", "Mapping", "MutableMapping", "MutableSequence", "Optional", "Union", "Self",
                      "String", "Int", "Long", "Double", "Float", "Boolean", "Char", "Unit", "Nothing", "Number", "Short",
                      "Byte", "Map", "MutableMap", "HashMap", "MutableList", "ArrayList", "MutableSet", "HashSet", "Array",
                      "Integer", "Object", "Void", "CharSequence", "StringBuilder", "string", "error", "int64", "int32",
                      "float64", "float32", "byte", "rune", "number", "boolean", "void", "Promise", "Record", "Partial"}


def first_error_line(root):
    """1-based line of the first ERROR/MISSING node, descending only into subtrees that contain one.
    Some grammars (Kotlin) flag `has_error` on a node without producing an ERROR child; then the
    innermost flagged node's line is reported."""
    best, innermost, stack = None, None, [root]
    while stack:
        n = stack.pop()
        if n.is_error or n.is_missing:
            if any(c.has_error for c in n.children):
                stack.extend(c for c in n.children if c.has_error)   # a wrapper ERROR: report the inner one
                continue
            line = n.start_point[0] + 1
            best = line if best is None or line < best else best
            continue
        if n.has_error:
            kids = [c for c in n.children if c.has_error]
            if not kids and (innermost is None or n.start_point[0] + 1 < innermost):
                innermost = n.start_point[0] + 1
            stack.extend(kids)
    return best if best is not None else innermost


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


def render_skeleton(file_node, nodes, refs, show_calls=True, max_calls=CAP_SKELETON_CALLS,
                    show_lines=True, max_imports=CAP_SKELETON_IMPORTS, elided=None):
    """Render one file's skeleton.

    `elided` is an optional counter the caller passes in to collect what was hidden across files;
    the caller reports the recovery path once at the end rather than repeating a flag name on
    every elided line. (Inline recovery hints get read as code and cost a retry -- the same
    finding rtk documents in core/filter.rs when it removed its interleaved markers.)
    """
    if elided is None:
        elided = Counter()
    # 0 (or negative) means "no cap"; len(refs) + 1 exceeds any per-symbol or per-file list, since
    # both the calls and the imports are drawn from refs.
    no_cap = len(refs) + 1
    max_calls = max_calls if max_calls > 0 else no_cap
    max_imports = max_imports if max_imports > 0 else no_cap
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
        head += ", parse errors" + (f" (first at L{extra['first_error_line']})" if extra.get("first_error_line") else "")
    lines.append(head + ")")
    if imports:
        if len(imports) > max_imports:
            elided["imports"] += len(imports) - max_imports
        lines.append("  imports: " + ", ".join(imports[:max_imports])
                     + (f" (+{len(imports) - max_imports} more)" if len(imports) > max_imports else ""))

    def rng(n):
        if not show_lines:
            return ""
        return f"  [L{n['line']}]" if n["line"] == n["end_line"] else f"  [L{n['line']}-{n['end_line']}]"

    def emit(parent_id, depth):
        for n in sorted(by_parent.get(parent_id, []), key=lambda x: x["line"]):
            ann = f"  @{' @'.join(a.split('(')[0] for a in n['annotations'])}" if n["annotations"] else ""
            sig_lines = n["signature"].split("\n")
            if len(sig_lines) <= CAP_SKELETON_SIG_LINES:
                sig = n["signature"]
            else:
                keep = CAP_SKELETON_SIG_LINES - 1
                hidden = len(sig_lines) - keep
                elided["signature lines"] += hidden
                # The recovery path is the item's own line range; repeat it only when --no-lines
                # suppressed the range that would otherwise be printed on this same line.
                where = "" if show_lines else f", read L{n['line']}-{n['end_line']}"
                sig = "\n".join(sig_lines[:keep]) + f"\n{'  ' * depth}    ... (+{hidden} signature lines{where})"
            lines.append(f"{'  ' * depth}{kind_label(n)}{sig}{rng(n)}{ann}")
            if show_calls and calls_by_src.get(n["id"]):
                cl = calls_by_src[n["id"]]
                cl = [(" ".join(c.split())[:57] + "...") if len(" ".join(c.split())) > 60 else " ".join(c.split()) for c in cl]
                if len(cl) > max_calls:
                    elided["calls"] += len(cl) - max_calls
                lines.append(f"{'  ' * (depth + 1)}calls: " + ", ".join(cl[:max_calls])
                             + (f" (+{len(cl) - max_calls} more)" if len(cl) > max_calls else ""))
            emit(n["id"], depth + 1)

    emit(file_node["id"], 1)
    if show_calls and calls_by_src.get(file_node["id"]):
        cl = calls_by_src[file_node["id"]]
        if len(cl) > max_calls:
            elided["calls"] += len(cl) - max_calls
        lines.append("  module-level calls: " + ", ".join(cl[:max_calls])
                     + (f" (+{len(cl) - max_calls} more)" if len(cl) > max_calls else ""))
    return "\n".join(lines)


def elision_notice(elided):
    """One line naming what was hidden and the flag that brings it back, or "" if nothing was.

    Printed once per run rather than per elided line -- see render_skeleton's docstring.
    """
    if not elided:
        return ""
    what = ", ".join(f"{n:,} {k}" for k, n in sorted(elided.items(), key=lambda kv: -kv[1]))
    raisable = [f for f, key in (("--max-calls", "calls"), ("--max-imports", "imports")) if key in elided]
    # Only signature lines were cut: those are recoverable from the [Lstart-Lend] range, not a flag.
    how = f"raise with {' / '.join(raisable)}" if raisable else "read the line ranges above"
    return f"-- elided: {what} ({how})"


def iter_source_files(root_dir, includes=None, excludes=None, extra_dirs=None, keep_dirs=None):
    """Yield repo-relative paths of supported files, honoring exclude dirs and glob filters.
    `keep_dirs` removes names from the default exclude set (a Terraform repo whose `build/` holds
    Cloud Build configs, a Go repo with a `vendor/` it wants indexed)."""
    excl_dirs = set(DEFAULT_EXCLUDE_DIRS) - set(keep_dirs or [])
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
            for rel in iter_source_files(ap, args.include, args.exclude, keep_dirs=args.keep_dir):
                paths.append(os.path.join(ap, rel))
        else:
            paths.append(ap)
    out, raw_tok, skel_tok, payload = [], 0, 0, []
    elided, guarded = Counter(), 0
    for ap in paths:
        rel = os.path.relpath(ap, root).replace(os.sep, "/")
        if rel.startswith("../"):
            rel = ap.replace(os.sep, "/")   # outside --root: an absolute path beats ../../../tmp/...
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
        raw_text = src.decode("utf-8", "replace")
        raw_t = approx_tokens(raw_text)
        file_elided = Counter()
        txt = render_skeleton(fnode, nodes, refs, show_calls=not args.no_calls,
                              show_lines=not args.no_lines, max_calls=args.max_calls,
                              max_imports=args.max_imports, elided=file_elided)
        shown_t = approx_tokens(txt)
        if shown_t >= raw_t:
            # never_worse: summarizing this file costs at least as much as reading it, so read it.
            # Keeps the header so multi-file output stays uniform, and drops the elision notice
            # for this file -- nothing is hidden when the source itself is on screen.
            txt = (f"{txt.split(chr(10), 1)[0]}  -- source is no larger than its skeleton, shown in full\n"
                   + raw_text.rstrip("\n"))
            shown_t = approx_tokens(txt)
            guarded += 1
        else:
            elided.update(file_elided)
        raw_tok += raw_t
        skel_tok += shown_t
        out.append(txt)
        payload.append({"file": fnode, "nodes": nodes, "refs": refs})
    if args.json:
        print(json.dumps(payload, indent=None))
        return
    print("\n\n".join(out))
    notice = elision_notice(elided)
    if notice:
        print("\n" + notice)
    if raw_tok and not args.no_stats:
        pct = 100 - (skel_tok * 100 // raw_tok)
        lead = "" if notice else chr(10)
        if pct <= 0:
            # Every file was small enough to trip the never_worse guard (or close to it). Say so
            # plainly instead of printing a negative percentage.
            print(f"{lead}-- {len(paths)} file(s): too small to summarize — read them directly "
                  f"(≈{skel_tok:,} vs ≈{raw_tok:,} tokens; ~4 chars/token estimate)")
        else:
            note = f"; {guarded} shown in full" if guarded else ""
            print(f"{lead}-- {len(paths)} file(s): ≈{skel_tok:,} tokens here vs "
                  f"≈{raw_tok:,} to read them in full ({pct}% less{note}; ~4 chars/token estimate)")


# ----------------------------------------------------------------------------------------------
# Graph build + linking
# ----------------------------------------------------------------------------------------------
def file_hash(data):
    return hashlib.sha1(data).hexdigest()


def legacy_graph_note(graph_path):
    """A one-line note when a pre-SQLite `graph.json` is still sitting beside the new artifact.

    Storage moved to SQLite in GRAPH_VERSION 8. The old file is never read again, and on a large
    repo it is not small -- TensorFlow's was 1.3 GB -- so silently leaving it costs real disk and
    makes "I already have a graph" look like a bug. Reported, never deleted: the engine does not
    remove files on a developer's behalf."""
    legacy = os.path.join(os.path.dirname(graph_path), "graph.json")
    if not os.path.exists(legacy):
        return None
    total = os.path.getsize(legacy)
    for extra in (legacy + ".cache", legacy + ".stamp"):
        if os.path.exists(extra):
            total += os.path.getsize(extra)
    size = f"{total / 1e9:.1f} GB" if total >= 1e9 else f"{total / 1e6:.0f} MB"
    return (f"note: {legacy} is from an older version ({size} including its sidecars). Storage is "
            f"SQLite now; that file is no longer read and can be deleted.")


def load_graph(path, with_cache=False):
    """Read a stored graph back as a plain dict. Only `build` uses this -- it needs the previous
    run's parse cache to decide what to re-parse. Queries go through Store, which never
    materialises the whole graph."""
    if not os.path.exists(path):
        return None
    try:
        st = Store(path)
    except sqlite3.Error:
        return None     # a graph from an older format: treat it as absent and rebuild
    try:
        g = {k: st.meta(k) for k in ("version", "engine", "root", "built_at", "stats")}
        if with_cache:
            g["files"] = st.parsed()
        return g
    finally:
        st.close()


def terraform_module_map(root_dir):
    """Map module keys -> directories from every .terraform/modules/modules.json found."""
    out = {}
    for dirpath, dirnames, _filenames in os.walk(root_dir):
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
    if lang in ("cpp", "c"):
        # `#include <vector>` is flagged system at extraction time and is always external.
        if ref.get("extra_kind") == "system":
            return None
        cands = [
            name,                                              # bazel-style, from the repo root
            os.path.normpath(os.path.join(d, name)),           # quoted, relative to the includer
        ]
        # Common include roots. A generated-header path (`third_party/x/y.h`) that is not in the
        # tree simply fails to resolve, which is the honest outcome.
        cands += [f"{r}/{name}" for r in ("include", "src", "third_party")]
        for c in cands:
            c = c.replace(os.sep, "/").lstrip("./")
            if c in files:
                return c
        # A header named only by basename: accept it when exactly one file in the tree matches,
        # never when several do (guessing between same-named headers invents edges).
        base = os.path.basename(name)
        if base == name:
            hits = [f for f in files if f.endswith("/" + base) or f == base]
            if len(hits) == 1:
                return hits[0]
        return None
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
            if ref.get("names") == ["*"] and not ref.get("static"):
                fs = pkgs.get(name)
                return ("dir:" + os.path.dirname(fs[0])) if fs else None
            pkg, _, cls = name.rpartition(".")   # `import static a.b.C.*` names the class C, not a package
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
        if ref.get("names") == ["*"] and not ref.get("static"):
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


def git_stamp(root_dir, includes, excludes, out_path=None, keep_dirs=None):
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
    h.update(f"{GRAPH_VERSION}|{engine}|{head}|{sorted(includes or [])}|{sorted(excludes or [])}|{sorted(keep_dirs or [])}|{root_dir}\n".encode())
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


PARALLEL_MIN_FILES = 200   # below this a worker pool costs more to start than it saves


def _parse_one(job):
    """Pool worker: read, hash and parse one file. Reads the file itself so only a path crosses the
    process boundary on the way in; returns None for a file that vanished or is not a source file."""
    root_dir, rel = job
    try:
        with open(os.path.join(root_dir, rel), "rb") as f:
            src = f.read()
    except OSError:
        return rel, None
    res = extract_file(rel, root_dir, src)
    if res is None:
        return rel, None
    fnode, nodes, refs = res
    return rel, {"hash": file_hash(src), "file_node": fnode, "nodes": nodes, "refs": refs}


def _physical_cores(cpus):
    """Distinct physical cores among the logical CPUs `cpus` (Linux sysfs), or None if unknown."""
    cores = set()
    for c in cpus:
        base = f"/sys/devices/system/cpu/cpu{c}/topology/"
        try:
            with open(base + "physical_package_id") as f:
                pkg = f.read().strip()
            with open(base + "core_id") as f:
                cores.add((pkg, f.read().strip()))
        except OSError:
            return None
    return len(cores) or None


def default_jobs():
    """Worker count: ASTGRAPH_JOBS if set, else the physical cores this process may run on.

    Physical, not logical: on a 4-core/8-thread laptop 8 workers parse and link no faster than 4
    (measured on TensorFlow: 71.5s vs 73.0s) and the 8 forked linkers need ~1 GB more memory."""
    env = os.environ.get("ASTGRAPH_JOBS", "").strip()
    if env.isdigit() and int(env) > 0:
        return int(env)
    try:
        cpus = os.sched_getaffinity(0)
    except AttributeError:          # macOS / Windows have no sched_getaffinity
        return max(1, os.cpu_count() or 1)
    return max(1, _physical_cores(cpus) or len(cpus))


def _mem_available_kb():
    """MemAvailable from /proc/meminfo (Linux), or None."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        pass
    return None


def _rss_kb():
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * (os.sysconf("SC_PAGE_SIZE") // 1024)
    except (OSError, ValueError, IndexError):
        return None


def parse_files(root_dir, rels, jobs=None):
    """Yield (rel, parsed-or-None) for every path in `rels`, in the order given.

    Parsing is per-file and independent, so it is spread over a process pool (tree-sitter work
    holds the GIL; threads would not help). Results come back in input order (imap), so the graph
    is identical to a serial build. Any failure to start the pool -- a sandbox without a writable
    /dev/shm for the pool's semaphores is the usual one -- falls back to parsing serially here.
    Linux uses fork (workers inherit the loaded parsers); elsewhere the platform default (spawn)
    is used. NOT VERIFIED on macOS or Windows."""
    jobs = default_jobs() if jobs is None else max(1, jobs)
    if jobs > 1 and len(rels) >= PARALLEL_MIN_FILES:
        import multiprocessing as mp
        pool = None
        try:
            ctx = mp.get_context("fork") if sys.platform.startswith("linux") else mp.get_context()
            pool = ctx.Pool(min(jobs, len(rels)))
        except (OSError, ValueError, ImportError) as exc:
            print(f"note: parallel parse unavailable ({exc}); parsing serially", file=sys.stderr)
        if pool is not None:
            try:
                yield from pool.imap(_parse_one, ((root_dir, rel) for rel in rels), chunksize=16)
            finally:
                pool.terminate()
                pool.join()
            return
    for rel in rels:
        yield _parse_one((root_dir, rel))


PARALLEL_LINK_MIN_FILES = 1500   # below this, forking the linker's state costs more than it saves
_LINK_JOB = None                   # the closure a forked link worker runs (inherited through fork)


def _link_worker(chunk):
    return [(rel, _LINK_JOB(rel)) for rel in chunk]


def resolve_calls_parallel(rels, job, jobs=None):
    """{rel: links record} for `rels`, resolved by `job` in forked worker processes; {} when it is
    not worth it or not possible (the caller then resolves serially, with the same result).

    Call resolution only reads the linker's indexes, so workers forked after they are built can
    each take a contiguous block of files (neighbours share lookups, so their memo caches warm up)
    and send back the replayable records; the parent replays them in file order, so the graph is
    the one a serial build writes. Linux only: the workers inherit closures through fork, which
    spawn cannot carry. gc.freeze() keeps the collector from touching (and so copying) every
    inherited page. Any failure to start the pool falls back to serial."""
    global _LINK_JOB
    jobs = default_jobs() if jobs is None else max(1, jobs)
    if jobs < 2 or len(rels) < PARALLEL_LINK_MIN_FILES or not sys.platform.startswith("linux"):
        return {}
    # Each forked worker ends up privately holding ~7% of the parent's memory (pages its lookups
    # touch get copied); measured on TensorFlow: 3.7 GB parent, +1.0 GB for 4 workers. Fit the
    # worker count to what is free, keeping 1 GB spare, rather than push the machine into swap.
    avail, rss = _mem_available_kb(), _rss_kb()
    if avail is not None and rss:
        fit = int((avail - 1024 * 1024) // max(1, rss * 0.1))
        if fit < jobs:
            if fit < 2:
                print("note: not enough free memory for a parallel link; resolving serially", file=sys.stderr)
                return {}
            jobs = fit
    import gc
    import multiprocessing as mp
    size = max(16, len(rels) // (jobs * 16))
    chunks = [rels[i:i + size] for i in range(0, len(rels), size)]
    _LINK_JOB = job
    gc.collect()
    gc.freeze()
    try:
        pool = mp.get_context("fork").Pool(jobs)
    except (OSError, ValueError) as exc:
        print(f"note: parallel link unavailable ({exc}); resolving serially", file=sys.stderr)
        gc.unfreeze()
        _LINK_JOB = None
        return {}
    try:
        out = {}
        for part in pool.imap(_link_worker, chunks):
            out.update(part)
        return out
    finally:
        pool.terminate()
        pool.join()
        gc.unfreeze()
        _LINK_JOB = None


def build_graph(root_dir, out_path, includes, excludes, full=False, quiet=False, keep_dirs=None, jobs=None):
    t0 = time.time()
    root_dir = os.path.abspath(root_dir)
    # Fast path: if the git working tree is byte-identical to the one the graph was built from, the
    # graph is current and even the (always full) link pass can be skipped.
    stamp = git_stamp(root_dir, includes, excludes, out_path, keep_dirs)
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
            old = load_graph(out_path, with_cache=True)
            # cached parses are only reusable if the engine that produced them is byte-identical:
            # extractors change what a parse records (argument types, re-export names, ...)
            if old.get("version") != GRAPH_VERSION or old.get("engine") != engine_hash:
                old = None
        except (OSError, ValueError):
            old = None
    tf_modules = terraform_module_map(root_dir)
    extra_dirs = sorted({v["dir"] for v in tf_modules.values() if os.path.isdir(os.path.join(root_dir, v["dir"]))})
    # `files` keeps walk order (link order, and so the graph, depends on it): unchanged files are
    # filled from the cache now, changed ones hold a placeholder until the parse pass fills them.
    files = {}
    to_parse = []
    reused = 0
    for rel in iter_source_files(root_dir, includes, excludes, extra_dirs=extra_dirs, keep_dirs=keep_dirs):
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
        files[rel] = None
        to_parse.append(rel)
    changed = 0
    for rel, parsed in parse_files(root_dir, to_parse, jobs):
        if parsed is None:
            del files[rel]
            continue
        files[rel] = parsed
        changed += 1
    prev = None
    if old is not None:
        old_files = old.get("files", {})
        delta = set(to_parse) | (set(old_files) - set(files))   # edited, added, removed
        iface, names, old_b = set(), set(), {}
        for rel in delta:
            o, n = old_files.get(rel), files.get(rel)
            if o is not None and o.get("links"):
                old_b[rel] = o["links"].get("b")
            if o is None or n is None:
                iface.add(rel)
                names |= parse_names(o if n is None else n)
                continue
            d = interface_delta(o, n)
            if d is not None:
                iface.add(rel)
                names |= d
        prev = {"changed": delta, "iface": iface, "names": names, "old_b": old_b}
    graph = link_graph(root_dir, files, tf_modules, prev, jobs)
    graph["version"] = GRAPH_VERSION
    graph["engine"] = engine_hash
    graph["root"] = root_dir
    graph["built_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    graph["parsed_now"] = changed
    write_store(out_path, graph, files)
    if stamp:
        # `options` lets a query refresh a stale graph with exactly the settings it was built with
        # (refresh_if_stale); without them it could only guess and might re-index a different tree.
        with open(stamp_path, "w") as f:
            json.dump({"version": GRAPH_VERSION, "stamp": stamp, "stats": graph["stats"], "built_at": graph["built_at"],
                       "options": {"root": root_dir, "include": includes, "exclude": excludes, "keep_dir": keep_dirs}}, f)
    elif os.path.exists(stamp_path):
        os.remove(stamp_path)
    note = legacy_graph_note(out_path)
    if note and not quiet:
        print(note, file=sys.stderr)
    if not quiet:
        s = graph["stats"]
        print(f"graph written to {out_path}: {s['files']} files ({changed} parsed, {reused} unchanged, "
              f"{graph['relinked']} re-linked), "
              f"{s['nodes']} nodes, {s['edges']} edges, {s['unresolved_refs']} unresolved refs, {time.time() - t0:.1f}s")
        for lang, c in sorted(s["languages"].items()):
            print(f"  {lang}: {c} files")
        if s["files"] == 0:
            print("  no supported source files found under this root. Supported extensions: "
                  + ", ".join(sorted(EXT_LANG)) + "; directories skipped by default: "
                  + ", ".join(sorted(DEFAULT_EXCLUDE_DIRS)) + ". Check --root, --include and --exclude.", file=sys.stderr)
    return graph


def parse_names(info):
    """Every index key a lookup could use to reach this parsed file: its definitions' names and
    qualified names, and the names its imports bind or re-export. Incremental linking re-resolves a
    file whose recorded lookups hit any of these for a file that changed."""
    out = set()
    for n in info["nodes"]:
        out.add(n["name"])
        out.add(n["qname"])
    for r in info["refs"]:
        if r["kind"] == "import":
            out.add(r["name"])
            out.update(r.get("names") or ())
            am = r.get("alias_map") or {}
            out.update(am.keys())
            out.update(am.values())
    return out


# Fields the linker writes into parsed nodes (and so into the parse cache): they are recomputed on
# every link, so they are not part of what a file "defines" when two parses are compared.
_LINK_WRITTEN = ("defined_at", "resolved_dir")


def _iface_key(n):
    extra = {k: v for k, v in (n.get("extra") or {}).items() if k not in _LINK_WRITTEN}
    if not extra.get("forward"):
        extra.pop("is_declaration", None)    # C++ decl/def pairing mark; forward declarations keep theirs
    parent = None if extra.get("receiver_type") else n.get("parent")   # Go methods are re-parented at link
    return json.dumps([n["id"], n["kind"], n["name"], n["qname"], n["line"], n.get("end_line"),
                       n.get("signature"), n.get("annotations"), parent, extra], sort_keys=True, default=str)


def _import_keys(info):
    return sorted(json.dumps([r["name"], sorted(r.get("names") or ()), sorted((r.get("alias_map") or {}).items())],
                             default=str) for r in info["refs"] if r["kind"] == "import")


def interface_delta(old, new):
    """What another file can observe changing between two parses of one file: None when nothing
    (a body-only edit that moved no definition), else the names whose definitions or imports
    differ. Another file sees this one only through its definition nodes, its file-level metadata
    (package) and its import bindings; bindings and inheritance are fingerprinted separately."""
    ok, nk = Counter(map(_iface_key, old["nodes"])), Counter(map(_iface_key, new["nodes"]))
    oi, ni = _import_keys(old), _import_keys(new)
    if ok == nk and oi == ni and old["file_node"].get("extra") == new["file_node"].get("extra"):
        return None
    if old["file_node"].get("extra") != new["file_node"].get("extra"):
        return parse_names(old) | parse_names(new)   # package moved: everything in it is somewhere else now
    names = set()
    by_key = {_iface_key(n): n for n in old["nodes"]}
    by_key.update({_iface_key(n): n for n in new["nodes"]})
    by_id = {n["id"]: n for n in old["nodes"]}
    by_id.update({n["id"]: n for n in new["nodes"]})
    for k in (ok - nk) + (nk - ok):
        n = by_key[k]
        # the node and every container above it: a type's members (fields, methods, companion
        # objects, impl blocks) are part of what a lookup of the type's name sees
        while n is not None:
            names.add(n["name"])
            names.add(n["qname"])
            n = by_id.get(n.get("parent"))
    if oi != ni:
        for info in (old, new):
            for r in info["refs"]:
                if r["kind"] == "import":
                    names.add(r["name"])
                    names.update(r.get("names") or ())
                    am = r.get("alias_map") or {}
                    names.update(am.keys())
                    names.update(am.values())
    return names


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


def link_graph(root_dir, files, tf_modules, prev=None, jobs=None):
    """Turn per-file nodes + raw refs into a global node list with resolved, confidence-labelled edges.

    `prev` ({"changed": paths added/edited/removed since the last build, "iface": the subset whose
    definitions, imports or metadata differ, "names": the names that differ, "old_b": their previous
    binding fingerprints}) enables incremental linking: a file whose recorded call-resolution
    dependencies (info["links"], written by the previous build) do not touch anything that changed
    gets its call edges replayed instead of re-resolved. The graph is the same either way."""
    nodes, edges = [], []
    node_by_id = {}
    by_name = defaultdict(list)        # simple name -> nodes (definitions only)
    var_by_name = defaultdict(list)    # file-level variables with a declared type (typed receiver roots), kept apart
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
            elif n["kind"] == "variable" and n["parent"] == rel and n.get("extra", {}).get("type"):
                var_by_name[n["name"]].append(n)
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

    # C/C++ header-implementation pairing. `void MatMulOp::Compute(...)` in a .cc and the
    # `void Compute(...)` it defines in a .h extract as two nodes that already share a qname
    # (cpp_function builds the out-of-line qname from the `Class::` qualifier). Pair them with a
    # `defines` edge and mark the declaration, so a caller can be pointed at the body rather than
    # at the prototype. Without this every C++ member appears twice with no relation between them.
    cpp_decl_to_def = {}
    cpp_defs = defaultdict(list)
    for n in nodes:
        if n["kind"] not in ("method", "constructor", "destructor", "function"):
            continue
        if os.path.splitext(n["file"])[1].lower() not in CPP_EXTS:
            continue
        # The marks below are written into the node, and so into the parse cache of an unchanged
        # header: clear the previous build's pairing first, or a definition that was renamed or
        # deleted leaves its declaration pointing at it (found by the incremental-vs-full trials).
        n["extra"].pop("is_declaration", None)
        n["extra"].pop("defined_at", None)
        cpp_defs[n["qname"]].append(n)
    for group in cpp_defs.values():
        if len(group) < 2:
            continue
        defs = [n for n in group if n["extra"].get("has_body")]
        decls = [n for n in group if not n["extra"].get("has_body")]
        if not defs or not decls:
            continue    # two declarations, or two definitions (overloads): not a header/impl pair
        # Prefer a single definition; with several (overloads sharing a name) link each declaration
        # to the one in the matching translation unit when there is one, else to the first.
        for d in decls:
            d["extra"]["is_declaration"] = True
            target = next((x for x in defs if os.path.splitext(x["file"])[0] == os.path.splitext(d["file"])[0]), defs[0])
            d["extra"]["defined_at"] = f"{target['file']}:{target['line']}"
            cpp_decl_to_def[d["id"]] = target["id"]
            edges.append({"src": target["id"], "dst": d["id"], "type": "defines",
                          "line": target["line"], "confidence": "exact"})

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
    alias_orig = defaultdict(dict)  # file -> {local alias: original name} for `from x import A as B`

    def orig(rel, name):
        return alias_orig.get(rel, {}).get(name, name)
    ns_ext = defaultdict(set)       # file -> local names bound to external packages/modules
    import_links = defaultdict(list)  # file -> [(names, alias_map, target files)] for re-export following
    java_static = defaultdict(list)   # java file -> [(member or '*', class name, class file)] from `import static`
    ext_nodes = {}
    unresolved = 0
    # pybind11 exports by the name Python calls them with. Ambiguous names are dropped outright:
    # if two extension modules export the same symbol there is no way to tell which one a Python
    # call means, and guessing would fabricate an edge across a language boundary.
    py_binding_index = {}
    for n in nodes:
        if n["kind"] == "py_binding":
            py_binding_index[n["name"]] = None if n["name"] in py_binding_index else n
    py_binding_index = {k: v for k, v in py_binding_index.items() if v is not None}
    op_def_index = {}
    for n in nodes:
        if n["kind"] == "op_def" and n["name"] not in op_def_index:
            op_def_index[n["name"]] = n
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
            if am.get(nm) and am[nm] != nm:
                alias_orig[r["_file"]][am[nm]] = nm
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
        # A C++ member resolved through a header lands on the prototype. Point the edge at the
        # definition instead, so `callers`, `trace-deps` and `path` all agree with `query symbol`,
        # which prefers the body. Without this the two halves of a C++ member are unconnected and
        # a traversal silently stops at the language's own declaration/definition split.
        if typ != "defines":
            dst = cpp_decl_to_def.get(dst, dst)
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
                if lang == "java" and r.get("static") and not target.startswith("dir:"):
                    parts = r["name"].split(".")
                    member = "*" if r.get("names") == ["*"] else parts[-1]
                    java_static[rel].append((member, parts[-1] if member == "*" else parts[-2], target))
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
                    # `registry/name/provider//modules/x` in a repo that contains `modules/x`: the example
                    # most likely exercises this repo's own copy of the module. Recorded as a lead.
                    if "//" in src_str:
                        sub = src_str.split("//", 1)[1].split("?", 1)[0].strip("/")
                        if sub and ("terraform_module", sub) in dir_nodes:
                            add_edge(r["src"], dir_nodes[("terraform_module", sub)]["id"], "uses_module", r["line"], "ambiguous")

    def binding_fp(rel):
        """Hash of everything pass 1 bound for `rel` (its imports, namespaces, re-export links). A
        file whose bindings moved -- a new module now shadows an external one, tsconfig paths
        changed -- cannot reuse its call edges, and neither can anything that followed its re-exports."""
        return hashlib.sha1(json.dumps([
            sorted(imports_of.get(rel, ())),
            sorted((k, sorted(v)) for k, v in ns_repo.get(rel, {}).items()),
            sorted(ns_ext.get(rel, ())),
            [[sorted(nm), sorted(am.items()), sorted(t)] for nm, am, t in import_links.get(rel, ())],
            sorted(alias_orig.get(rel, {}).items()),
            sorted(java_static.get(rel, ())),
        ]).encode()).hexdigest()

    # Names defined at the top level of each file (for re-export following)
    top_defs = defaultdict(dict)   # file -> {name: [nodes]}
    for n in nodes:
        if n.get("file") and n.get("parent") == n["file"] and n["kind"] not in ("file",):
            top_defs[n["file"]].setdefault(n["name"], []).append(n)

    # Dependency tracking for incremental linking. While a file's calls are resolved (pass 2b,
    # ~80% of link time), every name looked up in the definition indexes (including a type's name
    # when its members, impls or generic bounds are read), every file whose import bindings are
    # followed for a re-export, and every inheritance list consulted is recorded. The next build
    # replays the file's call edges unless one of those meets what changed: a name whose definition
    # (or a member of it) differs (interface_delta), a file whose bindings moved, or a parent list
    # that resolves differently. Memoized helpers keep the footprint of the computation that filled
    # them and replay it on a hit; otherwise a cache warmed by one file would hide the dependency
    # from every later file.
    _dep = [None]    # {"n": names looked up, "b": files whose import bindings were followed, "p": {node id: [parent ids]}}

    def _dn(name):
        d = _dep[0]
        if d is not None:
            d["n"].add(name)

    def _db(f):
        d = _dep[0]
        if d is not None:
            d["b"].add(f)

    def _new_dep():
        return {"n": set(), "b": set(), "p": {}}

    def _merge(fp):
        d = _dep[0]
        if d is not None and fp is not None:
            d["n"].update(fp["n"])
            d["b"].update(fp["b"])
            d["p"].update(fp["p"])

    def _footprinted(compute):
        """(value, footprint) of compute(); the footprint is also merged into the caller's record."""
        outer = _dep[0]
        _dep[0] = _new_dep()
        try:
            val = compute()
        finally:
            fp = _dep[0]
            _dep[0] = outer
        _merge(fp)
        return val, fp

    def _memo(cache, key, compute):
        hit = cache.get(key)
        if hit is None:
            hit = cache[key] = _footprinted(compute)
        else:
            _merge(hit[1])
        return hit[0]

    def _parents(nid):
        """parents_of[nid], recorded: an inheritance change elsewhere can re-route a call whose own
        names never mention the changed file."""
        ps = parents_of.get(nid, [])
        d = _dep[0]
        if d is not None:
            d["p"][nid] = [x["id"] for x in ps]
        return ps

    _fd_cache = {}

    _cinc_cache = {}

    def c_include_closure(rel, depth=4):
        """Files `rel` sees through `#include`, transitively (headers include headers), as a set. C has
        one global namespace: a free function is visible where a header it is declared in is included.
        Each file whose includes were followed is recorded, so editing an include re-links `rel`."""
        def compute():
            seen, frontier = {rel}, [rel]
            for _ in range(depth):
                nxt = []
                for f in frontier:
                    _db(f)
                    for g in sorted(imports_of.get(f, ())):
                        if g not in seen:
                            seen.add(g)
                            nxt.append(g)
                frontier = nxt
            seen.discard(rel)
            return seen
        return _memo(_cinc_cache, rel, compute)

    def files_defining(name, fset, depth=3, seen=None):
        """Files among `fset` that define `name` at top level, following `from x import name` re-exports
        (and wildcard imports) up to `depth` levels. Memoized on (name, files) for the top-level call."""
        if seen is None:
            return _memo(_fd_cache, (name, frozenset(fset)), lambda: files_defining(name, fset, depth, {}))
        out = set()
        for f in sorted(fset):   # sorted: the result must not depend on set iteration order
            if seen.get((f, name), -1) >= depth:
                continue         # already explored from here with at least this much depth left
            seen[(f, name)] = depth
            _dn(name)
            _db(f)
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
    field_types_raw = defaultdict(dict)   # class node id -> {field name: declared type text with generics}
    field_qual = defaultdict(dict)   # class node id -> {field name: package qualifier of its type or None}
    for n in nodes:
        if n["kind"] == "field" and n["parent"] and n["extra"].get("type"):
            field_types_raw[n["parent"]][n["name"]] = n["extra"]["type"]
            if n["extra"]["type"].startswith("<call"):
                field_types[n["parent"]][n["name"]] = n["extra"]["type"]   # resolved from the callee's return type on use
                field_qual[n["parent"]][n["name"]] = None
                continue
            q, base = split_qualifier(unwrap_optional(n["extra"]["type"]))   # `Optional["Repo"]` / `Repo | None` -> Repo
            field_types[n["parent"]][n["name"]] = base
            field_qual[n["parent"]][n["name"]] = q
    # A Python `@property` reads like a field: `self.query.chain()` goes through `def query(self)`. Its type
    # is the return annotation, else the type of the `self._x` it returns (py_returned_self_field).
    for n in nodes:
        if n["kind"] != "method" or not n["parent"] or n["name"] in field_types.get(n["parent"], {}):
            continue
        if not any(a.split("(")[0].rsplit(".", 1)[-1] in PY_PROPERTY_DECORATORS for a in (n.get("annotations") or [])):
            continue
        sig = n["signature"].split("\n")[0]
        t = sig.rsplit("->", 1)[1].strip() if "->" in sig else field_types_raw.get(n["parent"], {}).get(n["extra"].get("returns_field"))
        if not t:
            continue
        field_types_raw[n["parent"]][n["name"]] = t
        if t.startswith("<call"):
            field_types[n["parent"]][n["name"]], field_qual[n["parent"]][n["name"]] = t, None
        else:
            q, base = split_qualifier(unwrap_optional(t))
            field_types[n["parent"]][n["name"]], field_qual[n["parent"]][n["name"]] = base, q

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
    for _name, group in by_name.items():
        stubs = [c for c in group if c["kind"] in ("function", "method") and any(a.split(".")[-1].startswith("overload") for a in c.get("annotations", []))]
        for st in stubs:
            if any(c is not st and c["kind"] == st["kind"] and c["file"] == st["file"] and c.get("parent") == st.get("parent")
                   and not any(a.split(".")[-1].startswith("overload") for a in c.get("annotations", [])) for c in group):
                overload_stub.add(st["id"])

    _cands_cache = {}

    def candidates(name, kinds):
        """Definitions named `name` of the given kinds. Memoized: by_name and overload_stub are final
        once resolution starts, and callers only read the list (it is shared between calls). TensorFlow
        asks for the same few thousand C++ method names millions of times."""
        _dn(name)
        key = (name, frozenset(kinds))
        hit = _cands_cache.get(key)
        if hit is None:
            hit = _cands_cache[key] = [c for c in by_name.get(name, []) if c["kind"] in kinds and c["id"] not in overload_stub]
        return hit

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
                elif ch in ")]}>" and not (ch == ">" and j > 0 and sig[j - 1] == "-"):
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            inner = sig[idx + 1:j]
            params, cur, depth = [], "", 0
            for ci, ch in enumerate(inner):
                if ch in "([{<":
                    depth += 1
                elif ch in ")]}>" and not (ch == ">" and ci > 0 and inner[ci - 1] == "-"):
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
        return _memo(_sub_cache, (sub_name, sup_name), lambda: _is_subtype(sub_name, sup_name))

    def _is_subtype(sub_name, sup_name):
        res = False
        for start in candidates(sub_name, TYPE_LIKE_KINDS):
            seen, frontier = set(), [start]
            while frontier and not res:
                x = frontier.pop()
                if x["id"] in seen:
                    continue
                seen.add(x["id"])
                for pn in _parents(x["id"]):
                    if pn["name"] == sup_name:
                        res = True
                        break
                    frontier.append(pn)
            if res:
                break
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
                elif ch in ")]}>" and not (ch == ">" and j > 0 and sig[j - 1] == "-"):
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            inner = sig[idx + 1:j]
            params, cur, depth = [], "", 0
            for ci, ch in enumerate(inner):
                if ch in "([{<":
                    depth += 1
                elif ch in ")]}>" and not (ch == ">" and ci > 0 and inner[ci - 1] == "-"):
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
            fake["chain"] = [seg.split("(")[0].strip("*&!? ") for seg in split_chain(hint)][1:]
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
        def prefer_top(lst):   # `Request` means the top-level class, not `Dns.Request` nested elsewhere
            top = [c for c in lst if c.get("parent") == c.get("file")]
            return top if top and len(top) < len(lst) else lst
        same = [c for c in cands if c["file"] == rel]
        if same:
            return by_arity(prefer_top(same))[:1], "same_file"
        lang = lang_of(rel)
        if lang in ("java", "kotlin", "go"):  # same package, no import needed
            pkg = [c for c in cands if same_package(c["file"], rel)]
            if pkg:
                return by_arity(prefer_top(pkg))[:1], "package"
        imp = [c for c in cands if c["file"] in imports_of.get(rel, ())]
        if imp:
            return by_arity(prefer_top(imp))[:1], "import"
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
            return _memo(_defs_cache, (name, frozenset(fset)), lambda: defs_in(name, fset, depth, {}))
        out = []
        for f in sorted(fset):
            if seen.get((f, name), -1) >= depth:
                continue
            seen[(f, name)] = depth
            _dn(name)
            _db(f)
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

    def java_static_pick(name, rel):
        """`shouldContain(a, b)` after `import static a.b.Messages.shouldContain;` (or `Messages.*`): a static
        method of the imported class, overload chosen by the arguments. A single-member import shadows an
        on-demand (`*`) one, as in Java."""
        cands = candidates(name, {"method"})
        for want in (name, "*"):
            hits = []
            for member, cls, f in java_static.get(rel, ()):
                if member == want:
                    _db(f)
                    hits += [c for c in cands if c["file"] == f and (node_by_id.get(c.get("parent")) or {}).get("name") == cls]
            if hits:
                return by_arity(hits)[:1], "import"
        return [], None

    def admits_argc(c):
        """Could this candidate accept the call's argument count?

        Only consulted when the receiver's type is unknown, where the match rests on the name
        alone. A candidate that cannot even take the arguments is not a weak guess, it is a wrong
        one: in TensorFlow, `body_fn.getArgument(promise_index)` on an MLIR FuncOp was resolved to
        a same-file `AsyncWhilePass::getArgument()` that takes none.
        """
        argc = _cur.get("argc")
        if argc is None or c["kind"] not in ("function", "method", "constructor"):
            return True
        ar = arity(c)
        return ar is None or (ar[0] <= argc and (ar[1] is None or argc <= ar[1]))

    def lead_pick(cands, rel):
        """Receiver of unknown type: a same-file definition is plausible; otherwise a few same-language leads."""
        fam = LANG_FAMILY.get(lang_of(rel))
        src = _cur.get("src")
        main_code = not is_test_file(rel)

        def admissible(c):
            return ((not fam or LANG_FAMILY.get(lang_of(c["file"])) == fam)
                    and c["id"] != src                                    # `self._pool.handle_request()` is not a self-call
                    and admits_argc(c)
                    and not (main_code and is_test_file(c["file"])))      # main code never leads into a test helper

        # Same-file first, then the rest with an early exit: more than 3 survivors is no lead at all,
        # so there is no need to filter all of them. Common C++ method names (`Compute`, `Run`) have
        # thousands of candidates in TensorFlow, and filtering every one of them per call dominated
        # the link. Same result as filtering the whole list and then splitting it.
        for c in cands:
            if c["file"] == rel and admissible(c):
                return [c], "same_file"
        leads = []
        for c in cands:
            if admissible(c):
                leads.append(c)
                if len(leads) > 3:
                    return [], None   # 3 of 40 `copy` methods is noise, not a lead
        return (leads, "ambiguous") if leads else ([], None)

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

    # Kotlin companion objects by their enclosing class id. Parents are final by now (the Go
    # re-parenting above runs before any resolution), so this is computed once instead of scanning
    # every node of the owner's file on each lookup -- that scan was ~half the link time on TensorFlow.
    companions_of = defaultdict(list)
    impls_of = defaultdict(list)    # (name, file) -> Rust `impl` blocks, so owner_ids never scans by_name
    for kids in by_file.values():
        for kid in kids:
            if kid["kind"] == "class" and kid["extra"].get("companion"):
                companions_of[kid.get("parent")].append(kid["id"])
            elif kid["kind"] == "impl":
                impls_of[(kid["name"], kid["file"])].append(kid)
    _owner_cache = {}

    def owner_ids(tn):
        """Node ids whose children are members of type `tn`: the node itself plus, for Rust, the `impl`
        blocks of that name in its file. Ordered (the type first, impls by line) so nothing depends on
        set iteration order; two unrelated same-named classes in one file are never merged.
        Memoized per node id: by_name and the parent links do not change once resolution starts."""
        _dn(tn["name"])   # impls and companions: a change to either touches the type's name
        _dn(tn["qname"])
        hit = _owner_cache.get(tn["id"])
        if hit is not None:
            return hit
        ids = [tn["id"]]
        for alt in sorted((a for a in impls_of.get((tn["name"], tn["file"]), ()) if a["id"] != tn["id"]),
                          key=lambda a: a["line"]):
            ids.append(alt["id"])
        ids.extend(companions_of.get(tn["id"], ()))   # Kotlin: companion object members are reachable as Outer.member()
        _owner_cache[tn["id"]] = ids
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
            elif sig[j] in ")]}>" and not (sig[j] == ">" and j > 0 and sig[j - 1] == "-"):
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
        _dn(tn["name"])   # its signature and same-file impl blocks: a change to either touches this name
        _dn(tn["qname"])
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
                        out.setdefault(re.split(r"[\s:=]", it, maxsplit=1)[0], None)
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
                fake["chain"] = [seg.split("(")[0].strip("*&!? ") for seg in split_chain(hint)][1:]
            targets, conf, mode = resolve_call(fake, rel, lang_of(rel), cont)
            if not targets and hint:
                # synthetic or builtin members whose result type follows from the receiver:
                # Kotlin data-class `copy(...)`, enum `valueOf(...)`, and `dict[K, V].get/pop/setdefault(...)`
                rm, rn = receiver_mode(dict(fake, name=name), rel, lang_of(rel), cont)
                if rm == "typed" and rn is not None:
                    if name == "copy" or (name == "valueOf" and rn["kind"] == "enum"):
                        return "node", rn
                if name in ("get", "pop", "setdefault", "__getitem__"):
                    raw = None
                    hchain = split_chain(hint)
                    if root_type and len(hchain) == 1:
                        raw = root_type
                    elif cont is not None and len(hchain) == 2 and hchain[0] in ("self", "this"):
                        for oid in owner_ids(cont):
                            if hchain[1] in field_types_raw.get(oid, {}):
                                raw = field_types_raw[oid][hchain[1]]
                                break
                    kv = py_mapping_types(raw) if raw else None
                    if kv:
                        return resolve_type_ref(kv[1], rel, cont, depth + 1)
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
        type_text = unwrap_optional(type_text)
        q, base = split_qualifier(type_text)
        if not q and base in BUILTIN_TYPE_NAMES and not candidates(base, TYPE_LIKE_KINDS):
            return "ext", None   # `dict[str, Item].get()`, `list[T].append()`: builtin receivers never lead anywhere
        if q:
            if q in ns_ext.get(rel, ()):
                return "ext", None
            if q[:1].isupper() and "." not in q and depth < 3:
                # `Interceptor.Chain`: the qualifier is itself a type; the base is nested in it
                okind, outer = _resolve_type_ref(q, rel, cont, depth + 1)
                if okind == "node" and outer is not None:
                    nested = [c for c in candidates(base, TYPE_LIKE_KINDS) if c.get("parent") == outer["id"]]
                    if nested:
                        return "node", nested[0]
            if q in ns_repo.get(rel, {}):
                tn = type_node(base, rel, ns_repo[rel][q])
                return ("node", tn) if tn else ("none", None)
        if base in ns_repo.get(rel, {}):
            tn = type_node(orig(rel, base), rel, ns_repo[rel][base])
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
                frontier.extend(_parents(x["id"]))
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
                    nested = [c for c in candidates(seg, TYPE_LIKE_KINDS) if c.get("parent") == tn["id"]]
                    if nested:
                        tn = nested[0]   # `Order.Builder()` / `b.Audit()`: a nested or inner class constructor
                        continue
                    if seg == "copy" or (seg == "valueOf" and tn["kind"] == "enum"):
                        continue         # `Kind.valueOf("x").code()`, `order.copy(..).f()`: same type as the receiver
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
                nested = [c for c in candidates(seg, TYPE_LIKE_KINDS) if c.get("parent") == tn["id"]]
                if nested:
                    tn = nested[0]   # `Outer.Inner.f()`
                    continue
                props, _ = typed_pick([c for c in candidates(seg, {"method"}) if any(a.endswith(("property", "cached_property", "getter")) for a in c.get("annotations", []))], tn)
                if props:
                    rt = return_type(props[0])   # `item.heavy.area()`: a Python property segment
                    kind, nxt = resolve_type_ref(rt, props[0]["file"], container_of(props[0]["id"])) if rt else ("none", None)
                    if kind == "ext":
                        return ""
                    if nxt is None:
                        return None
                    tn = nxt
                    continue
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
    ctor_links = []   # (ref, file, class node, confidence, name) of `new X(args)`, linked to a constructor before pass 2b
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
                    if lang in ("java", "kotlin") and qual in res_ctx["java_pkgs"]:   # Kotlin: `: io.x.api.MultiRule()`
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
                    if not targets and hint[:1].isupper():
                        # `Interceptor.Chain` with `import okhttp3.Interceptor`: nested in the imported type
                        outer = type_node(orig(rel, hint), rel, ns_repo[rel][hint])
                        nested = [c for c in cands if outer is not None and c.get("parent") == outer["id"]]
                        if nested:
                            targets, conf = nested[:1], "import"
                elif hint is None and name in ns_repo.get(rel, {}):
                    on = orig(rel, name)   # `from x import Blueprint as SansioBlueprint`: look up the original name
                    ocands = [c for c in candidates(on, TYPE_LIKE_KINDS) if not is_within(c, r["src"])] if on != name else cands
                    targets, conf = bound_pick(ocands, on, ns_repo[rel][name], TYPE_LIKE_KINDS)   # from-imported type name
                    if not targets:
                        targets, conf = pick(cands, rel)
                elif hint and hint[:1].isupper():
                    # `Outer.Inner`: the qualifier is a type (imported, same package or in this file); prefer the
                    # member nested in it over any same-named type elsewhere
                    okind, outer = resolve_type_ref(hint, rel, None)
                    nested = [c for c in cands if okind == "node" and outer is not None and c.get("parent") == outer["id"]]
                    if nested:
                        nf = nested[0]["file"]
                        targets, conf = nested[:1], ("same_file" if nf == rel else ("package" if same_package(nf, rel) else "import"))
                    else:
                        targets, conf = pick(cands, rel)
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
                    if k == "instantiates" and r.get("argc") is not None and t["kind"] in ("class", "enum"):
                        # `new X(args)` also calls one constructor; picked after the inheritance index and call
                        # resolution exist (ctor_links below), since overloads are told apart by argument types
                        ctor_links.append((r, rel, t, conf, name))

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
        for pn in _parents(tn["id"]):
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
        raw = split_chain(hint)
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

        if first and re.match(r"^(?:[bBrRfFuU]{0,2}[\"'`]|[\[{]|\d)", first):
            return "external", None   # `", ".join(...)`, `b"".join(...)`, `{...}.get(...)`, `[...].append(...)`
        if first == "super" and cont is not None:
            # Python `super().m()`, Java/Kotlin `super.m()`: the method lives on an ancestor
            parents = _parents(cont["id"])
            if not parents:
                return "external", None
            return from_node(parents[0], chain[1:], flags[1:])
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
        if first == "this" and lang == "kotlin":
            # inside `fun T.f()` (including `this@f`), `this` is the extension receiver T, not the enclosing class
            rtype = (node_by_id.get(r["src"]) or {}).get("extra", {}).get("receiver_type")
            if rtype:
                tn = type_node(rtype.split("<")[0].rstrip("?").split(".")[-1], rel)
                if tn is not None:
                    return from_node(tn, chain[1:], flags[1:])
        if first in ("self", "this", "cls", "Self") and cont is not None:
            return from_node(cont, chain[1:], flags[1:])
        if lang == "kotlin" and first and not flags[0]:
            # inside `fun RuleSet.visitFile()`, a bare `rules` is `this.rules` on the extension receiver
            rn = ext_receiver_node(r, rel)
            if rn is not None and field_owner(rn, first) is not None:
                return from_node(rn, chain, flags)
        if cont is not None and any(first in field_types.get(oid, {}) for oid in owner_ids(cont)):
            return from_node(cont, chain, flags)
        if first and not first[0].isupper() and not flags[0]:
            # `currentDialect.functionProvider.f()`: a top-level typed variable declared in this file, in
            # the same package, or in a file an import (explicit or wildcard) binds
            tv = toplevel_var(first, rel, repo)
            if tv is not None:
                kind, tn = resolve_type_ref(tv["extra"]["type"], tv["file"], None)
                if kind == "ext":
                    return "external", None
                if tn is not None:
                    return from_node(tn, chain[1:], flags[1:])
                return "unknown", None
        if cont is not None and not flags[0]:
            # a property inherited from an ancestor, used without `this.` (Kotlin/Java/Python)
            anc, seen_anc, frontier = None, set(), list(_parents(cont["id"]))
            while frontier and anc is None:
                pn = frontier.pop(0)
                if pn["id"] in seen_anc:
                    continue
                seen_anc.add(pn["id"])
                if any(first in field_types.get(oid, {}) for oid in owner_ids(pn)):
                    anc = pn
                    break
                frontier.extend(_parents(pn["id"]))
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
                        tn = type_node(orig(rel, chain[k - 1]), rel, fset) if k == len(chain) or rest else None
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
        if lang == "python" and first and not flags[0] and is_test_file(rel):
            tn = fixture_type(first, rel, r["src"])   # `def test_x(app): app.register_blueprint(...)`
            if tn is not None:
                return from_node(tn, chain[1:], flags[1:])
        return "unknown", None

    def toplevel_var(name, rel, repo):
        """The unique file-level variable `name` with a declared type that is visible from `rel`."""
        _dn(name)
        cands = var_by_name.get(name, [])
        if not cands:
            return None
        star = ns_repo.get(rel, {}).get("*", set())
        vis = [c for c in cands if c["file"] == rel or (name in repo and c["file"] in repo[name]) or c["file"] in star
               or (lang_of(rel) in ("java", "kotlin", "go") and lang_of(c["file"]) in ("java", "kotlin", "go") and same_package(c["file"], rel))]
        return vis[0] if len(vis) == 1 else None

    def resolve_call(r, rel, lang, cont):
        """Resolve one call ref -> (targets, confidence, mode); mode 'external'/'builtin' mean no edge.
        When several same-arity overloads remain undecided, they are all returned as `ambiguous`."""
        saved = dict(_cur)   # re-entrant: typing an argument or receiver may resolve nested calls
        _cur.update({"argc": r.get("argc"), "arg_types": r.get("arg_types"), "rel": rel, "cont": cont, "overload_undecided": False, "src": r.get("src")})
        try:
            targets, conf, mode = _resolve_call(r, rel, lang, cont)
            if targets and _cur.get("overload_undecided") and conf in ("typed", "same_file", "package", "import"):
                _dn(r["name"])
                group = [c for c in by_name.get(r["name"], []) if c["qname"] == targets[0]["qname"] and c["file"] == targets[0]["file"]
                         and c["kind"] == targets[0]["kind"] and c["id"] not in overload_stub]
                group = by_arity(group)
                if len(group) > 1:
                    return group[:5], "ambiguous", mode
            return targets, conf, mode
        finally:
            _cur.clear()
            _cur.update(saved)

    def ext_receiver_node(r, rel):
        """Type node of the extension receiver when the call sits in a Kotlin `fun T.f()`, else None."""
        rtype = (node_by_id.get(r["src"]) or {}).get("extra", {}).get("receiver_type")
        if not rtype:
            return None
        return type_node(rtype.split("<")[0].rstrip("?").split(".")[-1], rel)

    def lambda_receiver(lam, rel, cont, r, want_it, want_this):
        """Type node of the implicit receiver (`apply`/`run`/DSL `T.() -> R`) or of `it` (`also`/`let`, collection
        functions on a `List<T>`) for a call inside a Kotlin lambda; None when unknown."""
        callee, recv = lam.get("callee") or "", lam.get("recv")

        def recv_node():
            fake = {"kind": "call", "name": callee, "hint": recv, "src": r["src"], "line": 0, "argc": None}
            if lam.get("hint_type"):
                fake["hint_type"], fake["chain"] = lam["hint_type"], lam.get("chain") or []
            m, p = receiver_mode(fake, rel, "kotlin", cont)
            return p if m == "typed" and p else None

        if recv and callee in KT_RECEIVER_LAMBDAS and not want_it:
            return recv_node()
        if recv and callee in KT_IT_LAMBDAS and want_it:
            return recv_node()
        if recv and callee in KT_ELEMENT_LAMBDAS and want_it:
            txt = lam.get("hint_type") if not lam.get("chain") else None
            owners = [cont] if cont is not None else []
            rn = ext_receiver_node(r, rel)
            if rn is not None:
                owners.append(rn)     # `fun RuleSet.f() = rules.flatMap { it.x() }`: `rules` is the receiver's
            if not txt and "." not in recv and "(" not in recv:
                for o in owners:
                    for oid in owner_ids(o):
                        if recv in field_types_raw.get(oid, {}):
                            txt = field_types_raw[oid][recv]   # `MutableList<Line>`: generics kept for the element type
                            break
                    if txt:
                        break
            elem = element_type(txt or "")
            if not elem:
                return None
            k, tn = resolve_type_ref(elem, rel, cont)
            return tn if k == "node" else None
        if not recv and callee and not want_it and not want_this:
            # DSL builder `order(id) { add(x) }`: the lambda's receiver is the `T` of the function's `T.() -> R` parameter
            fcands = [c for c in candidates(callee, {"function"})]
            fn, _ = pick(fcands, rel)
            if not fn and callee in ns_repo.get(rel, {}):
                fn, _ = bound_pick(fcands, callee, ns_repo[rel][callee], {"function"})
            if not fn and "*" in ns_repo.get(rel, {}):
                fn, _ = bound_pick(fcands, callee, ns_repo[rel]["*"], {"function"})
            for f in fn[:1]:
                for ptxt in params_of(f) or []:
                    m = re.search(r"([A-Za-z_][\w.]*)(?:<[^>]*>)?\.\(", ptxt)
                    if m:
                        k, tn = resolve_type_ref(m.group(1), f["file"], None)
                        return tn if k == "node" else None
        return None

    def fixture_type(name, rel, src_id):
        """pytest: an untyped test parameter `app` is the return type of the fixture function `app`
        (same file first, then conftest.py in the test's directory or above)."""
        src = node_by_id.get(src_id)
        if not src or src["kind"] not in ("function", "method"):
            return None
        if not any(re.match(r"^\**" + re.escape(name) + r"\b", p_) for p_ in (params_of(src) or [])):
            return None
        fx = [c for c in candidates(name, {"function"}) if any("fixture" in a for a in c.get("annotations", []))]
        if not fx:
            return None
        d = os.path.dirname(rel)

        def rank(c):
            cd = os.path.dirname(c["file"])
            if c["file"] == rel:
                return 0
            if os.path.basename(c["file"]) == "conftest.py" and (cd == d or cd == "" or d.startswith(cd + "/")):
                return 1 + d.count("/") - cd.count("/")
            return 50
        fx.sort(key=rank)
        if rank(fx[0]) >= 50 and len(fx) > 1:
            return None
        rt = return_type(fx[0])
        if not rt:
            return None
        k, tn = resolve_type_ref(rt, fx[0]["file"], container_of(fx[0]["id"]))
        return tn if k == "node" else None

    def _resolve_call(r, rel, lang, cont):
        name = r["name"]
        lam = r.get("lam")
        if lang == "kotlin" and lam:
            segs = split_chain(r.get("hint")) if r.get("hint") else []
            root = segs[0] if segs else ""
            if not segs or root in ("it", "this"):
                tn = lambda_receiver(lam, rel, cont, r, want_it=(root == "it"), want_this=(root == "this"))
                if tn is not None:
                    chain = [x.split("(")[0].strip("*&!? ") for x in segs[1:]]
                    flags = ["(" in x for x in segs[1:]]
                    t2 = follow_chain(tn, chain, flags) if chain else tn
                    if t2:
                        targets, conf = typed_pick(candidates(name, {"function", "method", "class", "constructor"}), t2)
                        if targets:
                            return targets, conf, "typed"
                        if name in KOTLIN_STDLIB_MEMBERS:
                            return [], None, "builtin"
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
                outer = node_by_id.get(cont.get("parent"))
                while not targets and outer is not None and outer.get("kind") in CONTAINER_KINDS:
                    targets, conf = typed_pick(candidates(name, {"method", "constructor"}), outer)   # inner class -> outer member
                    outer = node_by_id.get(outer.get("parent"))
            if not targets and lang == "java" and java_static.get(rel):
                targets, conf = java_static_pick(name, rel)   # members of the enclosing classes shadow static imports
            inv_type = r.get("invoke_type")
            if not targets and not inv_type and lang in ("kotlin", "java") and cont is not None:
                fo = field_owner(cont, name)   # `runner("1")` on a property: `operator fun invoke`
                if fo is not None and fo[1] and not fo[1].startswith("<"):
                    inv_type = ((fo[2] + ".") if fo[2] else "") + fo[1]
            if not targets and inv_type:
                ik, itn = resolve_type_ref(inv_type, rel, cont)   # `useCase()` -> `operator fun invoke`
                if ik == "node" and itn is not None:
                    targets, conf = typed_pick(candidates("invoke", {"method"}), itn)
            if not targets:
                free_kinds = {"function", "class", "struct", "constructor"} | ({"interface"} if lang in ("kotlin", "java") else set())
                cands = candidates(name, free_kinds)
                if name in ns_repo.get(rel, {}):
                    on = orig(rel, name)
                    ocands = candidates(on, {"function", "class", "struct", "constructor"}) if on != name else cands
                    targets, conf = bound_pick(ocands, on, ns_repo[rel][name], free_kinds)   # from-import wins over same-file shadowing
                if not targets:
                    same = [c for c in cands if c["file"] == rel]
                    if same:
                        targets, conf = by_arity(same)[:1], "same_file"
                    elif lang in ("java", "kotlin", "go"):
                        pkg = [c for c in cands if same_package(c["file"], rel)]
                        if pkg:
                            targets, conf = by_arity(pkg)[:1], "package"
                if not targets and "*" in ns_repo.get(rel, {}):
                    targets, conf = bound_pick(cands, name, ns_repo[rel]["*"], free_kinds)
                if not targets and lang == "rust":
                    targets, conf = pick(same_family(cands, rel), rel)
                if not targets and lang in ("c", "cpp"):
                    # `call(c, flags)` in multi.c: the prototype in an included header (add_edge then points
                    # the edge at the definition), else the one non-static definition in the C family.
                    vis = c_include_closure(rel)
                    seen_ = [c for c in cands if c["file"] in vis]
                    if seen_:
                        targets, conf = by_arity(seen_)[:1], "import"
                    else:
                        ext = [c for c in same_family(cands, rel) if not c["signature"].startswith("static ")]
                        targets, conf = pick(ext, rel)
        elif mode == "namespace":
            targets, conf = bound_pick(candidates(name, {"function", "class", "struct", "constructor", "method"}), name, payload, {"function", "class", "struct", "constructor", "method"})
        elif mode == "rust_path":
            targets, conf = pick(same_family(candidates(name, {"function", "method", "struct", "constructor"}), rel), rel)
        elif mode == "typed":
            targets, conf = typed_pick(candidates(name, {"function", "method", "class", "struct", "constructor"}), payload)
            enc = node_by_id.get(r["src"])
            if targets and lang in ("kotlin", "java") and (r.get("hint") or "").split(".")[0] == "super" \
                    and enc is not None and enc["name"] == name:
                # `super.visitFile(file)` inside `override fun visitFile(file: PsiFile)` calls what that
                # override overrides: a repo method with other parameter types is not it (the real one is
                # an external base class's)
                mine, theirs = param_type_names(enc), param_type_names(targets[0])
                if mine is not None and theirs is not None and mine != theirs:
                    return [], None, "external"
            if not targets and lang == "kotlin" and (name in KOTLIN_SYNTHETIC_MEMBERS or re.match(r"^component\d+$", name)):
                return [], None, "builtin"   # data-class copy/componentN, enum valueOf/values/entries, Any members
            if not targets:  # our type but no such member (embedded struct, macro, dynamic attr): keep as a lead
                targets, conf = lead_pick(candidates(name, {"method"}), rel)
        else:  # unknown receiver
            targets, conf = lead_pick(candidates(name, {"method"}), rel)
        return targets, conf, mode

    # Incremental linking: decide which files' call edges can be replayed from the previous build.
    bfp = {rel: binding_fp(rel) for rel in files}
    reuse = {}
    if prev is not None:
        # A changed file counts as changed for others only if what they can see of it changed:
        # its definitions/imports/metadata (iface) or what pass 1 bound for it. A body-only edit
        # re-links that file alone.
        rebound = {rel for rel in prev["changed"] if rel in files and prev["old_b"].get(rel) != bfp[rel]}
        rebound.update(rel for rel, info in files.items()
                       if info.get("links") is not None and info["links"].get("b") != bfp[rel])
        eff = set(prev["iface"]) | rebound
        touched = set(prev["names"])
        for rel, info in files.items():
            lk = info.get("links")
            if (lk is None or rel in eff or not rebound.isdisjoint(lk["fb"])
                    or not touched.isdisjoint(lk["n"])):
                continue
            if any([x["id"] for x in parents_of.get(nid, [])] != pids for nid, pids in lk["p"].items()):
                continue
            reuse[rel] = lk
    def resolve_file_calls(rel):
        """Resolve one file's calls into its replayable links record, without touching the graph.

        Each call's outcome is recorded: None = unresolved, [] = no edge (builtin), else
        [[target id before the C++ decl->def rewrite, confidence], ...]. Pure with respect to the
        graph being built, so it can run in a forked worker (resolve_calls_parallel)."""
        info = files[rel]
        lang = lang_of(rel)
        calls_out = []
        _dep[0] = _new_dep()
        try:
            for r in info["refs"]:
                if r["kind"] != "call":
                    continue
                name = r["name"]
                targets, conf, mode = resolve_call(r, rel, lang, container_of(r["src"]))
                if (not targets or mode == "external") and lang == "python":
                    # Cross-language bridge: a Python call that resolves to nothing in Python may be
                    # a pybind11 export from a C++ extension module. Only taken when exactly one
                    # binding carries the name and no Python definition does, because a wrong
                    # cross-language edge is worse than the honest gap it replaces.
                    _dn(name)
                    bind = py_binding_index.get(name)
                    if bind is not None and not [c for c in by_name.get(name, [])
                                                 if c["file"].endswith(".py")]:
                        calls_out.append([[bind["id"], "binding"]])
                        continue
                if mode == "external" or (mode != "builtin" and not targets):
                    calls_out.append(None)
                elif mode == "builtin":
                    calls_out.append([])
                else:
                    calls_out.append([[t["id"], conf] for t in targets])
            dep = _dep[0]
        finally:
            _dep[0] = None
        return {"c": calls_out, "n": sorted(dep["n"]), "fb": sorted(dep["b"]), "p": dep["p"], "b": bfp[rel]}

    # `new X(args)` -> the constructor overload it calls. Without this edge a call chain that goes through a
    # constructor (getNextTarEntry -> new TarArchiveEntry(..) -> parseTarHeader) stops at the class, and
    # `callers X.X@line` finds nothing. Recomputed in full on every build (not part of the per-file replay).
    for r, rel, t, conf, name in ctor_links:
        ctors = [c for c in candidates(t["name"], {"constructor"}) if c.get("parent") == t["id"]]
        if not ctors:
            continue
        saved = dict(_cur)
        _cur.update({"argc": r.get("argc"), "arg_types": r.get("arg_types"), "rel": rel,
                     "cont": container_of(r["src"]), "overload_undecided": False, "src": r.get("src")})
        try:
            pickc = by_arity(ctors)
        finally:
            _cur.clear()
            _cur.update(saved)
        for c in (pickc[:1] if len(pickc) == 1 else pickc[:3]):
            add_edge(r["src"], c["id"], "calls", r["line"], conf if len(pickc) == 1 else "ambiguous", name=name)

    # Pass 2b, step 1: resolve the calls of every file that cannot be replayed -- in forked worker
    # processes when there are many (a cold build), else here, lazily, in file order.
    stale = [rel for rel in files if rel not in reuse]
    relinked = len(stale)
    fresh = resolve_calls_parallel(stale, resolve_file_calls, jobs)

    # Pass 2b, step 2: calls (replayed from each file's record), then HCL/K8s references
    for rel, info in files.items():
        lang = lang_of(rel)
        d = os.path.dirname(rel) or "."
        lk = reuse.get(rel)
        if lk is None:
            lk = info["links"] = fresh.pop(rel) if rel in fresh else resolve_file_calls(rel)
        replay = iter(lk["c"])
        for r in info["refs"]:
            k = r["kind"]
            if k == "call":
                ent = next(replay)
                if ent is None:
                    unresolved += 1
                else:
                    for dst, conf in ent:
                        add_edge(r["src"], dst, "calls", r["line"], conf, name=r["name"])
            elif k == "registers_op":
                # REGISTER_KERNEL_BUILDER(Name("MatMul")..., MatMulOp<...>): the kernel class
                # implements the registered op. Both sides are C++, but the op name is what the
                # Python layer addresses, so this is the hop that makes an op reachable.
                op = op_def_index.get(r["name"])
                if op is None:
                    unresolved += 1
                    continue
                kernel = r.get("kernel")
                src_nodes = [c for c in by_name.get(kernel, []) if c["kind"] in ("class", "struct")] if kernel else []
                src_id = src_nodes[0]["id"] if len(src_nodes) == 1 else r["src"]
                add_edge(src_id, op["id"], "implements", r["line"],
                         "exact" if len(src_nodes) == 1 else "package", name=r["name"])
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
    return {"nodes": nodes, "edges": uniq, "stats": stats, "relinked": relinked}


# ----------------------------------------------------------------------------------------------
# Queries
# ----------------------------------------------------------------------------------------------
# ----------------------------------------------------------------------------------------------
# Graph storage (SQLite)
#
# A graph used to be one JSON blob that every query json.load-ed in full. That is fine at a few
# hundred files and fatal at twenty thousand: TensorFlow with C++ indexed produced a 1.3 GB file
# and a single `query stats` cost 12.8s and 5.3 GB RSS -- more memory than most laptops have.
#
# SQLite gives targeted queries what they actually need: a point lookup by id, the edges on one
# node, a name lookup. Whole-graph commands (find, overview, summary, path) still scan, but they
# stream rows instead of materialising the file. The lazy views below keep the existing query code
# working unchanged -- `g.nodes[id]`, `g.out[src]`, `g.g["edges"]` all still read naturally.
# ----------------------------------------------------------------------------------------------
SCHEMA = """
PRAGMA journal_mode=OFF;
PRAGMA synchronous=OFF;
CREATE TABLE IF NOT EXISTS meta  (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS nodes (
  id TEXT PRIMARY KEY, kind TEXT, name TEXT, lname TEXT, qname TEXT, lqname TEXT,
  file TEXT, line INTEGER, end_line INTEGER, signature TEXT,
  annotations TEXT, parent TEXT, extra TEXT);
CREATE TABLE IF NOT EXISTS edges (
  src TEXT, dst TEXT, type TEXT, line INTEGER, confidence TEXT, name TEXT);
CREATE TABLE IF NOT EXISTS parsed (path TEXT PRIMARY KEY, blob TEXT);
"""
INDEXES = """
CREATE INDEX IF NOT EXISTS ix_nodes_lname  ON nodes(lname);
CREATE INDEX IF NOT EXISTS ix_nodes_lqname ON nodes(lqname);
CREATE INDEX IF NOT EXISTS ix_nodes_file   ON nodes(file);
CREATE INDEX IF NOT EXISTS ix_nodes_kind   ON nodes(kind);
CREATE INDEX IF NOT EXISTS ix_nodes_parent ON nodes(parent);
CREATE INDEX IF NOT EXISTS ix_edges_src    ON edges(src);
CREATE INDEX IF NOT EXISTS ix_edges_dst    ON edges(dst);
CREATE INDEX IF NOT EXISTS ix_edges_type   ON edges(type);
"""
NODE_COLS = ("id", "kind", "name", "qname", "file", "line", "end_line", "signature",
             "annotations", "parent", "extra")


def _row_to_node(r):
    return {"id": r[0], "kind": r[1], "name": r[2], "qname": r[3], "file": r[4], "line": r[5],
            "end_line": r[6], "signature": r[7], "annotations": json.loads(r[8] or "[]"),
            "parent": r[9], "extra": json.loads(r[10] or "{}")}


_NODE_SELECT = ("SELECT id,kind,name,qname,file,line,end_line,signature,annotations,parent,extra "
                "FROM nodes")


def _row_to_edge(r):
    e = {"src": r[0], "dst": r[1], "type": r[2], "line": r[3], "confidence": r[4]}
    if r[5] is not None:
        e["name"] = r[5]
    return e


_EDGE_SELECT = "SELECT src,dst,type,line,confidence,name FROM edges"


def replace_with_retry(src, dst, attempts=10, delay=0.15):
    """Move `src` onto `dst`, retrying briefly on a Windows sharing violation.

    On POSIX this is one atomic rename and a reader holding the old file keeps reading it. On
    Windows os.replace raises PermissionError while another process has the destination open, so
    a query running at the wrong moment would fail a rebuild. Queries open and close the database
    quickly, so a short backoff covers it; the final attempt is allowed to raise.

    NOT VERIFIED ON WINDOWS -- written from the documented behaviour, no Windows machine here.
    """
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay)


def write_store(path, graph, files):
    """Write nodes, edges, meta and the per-file parse cache to a fresh database.

    Built under a name unique to this process, then moved into place. Two builds on the same root
    used to share `graph.db.tmp`, so whichever renamed first pulled the file out from under the
    other, which died with FileNotFoundError. Distinct names let both finish; the last os.replace
    wins, and that rename is atomic, so a reader never sees a half-written database.
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{int(time.time() * 1000) % 100000:05d}.tmp"
    try:
        con = sqlite3.connect(tmp)
        try:
            con.executescript(SCHEMA)
            con.executemany(
                "INSERT OR REPLACE INTO nodes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(n["id"], n["kind"], n["name"], n["name"].lower(), n["qname"], n["qname"].lower(),
                  n["file"], n["line"], n["end_line"], n["signature"],
                  json.dumps(n.get("annotations") or []), n.get("parent"),
                  json.dumps(n.get("extra") or {})) for n in graph["nodes"]])
            con.executemany(
                "INSERT INTO edges VALUES (?,?,?,?,?,?)",
                [(e["src"], e["dst"], e["type"], e.get("line"), e.get("confidence"), e.get("name"))
                 for e in graph["edges"]])
            con.executemany("INSERT OR REPLACE INTO parsed VALUES (?,?)",
                            [(rel, json.dumps(info, separators=(",", ":")))
                             for rel, info in files.items()])
            con.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)",
                            [(k, json.dumps(graph.get(k)))
                             for k in ("version", "engine", "root", "built_at", "stats")])
            con.executescript(INDEXES)
            con.commit()
        finally:
            con.close()
        replace_with_retry(tmp, path)
    except BaseException:
        # Never leave a partial database lying around for the next run to trip over.
        for leftover in (tmp, tmp + "-journal"):
            if os.path.exists(leftover):
                try:
                    os.remove(leftover)
                except OSError:
                    pass
        raise


class NodeView:
    """`g.nodes` without holding every node in memory: point lookups hit an index and are cached."""

    def __init__(self, store):
        self.s = store
        self._c = {}

    def __getitem__(self, nid):
        n = self.get(nid)
        if n is None:
            raise KeyError(nid)
        return n

    def get(self, nid, default=None):
        if nid in self._c:
            return self._c[nid]
        r = self.s.con.execute(_NODE_SELECT + " WHERE id=?", (nid,)).fetchone()
        n = _row_to_node(r) if r else None
        self._c[nid] = n
        return n if n is not None else default

    def __contains__(self, nid):
        return self.get(nid) is not None

    def values(self):
        return self.s.iter_nodes()

    def __iter__(self):
        return (n["id"] for n in self.s.iter_nodes())


class AdjView:
    """`g.out[src]` / `g.inc[dst]`: the edges on one node, fetched by index."""

    def __init__(self, store, column):
        self.s, self.col = store, column
        self._c = {}

    def __getitem__(self, key):
        return self.get(key)

    def get(self, key, default=None):
        if key not in self._c:
            rows = self.s.con.execute(f"{_EDGE_SELECT} WHERE {self.col}=?", (key,)).fetchall()
            self._c[key] = [_row_to_edge(r) for r in rows]
        return self._c[key] or (default if default is not None else [])


class LazyGraph:
    """Stands in for the old graph dict. `nodes` and `edges` stream from SQLite when a command
    genuinely needs every row (find, overview, summary, path); everything else avoids them."""

    def __init__(self, store):
        self.s = store

    def __getitem__(self, key):
        if key == "nodes":
            return list(self.s.iter_nodes())
        if key == "edges":
            return list(self.s.iter_edges())
        if key == "files":
            return self.s.parsed()
        v = self.s.meta(key)
        if v is None:
            raise KeyError(key)
        return v

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default


class Store:
    def __init__(self, path):
        self.path = path
        self.con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self.con.execute("PRAGMA query_only=ON")
        self._meta, self._parsed = {}, None

    def meta(self, key):
        if key not in self._meta:
            r = self.con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            self._meta[key] = json.loads(r[0]) if r and r[0] is not None else None
        return self._meta[key]

    def iter_nodes(self, where="", params=()):
        for r in self.con.execute(_NODE_SELECT + (" " + where if where else ""), params):
            yield _row_to_node(r)

    def iter_edges(self, where="", params=()):
        for r in self.con.execute(_EDGE_SELECT + (" " + where if where else ""), params):
            yield _row_to_edge(r)

    def by_name(self, lowered):
        rows = self.con.execute(_NODE_SELECT + " WHERE lname=? OR lqname=?", (lowered, lowered)).fetchall()
        out = [_row_to_node(r) for r in rows]
        # A qualified name the caller wrote without its namespace: `OpKernel::Compute` is stored as
        # `tensorflow.OpKernel.Compute`. Match on the suffix here rather than letting resolve() fall
        # through to a substring scan, which materialised all 443k nodes (673 MB) on TensorFlow.
        if not out and "." in lowered:
            rows = self.con.execute(_NODE_SELECT + " WHERE lqname LIKE ?", ("%." + lowered,)).fetchall()
            out = [_row_to_node(r) for r in rows]
        # file nodes are also addressable by path
        rows = self.con.execute(_NODE_SELECT + " WHERE kind='file' AND lower(file)=?", (lowered,)).fetchall()
        seen = {n["id"] for n in out}
        return out + [n for n in (_row_to_node(r) for r in rows) if n["id"] not in seen]

    def substring(self, ql, limit=5000):
        """Nodes whose qname or path contains `ql`. Filtered in SQL: the Python equivalent built a
        list of every node in the graph to find a handful of matches."""
        pat = f"%{ql}%"
        rows = self.con.execute(
            _NODE_SELECT + " WHERE lqname LIKE ? OR lower(file) LIKE ? LIMIT ?",
            (pat, pat, limit)).fetchall()
        return [_row_to_node(r) for r in rows]

    def kind_histogram(self, table, column):
        """{value: count} for one column, counted in SQL."""
        assert table in ("nodes", "edges") and column in ("kind", "type", "confidence")
        return dict(sorted(self.con.execute(
            f"SELECT {column}, COUNT(*) FROM {table} GROUP BY {column}")))

    def nodes_of_kind(self, kinds, names=None):
        """Nodes of the given kinds, optionally restricted to a set of names. Both columns are
        indexed, so this replaces a pass over every node in the graph."""
        q = _NODE_SELECT + " WHERE kind IN (" + ",".join("?" * len(kinds)) + ")"
        params = list(kinds)
        if names:
            q += " AND lname IN (" + ",".join("?" * len(names)) + ")"
            params += [n.lower() for n in names]
        return [_row_to_node(r) for r in self.con.execute(q, params)]

    def degree_counts(self):
        """(indegree, outdegree) per node id, aggregated in SQL.

        The fast path for a plain `overview`: with no --no-tests or --lang filter nothing is
        decided per edge, so there is no reason to hand 1.43M rows to Python. GROUP BY returns
        ~108k and ~210k rows instead."""
        base = "FROM edges WHERE type != 'contains' AND confidence != 'ambiguous' GROUP BY "
        indeg = {r[0]: r[1] for r in self.con.execute("SELECT dst, COUNT(*) " + base + "dst")}
        outdeg = {r[0]: r[1] for r in self.con.execute("SELECT src, COUNT(*) " + base + "src")}
        return indeg, outdeg

    def ranking_edges(self):
        """(src, dst, src_file, dst_file, dst_kind) for every edge that counts toward centrality.

        One join instead of the old shape, which listed all 1.43M edges and then did two point
        lookups per edge to find their files -- 2.86M queries, ~15s and 1.4 GB on TensorFlow.
        `contains` and `ambiguous` are dropped in SQL because they never count."""
        q = ("SELECT e.src, e.dst, ns.file, nd.file, nd.kind "
             "FROM edges e JOIN nodes ns ON ns.id=e.src JOIN nodes nd ON nd.id=e.dst "
             "WHERE e.type != 'contains' AND e.confidence != 'ambiguous'")
        return self.con.execute(q)

    def node_meta_closure(self, ids):
        """{id: {id, kind, qname, file, parent}} for `ids` and their parent chains.

        Slim on purpose -- the centrality rollup reads only kind/file/parent/qname, never the
        signature, annotations or `extra` that make up most of a row -- and targeted on purpose:
        on TensorFlow only 108k of 443k nodes have an incoming edge, so fetching every node's
        metadata (an earlier attempt) cost more, not less. Parents are closed over iteratively
        because containment is shallow; the bound stops a cycle from looping forever."""
        meta, want, rounds = {}, {i for i in ids if i}, 0
        while want and rounds < 24:
            batch = list(want)
            for i in range(0, len(batch), 500):
                chunk = batch[i:i + 500]
                q = ("SELECT id,kind,qname,file,parent FROM nodes WHERE id IN ("
                     + ",".join("?" * len(chunk)) + ")")
                for r in self.con.execute(q, chunk):
                    meta[r[0]] = {"id": r[0], "kind": r[1], "qname": r[2], "file": r[3], "parent": r[4]}
            want = {m["parent"] for m in meta.values() if m["parent"] and m["parent"] not in meta}
            rounds += 1
        return meta

    def nodes_by_ids(self, ids):
        """{id: node} for many ids in a few queries rather than one query each."""
        out, ids = {}, list(ids)
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            rows = self.con.execute(
                _NODE_SELECT + " WHERE id IN (" + ",".join("?" * len(chunk)) + ")", chunk).fetchall()
            for r in rows:
                n = _row_to_node(r)
                out[n["id"]] = n
        return out

    def substring_wide(self, ql, limit=20000):
        """Like `substring`, but also matches the bare name -- what `find` searches."""
        pat = f"%{ql}%"
        rows = self.con.execute(
            _NODE_SELECT + " WHERE lname LIKE ? OR lqname LIKE ? OR lower(file) LIKE ? LIMIT ?",
            (pat, pat, pat, limit)).fetchall()
        return [_row_to_node(r) for r in rows]

    def kotlin_methods(self):
        """Methods declared in Kotlin files -- the only candidates for extension-method cards."""
        return list(self.iter_nodes(
            "WHERE kind='method' AND (file LIKE '%.kt' OR file LIKE '%.kts')"))

    def resolved_lines_for(self, src_ids):
        """{(src, line, name)} for the edges leaving `src_ids` -- used to tell which of a file's
        raw refs the linker managed to resolve."""
        ids = list(src_ids)
        out = set()
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = "SELECT src,line,name FROM edges WHERE src IN (" + ",".join("?" * len(chunk)) + ")"
            out.update((r[0], r[1], r[2]) for r in self.con.execute(q, chunk))
        return out

    def inbound_files(self, dst_ids):
        """{dst: Counter(source file -> rows)} for the non-structural, non-ambiguous edges into
        `dst_ids`. Grouped in SQL so a 6,000-line file's symbols cost one indexed pass."""
        out = defaultdict(Counter)
        ids = list(dst_ids)
        for i in range(0, len(ids), 500):          # stay under SQLite's variable limit
            chunk = ids[i:i + 500]
            q = ("SELECT e.dst, n.file, COUNT(*) FROM edges e JOIN nodes n ON n.id = e.src "
                 "WHERE e.type != 'contains' AND e.confidence != 'ambiguous' AND e.dst IN ("
                 + ",".join("?" * len(chunk)) + ") GROUP BY e.dst, n.file")
            for dst, f, c in self.con.execute(q, chunk):
                out[dst][f] += c
        return out

    def count_ambiguous_into(self, dst_ids):
        """How many `ambiguous` edges point at any of `dst_ids`, without materialising the table."""
        ids = list(dst_ids)
        if not ids:
            return 0
        total = 0
        for i in range(0, len(ids), 500):          # stay under SQLite's variable limit
            chunk = ids[i:i + 500]
            q = ("SELECT COUNT(*) FROM edges WHERE confidence='ambiguous' AND dst IN ("
                 + ",".join("?" * len(chunk)) + ")")
            total += self.con.execute(q, chunk).fetchone()[0]
        return total

    def count_ambiguous_from(self, src_ids):
        """How many `ambiguous` edges leave any of `src_ids` (the callees-side twin of count_ambiguous_into)."""
        ids = list(src_ids)
        total = 0
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            q = ("SELECT COUNT(*) FROM edges WHERE confidence='ambiguous' AND src IN ("
                 + ",".join("?" * len(chunk)) + ")")
            total += self.con.execute(q, chunk).fetchone()[0]
        return total

    def file_langs(self):
        return {r[0]: (json.loads(r[1] or "{}") or {}).get("language")
                for r in self.con.execute("SELECT file, extra FROM nodes WHERE kind='file'")}

    def parsed(self):
        if self._parsed is None:
            self._parsed = {r[0]: json.loads(r[1])
                            for r in self.con.execute("SELECT path, blob FROM parsed")}
        return self._parsed

    def parsed_one(self, path):
        r = self.con.execute("SELECT blob FROM parsed WHERE path=?", (path,)).fetchone()
        return json.loads(r[0]) if r else None

    def parsed_all_for_build(self):
        return self.parsed()

    def close(self):
        self.con.close()


class NameView:
    """`g.by_name[x]`: an indexed lookup instead of a prebuilt dictionary of every node."""

    def __init__(self, store):
        self.s = store
        self._c = {}

    def __getitem__(self, key):
        return self.get(key)

    def get(self, key, default=None):
        if key not in self._c:
            self._c[key] = self.s.by_name(key)
        return self._c[key] or (default if default is not None else [])


class G:
    def __init__(self, store):
        self.store = store
        self.g = LazyGraph(store)
        self.nodes = NodeView(store)
        self.out = AdjView(store, "src")
        self.inc = AdjView(store, "dst")
        self.by_name = NameView(store)
        self.file_lang = store.file_langs()

    def parse_cache(self):
        """EVERY file's parse cache, materialised. Prefer `store.parsed_one(path)` -- this loads
        20,805 rows and ~2.5 GB on a repo the size of TensorFlow. Kept for whole-graph tooling."""
        return self.store.parsed()
    def children(self, nid):
        return sorted((self.nodes[e["dst"]] for e in self.out.get(nid, []) if e["type"] == "contains"), key=lambda n: n["line"])

    def resolve(self, query, kinds=None):
        """Match a user query to nodes: exact id, exact (q)name, file path, `file:name`, then substring."""
        q = query.strip()
        if q in self.nodes:
            return [self.nodes[q]]
        # A path typed from a subdirectory (`cd tests && query file foo.py`) is relative to the cwd,
        # graph paths to the repo root: re-anchor it when that names an indexed file.
        prefix = getattr(self, "cwd_prefix", None)
        if prefix and "::" not in q:
            fpart, sep, rest = q.partition(":")
            cand = os.path.normpath(os.path.join(prefix, fpart)).replace(os.sep, "/")
            if os.path.splitext(fpart)[1].lower() in EXT_LANG and cand in self.nodes:
                q = cand + sep + rest
                if q in self.nodes:
                    return [self.nodes[q]]
        # C++ names are written with `::` but stored with the engine's `.` qname separator, so
        # `MatMulOp::Compute` and `tensorflow::ops::MatMulOp` resolve like any other qname.
        if "::" in q:
            dotted = q.replace("::", ".")
            hit = self.by_name.get(dotted.lower(), [])
            if hit:
                return list({n["id"]: n for n in hit}.values())
            q = dotted
        if ":" in q and "::" not in q:
            fpart, npart = q.split(":", 1)
            line = None
            if "@" in npart and npart.rsplit("@", 1)[1].isdigit():   # `readers.py:read_csv@1283`
                npart, line = npart.rsplit("@", 1)
                line = int(line)
            hits = [n for n in self.by_name.get(npart.lower(), [])
                    if (n["file"] == fpart or n["file"].endswith("/" + fpart)) and (line is None or n["line"] == line)]
            exact_path = [n for n in hits if n["file"] == fpart]   # `variables.tf:var.x` means the root file when it exists
            return list({n["id"]: n for n in (exact_path or hits)}.values())
        m = re.match(r"^(.+)@(\d+)$", q)
        if m and ":" not in q:
            # `ObjectMapper.readValue@3860` picks one overload: the definition starting on that line, else
            # the one whose body contains it (models quote call-site lines from `called by` rows).
            line, named = int(m.group(2)), self.resolve(m.group(1), kinds)
            at = [n for n in named if n["line"] == line] or \
                 sorted((n for n in named if n["line"] <= line <= (n.get("end_line") or n["line"])),
                        key=lambda n: (n.get("end_line") or n["line"]) - n["line"])[:1]
            if at:
                return at
        exact = self.by_name.get(q.lower(), [])
        if kinds:
            exact = [n for n in exact if n["kind"] in kinds]
        if exact:
            return list({n["id"]: n for n in exact}.values())
        ql = q.lower()
        subs = self.store.substring(ql)
        if kinds:
            subs = [n for n in subs if n["kind"] in kinds]
        return subs

    def in_edges(self, nid, types):
        return [e for e in self.inc.get(nid, []) if e["type"] in types]

    def out_edges(self, nid, types):
        return [e for e in self.out.get(nid, []) if e["type"] in types]


class QueryError(Exception):
    """A query that cannot answer one name (not found, ambiguous). Raised instead of exiting so a
    multi-name query (`symbol A B C`) still answers the other names; `stdout` marks output that is
    useful to the reader (the list of candidates) rather than an error message."""
    def __init__(self, text, stdout=False):
        super().__init__(text)
        self.text, self.stdout = text, stdout


def find_graph(start=None):
    """The nearest `<dir>/.ast-graph/graph.db` at or above `start` (default: cwd), the way git finds
    `.git`. Agents `cd` into subdirectories to run tests; their next query must still find the graph."""
    d = os.path.abspath(start or os.getcwd())
    while True:
        p = os.path.join(d, DEFAULT_GRAPH)
        if os.path.exists(p):
            return p
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def default_layout_root(graph_path):
    """<root> when graph_path is <root>/.ast-graph/graph.db (the default layout), else None."""
    gp = os.path.abspath(graph_path)
    tail = os.sep + os.path.normpath(DEFAULT_GRAPH)
    return os.path.dirname(os.path.dirname(gp)) if gp.endswith(tail) else None


def refresh_if_stale(graph_path):
    """Rebuild the graph in place when the git working tree changed since it was built, with the
    options that build recorded. Returns a one-line note, or None when nothing was done.

    This removes the separate `build` call an agent otherwise makes before every question: the stamp
    check is a `git status` plus hashing the changed files (well under a second), and the rebuild is
    incremental. Skipped when there is no stamp to compare (not a git checkout, or a graph built
    before options were recorded) and when ASTGRAPH_NO_REFRESH is set."""
    if os.environ.get("ASTGRAPH_NO_REFRESH"):
        return None
    try:
        with open(graph_path + ".stamp") as f:
            prev = json.load(f)
    except (OSError, ValueError):
        return None
    opts = prev.get("options")
    if not opts:
        # Built before stamps recorded their options: it cannot be refreshed safely (the options are
        # unknown), but it must not go stale silently either -- say so once the engine has changed.
        try:
            with open(os.path.abspath(__file__), "rb") as f:
                engine = hashlib.sha1(f.read()).hexdigest()
            st = Store(graph_path)
            built_by = st.meta("engine")
            st.close()
        except (OSError, sqlite3.Error):
            return None
        if built_by and built_by != engine:
            return ("(note: this graph was built by an older engine and is not refreshed automatically; "
                    "run `build` once and later queries will keep it current)")
        return None
    root = opts.get("root")
    if not root or not os.path.isdir(root):
        root = default_layout_root(graph_path)   # repo moved since the build
        if root is None:
            return None
    stamp = git_stamp(root, opts.get("include"), opts.get("exclude"), graph_path, opts.get("keep_dir"))
    if stamp is None or (stamp == prev.get("stamp") and prev.get("version") == GRAPH_VERSION):
        return None
    t0 = time.time()
    g = build_graph(root, graph_path, opts.get("include"), opts.get("exclude"), quiet=True, keep_dirs=opts.get("keep_dir"))
    if g is None:
        return None
    return f"(graph refreshed before this query: {g.get('parsed_now', '?')} file(s) re-parsed, {time.time() - t0:.1f}s)"


def fmt_node(n, with_file=True):
    loc = f"{n['file']}:{n['line']}" if n.get("file") and n.get("line") else n.get("file") or ""
    ann = f" @{' @'.join(a.split('(')[0] for a in n['annotations'])}" if n.get("annotations") else ""
    sig = n["signature"].split("\n")
    sig = sig[0] + (" ..." if len(sig) > 1 else "")   # multi-line signatures: first line only
    return f"{kind_label(n)}{sig}{ann}" + (f"  ({loc})" if with_file and loc else "")


def ensure_one(g, query, kinds=None):
    matches = g.resolve(query, kinds)
    if not matches:
        bare = re.split(r"[.:/]", query.strip())[-1] or query
        raise QueryError(f"no symbol or file matches '{query}'. Try: query find {bare}" + (" (then `symbol <id>`; the member may live on another class)" if bare != query else ""))
    if len(matches) > 1 and not all(m["id"] == matches[0]["id"] for m in matches):
        qn = query.split(":", 1)[1] if ":" in query and "::" not in query else query
        qn = qn.replace("::", ".")      # C++ callers write `Class::member`; qnames are dotted
        exact = [m for m in matches
                 if m["name"].lower() == qn.lower() or m["qname"].lower() == qn.lower()
                 or m["qname"].lower().endswith("." + qn.lower())   # `MatMulOp.Compute` in a namespace
                 or m["file"] == query]
        # Lookup is case-insensitive, but in Go, Java, C++ ... case is part of the name: `Context.Plan`
        # (exported) and `Context.plan` are different methods. A case-exact match wins.
        same_case = [m for m in exact if m["name"] == qn or m["qname"] == qn or m["qname"].endswith("." + qn)]
        if same_case and len(same_case) < len(exact):
            exact = same_case
        for narrow in (lambda m: not is_test_file(m["file"]),
                       # A C++ member matches twice: the header declaration and the .cc definition.
                       # The body is what a reader wants, so prefer it over the prototype.
                       lambda m: not m.get("extra", {}).get("is_declaration"),
                       lambda m: m.get("parent") == m.get("file")):
            sub = [m for m in exact if narrow(m)]   # production code before tests, top-level before nested
            if sub and len(sub) < len(exact):
                exact = sub
        if len(exact) == 1:
            return exact[0]
        preferred = [m for m in exact if m["kind"] in TYPE_LIKE_KINDS | {"k8s_object", "resource", "module_call", "file", "terraform_module", "package"}]
        if len(preferred) == 1:
            return preferred[0]
        raise QueryError(f"'{query}' is ambiguous ({len(matches)} matches). Re-run with one of these ids or `file:name`:\n"
                         + "\n".join(f"  {m['id']}    {fmt_node(m)}" for m in matches[:25]), stdout=True)
    return matches[0]


def q_find(g, args):
    ql = args.name.lower()
    langs = set(getattr(args, "lang", None) or [])

    def lang_of(n):
        return g.file_lang.get(n["file"]) if n.get("file") else None

    # Candidates come from SQL (`lname`/`lqname`/`file` LIKE) instead of a pass over every node:
    # the Python scan built 443k dicts on TensorFlow to return a page of matches.
    cands = g.store.substring_wide(ql)
    res = [n for n in cands if (ql in n["name"].lower() or ql in n["qname"].lower() or ql in n["file"].lower())
           and (not args.kind or n["kind"] in args.kind) and n["kind"] != "file" or (n["kind"] == "file" and ql in n["file"].lower() and (not args.kind or "file" in args.kind))]
    if langs:
        res = [n for n in res if lang_of(n) in langs]
    if getattr(args, "no_tests", False):
        res = [n for n in res if not (n.get("file") and is_test_file(n["file"]))]

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
    if args.kind:
        res = [n for n in res if rank(n)[0] < 5]   # a path-only hit is not a symbol of that kind
    exact_n = sum(1 for n in res if rank(n)[0] <= 1)
    if exact_n and len(res) > exact_n + 15:
        res = res[: exact_n + 15]   # exact matches plus a few substring hits; the rest is noise for a name lookup
    total = len(res) if not (exact_n and len(res) == exact_n + 15) else len(res) + 1
    total = max(total, len(res))
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


def param_type_names(n):
    """Parameter type names of a Kotlin/Java callable from its signature text (`fun f(a: A, b: B<C> = x)`,
    `void f(final A a, B... b)`), generics and nullability dropped; None when the text cannot be parsed.
    An override has the same list as the method it overrides; an overload of the same name does not."""
    sig = (n.get("signature") or "").split("\n")[0]
    i = sig.find(n.get("name", "") + "(")
    i = sig.find("(", i if i >= 0 else 0)
    if i < 0:
        return None
    depth, j = 0, i
    for j in range(i, len(sig)):
        depth += sig[j] in "(<[" and 1 or (-1 if sig[j] in ")>]" else 0)
        if depth == 0:
            break
    inner = sig[i + 1:j].strip()
    if not inner:
        return []
    out = []
    for p in split_top(inner):
        p = re.sub(r"@\w+(\([^)]*\))?\s*", "", p.split("=")[0]).strip()
        p = re.sub(r"\b(final|vararg|val|var|crossinline|noinline)\s+", "", p)
        t = p.split(":", 1)[1] if ":" in p else p.rsplit(" ", 1)[0] if " " in p else p
        out.append(re.sub(r"<.*>", "", t).replace("...", "[]").strip().rstrip("?").split(".")[-1])
    return out


def overrides_of(g, n, want_down=True):
    """(methods this one overrides, methods overriding it): same-named members across extends/implements,
    three levels each way. Empty for anything that is not a member of a container.

    `want_down=False` skips the descendant walk. That direction is unbounded on a hub base class --
    walking OpKernel's subclasses three levels deep cost 670 MB on TensorFlow -- so callers that
    only need the ancestors (dispatch_note) must not pay for it."""
    par = g.nodes.get(n.get("parent"))
    if n["kind"] not in ("method", "function") or par is None or par["kind"] not in CONTAINER_KINDS:
        return [], []

    jvm = os.path.splitext(n["file"] or "")[1] in (".kt", ".kts", ".java")
    mine = param_type_names(n) if jvm else None

    def same_params(k):
        # Kotlin/Java: a same-named member with other parameter types is an overload (MaxLineLength's
        # private `visit(KtFileContent)` next to BaseRule's `visit(KtFile)`), not an override
        if mine is None:
            return True
        theirs = param_type_names(k)
        return theirs is None or theirs == mine

    def related(start, down):
        seen, out, frontier = {start["id"]}, [], [(start, 0)]
        while frontier:
            cls, depth = frontier.pop(0)
            if depth >= 3:
                continue
            edges = g.in_edges(cls["id"], {"extends", "implements"}) if down else g.out_edges(cls["id"], {"extends", "implements"})
            for e in edges:
                nxt = g.nodes.get(e["src"] if down else e["dst"])
                if nxt is None or nxt["id"] in seen or nxt["kind"] not in CONTAINER_KINDS:
                    continue
                seen.add(nxt["id"])
                out.extend(k for k in g.children(nxt["id"]) if k["name"] == n["name"] and k["kind"] in ("method", "function")
                           and same_params(k))
                frontier.append((nxt, depth + 1))
        return out
    return related(par, False), (related(par, True) if want_down else [])


def dispatch_note(g, n):
    """One line telling the reader that callers may reach `n` through the method it overrides."""
    ups, _ = overrides_of(g, n, want_down=False)
    if not ups:
        return None
    base = ups[0]
    direct = len([e for e in g.inc.get(base["id"], []) if e["type"] == "calls"])
    return (f"note: {n['qname']} overrides {base['qname']} ({base['file']}:{base['line']}); {direct} direct caller(s) of the base "
            f"method reach this override by dynamic dispatch and are listed under `query callers {base['qname']}`")


def q_symbol(g, args):
    n = ensure_one(g, args.name)
    ups, downs = overrides_of(g, n)
    if args.json:
        loc = lambda k: {"id": k["id"], "qname": k["qname"], "file": k["file"], "line": k["line"]}   # noqa: E731
        print(json.dumps({"node": n, "children": g.children(n["id"]),
                          "out": g.out.get(n["id"], []), "in": g.inc.get(n["id"], []),
                          "overrides": [loc(k) for k in ups], "overridden_by": [loc(k) for k in downs]}))
        return
    print(fmt_node(n))
    if n["extra"]:
        ex = {k: v for k, v in n["extra"].items() if v not in (None, "", [], {}) and k not in ("language",)}
        if ex:
            print("  extra: " + json.dumps(ex)[:400])
    show_all = getattr(args, "all", False)
    cap = None if show_all else getattr(args, "limit", 40)
    if ups or downs:
        if ups:
            print("├── Overrides: " + ", ".join(f"{k['qname']} ({k['file']}:{k['line']})" for k in ups[:None if show_all else CAP_OVERRIDES]))
        if downs:
            shown = downs if show_all else downs[:CAP_OVERRIDDEN_BY]
            print(f"├── Overridden by ({len(downs)}): " + ", ".join(f"{k['qname']} ({k['file']}:{k['line']})" for k in shown)
                  + (f" ... (+{len(downs) - len(shown)}; --all lists every one)" if len(downs) > len(shown) else ""))

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
        # Kotlin extension methods declared on this type. Narrowed in SQL to Kotlin methods first:
        # scanning every node here cost ~1 GB on TensorFlow looking for a language it does not use.
        exts = [m for m in g.store.kotlin_methods()
                if m.get("extra", {}).get("receiver_type") == n["name"]
                and m["id"] not in own and m.get("parent") != n["id"]
                and (is_test_file(n["file"]) or not is_test_file(m["file"]))]   # test-only extensions stay off production cards
        if exts:
            kids = kids + [dict(m, signature=m["signature"] + f"   [extension, {m['file']}]") for m in sorted(exts, key=lambda m: (m["file"], m["line"]))]
    # Per-member call lists were the bulk of a class card (2-4k tokens for one Django class) and are
    # rarely what the question needs; `--calls` (or `--all`) brings them back.
    with_calls = show_all or getattr(args, "calls", False)
    if kids:
        print("├── Members" + (f" ({len(kids)}, showing {cap})" if cap and len(kids) > cap else "")
              + ("" if with_calls else "  (--calls adds each member's calls)"))
        for k in kids[:cap]:
            end = f"-{k['end_line']}" if k.get("end_line") and k["end_line"] != k["line"] else ""
            print(f"│   ├── {fmt_node(k, with_file=False)}  [L{k['line']}{end}]")
            if k["kind"] in ("object", "class") and k["signature"].startswith("companion"):
                # Kotlin companion members are the class's static API: list them under it
                for cm in g.children(k["id"])[:None if show_all else 12]:
                    cend = f"-{cm['end_line']}" if cm.get("end_line") and cm["end_line"] != cm["line"] else ""
                    print(f"│   │     {fmt_node(cm, with_file=False)}  [L{cm['line']}{cend}]")
            if not with_calls:
                continue
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
    # One row, not the whole cache: `parse_cache()` materialises every file's refs, which on
    # TensorFlow is 20,805 rows and 2.5 GB of RSS -- to print a handful of unresolved names.
    finfo = g.store.parsed_one(n["file"]) or {}
    member_ids = {n["id"]} | {k["id"] for k in kids}
    # Only this symbol's own members can have resolved refs, so ask for their edges rather than
    # building a set from every edge in the graph: that scan alone cost 3.6 GB on TensorFlow, for
    # a card that displays at most a few dozen rows.
    resolved_lines = g.store.resolved_lines_for(member_ids)
    unresolved = []
    for r in finfo.get("refs", []):
        if r["src"] in member_ids and r["kind"] in ("call", "extends", "implements", "instantiates", "import") \
                and (r["src"], r["line"], r["name"]) not in resolved_lines and (r["src"], r["line"], None) not in resolved_lines:
            label = f"{r['kind']} {r['hint'] + '.' if r.get('hint') else ''}{r['name']}"
            if label not in unresolved:
                unresolved.append(label)
    if unresolved:
        print("├── Unresolved (external or not indexed): " + ", ".join(unresolved[:CAP_UNRESOLVED]) + (f" (+{len(unresolved) - CAP_UNRESOLVED})" if len(unresolved) > CAP_UNRESOLVED else ""))
    ins = [e for e in g.inc.get(n["id"], []) if e["type"] != "contains"]
    # also include incoming edges to members (e.g. callers of a class's methods)
    member_ins = []
    for k in kids:
        for e in g.inc.get(k["id"], []):
            if e["type"] != "contains":
                member_ins.append((k, e))
    # Uses from test files are folded into one per-file count line (unless --all, or the symbol is
    # itself test code): on a library class they were most of the card -- 41 of the 46 rows of
    # Django's `Window` card -- while the question is almost always about production users.
    test_uses = Counter()
    if not show_all and not is_test_file(n["file"] or ""):
        def from_test(e):
            s = g.nodes.get(e["src"])
            return bool(s and s.get("file") and is_test_file(s["file"]))
        for e in ins:
            if from_test(e):
                test_uses[g.nodes[e["src"]]["file"]] += 1
        for _k, e in member_ins:
            if from_test(e):
                test_uses[g.nodes[e["src"]]["file"]] += 1
        ins = [e for e in ins if not from_test(e)]
        member_ins = [(k, e) for k, e in member_ins if not from_test(e)]
    # Uses from inside the class itself (its members using a nested type or each other) say nothing about
    # who uses it from outside, and on a large class they were most of the section; --all keeps them.
    internal = 0
    if not show_all and n["kind"] in CONTAINER_KINDS:
        def inside(sid, depth=0):
            x = g.nodes.get(sid)
            while x is not None and depth < 8:
                if x.get("parent") == n["id"] or x["id"] == n["id"]:
                    return True
                x, depth = g.nodes.get(x.get("parent")), depth + 1
            return False
        before = len(ins) + len(member_ins)
        ins = [e for e in ins if not inside(e["src"])]
        member_ins = [(k, e) for k, e in member_ins if not inside(e["src"])]
        internal = before - len(ins) - len(member_ins)
    # `new X(args)` records an `instantiates` edge to the class and a `calls` edge to the constructor: on the
    # class card the second says nothing new
    inst = {(e["src"], e.get("line")) for e in ins if e["type"] == "instantiates"}
    member_ins = [(k, e) for k, e in member_ins
                  if not (k["kind"] == "constructor" and e["type"] == "calls" and (e["src"], e.get("line")) in inst)]
    rows = [(None, e) for e in ins] + [(k["name"], e) for k, e in member_ins]
    if rows or test_uses or internal:
        total_in = len(rows)
        print("└── Used by (incoming)" + (f" ({total_in}, showing {cap}; `--all` for everything, `query callers` to traverse)" if cap and total_in > cap else ""))
        rows.sort(key=lambda r: (r[1]["confidence"] == "ambiguous", (g.nodes.get(r[1]["src"]) or {}).get("file") or "", r[1].get("line") or 0))
        # One line per file, the path printed once: rows used to repeat a 100+ character path each
        by_file, items = {}, {}
        for mem, e in rows[:cap]:
            src = g.nodes.get(e["src"])
            if src is None:
                continue
            key = (src["file"], e["type"], mem, src["id"], e["confidence"])
            if key not in items:
                items[key] = []
                by_file.setdefault(src["file"], []).append(key)
            items[key].append(e.get("line") or src["line"])
        for f, keys in by_file.items():
            parts = []
            for key in keys:
                _f, typ, mem, sid, conf = key
                lines = ",".join(str(x) for x in sorted(set(items[key])))
                parts.append(f"{typ}{' ' + mem if mem else ''} from {g.nodes[sid]['qname']} L{lines} ({conf})")
            print(f"    ├── {f}: " + "; ".join(parts))
        if cap and len(rows) > cap:
            print(f"    ├── ... {len(rows) - cap} more; " + by_file_summary([g.nodes[e["src"]] for _, e in rows[cap:] if e["src"] in g.nodes]))
        if internal:
            print(f"    ├── + {internal} use(s) from inside {n['name']} itself (--all lists them)")
        if test_uses:
            top = sorted(test_uses.items(), key=lambda kv: (-kv[1], kv[0]))
            print(f"    └── + {sum(test_uses.values())} more from {len(top)} test file(s): "
                  + ", ".join(f"{f} ({c})" for f, c in top[:5]) + (f", +{len(top) - 5} files" if len(top) > 5 else "")
                  + " (--all lists them)")


def bfs(g, start_ids, direction, types, depth, include_ambiguous, no_tests=False):
    """Breadth-first over edges. direction 'in' = who depends on start, 'out' = what start depends on.
    `no_tests` drops edges whose other end is in a test file (and does not traverse through them)."""
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
            if no_tests:
                on = g.nodes.get(other)
                if on and on.get("file") and is_test_file(on["file"]):
                    continue
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
    if (n["kind"] in CONTAINER_KINDS or n["kind"] == "file") and not getattr(args, "no_members", False):
        targets += [k["id"] for k in g.children(n["id"])]
    # Every dependency relation except file-level imports, so Terraform/K8s references count as "callers".
    no_tests = getattr(args, "no_tests", False)
    seen, hops = bfs(g, targets, direction, DEP_EDGE_TYPES - {"imports"}, args.depth, args.include_ambiguous, no_tests)
    # Direct edges left out because the receiver's type is unknown and only the name matched. Saying so
    # matters: a silent omission sent agents back to grep to double-check the graph.
    if args.include_ambiguous:
        hidden = 0
    elif no_tests:   # count only what --no-tests would have shown: walk the (indexed) direct edges
        hidden = 0
        for t in targets:
            for e in (g.inc.get(t, []) if direction == "in" else g.out.get(t, [])):
                o = g.nodes.get(e["src"] if direction == "in" else e["dst"])
                if e["confidence"] == "ambiguous" and e["type"] != "contains" and o and not (o.get("file") and is_test_file(o["file"])):
                    hidden += 1
    else:
        hidden = g.store.count_ambiguous_into(set(targets)) if direction == "in" else g.store.count_ambiguous_from(set(targets))
    if args.json:
        print(json.dumps({"root": n["id"], "levels": seen, "hops": [(a, e, b, l) for a, e, b, l in hops],
                          "note": dispatch_note(g, n) if direction == "in" else None, "ambiguous_hidden": hidden}))
        return
    label = "callers of" if direction == "in" else "callees of"
    print(f"{label} {n['qname']}  ({n['file']}:{n['line']})  depth={args.depth}" + ("  (no tests)" if no_tests else ""))
    note = dispatch_note(g, n) if direction == "in" else None
    if note:
        print("  " + note)
    hidden_note = (f"  + {hidden} ambiguous edge(s) not shown (receiver type unknown, name matches): add --include-ambiguous"
                   if hidden else None)
    if not hops:
        print("  none found (no resolved edges)")
        if hidden_note:
            print(hidden_note)
        return
    max_rows = getattr(args, "max_rows", 200)
    mode = "summary" if getattr(args, "summary", False) else "files" if getattr(args, "files_only", False) else "rows"
    if mode == "rows" and len(hops) > max_rows:
        mode = "summary"
        print(f"  ({len(hops)} rows exceed --max-rows {max_rows}; showing the summary. Use --files-only for the file list, --max-rows N for all rows.)")
    arrow = "<-" if direction == "in" else "->"
    if mode == "rows":
        # Compact rows: the queried symbol is in the header, so a direct row starts at the arrow, and the
        # edge type is only spelled out when it is not `calls`. Deeper hops (and members of a class
        # target) keep the name they hang off.
        for a, e, b, lvl in hops:
            other = g.nodes[b]
            this = g.nodes[a]
            if direction == "in":
                loc = f"{other['file']}:{e.get('line') or other['line']}"                  # caller file, call-site line
            else:
                loc = f"L{e.get('line') or '?'} -> {other['file']}:{other['line']}"      # call-site line -> definition
            kind = e["confidence"] if e["type"] == "calls" else f"{e['type']}, {e['confidence']}"
            head = f"{arrow}" if (lvl == 1 and a == n["id"]) else f"{this['qname']} {arrow}"
            print(f"  {'  ' * (lvl - 1)}{head} {other['qname']}  {loc}  [{kind}]")
        if hidden_note:
            print(hidden_note)
        return
    per_file = defaultdict(lambda: {"level": 99, "rows": 0})
    per_sym = defaultdict(lambda: [0, 99, None])
    mix = defaultdict(int)
    for _a, e, b, lvl in hops:
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
    if hidden_note:
        print(hidden_note)


SOURCE_IN_TYPES = {"calls", "instantiates", "extends", "implements", "references"}


def q_source(g, args):
    """One call instead of `symbol` + a ranged read + a confirming grep: where the symbol is, who
    calls it, what it calls, and its exact source lines. Without it a question takes three calls
    (symbol, a ranged read, a confirming grep), and every call re-reads the conversation."""
    n = ensure_one(g, args.name)
    if n["kind"] == "file":
        raise QueryError(f"'{args.name}' is a file; `query file {n['file']}` gives its outline, `source <symbol>` a definition")
    start = n["line"] or 1
    end = n.get("end_line") or start
    print(f"{fmt_node(n, with_file=False)}  {n['file']}:{start}-{end} ({end - start + 1} lines)")
    no_tests = getattr(args, "no_tests", False)

    def where(o, line):
        # same file as the symbol: just the line; elsewhere: the path the reader needs for a next step
        return f"L{line}" if o["file"] == n["file"] else f"{o['file']}:{line}"

    def ref_line(label, edges, other_key, line_of, query_cmd):
        groups = {}                                   # other node -> [lines, ambiguous?, edge type]
        def order(e):   # firm before ambiguous, library code before tests, then by line
            o = g.nodes.get(e[other_key]) or {}
            return (e["confidence"] == "ambiguous", bool(o.get("file") and is_test_file(o["file"])), o.get("file") or "", e.get("line") or 0)
        for e in sorted(edges, key=order):
            o = g.nodes.get(e[other_key])
            if o is None or (no_tests and o.get("file") and is_test_file(o["file"])):
                continue
            gr = groups.setdefault(o["id"], [o, [], True, e["type"]])
            gr[1].append(line_of(e, o))
            gr[2] = gr[2] and e["confidence"] == "ambiguous"
        if not groups:
            print(f"{label}: none resolved")
            return
        rows = list(groups.values())
        amb = sum(1 for r in rows if r[2])
        parts = []
        for o, lines, is_amb, typ in rows[:args.refs]:
            locs = sorted(set(lines))
            loc = where(o, locs[0]) + ("," + ",".join(str(x) for x in locs[1:4]) if len(locs) > 1 else "")
            parts.append(f"{o['qname']} {loc}" + ("" if typ == "calls" else f" [{typ}]") + ("?" if is_amb else ""))
        more = f" (+{len(rows) - args.refs}: query {query_cmd} {n['qname']})" if len(rows) > args.refs else ""
        print(f"{label} ({len(rows)}" + (f", {amb} ambiguous marked ?" if amb else "") + "): " + ", ".join(parts) + more)

    if not args.no_refs:
        note = dispatch_note(g, n)
        if note:
            print(note)
        ref_line("called by", g.in_edges(n["id"], SOURCE_IN_TYPES), "src",
                 lambda e, o: e.get("line") or o["line"], "callers")
        if n["kind"] not in CONTAINER_KINDS:
            ref_line("calls", g.out_edges(n["id"], {"calls", "instantiates"}), "dst",
                     lambda e, o: o["line"], "callees")
    path = os.path.join(getattr(g, "root", "") or "", n["file"])
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        print(f"(source not readable at {path}; the graph may come from another checkout)")
        return
    span = lines[start - 1:end]
    limit = args.max_lines
    # One budget for every name in this call (`source A B C`): a multi-name answer stays one call
    # without the size of several whole files. --max-lines 0 lifts both caps.
    if limit:
        left = getattr(g, "source_lines_left", CAP_SOURCE_TOTAL)
        if left < 10 and len(span) > left:
            print(f"-- body not shown: this call's {CAP_SOURCE_TOTAL}-line budget is spent; `source {n['qname']}` "
                  f"on its own, or read L{start}-{end}")
            return
        limit = min(limit, left)
    if limit and len(span) > limit:
        kids = g.children(n["id"]) if n["kind"] in CONTAINER_KINDS else []
        if kids:
            # a big class: its members with exact ranges beat the first N lines of its body
            for k in kids:
                rng = f"L{k['line']}" + (f"-{k['end_line']}" if k.get("end_line") and k["end_line"] != k["line"] else "")
                print(f"  {fmt_node(k, with_file=False)}  [{rng}]")
            print(f"-- {len(span)} lines: members listed instead of the body; `source {n['qname']}.<member>` for one, --max-lines 0 for all")
            g.source_lines_left = getattr(g, "source_lines_left", CAP_SOURCE_TOTAL) - len(kids)
            return
        span = span[:limit]
    for i, text in enumerate(span, start):
        print(f"{i:5} {text}")
    if limit:
        g.source_lines_left = getattr(g, "source_lines_left", CAP_SOURCE_TOTAL) - len(span)
    if limit and end - start + 1 > limit:
        print(f"-- L{start + limit}-{end} not shown ({end - start + 1 - limit} lines): read that range, or --max-lines 0 for all")


def q_tests_for(g, args):
    """Test functions that reach a symbol (transitively, through callers), grouped by test file: which
    tests to read and run for a change, instead of grepping tests/ for names. Tests often call through
    untyped locals, so paths through ambiguous edges are included and marked `?`; firm paths come first."""
    n = ensure_one(g, args.name)
    start = with_owner_if_constructor(g, n)
    if n["kind"] in CONTAINER_KINDS:
        start += [k["id"] for k in g.children(n["id"])]
    types = DEP_EDGE_TYPES - {"imports"}
    firm, _ = bfs(g, start, "in", types, args.depth, False)
    reach, _ = ({}, None) if args.no_ambiguous else bfs(g, start, "in", types, args.depth, True)
    hits = defaultdict(list)                 # test file -> [(ambiguous?, hop, node)]
    for nid, hop in list(firm.items()) + [(k, v) for k, v in reach.items() if k not in firm]:
        t = g.nodes.get(nid)
        if t and t["kind"] in ("function", "method") and t.get("file") and is_test_file(t["file"]) and nid not in start:
            hits[t["file"]].append((nid not in firm, hop, t))
    print(f"tests reaching {n['qname']}  ({n['file']}:{n['line']})  depth={args.depth}"
          + ("" if args.no_ambiguous else "; ? = only through an ambiguous edge"))
    if not hits:
        print("  none found" + ("" if args.no_ambiguous else " (tests calling through fixtures or untyped helpers may still exist)"))
        return
    # files with firm hits first, then by number of tests
    order = sorted(hits.items(), key=lambda kv: (all(a for a, _, _ in kv[1]), -len(kv[1]), kv[0]))
    for f, rows in order[:args.top]:
        rows.sort(key=lambda r: (r[0], r[1], r[2]["line"]))
        shown = ", ".join(f"{t['qname']} L{t['line']}{'?' if amb else ''}" for amb, _, t in rows[:CAP_TESTS_PER_FILE])
        more = f" (+{len(rows) - CAP_TESTS_PER_FILE})" if len(rows) > CAP_TESTS_PER_FILE else ""
        print(f"  {f} ({len(rows)}): {shown}{more}")
    total = sum(len(r) for r in hits.values())
    firm_n = sum(1 for r in hits.values() for a, _, _ in r if not a)
    tail = f"; +{len(order) - args.top} more files (--top N)" if len(order) > args.top else ""
    print(f"  {total} test functions in {len(hits)} files ({firm_n} through firm edges){tail}")


def for_each_name(fn):
    """Run a single-name query for every name given (`symbol A B C`), so a question that needs several
    symbols is one call. A name that fails (not found, ambiguous) is reported inline and the others
    still run; the exit code is non-zero only when every name failed."""
    def run(g, args):
        names = args.name if isinstance(args.name, list) else [args.name]
        if len(names) == 1:
            return fn(g, argparse.Namespace(**{**vars(args), "name": names[0]}))
        failed = 0
        for i, name in enumerate(names):
            if i and not getattr(args, "json", False):
                print()
            try:
                fn(g, argparse.Namespace(**{**vars(args), "name": name}))
            except QueryError as e:
                failed += 1
                print(e.text if e.stdout else f"{name}: {e.text}")
        if failed == len(names):
            sys.exit(1)
    return run


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
    seen, hops = bfs(g, start, "in", DEP_EDGE_TYPES, args.depth, args.include_ambiguous, getattr(args, "no_tests", False))
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
    print(f"### Blast Radius & Downstream Impact: {n['kind']} {n['qname']}  ({n['file']}:{n['line']})  depth={args.depth}"
          + ("  (test files excluded)" if getattr(args, "no_tests", False) else ""))
    note = dispatch_note(g, n)
    if note:
        print(note)
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
            for lvl, dep, _e, _tgt in i["items"]:
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
            for _lvl, _dep, e, _tgt in i["items"]:
                mix[(e["type"], e["confidence"])] += 1
        print("Relationship mix: " + ", ".join(f"{t}/{c}={v}" for (t, c), v in sorted(mix.items(), key=lambda kv: -kv[1])))
    print()
    print(f"Files affected: {len(per_file)} (direct: {len(direct)}, transitive: {len(per_file) - len(direct)}); dependency rows: {total_rows}")
    if tests:
        shown_t = tests[: args.top * 2]
        head = "Tests reached through resolved edges (a lower bound; tests that reach the target through fixtures or untyped receivers are not linked)"
        tail = f" (+{len(tests) - len(shown_t)} more; --files-only lists all)" if len(tests) > len(shown_t) else ""
        if len(shown_t) <= 5:
            print(head + ": " + ", ".join(shown_t) + tail)
        else:
            print(head + ":" + tail)
            for t in shown_t:
                print(f"  - {t}")
    # Counted in SQL rather than by scanning every edge: on TensorFlow that scan alone pulled the
    # whole edge table into memory for what is otherwise a targeted, indexed query.
    amb = g.store.count_ambiguous_into(start_set)
    if amb and not args.include_ambiguous:
        print(f"Note: {amb} ambiguous edge(s) to the target were excluded; re-run with --include-ambiguous to see them.")


def q_overview(g, args):
    """Centrality overview: hub symbols, hub files, and directory summary."""
    indeg, outdeg = defaultdict(int), defaultdict(int)
    test_files = {f for f in g.file_lang if is_test_file(f)} if getattr(args, "no_tests", False) else set()

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
    lang_files = {f for f, lg in g.file_lang.items() if lg in langs_ok} if langs_ok else None
    if not test_files and lang_files is None:
        indeg, outdeg = g.store.degree_counts()          # nothing to decide per edge: aggregate in SQL
    else:
        for src, dst, sf, df, dk in g.store.ranking_edges():
            if test_files and (sf in test_files or in_test_module(src)):
                continue   # --no-tests: usage from test files or inline test modules does not make a symbol a hub
            if lang_files is not None and (sf not in lang_files or (df not in lang_files and dk not in ("external", "external_module"))):
                continue   # --lang: rank only within the requested language(s); externals keep their counts
            indeg[dst] += 1
            outdeg[src] += 1
    # roll member usage up to the containing symbol and file
    file_in = defaultdict(int)
    sym_in = defaultdict(int)
    # Slim metadata for every node, once: the loop below reads only kind/file/parent/qname, and
    # the parent walk would otherwise issue a point query per hop.
    meta = g.store.node_meta_closure(indeg)
    for nid, c in indeg.items():
        n = meta.get(nid)
        if not n:
            continue
        if n["file"]:
            file_in[n["file"]] += c
        p = n
        while p is not None and p["kind"] not in CONTAINER_KINDS and p.get("parent"):
            p = meta.get(p["parent"])
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
    for f, lg in g.file_lang.items():          # already loaded; no need to list every node again
        dirs[os.path.dirname(f) or "."][lg or "?"] += 1
    print("\nDirectories (files by language):")
    shown_dirs = sorted(dirs.items(), key=lambda kv: (-sum(kv[1].values()), kv[0]))[: args.top * 2]   # largest first
    for d, langs in shown_dirs:
        print(f"  {d}: " + ", ".join(f"{k}={v}" for k, v in sorted(langs.items())))
    if len(dirs) > len(shown_dirs):
        print(f"  ... {len(dirs) - len(shown_dirs)} more directories (query stats / query file for any of them)")
    print(f"\nMost depended-upon symbols (incoming calls/extends/implements/references, top {args.top}):")
    groups = {}   # same kind + qualified name in several directories (generated module copies) -> one row
    for nid, c in sorted(sym_in.items(), key=lambda kv: -kv[1]):
        n = g.nodes[nid]
        k = (n["kind"], n["qname"])
        if k in groups:
            groups[k][2] += 1
        else:
            groups[k] = [n, c, 0]
    for n, c, copies in list(groups.values())[: args.top]:
        print(f"  {c:4d}  {fmt_node(n)}" + (f"  (+{copies} same-named cop{'y' if copies == 1 else 'ies'} in other directories)" if copies else ""))
    print(f"\nMost depended-upon files (top {args.top}):")
    for f, c in sorted(file_in.items(), key=lambda kv: -kv[1])[: args.top]:
        print(f"  {c:4d}  {f}")
    # Both of the lookups below are indexed on `kind`. Listing every node to find them cost
    # 0.64 GB and 2.8s on TensorFlow -- to select 477 externals and a handful of entry points.
    ext = g.store.nodes_of_kind(("external", "external_module"))
    if ext:
        ext_in = sorted(((indeg.get(n["id"], 0), n["name"]) for n in ext), reverse=True)[: args.top]
        print(f"\nExternal dependencies (by import count, top {args.top}): " + ", ".join(f"{name} ({c})" for c, name in ext_in))
    entry = [n for n in g.store.nodes_of_kind(("function",), names=("main", "lambda_handler", "cli"))
             if indeg.get(n["id"], 0) == 0 and not (test_files and n["file"] in test_files) and not is_test_file(n["file"])]
    if entry:
        print("\nLikely entry points: " + ", ".join(f"{n['qname']} ({n['file']})" for n in entry[: args.top]))


def q_file(g, args):
    n = ensure_one(g, args.path, kinds={"file"})
    # The parse cache lives in a sidecar so that ordinary queries never load it (62% of the bytes
    # on a large C++ repo). `query file` is the one command that needs it, so it pays here.
    info = g.store.parsed_one(n["file"])
    if not info:
        sys.exit(f"{n['file']} has no parse-cache row; re-run `build`")
    ins = [e for e in g.inc.get(n["id"], []) if e["type"] == "imports"]
    if args.json:
        out = {"file": info["file_node"], "nodes": info["nodes"], "refs": info["refs"],
               "imported_by": sorted({g.nodes[e["src"]]["file"] for e in ins if e["src"] in g.nodes})}
        if args.used_by:
            out["used_by"] = [{"id": n["id"], "files": nf, "refs": nr}
                              for nf, nr, n in used_by_rows(g, n["file"], info["nodes"], args.no_tests, args.within)[:args.used_by]]
        print(json.dumps(out))
        return
    if args.used_by:
        # first, so `| head` keeps it: a large file's skeleton runs to hundreds of lines
        print_used_by(g, n["file"], info["nodes"], args.used_by, args.no_tests, args.within)
    elided = Counter()
    print(render_skeleton(info["file_node"], info["nodes"], info["refs"], show_calls=not args.no_calls, elided=elided))
    # `query file` has no --max-calls/--max-imports of its own; point at the command that does.
    if elided:
        what = ", ".join(f"{n:,} {k}" for k, n in sorted(elided.items(), key=lambda kv: -kv[1]))
        print(f"-- elided: {what} (raise with `skeleton {info['file_node']['file']} --max-calls N --max-imports N`)")
    if ins:
        print("  imported by: " + ", ".join(sorted({g.nodes[e['src']]['file'] for e in ins if e['src'] in g.nodes})[:CAP_IMPORTED_BY]))


def in_scope(f, within):
    """`--within` match: a directory prefix (`app`, `app/`) or a glob (`src/**/api_*.py`)."""
    for w in within:
        if any(ch in w for ch in "*?["):
            if fnmatch.fnmatch(f, w):
                return True
        elif f == w or f.startswith(w.rstrip("/") + "/"):
            return True
    return False


def used_by_rows(g, path, nodes, no_tests, within=None):
    """The file's symbols ranked by how many *other* files use them: [(files, refs, node)].
    Answers "what in this file matters to the rest of the repo" (or to one part of it, with
    `within`) in one call instead of a `callers` query per symbol."""
    inbound = g.store.inbound_files(n["id"] for n in nodes)
    rows = []
    for n in nodes:
        files = {f: c for f, c in inbound.get(n["id"], {}).items()
                 if f and f != path and not (no_tests and is_test_file(f))
                 and (not within or in_scope(f, within))}
        if files:
            rows.append((len(files), sum(files.values()), n))
    rows.sort(key=lambda r: (-r[0], -r[1], r[2]["line"]))
    return rows


def print_used_by(g, path, nodes, top, no_tests, within=None):
    rows = used_by_rows(g, path, nodes, no_tests, within)
    scope = "non-test files" if no_tests else "files"
    if within:
        scope += " under " + ", ".join(within)
    print(f"{path}: symbols used by other {scope} (top {min(top, len(rows))} of {len(rows)} with outside users; skeleton follows)")
    for nf, nr, n in rows[:top]:
        print(f"    {nf:5d} files {nr:6d} refs  {n['kind']} {n['qname']}  [L{n['line']}]")


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
            if e["type"] == "contains" or (e["confidence"] == "ambiguous" and not getattr(args, "include_ambiguous", False)):
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
    # Histograms come from GROUP BY; the Python equivalent built 443k node dicts and 1.43M edge
    # dicts to produce two dozen counters.
    s["node_kinds"] = g.store.kind_histogram("nodes", "kind")
    s["edge_types"] = g.store.kind_histogram("edges", "type")
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
    sk.add_argument("--keep-dir", action="append", help="directory name to index although it is excluded by default (build, dist, target, vendor ...)")
    sk.add_argument("--no-calls", action="store_true", help="omit the 'calls:' lines")
    sk.add_argument("--no-lines", action="store_true", help="omit line ranges")
    sk.add_argument("--max-calls", type=int, default=CAP_SKELETON_CALLS,
                    help=f"calls listed per symbol before '(+N more)' (default {CAP_SKELETON_CALLS}; 0 = no cap)")
    sk.add_argument("--max-imports", type=int, default=CAP_SKELETON_IMPORTS,
                    help=f"imports listed per file before '(+N more)' (default {CAP_SKELETON_IMPORTS}; 0 = no cap)")
    sk.add_argument("--no-stats", action="store_true", help="omit the trailing token-estimate line")
    sk.add_argument("--json", action="store_true")
    sk.set_defaults(fn=cmd_skeleton)

    b = sub.add_parser("build", help="build or incrementally refresh the relationship graph")
    b.add_argument("--root", default=".")
    b.add_argument("--out", default=None, help=f"graph file (default <root>/{DEFAULT_GRAPH})")
    b.add_argument("--include", action="append")
    b.add_argument("--exclude", action="append")
    b.add_argument("--keep-dir", action="append", help="directory name to index although it is excluded by default (build, dist, target, vendor ...)")
    b.add_argument("--full", action="store_true", help="ignore the cached graph and re-parse everything")
    b.add_argument("--quiet", action="store_true")
    b.add_argument("--jobs", type=int, default=None,
                   help="parser processes (default: all CPUs, or $ASTGRAPH_JOBS; 1 = serial)")
    b.set_defaults(fn=lambda a: build_graph(a.root, a.out or os.path.join(a.root, DEFAULT_GRAPH), a.include, a.exclude, a.full, a.quiet, a.keep_dir, a.jobs))

    q = sub.add_parser("query", help="query a built graph")
    q.add_argument("--graph", default=DEFAULT_GRAPH)
    q.add_argument("--root", default=None, help=f"repo root; the graph is read from <root>/{DEFAULT_GRAPH} unless --graph is given "
                                               f"(default: the nearest {DEFAULT_GRAPH} at or above the current directory)")
    q.add_argument("--no-refresh", action="store_true",
                   help="do not rebuild a graph that is stale against the git working tree (also: ASTGRAPH_NO_REFRESH=1)")
    qs = q.add_subparsers(dest="qcmd", required=True)

    x = qs.add_parser("find", help="search symbols/files by name (exact-name matches first)")
    x.add_argument("name"); x.add_argument("--kind", action="append"); x.add_argument("--limit", type=int, default=CAP_FIND); x.add_argument("--json", action="store_true")
    x.add_argument("--lang", action="append", help="only symbols from files of this language (repeatable)")
    x.add_argument("--no-tests", action="store_true", help="hide symbols defined in test files")
    x.set_defaults(qfn=q_find)
    x = qs.add_parser("symbol", help="architecture card for one or more symbols: members, dependencies, dependents")
    x.add_argument("name", nargs="+"); x.add_argument("--json", action="store_true")
    x.add_argument("--limit", type=int, default=CAP_SYMBOL_SECTION, help=f"max rows per section (default {CAP_SYMBOL_SECTION}; hubs get a per-file summary for the rest)")
    x.add_argument("--calls", action="store_true", help="list each member's calls (off by default: they dominate a class card)")
    x.add_argument("--all", action="store_true", help="no caps (implies --calls)")
    x.set_defaults(qfn=for_each_name(q_symbol))
    x = qs.add_parser("source", help="definition + callers + callees + exact source lines of one or more symbols, in one call")
    x.add_argument("name", nargs="+")
    x.add_argument("--max-lines", type=int, default=CAP_SOURCE_LINES,
                   help=f"body lines before cutting (default {CAP_SOURCE_LINES}; 0 = whole body; a longer class prints its member outline)")
    x.add_argument("--refs", type=int, default=CAP_SOURCE_REFS, help=f"callers/callees named per symbol (default {CAP_SOURCE_REFS})")
    x.add_argument("--no-refs", action="store_true", help="only the source lines")
    x.add_argument("--no-tests", action="store_true", help="leave callers/callees in test files out")
    x.set_defaults(qfn=for_each_name(q_source))
    x = qs.add_parser("tests-for", help="test functions that reach these symbols, grouped by test file (which tests to run)")
    x.add_argument("name", nargs="+")
    x.add_argument("--depth", type=int, default=3, help="caller hops to follow (default 3)")
    x.add_argument("--no-ambiguous", action="store_true", help="only paths made of firm edges")
    x.add_argument("--top", type=int, default=CAP_TESTS_FILES, help=f"test files listed (default {CAP_TESTS_FILES})")
    x.set_defaults(qfn=for_each_name(q_tests_for))
    for cmd, direction, helptext in (("callers", "in", "who calls/extends/instantiates these symbols (transitive with --depth)"),
                                     ("callees", "out", "what these symbols call/instantiate (transitive with --depth)")):
        x = qs.add_parser(cmd, help=helptext)
        x.add_argument("name", nargs="+"); x.add_argument("--depth", type=int, default=1); x.add_argument("--include-ambiguous", action="store_true"); x.add_argument("--json", action="store_true")
        x.add_argument("--no-tests", action="store_true", help="drop edges from/to test files")
        x.add_argument("--summary", action="store_true", help="directories, most frequent symbols and relationship mix instead of rows")
        x.add_argument("--files-only", action="store_true", help="only the files, grouped by hop")
        x.add_argument("--max-rows", type=int, default=CAP_ROWS, help=f"above this many rows the listing degrades to --summary (default {CAP_ROWS})")
        x.add_argument("--top", type=int, default=CAP_SUMMARY_TOP, help="rows per section in --summary")
        x.add_argument("--no-members", action="store_true", help="for a class/file target: only edges to the class itself (instantiations, extends, references), not to its members")
        x.set_defaults(qfn=for_each_name((lambda d: (lambda g, a: q_callers(g, a, d)))(direction)))
    x = qs.add_parser("trace-deps", help="blast radius: every file/symbol that depends on a target")
    x.add_argument("target", help="file path, symbol name, qualified name, or node id")
    x.add_argument("--depth", type=int, default=3); x.add_argument("--per-file", type=int, default=CAP_PER_FILE)
    x.add_argument("--include-ambiguous", action="store_true"); x.add_argument("--json", action="store_true")
    x.add_argument("--summary", action="store_true", help="directories, most-connected dependents and relationship mix instead of the per-edge table")
    x.add_argument("--files-only", action="store_true", help="only the affected files, grouped by hop")
    x.add_argument("--max-rows", type=int, default=CAP_ROWS, help=f"above this many dependency rows the table degrades to --summary (default {CAP_ROWS})")
    x.add_argument("--top", type=int, default=CAP_SUMMARY_TOP, help="rows per section in --summary")
    x.add_argument("--no-tests", action="store_true", help="leave dependents in test files out")
    x.set_defaults(qfn=q_trace_deps)
    x = qs.add_parser("overview", help="centrality ranking: hub symbols, hub files, directories, externals")
    x.add_argument("--top", type=int, default=CAP_SUMMARY_TOP); x.add_argument("--json", action="store_true")
    x.add_argument("--no-tests", action="store_true", help="ignore usage coming from test files when ranking hubs")
    x.add_argument("--lang", action="append", help="rank only symbols/files of this language (repeatable, e.g. --lang python)")
    x.set_defaults(qfn=q_overview)
    x = qs.add_parser("file", help="skeleton of a file from the graph, plus who imports it")
    x.add_argument("path"); x.add_argument("--no-calls", action="store_true"); x.add_argument("--json", action="store_true")
    x.add_argument("--used-by", type=int, nargs="?", const=15, default=0, metavar="N",
                   help="also rank the file's symbols by how many other files use them (top N, default 15)")
    x.add_argument("--no-tests", action="store_true", help="with --used-by: ignore usage from test files")
    x.add_argument("--within", action="append", metavar="DIR_OR_GLOB",
                   help="with --used-by: count only users in this directory or glob (repeatable)")
    x.set_defaults(qfn=q_file)
    x = qs.add_parser("path", help="shortest dependency path from one symbol/file to another")
    x.add_argument("src"); x.add_argument("dst"); x.add_argument("--json", action="store_true")
    x.add_argument("--include-ambiguous", action="store_true", help="allow ambiguous (lead) edges on the path")
    x.set_defaults(qfn=q_path)
    x = qs.add_parser("stats", help="graph statistics")
    x.set_defaults(qfn=q_stats)

    def run_query(a):
        if a.root and a.graph == DEFAULT_GRAPH:
            a.graph = os.path.join(a.root, DEFAULT_GRAPH)
        elif a.graph == DEFAULT_GRAPH and not os.path.exists(a.graph):
            a.graph = find_graph() or a.graph          # e.g. the agent `cd`-ed into tests/ to run them
        if not os.path.exists(a.graph):
            msg = f"graph not found at {a.graph}. Build it first: astgraph.py build --root <repo>"
            note = legacy_graph_note(a.graph)
            sys.exit(msg + (f"\n{note}" if note else ""))
        if not a.no_refresh:
            note = refresh_if_stale(a.graph)
            if note:
                print(note, file=sys.stderr)          # stderr: keeps --json output parseable
        try:
            g = G(Store(a.graph))
        except sqlite3.Error as e:
            sys.exit(f"{a.graph} is not a readable graph ({e}). Rebuild it: astgraph.py build --root <repo>")
        g.graph_path = a.graph
        # Where file paths in the graph are anchored: the default layout is <root>/.ast-graph/graph.db,
        # which survives the repo being moved; a custom --graph falls back to the root recorded at build.
        g.root = default_layout_root(a.graph) or g.store.meta("root") or ""
        rel = os.path.relpath(os.getcwd(), g.root) if g.root else "."
        g.cwd_prefix = rel if rel != "." and not rel.startswith("..") else None
        try:
            a.qfn(g, a)
            sys.stdout.flush()
        except QueryError as e:
            if e.stdout:
                print(e.text)
                sys.stdout.flush()
                sys.exit(1)
            sys.exit(e.text)
        except BrokenPipeError:
            # `query ... | head` is how agents read long output; a closed pipe is the reader being
            # done, not an error worth a stderr message in their context.
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
            sys.exit(0)
    q.set_defaults(fn=run_query)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
