"""Read the text a `.tsx` page would actually RENDER, straight out of the source.

WHY THIS MODULE EXISTS
----------------------
A per-page pin test (for the since-removed FrontierPage) once caught a class of defect
no other test in this repo structurally can: a number **typed into JSX** that agrees with
no artifact, no API response and no version of either.

Two callers need the same JSX-to-text step:

* the per-page PIN tests, which read one anchored element and compare it to an artifact;
* ``test_pages_do_not_publish_unverified_numbers.py``, the class-level guard, which walks
  EVERY text node of EVERY page and demands that each rendered number be either pinned or
  explicitly allowlisted.

So the extraction lives here once, and both import it. Nothing in this module knows about
artifacts, pins or allowlists — it only answers "what would a browser put on screen?"

WHAT COUNTS AS RENDERED TEXT
----------------------------
Only JSX **text nodes** — the characters between an element's `>` and the next `<` or `{`.
Everything else in a `.tsx` file reaches the screen through a value, not as a literal, and
is deliberately excluded:

* JS/TS statements, imports, type declarations, chart-config constants;
* `//`, `/* */` and `{/* */}` comments;
* every attribute — `className`, `style`, `data-*`, `key`, `aria-*`, SVG `d`/`viewBox`/
  coordinates, `stroke-width`, and so on;
* `{...}` expression containers, whose numbers come from props, state or the API and
  therefore cannot drift away from the backend the way a literal can. JSX *nested inside*
  such an expression (ternaries, `.map()` bodies) IS descended into, because its text
  nodes do render.

THE TWO PARSING TRAPS THIS FILE HANDLES
---------------------------------------
Both were found by running the scanner against this repo and watching it emit source code
as if it were prose:

1. **Regex literals.** `text.split(/(\\`[^\\`]+\\`)/g)` in `BenchmarkPage.tsx` and
   ``.replace(/\\`([^\\`]*)\\`/g, '$1')`` in `ModelCardPage.tsx` contain backticks. Treating
   them as division made the scanner open a template literal and swallow the rest of the
   file: BenchmarkPage reported 12 text nodes instead of 371, ModelCardPage reported 0.
   A check that silently sees nothing is worse than no check, so `/` is now resolved to
   regex-or-division from the preceding token.

2. **The token before `<` must ignore comments.** `BenchmarkPage.tsx` puts a four-line
   `//` comment between `return (` and its `<p>`. Looking backwards at raw characters
   found `.` (the end of the comment's last sentence) and refused to call the `<p` JSX, so
   an entire component's prose vanished. The scanner therefore tracks the last SIGNIFICANT
   token as it goes forward, and comments never update it.

`<` is JSX only in expression position; after a value (identifier, number, string, `)`,
`]`) it is a comparison or a TypeScript generic — `useState<Foo | null>`, `Record<string,
number>`, `n < 3`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: Characters that may appear in a JS identifier.
_IDENT = re.compile(r"[A-Za-z0-9_$]")
_IDENT_RUN = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_TAG_NAME = re.compile(r"[A-Za-z_$][\w.$:-]*")

#: Keywords after which a value cannot have just ended, so `<` starts JSX and `/` starts
#: a regex. `return <div>` and `return /re/.test(x)` are the ones that matter here.
_EXPR_KEYWORDS = frozenset(
    {
        "return", "case", "yield", "await", "default", "else", "do", "in", "of",
        "typeof", "throw", "new", "delete", "void", "instanceof",
    }
)

#: Sentinel for "the last token was a VALUE" (identifier, number, string, `)`, `]`).
_VALUE = "\x00"

_ENTITIES = {
    "&nbsp;": " ", "&mdash;": "—", "&ndash;": "–", "&amp;": "&", "&lt;": "<",
    "&gt;": ">", "&quot;": '"', "&apos;": "'", "&rsquo;": "’", "&lsquo;": "‘",
    "&rdquo;": "”", "&ldquo;": "“", "&times;": "×", "&minus;": "−",
    "&Delta;": "Δ", "&delta;": "δ", "&hellip;": "…", "&middot;": "·", "&deg;": "°",
    "&plusmn;": "±", "&asymp;": "≈", "&le;": "≤", "&ge;": "≥", "&ne;": "≠",
    "&sup2;": "²", "&frac12;": "½", "&larr;": "←", "&rarr;": "→", "&zwnj;": "",
    "&shy;": "", "&#8203;": "",
}


@dataclass(frozen=True)
class JsxText:
    """One JSX text node: what it says, where, and what anchors it sits inside."""

    text: str
    line: int
    #: `data-testid` values of every enclosing element, outermost first.
    testids: tuple[str, ...]

    @property
    def rendered(self) -> str:
        return to_rendered_text(self.text)


# ── The scanner ──────────────────────────────────────────────────────────────


class _Scanner:
    def __init__(self, src: str) -> None:
        self.s = src
        self.n = len(src)
        self.i = 0
        self.nodes: list[JsxText] = []

    def _line_of(self, idx: int) -> int:
        return self.s.count("\n", 0, idx) + 1

    # -- literal skippers ----------------------------------------------------

    def _skip_string(self, quote: str) -> None:
        self.i += 1
        while self.i < self.n:
            c = self.s[self.i]
            if c == "\\":
                self.i += 2
                continue
            if c == quote:
                self.i += 1
                return
            self.i += 1

    def _skip_template(self) -> None:
        self.i += 1
        while self.i < self.n:
            c = self.s[self.i]
            if c == "\\":
                self.i += 2
                continue
            if c == "`":
                self.i += 1
                return
            if c == "$" and self.s[self.i + 1 : self.i + 2] == "{":
                self.i += 2
                self._code(stop="}", opener="{")
                continue
            self.i += 1

    def _skip_comment(self) -> bool:
        two = self.s[self.i : self.i + 2]
        if two == "//":
            nl = self.s.find("\n", self.i)
            self.i = self.n if nl == -1 else nl
            return True
        if two == "/*":
            end = self.s.find("*/", self.i + 2)
            self.i = self.n if end == -1 else end + 2
            return True
        return False

    def _skip_regex(self) -> None:
        """Consume a `/.../flags` literal. Bails at a newline rather than running away."""
        self.i += 1
        in_class = False
        while self.i < self.n:
            c = self.s[self.i]
            if c == "\\":
                self.i += 2
                continue
            if c == "\n":
                return
            if c == "[":
                in_class = True
            elif c == "]":
                in_class = False
            elif c == "/" and not in_class:
                self.i += 1
                while self.i < self.n and self.s[self.i] in "dgimsuvy":
                    self.i += 1
                return
            self.i += 1

    # -- code mode -----------------------------------------------------------

    def _code(self, stop: str | None, opener: str = "") -> None:
        """Scan JS/TS until the unmatched `stop` bracket (consumed) or EOF.

        `last` is the previous SIGNIFICANT token — never a comment. It decides whether
        `<` opens JSX and whether `/` opens a regex; both are only possible where a value
        may begin, i.e. NOT straight after another value.
        """
        depth = 0
        last = opener  # "{" for an expression container: a value may begin there.
        last_word = ""
        while self.i < self.n:
            c = self.s[self.i]
            if c.isspace():
                self.i += 1
                continue
            if c == "/" and self._skip_comment():
                continue  # a comment is not a token: `last` stands.
            if c == "/":
                if self._value_may_begin(last, last_word):
                    self._skip_regex()
                else:
                    self.i += 1
                last, last_word = "/", ""
                continue
            if c in "\"'":
                self._skip_string(c)
                last, last_word = _VALUE, ""
                continue
            if c == "`":
                self._skip_template()
                last, last_word = _VALUE, ""
                continue
            if c in "{([":
                depth += 1
                self.i += 1
                last, last_word = c, ""
                continue
            if c in "})]":
                if depth == 0 and stop is not None and c == stop:
                    self.i += 1
                    return
                depth -= 1
                self.i += 1
                # `)` and `]` end a value; `}` ends a block or an object literal.
                last, last_word = (_VALUE if c in ")]" else c), ""
                continue
            if c == "<" and self._is_jsx_start(last, last_word):
                self._jsx_element(())
                last, last_word = _VALUE, ""
                continue
            m = _IDENT_RUN.match(self.s, self.i)
            if m:
                self.i = m.end()
                last_word = m.group(0)
                last = _VALUE if last_word not in _EXPR_KEYWORDS else last_word
                continue
            if c.isdigit():
                while self.i < self.n and _IDENT.match(self.s[self.i]) or (
                    self.i < self.n and self.s[self.i] == "."
                ):
                    self.i += 1
                last, last_word = _VALUE, ""
                continue
            self.i += 1
            last, last_word = c, ""

    @staticmethod
    def _value_may_begin(last: str, last_word: str) -> bool:
        if last is _VALUE:
            return False
        if last == "" or last in "(,=:[!&|?{};+-*%<>~^/":
            return True
        return last_word in _EXPR_KEYWORDS

    def _is_jsx_start(self, last: str, last_word: str) -> bool:
        nxt = self.s[self.i + 1 : self.i + 2]
        if not (nxt == ">" or nxt == "/" or (nxt[:1].isalpha() or nxt in "_$")):
            return False
        return self._value_may_begin(last, last_word)

    # -- jsx mode ------------------------------------------------------------

    def _jsx_element(self, testids: tuple[str, ...]) -> None:
        """Consume one JSX element (open tag, children, close tag) from `self.i`."""
        self.i += 1  # past "<"
        m = _TAG_NAME.match(self.s, self.i)
        if m:
            self.i = m.end()  # a missing name is the `<>` fragment
        testid: str | None = None

        while self.i < self.n:  # attributes
            c = self.s[self.i]
            if c == "/" and self.s[self.i + 1 : self.i + 2] == ">":
                self.i += 2
                return  # self-closing: renders no text of its own
            if c == "/" and self._skip_comment():
                continue
            if c == ">":
                self.i += 1
                break
            if c in "\"'":
                start = self.i
                self._skip_string(c)
                if re.search(r"data-testid\s*=\s*$", self.s[max(0, start - 16) : start]):
                    testid = self.s[start + 1 : self.i - 1]
                continue
            if c == "{":
                self.i += 1
                self._code(stop="}", opener="{")
                continue
            self.i += 1
        else:  # pragma: no cover - only reachable on a truncated file
            return

        stack = testids + ((testid,) if testid else ())
        buf = self.i
        while self.i < self.n:  # children
            c = self.s[self.i]
            if c == "<":
                self._emit(buf, self.i, stack)
                if self.s[self.i + 1 : self.i + 2] == "/":
                    close = self.s.find(">", self.i)
                    self.i = self.n if close == -1 else close + 1
                    return
                self._jsx_element(stack)
                buf = self.i
                continue
            if c == "{":
                self._emit(buf, self.i, stack)
                self.i += 1
                self._code(stop="}", opener="{")
                buf = self.i
                continue
            self.i += 1
        self._emit(buf, self.i, stack)  # pragma: no cover - truncated file

    def _emit(self, a: int, b: int, stack: tuple[str, ...]) -> None:
        raw = self.s[a:b]
        if raw.strip():
            self.nodes.append(JsxText(raw, self._line_of(a), stack))


# ── Public helpers ───────────────────────────────────────────────────────────


def text_nodes(source: str) -> list[JsxText]:
    """Every JSX text node in `source`, in document order."""
    sc = _Scanner(source)
    sc._code(stop=None)
    return sc.nodes


def decode_entities(text: str) -> str:
    """Resolve the HTML entities these pages actually use, plus numeric ones.

    Numeric entities matter for the number guard: `&#8203;` is a zero-width space, and
    left alone its digits would read as a published figure.
    """
    for name, char in _ENTITIES.items():
        text = text.replace(name, char)
    text = re.sub(r"&#x([0-9A-Fa-f]+);", lambda m: chr(int(m.group(1), 16)), text)
    return re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), text)


def to_rendered_text(body: str) -> str:
    """Collapse a chunk of JSX to the plain text a browser would show."""
    text = re.sub(r"\{\s*['\"]\s*['\"]\s*\}", " ", body)  # {' '} spacers
    text = re.sub(r"<[^>]*>", " ", text)  # tags
    return re.sub(r"\s+", " ", decode_entities(text)).strip()


def element_body(source: str, testid: str, *, where: str = "the page") -> str:
    """The raw JSX between the open/close tags of the element carrying `testid`.

    Walks the tag stack for that element's own tag name, so a nested element of the same
    kind cannot truncate the body. Raises rather than skipping when the testid is gone —
    a deleted anchor must fail loudly, never quietly pass.
    """
    marker = f'data-testid="{testid}"'
    idx = source.find(marker)
    assert idx != -1, (
        f"{where} no longer contains data-testid={testid!r}. A published number is "
        f"pinned to an artifact through that anchor; if the element was renamed, update "
        f"the anchor rather than dropping the pin."
    )
    open_start = source.rfind("<", 0, idx)
    tag_match = re.match(r"<([A-Za-z][A-Za-z0-9.]*)", source[open_start:])
    assert tag_match is not None, f"could not find the opening tag for {testid!r}"
    tag = tag_match.group(1)

    open_end = source.index(">", idx)
    assert source[open_end - 1] != "/", f"{testid!r} is on a self-closing tag; nothing to read"

    tag_re = re.compile(rf"<(/?){re.escape(tag)}(?=[\s/>])")
    depth = 1
    pos = open_end + 1
    body_start = pos
    while True:
        m = tag_re.search(source, pos)
        assert m is not None, f"unbalanced <{tag}> while reading {testid!r}"
        if m.group(1) == "/":
            depth -= 1
            if depth == 0:
                return source[body_start : m.start()]
        else:
            close = source.index(">", m.end())
            if source[close - 1] != "/":  # an immediately self-closed tag does not nest
                depth += 1
        pos = m.end()


def rendered(source: str, testid: str, *, where: str = "the page") -> str:
    """The plain text a browser shows for the element carrying `testid`."""
    return to_rendered_text(element_body(source, testid, where=where))


def strip_comments(source: str) -> str:
    """Drop `/* */` and `//` comments — useful for "this literal must be gone" checks."""
    code = re.sub(r"/\*.*?\*/", " ", source, flags=re.DOTALL)
    return re.sub(r"^\s*//.*$", " ", code, flags=re.MULTILINE)
