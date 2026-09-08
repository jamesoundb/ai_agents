"""Multi-document YAML loading with PyYAML when present, else tree-sitter (no third-party YAML lib)."""
import re


def _ts_load_all(text):
    from tree_sitter_language_pack import get_parser
    src = text.encode("utf-8")
    tree = get_parser("yaml").parse(src)

    def t(n):
        return src[n.start_byte:n.end_byte].decode("utf-8", "replace")

    def scalar(s):
        s = s.strip()
        if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
            return s[1:-1]
        if s in ("true", "True"):
            return True
        if s in ("false", "False"):
            return False
        if s in ("null", "~", ""):
            return None
        if re.fullmatch(r"-?\d+", s):
            return int(s)
        if re.fullmatch(r"-?\d+\.\d+", s):
            return float(s)
        return s

    def conv(n):
        ty = n.type
        if ty in ("document", "block_node", "flow_node"):
            vals = [conv(c) for c in n.named_children if c.type not in ("comment", "anchor", "tag")]
            vals = [v for v in vals if v is not None]
            return vals[0] if vals else None
        if ty in ("block_mapping", "flow_mapping"):
            out = {}
            for p in n.named_children:
                if p.type in ("block_mapping_pair", "flow_pair"):
                    k, v = p.child_by_field_name("key"), p.child_by_field_name("value")
                    key = conv(k) if k is not None else None
                    if isinstance(key, (str, int, float, bool)):
                        out[key] = conv(v) if v is not None else None
            return out
        if ty in ("block_sequence", "flow_sequence"):
            out = []
            for it in n.named_children:
                if it.type == "block_sequence_item":
                    out.append(conv(it.named_children[0]) if it.named_children else None)
                elif it.type != "comment":
                    out.append(conv(it))
            return out
        if ty in ("plain_scalar", "single_quote_scalar", "double_quote_scalar", "string_scalar", "integer_scalar", "float_scalar", "boolean_scalar", "null_scalar"):
            return scalar(t(n))
        if ty == "block_scalar":
            txt = t(n)
            return txt.split("\n", 1)[1] if "\n" in txt else ""
        if n.named_children:
            vals = [conv(c) for c in n.named_children if c.type != "comment"]
            vals = [v for v in vals if v is not None]
            return vals[0] if vals else None
        return scalar(t(n)) if n.is_named else None

    docs, lines = [], []
    for d in tree.root_node.named_children:
        if d.type == "document":
            docs.append(conv(d))
            lines.append(d.start_point[0] + 1)
    return docs, lines


def load_all(text):
    """Return (documents, start_lines). Documents are plain dict/list/scalars."""
    try:
        import yaml  # PyYAML
        docs, lines = [], []
        for node in yaml.compose_all(text):
            if node is None:
                continue
            lines.append(node.start_mark.line + 1)
            docs.append(yaml.safe_load(yaml.serialize(node)))
        return docs, lines
    except ImportError:
        return _ts_load_all(text)
