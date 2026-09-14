"""DSL ↔ DB 方言文字列のレンダラとパーサ。企画書 §9.3。

- 方言は config/dialects/*.json で定義（演算子・括弧・フィールド書式・文字数上限）
- render(query) は決定的。parse(text) は人が書いた既存の式を DSL に取り込むために使う
- parse(render(q)).signature() == q.signature() の往復を全方言で必須にする
- 文字数上限を超える式は Block 単位で分割し、分割式の集合（結果は和集合）として扱う
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .document import FIELDS, SCHEMES
from .dsl import Block, Code, Query, Term


class RenderError(ValueError):
    pass


class ParseError(ValueError):
    pass


@dataclass
class Dialect:
    id: str
    label: str = ""
    and_: str = " AND "
    or_: str = " OR "
    not_: str = " NOT "
    block_open: str = "("
    block_close: str = ")"
    group_open: str = "("
    group_close: str = ")"
    field_sep: str = "="
    field_position: str = "prefix"       # prefix: FIELD=text / suffix: text/FIELD
    quote: str = '"'
    fields: dict = field(default_factory=lambda: {f: f for f in FIELDS})
    schemes: dict = field(default_factory=lambda: {s: s for s in SCHEMES})
    code_format: dict = field(default_factory=dict)
    proximity: str | None = None
    truncation: str = ""
    max_chars: int = 4000

    @classmethod
    def from_dict(cls, d: dict) -> "Dialect":
        return cls(
            id=d.get("id", "generic"), label=d.get("label", ""),
            and_=d.get("and", " AND "), or_=d.get("or", " OR "), not_=d.get("not", " NOT "),
            block_open=d.get("block_open", "("), block_close=d.get("block_close", ")"),
            group_open=d.get("group_open", "("), group_close=d.get("group_close", ")"),
            field_sep=d.get("field_sep", "="), field_position=d.get("field_position", "prefix"),
            quote=d.get("quote", '"') or "", fields=dict(d.get("fields") or {}),
            schemes=dict(d.get("schemes") or {}), code_format=dict(d.get("code_format") or {}),
            proximity=d.get("proximity"), truncation=d.get("truncation", "") or "",
            max_chars=int(d.get("max_chars") or 4000),
        )

    def supports_field(self, f: str) -> bool:
        return f in self.fields

    def rev_fields(self) -> dict:
        return {v: k for k, v in self.fields.items()}

    def rev_schemes(self) -> dict:
        return {v: k for k, v in self.schemes.items()}


def load_dialect(dialect_id: str) -> Dialect:
    from ..config import load_dialect as _load
    return Dialect.from_dict(_load(dialect_id))


# ================================================================== render

def _term_atoms(term: Term, d: Dialect) -> list[str]:
    atoms = []
    for f in term.fields:
        if f not in d.fields:
            raise RenderError(f"方言 {d.id} はフィールド {f} に対応していません")
        body = f"{d.quote}{term.text}{d.quote}"
        tag = d.fields[f]
        atoms.append(f"{tag}{d.field_sep}{body}" if d.field_position == "prefix"
                     else f"{body}{d.field_sep}{tag}")
    return atoms


def _code_atom(code: Code, d: Dialect) -> str:
    if code.scheme not in d.schemes:
        raise RenderError(f"方言 {d.id} は分類体系 {code.scheme} に対応していません")
    fmt = d.code_format.get(code.scheme, "{code}")
    body = fmt.format(code=code.code)
    tag = d.schemes[code.scheme]
    return f"{tag}{d.field_sep}{body}" if d.field_position == "prefix" else f"{body}{d.field_sep}{tag}"


def render_block(block: Block, d: Dialect) -> str:
    t_atoms = [a for t in block.adopted_terms() for a in _term_atoms(t, d)]
    c_atoms = [_code_atom(c, d) for c in block.adopted_codes()]
    if not t_atoms and not c_atoms:
        raise RenderError(f"Block {block.axis_id} に採用済みの語・コードがありません")
    if t_atoms and c_atoms and block.term_code_join == "AND":
        inner = (f"{d.group_open}{d.or_.join(t_atoms)}{d.group_close}{d.and_}"
                 f"{d.group_open}{d.or_.join(c_atoms)}{d.group_close}")
    else:
        inner = d.or_.join(t_atoms + c_atoms)
    return f"{d.block_open}{inner}{d.block_close}"


def render(query: Query, d: Dialect) -> str:
    blocks = query.active_blocks()
    if not blocks:
        raise RenderError("採用済みの Block がありません")
    text = d.and_.join(render_block(b, d) for b in blocks)
    for ex in query.active_exclusions():
        text += f"{d.not_}{render_block(ex, d)}"
    return text


def _adopted_only(query: Query) -> Query:
    q = query.copy()
    for b in q.blocks:
        b.terms = b.adopted_terms()
        b.codes = b.adopted_codes()
    for b in q.exclusions:
        b.terms = b.adopted_terms()
        b.codes = b.adopted_codes()
    q.blocks = [b for b in q.blocks if not b.is_empty()]
    q.exclusions = [b for b in q.exclusions if not b.is_empty()]
    return q


def split_query(query: Query, d: Dialect, max_chars: int | None = None) -> list[Query]:
    """文字数上限を超える式を、結果の和集合が元と一致するように分割する。"""
    limit = max_chars or d.max_chars
    q = _adopted_only(query)
    if len(render(q, d)) <= limit:
        return [q]
    blocks = q.active_blocks()
    target = max(blocks, key=lambda b: (len(b.adopted_terms()) + len(b.adopted_codes())))
    terms, codes = target.adopted_terms(), target.adopted_codes()
    parts: list[Query] = []
    if len(terms) >= 2:
        h = len(terms) // 2
        first, second = q.copy(), q.copy()
        fb, sb = first.block(target.axis_id), second.block(target.axis_id)
        fb.terms = terms[:h]
        sb.terms = terms[h:]
        if target.effective_join() == "OR":
            sb.codes = []
        parts = [first, second]
    elif len(codes) >= 2 and target.effective_join() == "OR":
        h = len(codes) // 2
        first, second = q.copy(), q.copy()
        fb, sb = first.block(target.axis_id), second.block(target.axis_id)
        fb.codes = codes[:h]
        sb.terms, sb.codes = [], codes[h:]
        parts = [first, second]
    else:
        return [q]   # これ以上分割できない（呼び出し側で too_long を判定）
    out: list[Query] = []
    for p in parts:
        out.extend(split_query(p, d, limit))
    return out


def render_parts(query: Query, d: Dialect) -> list[dict]:
    """[{part_no, text, chars, too_long}] を返す。"""
    parts = []
    for i, q in enumerate(split_query(query, d), start=1):
        text = render(q, d)
        parts.append({"part_no": i, "text": text, "chars": len(text),
                      "too_long": len(text) > d.max_chars})
    return parts


# ================================================================== parse

class _Tok:
    __slots__ = ("kind", "value")

    def __init__(self, kind: str, value: str):
        self.kind, self.value = kind, value

    def __repr__(self) -> str:  # pragma: no cover
        return f"{self.kind}:{self.value}"


def _tokenize(text: str, d: Dialect) -> list[_Tok]:
    ops = {
        d.and_.strip(): "AND", d.or_.strip(): "OR", d.not_.strip(): "NOT",
        d.block_open: "OPEN", d.block_close: "CLOSE", d.group_open: "OPEN", d.group_close: "CLOSE",
    }
    ops = {k: v for k, v in ops.items() if k}
    op_syms = sorted(ops, key=len, reverse=True)
    toks: list[_Tok] = []
    i, n = 0, len(text)

    def is_alpha_sym(s: str) -> bool:
        return s.isalpha()

    def op_at(pos: int, prev: _Tok | None) -> tuple[str, str] | None:
        for sym in op_syms:
            if not text.startswith(sym, pos):
                continue
            kind = ops[sym]
            if is_alpha_sym(sym):
                end = pos + len(sym)
                before_ok = pos == 0 or not text[pos - 1].isalnum()
                after_ok = end >= n or not text[end].isalnum()
                if not (before_ok and after_ok):
                    continue
            if kind == "NOT" and not is_alpha_sym(sym):
                # 記号の NOT（例: "-"）は閉じ括弧の直後だけ演算子として扱う（語中のハイフンを守る）
                if prev is None or prev.kind != "CLOSE":
                    continue
            return sym, kind
        return None

    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        op = op_at(i, toks[-1] if toks else None)
        if op:
            toks.append(_Tok(op[1], op[0]))
            i += len(op[0])
            continue
        # アトム: 引用符内は丸ごと、以外は空白・演算子まで
        start = i
        buf = []
        while i < n:
            c = text[i]
            if d.quote and c == d.quote:
                j = text.find(d.quote, i + 1)
                if j == -1:
                    raise ParseError(f"引用符が閉じていません: 位置 {i}")
                buf.append(text[i:j + 1])
                i = j + 1
                continue
            if c.isspace() and d.quote:
                break
            op2 = op_at(i, None)
            if op2 and op2[1] != "NOT":
                break
            buf.append(c)
            i += 1
        atom = "".join(buf).strip()
        if not atom:
            raise ParseError(f"解析できない文字です: 位置 {start} '{text[start]}'")
        toks.append(_Tok("ATOM", atom))
    return toks


class _Parser:
    def __init__(self, toks: list[_Tok]):
        self.toks, self.pos = toks, 0

    def peek(self) -> _Tok | None:
        return self.toks[self.pos] if self.pos < len(self.toks) else None

    def take(self, kind: str | None = None) -> _Tok:
        tok = self.peek()
        if tok is None or (kind and tok.kind != kind):
            raise ParseError(f"予期しないトークン: {tok!r}（期待: {kind}）")
        self.pos += 1
        return tok

    # top := or_expr (NOT or_expr)*
    def parse_top(self):
        base = self.parse_or()
        excl = []
        while self.peek() and self.peek().kind == "NOT":
            self.take("NOT")
            excl.append(self.parse_or())
        if self.peek() is not None:
            raise ParseError(f"末尾に余分なトークンがあります: {self.peek()!r}")
        return base, excl

    def parse_or(self):
        items = [self.parse_and()]
        while self.peek() and self.peek().kind == "OR":
            self.take("OR")
            items.append(self.parse_and())
        return items[0] if len(items) == 1 else ("OR", _flatten("OR", items))

    def parse_and(self):
        items = [self.parse_primary()]
        while self.peek() and self.peek().kind == "AND":
            self.take("AND")
            items.append(self.parse_primary())
        return items[0] if len(items) == 1 else ("AND", _flatten("AND", items))

    def parse_primary(self):
        tok = self.peek()
        if tok is None:
            raise ParseError("式が途中で終わっています")
        if tok.kind == "OPEN":
            self.take("OPEN")
            node = self.parse_or()
            self.take("CLOSE")
            return ("GROUP", node)
        if tok.kind == "ATOM":
            self.take("ATOM")
            return ("ATOM", tok.value)
        raise ParseError(f"予期しないトークン: {tok!r}")


def _flatten(kind: str, items: list) -> list:
    out = []
    for it in items:
        if isinstance(it, tuple) and it[0] == kind:
            out.extend(it[1])
        else:
            out.append(it)
    return out


def _strip_group(node):
    while isinstance(node, tuple) and node[0] == "GROUP":
        node = node[1]
    return node


def _classify_atom(atom: str, d: Dialect) -> tuple[str, str, str]:
    """→ ("term", text, field) | ("code", code, scheme)"""
    rf, rs = d.rev_fields(), d.rev_schemes()
    if d.field_position == "prefix":
        if d.field_sep not in atom:
            raise ParseError(f"フィールド指定がありません: {atom}")
        tag, body = atom.split(d.field_sep, 1)
    else:
        if d.field_sep not in atom:
            raise ParseError(f"フィールド指定がありません: {atom}")
        body, tag = atom.rsplit(d.field_sep, 1)
    if d.quote and len(body) >= 2 and body[0] == d.quote and body[-1] == d.quote:
        body = body[1:-1]
    if tag in rs:
        return ("code", body, rs[tag])
    if tag in rf:
        return ("term", body, rf[tag])
    raise ParseError(f"未知のフィールド／体系です: {tag}（{atom}）")


def _atoms_of(node, d: Dialect) -> list[tuple[str, str, str]]:
    node = _strip_group(node)
    if isinstance(node, tuple) and node[0] == "ATOM":
        return [_classify_atom(node[1], d)]
    if isinstance(node, tuple) and node[0] == "OR":
        out = []
        for it in node[1]:
            out.extend(_atoms_of(it, d))
        return out
    raise ParseError("Block 内の構造が対応外です（OR で結ばれた語・コードの列を想定）")


def _make_block(node, axis_id: str, d: Dialect) -> Block:
    node = _strip_group(node)
    join = "OR"
    if isinstance(node, tuple) and node[0] == "AND":
        if len(node[1]) != 2:
            raise ParseError("Block 内の AND は（語群）AND（コード群）の 2 項のみ対応")
        left, right = _atoms_of(node[1][0], d), _atoms_of(node[1][1], d)
        kinds_l, kinds_r = {a[0] for a in left}, {a[0] for a in right}
        if kinds_l == {"term"} and kinds_r == {"code"} or kinds_l == {"code"} and kinds_r == {"term"}:
            atoms = left + right
            join = "AND"
        else:
            raise ParseError("Block 内の AND は語群とコード群の組み合わせのみ対応")
    else:
        atoms = _atoms_of(node, d)
    block = Block(axis_id=axis_id, axis_name="", required=True, term_code_join=join)
    terms: dict[str, Term] = {}
    for kind, body, tag in atoms:
        if kind == "term":
            if body in terms:
                if tag not in terms[body].fields:
                    terms[body].fields.append(tag)
                    terms[body].fields = [f for f in FIELDS if f in terms[body].fields]
            else:
                terms[body] = Term(text=body, fields=[tag], origin="parsed", status="adopted")
        else:
            block.codes.append(Code(scheme=tag, code=body, origin="parsed", status="adopted"))
    block.terms = list(terms.values())
    return block


def parse(text: str, d: Dialect) -> Query:
    toks = _tokenize(text.strip(), d)
    if not toks:
        raise ParseError("空の式です")
    base, excl = _Parser(toks).parse_top()
    # 最上位の AND は Block 間の AND。括弧で包まれた 1 つの Block（内側に語群 AND コード群を持つ）は
    # 剥がさずに _make_block へ渡す（(( … ) AND ( … )) と ( … ) AND ( … ) を区別する）
    block_nodes = base[1] if isinstance(base, tuple) and base[0] == "AND" else [base]
    q = Query()
    for i, node in enumerate(block_nodes):
        q.blocks.append(_make_block(node, _axis_label(i), d))
    for j, node in enumerate(excl):
        q.exclusions.append(_make_block(node, f"X{j + 1}", d))
    return q


def _axis_label(i: int) -> str:
    label = ""
    i += 1
    while i > 0:
        i, rem = divmod(i - 1, 26)
        label = chr(65 + rem) + label
    return label


def roundtrip_ok(query: Query, d: Dialect) -> bool:
    try:
        return parse(render(query, d), d).signature() == query.signature()
    except (ParseError, RenderError):
        return False
