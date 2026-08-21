"""Lucene-style query language over the in-memory log records.

The records are flat dicts, so a query is just parse -> AST -> predicate. No
index and no scoring exists here, which is the one part of Lucene that does not
carry over: ``^boost`` and ``~fuzzy`` are relevance features and are rejected
with an explanatory error rather than silently ignored.

Supported
---------
``level:WARN``                 field term (facet fields match exactly)
``message:timeout``            text fields match on substring
``"connection refused"``       quoted phrase (no wildcard interpretation)
``k8s_pod:pu-epa-*-ram-*``     ``*`` and ``?`` wildcards
``logger:/Get.*Service/``      regular expression
``req_duration_ms:[100 TO 500]``   inclusive range (``{}`` exclusive, mixable)
``req_duration_ms:>=100``      open-ended comparison
``time:[2026-07-09T08:00:00Z TO *]``  time range, ``*`` = unbounded
``level:(WARN OR ERROR)``      field-scoped group
``a AND b``, ``a OR b``, ``NOT a``, ``&&``, ``||``, ``!``, ``+a -b``
``404``                        bare term: matches any column
Implicit operator between clauses is AND, as in Kibana.

Public API
----------
``compile_query(text, columns) -> Predicate | None``
    ``columns`` is the ``columns.json`` metadata list; it decides whether a
    field compares as a number, a timestamp, an exact keyword or a substring.
    Returns ``None`` for an empty query. Raises ``LuceneError`` on bad syntax,
    an unknown field, or an unsupported Lucene feature.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping, Sequence

__all__ = ["LuceneError", "compile_query", "parse", "field_names"]

Predicate = Callable[[Mapping[str, Any]], bool]


class LuceneError(ValueError):
    """Bad query: syntax, unknown field, or an unsupported Lucene feature.

    ``pos`` is the offset into the query text, so the UI can point at it.
    """

    def __init__(self, message: str, pos: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.pos = pos


# --------------------------------------------------------------------------- #
# tokenizer
# --------------------------------------------------------------------------- #

# '-' and '+' are NOT special: they occur inside real values all the time
# (pod names, ISO timestamps). They become operators only at a clause start,
# which _prefix_op below decides from the surrounding whitespace.
# ':' is handled by the field/value rule in the word scanner, not here
_SPECIALS = set('()[]{}"/<>=')
_KEYWORDS = {"AND", "OR", "NOT", "TO"}

#: after these tokens a word is a *value*, so it may contain ':' (timestamps,
#: host:port). Anywhere else the first ':' splits ``field:value``.
_VALUE_CONTEXT = {":", "[", "{", ">", ">=", "<", "<="}


@dataclass
class Token:
    kind: str      # word | quoted | regex | punct | eof
    text: str
    pos: int
    quoted: bool = False


def _tokenize(text: str) -> list[Token]:
    out: list[Token] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue

        if ch == '"':
            j, buf = i + 1, []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                    continue
                buf.append(text[j])
                j += 1
            if j >= n:
                raise LuceneError("unterminated quoted string", i)
            out.append(Token("quoted", "".join(buf), i, quoted=True))
            i = j + 1
            continue

        if ch == "/":
            j, buf = i + 1, []
            while j < n and text[j] != "/":
                if text[j] == "\\" and j + 1 < n:
                    buf.append(text[j:j + 2])
                    j += 2
                    continue
                buf.append(text[j])
                j += 1
            if j >= n:
                raise LuceneError("unterminated regular expression", i)
            out.append(Token("regex", "".join(buf), i))
            i = j + 1
            continue

        if ch in "<>=":
            op = ch
            if i + 1 < n and text[i + 1] == "=":
                op += "="
                i += 1
            out.append(Token("punct", op, i))
            i += 1
            continue

        if ch in "()[]{}:":
            out.append(Token("punct", ch, i))
            i += 1
            continue

        # '+' '-' '!' are operators only when they open a clause: preceded by
        # whitespace/nothing/an opening token and followed by a non-space.
        if ch in "+-!":
            at_start = i == 0 or text[i - 1].isspace()
            prev_opens = bool(out) and out[-1].kind == "punct" and out[-1].text in ("(", ":")
            attaches = i + 1 < n and not text[i + 1].isspace()
            if (at_start or prev_opens) and attaches:
                out.append(Token("punct", ch, i))
                i += 1
                continue

        value_ctx = bool(out) and (
            (out[-1].kind == "punct" and out[-1].text in _VALUE_CONTEXT)
            or (out[-1].kind == "word" and out[-1].text == "TO")
        )
        j, buf = i, []
        while j < n and not text[j].isspace() and text[j] not in _SPECIALS:
            if text[j] == "\\" and j + 1 < n:
                buf.append(text[j + 1])
                j += 2
                continue
            if text[j] == ":" and not value_ctx:
                break                      # the first ':' splits field from value
            buf.append(text[j])
            j += 1
        if j == i:      # a special char in a spot where a word was expected
            buf.append(text[j])
            j += 1
        out.append(Token("word", "".join(buf), i))
        i = j

    out.append(Token("eof", "", len(text)))
    return out


# --------------------------------------------------------------------------- #
# AST
# --------------------------------------------------------------------------- #


@dataclass
class Term:
    """A value to match: bare word, quoted phrase, wildcard, or regex."""
    value: str
    quoted: bool = False
    regex: bool = False
    pos: int = 0


@dataclass
class Range:
    lo: str | None          # None = unbounded
    hi: str | None
    lo_incl: bool = True
    hi_incl: bool = True
    pos: int = 0


@dataclass
class Field:
    name: str
    node: Any               # Term | Range | Bool
    pos: int = 0


@dataclass
class Bool:
    op: str                 # and | or
    clauses: list[Any]


@dataclass
class Not:
    node: Any


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #


class _Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self.toks = tokens
        self.i = 0

    def peek(self) -> Token:
        return self.toks[self.i]

    def next(self) -> Token:
        tok = self.toks[self.i]
        if tok.kind != "eof":
            self.i += 1
        return tok

    def accept_word(self, word: str) -> bool:
        tok = self.peek()
        if tok.kind == "word" and not tok.quoted and tok.text == word:
            self.next()
            return True
        return False

    def accept_punct(self, ch: str) -> bool:
        tok = self.peek()
        if tok.kind == "punct" and tok.text == ch:
            self.next()
            return True
        return False

    # query := or_expr
    def parse(self) -> Any:
        node = self.parse_or()
        tok = self.peek()
        if tok.kind != "eof":
            raise LuceneError(f"unexpected {tok.text!r}", tok.pos)
        return node

    def parse_or(self) -> Any:
        clauses = [self.parse_and()]
        while True:
            if self.accept_word("OR") or self.accept_punct("|") or self.accept_word("||"):
                clauses.append(self.parse_and())
            else:
                break
        return clauses[0] if len(clauses) == 1 else Bool("or", clauses)

    def parse_and(self) -> Any:
        clauses = [self.parse_unary()]
        while True:
            tok = self.peek()
            if tok.kind == "eof":
                break
            if tok.kind == "word" and tok.text in ("OR", "||") and not tok.quoted:
                break
            if tok.kind == "punct" and tok.text == ")":
                break
            self.accept_word("AND") or self.accept_word("&&")
            clauses.append(self.parse_unary())
        return clauses[0] if len(clauses) == 1 else Bool("and", clauses)

    def parse_unary(self) -> Any:
        tok = self.peek()
        if (tok.kind == "word" and tok.text == "NOT" and not tok.quoted) or (
            tok.kind == "punct" and tok.text in ("!", "-")
        ):
            self.next()
            return Not(self.parse_unary())
        if tok.kind == "punct" and tok.text == "+":
            self.next()          # "required" is the default already
            return self.parse_unary()
        return self.parse_primary()

    def parse_primary(self) -> Any:
        tok = self.next()

        if tok.kind == "punct" and tok.text == "(":
            node = self.parse_or()
            if not self.accept_punct(")"):
                raise LuceneError("missing ')'", self.peek().pos)
            return node

        if tok.kind == "punct" and tok.text in ("[", "{"):
            return self.parse_range(tok)

        if tok.kind == "punct" and tok.text in (">", ">=", "<", "<="):
            return self.parse_open_range(tok)

        if tok.kind in ("word", "quoted", "regex"):
            self._reject_unsupported(tok)
            # field:...
            if tok.kind == "word" and self.peek().kind == "punct" and self.peek().text == ":":
                self.next()
                return Field(tok.text, self.parse_field_value(tok), tok.pos)
            return Term(tok.text, quoted=tok.quoted, regex=(tok.kind == "regex"), pos=tok.pos)

        if tok.kind == "eof":
            raise LuceneError("query ends unexpectedly", tok.pos)
        raise LuceneError(f"unexpected {tok.text!r}", tok.pos)

    def parse_field_value(self, field_tok: Token) -> Any:
        tok = self.peek()
        if tok.kind == "punct" and tok.text == "(":
            self.next()
            node = self.parse_or()
            if not self.accept_punct(")"):
                raise LuceneError("missing ')'", self.peek().pos)
            return node
        if tok.kind == "punct" and tok.text in ("[", "{"):
            self.next()
            return self.parse_range(tok)
        if tok.kind == "punct" and tok.text in (">", ">=", "<", "<="):
            self.next()
            return self.parse_open_range(tok)
        if tok.kind in ("word", "quoted", "regex"):
            self.next()
            self._reject_unsupported(tok)
            return Term(tok.text, quoted=tok.quoted, regex=(tok.kind == "regex"), pos=tok.pos)
        raise LuceneError(f"missing value after {field_tok.text!r}:", tok.pos)

    def parse_range(self, open_tok: Token) -> Range:
        lo = self.next()
        if lo.kind not in ("word", "quoted"):
            raise LuceneError("bad range start", lo.pos)
        if not self.accept_word("TO"):
            raise LuceneError("range needs 'TO' (e.g. [100 TO 500])", self.peek().pos)
        hi = self.next()
        if hi.kind not in ("word", "quoted"):
            raise LuceneError("bad range end", hi.pos)
        close = self.next()
        if close.kind != "punct" or close.text not in ("]", "}"):
            raise LuceneError("range is not closed with ']' or '}'", close.pos)
        return Range(
            lo=None if lo.text == "*" else lo.text,
            hi=None if hi.text == "*" else hi.text,
            lo_incl=open_tok.text == "[",
            hi_incl=close.text == "]",
            pos=open_tok.pos,
        )

    def parse_open_range(self, op_tok: Token) -> Range:
        val = self.next()
        if val.kind not in ("word", "quoted"):
            raise LuceneError(f"missing value after {op_tok.text!r}", val.pos)
        if op_tok.text.startswith(">"):
            return Range(lo=val.text, hi=None, lo_incl="=" in op_tok.text, pos=op_tok.pos)
        return Range(lo=None, hi=val.text, hi_incl="=" in op_tok.text, pos=op_tok.pos)

    @staticmethod
    def _reject_unsupported(tok: Token) -> None:
        """Boost and fuzzy are scoring features; there is no ranking here."""
        if tok.quoted or tok.kind == "regex":
            return
        if "^" in tok.text:
            raise LuceneError(
                "'^' boost is not supported: results are not scored or ranked", tok.pos
            )
        if tok.text.endswith("~") or re.search(r"~\d*(\.\d+)?$", tok.text):
            raise LuceneError(
                "'~' fuzzy/proximity is not supported: there is no index to match against",
                tok.pos,
            )


def parse(text: str) -> Any | None:
    """Parse query text into an AST, or ``None`` when it is blank."""
    if not text or not text.strip():
        return None
    return _Parser(_tokenize(text)).parse()


# --------------------------------------------------------------------------- #
# compilation: AST -> predicate
# --------------------------------------------------------------------------- #


def field_names(columns: Sequence[Mapping[str, Any]] | None) -> list[str]:
    return [str(c["name"]) for c in (columns or []) if not str(c["name"]).startswith("_")]


def _kinds(columns: Sequence[Mapping[str, Any]] | None) -> dict[str, str]:
    return {str(c["name"]): str(c.get("kind") or "text") for c in (columns or [])}


def _wildcard_re(value: str) -> re.Pattern | None:
    """Compile ``*``/``?`` wildcards into an anchored regex, or None if plain."""
    if "*" not in value and "?" not in value:
        return None
    out = []
    for ch in value:
        if ch == "*":
            out.append(".*")
        elif ch == "?":
            out.append(".")
        else:
            out.append(re.escape(ch))
    return re.compile("^" + "".join(out) + "$", re.IGNORECASE)


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _as_time(value: Any, _parse_time: Callable[[Any], datetime | None]) -> datetime | None:
    return _parse_time(value)


def _term_matcher(term: Term, kind: str, parse_time: Callable[[Any], datetime | None]):
    """Return ``cell -> bool`` for one term against one field kind."""
    if term.regex:
        try:
            rx = re.compile(term.value, re.IGNORECASE)
        except re.error as exc:
            raise LuceneError(f"invalid regular expression: {exc}", term.pos) from exc
        return lambda cell: cell is not None and rx.search(str(cell)) is not None

    wild = None if term.quoted else _wildcard_re(term.value)
    if wild is not None:
        return lambda cell: cell is not None and wild.match(str(cell)) is not None

    needle = term.value.lower()

    if kind == "number":
        wanted = _as_number(term.value)
        if wanted is not None:
            def num(cell: Any) -> bool:
                got = _as_number(cell)
                return got is not None and got == wanted
            return num

    if kind == "time":
        # A partial timestamp means the period it names: "2026-07-09" is that
        # whole day, not midnight exactly. Only a value carrying a time of day
        # is compared as an instant.
        complete = ":" in term.value
        wanted_t = parse_time(term.value) if complete else None
        if wanted_t is not None:
            def tim(cell: Any) -> bool:
                got = parse_time(cell)
                return got is not None and got == wanted_t
            return tim
        return lambda cell: cell is not None and str(cell).lower().startswith(needle)

    if kind == "text":
        # free text matches on substring, like a analyzed Lucene text field
        return lambda cell: cell is not None and needle in str(cell).lower()

    # facet / keyword fields match the whole value
    return lambda cell: cell is not None and str(cell).lower() == needle


def _range_matcher(rng: Range, kind: str, parse_time: Callable[[Any], datetime | None]):
    if kind == "time":
        lo = parse_time(rng.lo) if rng.lo is not None else None
        hi = parse_time(rng.hi) if rng.hi is not None else None
        if rng.lo is not None and lo is None:
            raise LuceneError(f"not a timestamp: {rng.lo!r}", rng.pos)
        if rng.hi is not None and hi is None:
            raise LuceneError(f"not a timestamp: {rng.hi!r}", rng.pos)

        def tim(cell: Any) -> bool:
            got = parse_time(cell)
            if got is None:
                return False
            if lo is not None and (got < lo or (got == lo and not rng.lo_incl)):
                return False
            if hi is not None and (got > hi or (got == hi and not rng.hi_incl)):
                return False
            return True
        return tim

    lo_n = _as_number(rng.lo) if rng.lo is not None else None
    hi_n = _as_number(rng.hi) if rng.hi is not None else None
    numeric = (rng.lo is None or lo_n is not None) and (rng.hi is None or hi_n is not None)

    if numeric:
        def num(cell: Any) -> bool:
            got = _as_number(cell)
            if got is None:
                return False
            if lo_n is not None and (got < lo_n or (got == lo_n and not rng.lo_incl)):
                return False
            if hi_n is not None and (got > hi_n or (got == hi_n and not rng.hi_incl)):
                return False
            return True
        return num

    lo_s = rng.lo.lower() if rng.lo is not None else None
    hi_s = rng.hi.lower() if rng.hi is not None else None

    def txt(cell: Any) -> bool:
        if cell is None:
            return False
        got = str(cell).lower()
        if lo_s is not None and (got < lo_s or (got == lo_s and not rng.lo_incl)):
            return False
        if hi_s is not None and (got > hi_s or (got == hi_s and not rng.hi_incl)):
            return False
        return True
    return txt


def _suggest(name: str, known: Iterable[str]) -> str:
    import difflib
    near = difflib.get_close_matches(name, list(known), n=3, cutoff=0.6)
    return f" — did you mean {', '.join(near)}?" if near else ""


def compile_query(
    text: str,
    columns: Sequence[Mapping[str, Any]] | None = None,
    *,
    parse_time: Callable[[Any], datetime | None] | None = None,
    search_cols: Sequence[str] | None = None,
) -> Predicate | None:
    """Compile query text into ``record -> bool``, or ``None`` when blank.

    ``columns`` supplies each field's kind (facet/number/time/text); an unknown
    field name is an error, with a suggestion when one is close.
    """
    ast = parse(text)
    if ast is None:
        return None

    if parse_time is None:
        from cloudlogs.query import parse_time as _pt
        parse_time = _pt

    kinds = _kinds(columns)
    known = field_names(columns)
    free_cols = list(search_cols) if search_cols is not None else known

    def compile_node(node: Any, field: str | None) -> Predicate:
        if isinstance(node, Bool):
            preds = [compile_node(c, field) for c in node.clauses]
            if node.op == "and":
                return lambda rec: all(p(rec) for p in preds)
            return lambda rec: any(p(rec) for p in preds)

        if isinstance(node, Not):
            inner = compile_node(node.node, field)
            return lambda rec: not inner(rec)

        if isinstance(node, Field):
            if known and node.name not in kinds:
                raise LuceneError(
                    f"unknown field {node.name!r}" + _suggest(node.name, known), node.pos
                )
            return compile_node(node.node, node.name)

        if isinstance(node, Range):
            if field is None:
                raise LuceneError("a range needs a field (e.g. req_duration_ms:[100 TO 500])", node.pos)
            match = _range_matcher(node, kinds.get(field, "text"), parse_time)
            return lambda rec: match(rec.get(field))

        if isinstance(node, Term):
            if field is not None:
                match = _term_matcher(node, kinds.get(field, "text"), parse_time)
                return lambda rec: match(rec.get(field))
            # bare term: any column, always substring/wildcard over the text
            match_any = _term_matcher(node, "text", parse_time)
            cols = free_cols

            def anywhere(rec: Mapping[str, Any]) -> bool:
                if cols:
                    return any(match_any(rec.get(c)) for c in cols)
                return any(
                    match_any(v) for k, v in rec.items() if not str(k).startswith("_")
                )
            return anywhere

        raise LuceneError("could not compile the query")

    return compile_node(ast, None)
