"""A deliberately small, strict, single-document YAML dialect (stdlib only).

Only lowercase true/false/null and ~ resolve specially. Decimal integers have
no leading zeroes; decimal floats may have an exponent. In particular 010,
0x1F, .inf, True and NULL are strings, as required by orcexe's schema subset.
All document markers (including a leading ---) and multiline scalars/flows
are rejected. Keys must resolve to strings. Container depth is at most 64.
Indentation widths may differ between nesting levels, but siblings must align.
Decimal integers are limited to 4096 characters to bound conversion work.
"""

import re
from pathlib import Path


_MAX_BYTES = 1024 * 1024
_MAX_DEPTH = 64
_INT = re.compile(r"[+-]?(?:0|[1-9][0-9]*)\Z")
_FLOAT = re.compile(
    r"[+-]?(?:(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"
    r"|[0-9]+[eE][+-]?[0-9]+)\Z"
)


class MiniYamlError(ValueError):
    """An unsupported or malformed construct at a 1-based source position."""

    def __init__(self, message, line=1, col=1):
        self.line = line
        self.col = col
        super().__init__("{} (line {}, column {})".format(message, line, col))


def _fail(message, line, col):
    raise MiniYamlError(message, line, col)


def _resolve(value, line, col):
    if value in ("null", "~", ""):
        return None
    if value in ("true", "false"):
        return value == "true"
    if _INT.fullmatch(value):
        # Bound conversion even on Python versions without int's digit limit.
        if len(value) > 4096:
            _fail("decimal integer too long", line, col)
        return int(value)
    if _FLOAT.fullmatch(value):
        return float(value)
    return value


def _plain(value, line, col, flow=False):
    if value.startswith(("---", "...")) and (
        len(value) == 3 or value[3] in " \t"
    ):
        _fail("document marker", line, col)
    names = {"&": "anchor", "*": "alias", "!": "tag", "|": "block scalar",
             ">": "block scalar", "?": "complex key", "%": "directive"}
    if value and (value[0] in "?:,[]{}#&*!|>'\"%@`" or
                  value[0] == "-" and (len(value) == 1 or value[1] in " \t")):
        _fail(names.get(value[0], "plain scalar indicator"), line, col)
    for i, char in enumerate(value):
        if char == ":" and (i + 1 == len(value) or value[i + 1] in " \t"
                            or flow and value[i + 1] in "[],{}"):
            _fail("plain scalar colon", line, col + i)
        if flow and char in ",[]{}":
            _fail("plain scalar flow indicator", line, col + i)
    return _resolve(value, line, col)


def _scan(text):
    """Remove comments and locate the first block mapping separator in one pass.

    Quotes begin only at a token boundary, so apostrophes in don't and quotes
    inside plain strings do not hide comments or mapping separators.
    """
    quote = None
    token_start = True
    depth = 0
    colon = None
    quoted_end = False
    i = 0
    while i < len(text):
        char = text[i]
        if quote:
            if quote == '"' and char == "\\":
                i += 2
                continue
            if char == quote:
                if quote == "'" and i + 1 < len(text) and text[i + 1] == "'":
                    i += 2
                    continue
                quote = None
                quoted_end = True
            i += 1
            continue
        if char == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i].rstrip(" \t"), colon
        if char in "\"'" and token_start:
            quote = char
            token_start = False
        elif char in "[{" and (token_start or depth):
            depth += 1
            token_start = True
        elif char in "]}" and depth:
            depth -= 1
            token_start = False
        elif char == "," and depth:
            token_start = True
        elif char == ":" and (i + 1 == len(text) or text[i + 1] in " \t"
                               or depth and (text[i + 1] in "[],{}" or quoted_end)):
            if depth == 0 and colon is None:
                colon = i
            token_start = True
            quoted_end = False
        elif char == "-" and token_start and (i + 1 == len(text) or text[i + 1] in " \t"):
            token_start = True
        elif char not in " \t":
            token_start = False
            quoted_end = False
        i += 1
    return text.rstrip(" \t"), colon


class _Inline:
    def __init__(self, text, line, col):
        self.text, self.line, self.col = text, line, col
        self.pos = 0

    def error(self, message, pos=None):
        _fail(message, self.line, self.col + (self.pos if pos is None else pos))

    def space(self):
        while self.pos < len(self.text) and self.text[self.pos] in " \t":
            self.pos += 1

    def quoted(self):
        start = self.pos
        quote = self.text[self.pos]
        self.pos += 1
        result = []
        escapes = {"\\": "\\", '"': '"', "/": "/", "n": "\n", "t": "\t",
                   "r": "\r", "0": "\0"}
        while self.pos < len(self.text):
            char = self.text[self.pos]
            self.pos += 1
            if char == quote:
                if quote == "'" and self.pos < len(self.text) and self.text[self.pos] == "'":
                    result.append("'")
                    self.pos += 1
                    continue
                return "".join(result)
            if char == "\\" and quote == '"':
                escape_pos = self.pos - 1
                if self.pos == len(self.text):
                    self.error("unterminated quoted scalar", start)
                char = self.text[self.pos]
                self.pos += 1
                if char == "u":
                    digits = self.text[self.pos:self.pos + 4]
                    if len(digits) != 4 or any(c not in "0123456789abcdefABCDEF" for c in digits):
                        self.error("Unicode escape", escape_pos)
                    code = int(digits, 16)
                    if 0xD800 <= code <= 0xDFFF:
                        self.error("Unicode surrogate escape", escape_pos)
                    result.append(chr(code))
                    self.pos += 4
                elif char in escapes:
                    result.append(escapes[char])
                else:
                    self.error("unsupported quoted escape", escape_pos)
            else:
                result.append(char)
        self.error("unterminated quoted scalar", start)

    def key(self, key, pos):
        if not isinstance(key, str):
            self.error("non-string mapping key", pos)
        if key == "<<":
            self.error("merge key", pos)
        return key

    def value(self, depth, flow=False):
        self.space()
        start = self.pos
        if start == len(self.text):
            return None
        char = self.text[start]
        if char in "[{":
            if depth >= _MAX_DEPTH:
                self.error("nesting depth exceeds 64")
            return self.collection(depth + 1, char)
        if char in "\"'":
            return self.quoted()
        while self.pos < len(self.text):
            if flow and self.text[self.pos] in ",]}":
                break
            self.pos += 1
        raw = self.text[start:self.pos].rstrip(" \t")
        if not raw:
            self.error("empty flow item", start)
        return _plain(raw, self.line, self.col + start, flow)

    def collection(self, depth, opening):
        opening_pos = self.pos
        closing = "]" if opening == "[" else "}"
        result = [] if opening == "[" else {}
        self.pos += 1
        self.space()
        if self.pos < len(self.text) and self.text[self.pos] == closing:
            self.pos += 1
            return result
        while True:
            self.space()
            if self.pos == len(self.text):
                self.error("multiline or unterminated flow collection", opening_pos)
            start = self.pos
            if opening == "[":
                item = self.value(depth, True)
                if self.pos == start:
                    self.error("empty flow item")
                result.append(item)
            else:
                if self.pos < len(self.text) and self.text[self.pos] in "\"'":
                    key = self.quoted()
                else:
                    while self.pos < len(self.text):
                        char = self.text[self.pos]
                        if char in ",[]{}" or char == ":" and (
                            self.pos + 1 == len(self.text) or
                            self.text[self.pos + 1] in " \t" or
                            self.text[self.pos + 1] in "[],{}"
                        ):
                            break
                        self.pos += 1
                    raw = self.text[start:self.pos].rstrip(" \t")
                    if not raw:
                        self.error("empty or complex mapping key", start)
                    key = _plain(raw, self.line, self.col + start, True)
                key = self.key(key, start)
                self.space()
                if self.pos == len(self.text) or self.text[self.pos] != ":":
                    self.error("flow mapping separator", start)
                if key in result:
                    self.error("duplicate mapping key", start)
                self.pos += 1
                self.space()
                result[key] = (None if self.pos < len(self.text) and
                               self.text[self.pos] in ",}" else self.value(depth, True))
            self.space()
            if self.pos == len(self.text):
                self.error("multiline or unterminated flow collection", opening_pos)
            if self.text[self.pos] == closing:
                self.pos += 1
                return result
            if self.text[self.pos] != ",":
                self.error("flow collection separator or trailing content")
            self.pos += 1
            self.space()
            if self.pos < len(self.text) and self.text[self.pos] == closing:
                self.pos += 1
                return result

    def parse(self, depth):
        value = self.value(depth)
        self.space()
        if self.pos != len(self.text):
            self.error("trailing content after quoted scalar or flow collection")
        return value


class _Parser:
    def __init__(self, text):
        self.lines = []
        self.pos = 0
        for number, raw in enumerate(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"), 1):
            for i, char in enumerate(raw):
                code = ord(char)
                if (code < 32 and char != "\t" or 0x7F <= code <= 0x9F or
                        code in (0x2028, 0x2029, 0xFFFE, 0xFFFF) or
                        0xD800 <= code <= 0xDFFF):
                    _fail("unsupported control or Unicode character", number, i + 1)
            indent = len(raw) - len(raw.lstrip(" "))
            if indent < len(raw) and raw[indent] == "\t":
                _fail("tab in indentation", number, indent + 1)
            content, colon = _scan(raw[indent:])
            if content:
                if content in ("---", "...") or content.startswith(("--- ", "... ")):
                    _fail("document marker", number, indent + 1)
                self.lines.append((indent, content, number, colon))

    @staticmethod
    def sequence(line):
        text = line[1]
        return text == "-" or text.startswith(("- ", "-\t"))

    def error(self, message, line=None):
        line = self.lines[self.pos] if line is None else line
        _fail(message, line[2], line[0] + 1)

    def node(self, indent, depth):
        line = self.lines[self.pos]
        if depth >= _MAX_DEPTH and (self.sequence(line) or line[3] is not None):
            self.error("nesting depth exceeds 64")
        if self.sequence(line):
            return self.sequence_block(indent, depth + 1)
        if line[3] is not None:
            return self.mapping(indent, depth + 1)
        self.pos += 1
        return _Inline(line[1], line[2], indent + 1).parse(depth)

    def entry(self, line, result, depth):
        indent, text, number, colon = line
        reader = _Inline(text[:colon].rstrip(" \t"), number, indent + 1)
        key = reader.key(reader.parse(depth), 0)
        if key in result:
            self.error("duplicate mapping key", line)
        rest = text[colon + 1:]
        offset = len(rest) - len(rest.lstrip(" \t"))
        rest = rest.lstrip(" \t")
        if rest:
            result[key] = _Inline(rest, number, indent + colon + offset + 2).parse(depth)
        else:
            result[key] = self.child(indent, depth)

    def child(self, indent, depth):
        if self.pos < len(self.lines):
            line = self.lines[self.pos]
            if line[0] > indent or line[0] == indent and self.sequence(line):
                return self.node(line[0], depth)
        return None

    def mapping(self, indent, depth, first=None):
        result = {}
        if first is not None:
            self.entry(first, result, depth)
        while self.pos < len(self.lines):
            line = self.lines[self.pos]
            if line[0] < indent or line[0] == indent and self.sequence(line):
                break
            if line[0] != indent:
                self.error("inconsistent mapping indentation")
            if line[3] is None:
                self.error("block mapping separator or multiline scalar")
            self.pos += 1
            self.entry(line, result, depth)
        return result

    def sequence_block(self, indent, depth, first=None):
        result = []
        while first is not None or self.pos < len(self.lines):
            line = first if first is not None else self.lines[self.pos]
            if line[0] < indent or line[0] == indent and not self.sequence(line):
                break
            if line[0] != indent:
                self.error("inconsistent sequence indentation")
            if first is None:
                self.pos += 1
            first = None
            text = line[1][1:]
            spaces = len(text) - len(text.lstrip(" \t"))
            if "\t" in text[:spaces]:
                _fail("tab in sequence indentation", line[2],
                      indent + 2 + text.index("\t"))
            text = text.lstrip(" \t")
            col = indent + 1 + spaces
            if not text:
                # Unlike a mapping value, an empty sequence item cannot consume
                # another dash at the same indentation as a nested sequence.
                if self.pos < len(self.lines) and self.lines[self.pos][0] > indent:
                    result.append(self.node(self.lines[self.pos][0], depth))
                else:
                    result.append(None)
            else:
                _, colon = _scan(text)
                if text == "-" or text.startswith(("- ", "-\t")):
                    if depth >= _MAX_DEPTH:
                        self.error("nesting depth exceeds 64", line)
                    result.append(self.sequence_block(
                        col, depth + 1, (col, text, line[2], colon)))
                elif colon is not None:
                    if depth >= _MAX_DEPTH:
                        self.error("nesting depth exceeds 64", line)
                    result.append(self.mapping(col, depth + 1, (col, text, line[2], colon)))
                else:
                    result.append(_Inline(text, line[2], col + 1).parse(depth))
        return result

    def parse(self):
        if not self.lines:
            return None
        if self.lines[0][0] != 0:
            self.error("root indentation")
        value = self.node(0, 0)
        if self.pos != len(self.lines):
            self.error("inconsistent indentation or multiline scalar")
        return value


def loads(text: str) -> object:
    """Parse the documented subset; unsupported input raises MiniYamlError."""
    return _Parser(text.removeprefix("\ufeff")).parse()


def load_path(path) -> object:
    """Read at most 1 MiB of UTF-8, stripping an optional UTF-8 BOM.

    Filesystem errors propagate; size and encoding errors are MiniYamlError.
    The byte limit includes the BOM and is checked on the actual bounded read.
    """
    with Path(path).open("rb") as stream:
        raw = stream.read(_MAX_BYTES + 1)
    if len(raw) > _MAX_BYTES:
        raise MiniYamlError("file size exceeds 1 MiB")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        prefix = raw[:exc.start].decode("utf-8").removeprefix("\ufeff")
        prefix = prefix.replace("\r\n", "\n").replace("\r", "\n")
        raise MiniYamlError("invalid UTF-8 encoding", prefix.count("\n") + 1,
                            len(prefix.rsplit("\n", 1)[-1]) + 1) from exc
    return loads(text)
