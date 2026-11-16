"""Subset contract, independent emitters, fixtures, and adversarial bounds."""

import ast
import json
from pathlib import Path
import random
import re
import tempfile
import time
import unittest

from tests.unit._timing import scaled

from orcexe.miniyaml import MiniYamlError, load_path, loads


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "miniyaml"
SEED = 7072026
ROUND_TRIPS = 3000
TRAP_STRINGS = ["yes", "no", "on", "off", "2026-11-14", "0.159.0", "0x1F",
                "010", ".inf", "True", "NULL", "true", "false", "null", "~"]


def scalar(value, style):
    """Independent conservative emitter; deliberately vary the quoting style."""
    if not isinstance(value, str):
        return json.dumps(value)
    safe = bool(re.fullmatch(r"[\w./+-]+", value, re.UNICODE))
    numeric = bool(re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", value))
    if style == 0 and safe and not numeric and value not in ("true", "false", "null", "-"):
        return value
    if style == 2 and not any(c in value for c in "\n\r\t\0"):
        return "'" + value.replace("'", "''") + "'"
    return json.dumps(value, ensure_ascii=False).replace("\\u0000", "\\0")


def flow(value, style):
    if isinstance(value, dict):
        return "{" + ", ".join(scalar(k, style) + ": " + flow(v, style)
                               for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(flow(v, style) for v in value) + "]"
    return scalar(value, style)


def emit(value, style, indent=0, depth=0):
    """Block, flow, and mixed, including compact maps and indentless lists."""
    pad = " " * indent
    if (style == 1 or not isinstance(value, (dict, list)) or not value or
            style == 2 and depth % 3 == 2):
        return pad + flow(value, style)
    lines = []
    if isinstance(value, dict):
        for key, item in value.items():
            prefix = pad + scalar(key, style) + ":"
            if isinstance(item, (dict, list)) and item:
                child_indent = indent if style == 0 and isinstance(item, list) else indent + 2
                lines.extend([prefix, emit(item, style, child_indent, depth + 1)])
            else:
                lines.append(prefix + " " + flow(item, style))
    else:
        for item in value:
            if isinstance(item, (dict, list)) and item:
                child = emit(item, style, indent + 2, depth + 1)
                if style == 2:
                    lines.append(pad + "- " + child[indent + 2:])
                else:
                    lines.extend([pad + "-", child])
            else:
                lines.append(pad + "- " + flow(item, style))
    return "\n".join(lines)


def random_value(rng, depth=0):
    choice = rng.randrange(9 if depth < 6 else 6)
    if choice == 0:
        return None
    if choice == 1:
        return bool(rng.randrange(2))
    if choice == 2:
        return rng.randint(-100000, 100000)
    if choice == 3:
        return rng.randint(-100000, 100000) / 128.0
    if choice in (4, 5):
        if rng.randrange(3) == 0:
            return rng.choice(TRAP_STRINGS)
        alphabet = "abcXYZ019 :#,\"'{}[]/\\-+!?&*|>%@`~ı雪🙂\n\t\r\0"
        return "".join(rng.choice(alphabet) for _ in range(rng.randrange(24)))
    if choice in (6, 7):
        return [random_value(rng, depth + 1) for _ in range(rng.randrange(4))]
    result = {}
    for _ in range(rng.randrange(4)):
        key = rng.choice(TRAP_STRINGS + ["", "b.c", "a: b", "#", "雪", "quotes'\"", "{}[]"])
        result[key] = random_value(rng, depth + 1)
    return result


class MiniYamlTests(unittest.TestCase):
    def test_fixtures(self):
        files = sorted(FIXTURES.glob("*.yaml"))
        self.assertGreaterEqual(len(files), 4)
        for path in files:
            with self.subTest(fixture=path.name):
                with path.with_suffix(".json").open(encoding="utf-8") as stream:
                    expected = json.load(stream)
                self.assertEqual(load_path(path), expected)

    def test_generated_round_trip(self):
        rng = random.Random(SEED)
        for index in range(ROUND_TRIPS):
            value = random_value(rng)
            for style in range(3):
                text = emit(value, style) + "\n\n# end\n"
                with self.subTest(seed=SEED, index=index, style=style):
                    self.assertEqual(loads(text), value,
                                     "seed={} index={} style={} text={!r}".format(SEED, index, style, text))

    def test_scalar_traps(self):
        cases = [("true", True), ("false", False), ("null", None), ("~", None),
                 ("", None), ("0", 0), ("-12", -12), ("+3", 3), ("-0", 0),
                 ("1.5", 1.5), ("-0.25", -0.25), ("1e3", 1000.0),
                 (".5", 0.5), ("1.", 1.0), ("+1E-3", 0.001)]
        cases += [(v, v) for v in TRAP_STRINGS if v not in ("true", "false", "null", "~")]
        cases += [(v, v) for v in ["False", "Null", "TRUE", "FALSE", "+010", "0o17", "1_000", "-.inf", ".nan", "--json", "-C", "D+Y", "a#b"]]
        for text, expected in cases:
            with self.subTest(text=text):
                actual = loads("value: " + text)["value"]
                self.assertEqual(actual, expected)
                self.assertIs(type(actual), type(expected))

    def test_rejection_table(self):
        # source, offending line, latest permitted column, named construct
        cases = [
            ("a:\n \tx: 1", 2, 2, "tab"),
            ("- \tx: 1", 1, 3, "tab"),
            ("a: 1\n b: 2", 2, 2, "indentation"),
            ("a:\n  b: 1\n c: 2", 3, 2, "indentation"),
            ("a: 1\na: 2", 2, 1, "duplicate"),
            ("a: {x: 1, x: 2}", 1, 11, "duplicate"),
            ("a: &id value", 1, 4, "anchor"),
            ("a: *id", 1, 4, "alias"),
            ("a: !str value", 1, 4, "tag"),
            ("a: |\n  text", 1, 4, "block scalar"),
            ("a: >\n  text", 1, 4, "block scalar"),
            ("---\na: 1", 1, 1, "document marker"),
            ("a: 1\n...", 2, 1, "document marker"),
            ("? key: value", 1, 1, "complex key"),
            ("<<: value", 1, 1, "merge key"),
            ("a: {'<<': value}", 1, 5, "merge key"),
            ("%YAML 1.2\na: 1", 1, 1, "directive"),
            ("a: [1,\n  2]", 1, 4, "flow collection"),
            ("a: {x:\n  2}", 1, 4, "flow collection"),
            ("a: value: other", 1, 9, "plain scalar colon"),
            ("a: - value", 1, 4, "indicator"),
            ("a: ?value", 1, 4, "complex key"),
            ("a: :value", 1, 4, "indicator"),
            ("a: ,value", 1, 4, "indicator"),
            ("a: ]value", 1, 4, "indicator"),
            ("a: }value", 1, 4, "indicator"),
            ("a: @value", 1, 4, "indicator"),
            ("a: `value", 1, 4, "indicator"),
            ("a: [a[b]", 1, 6, "flow indicator"),
            ("a: [a{b]", 1, 6, "flow indicator"),
            ("a: [a]b]", 1, 7, "trailing content"),
            ("a: [a}b]", 1, 6, "separator"),
            ("a: [a,,b]", 1, 7, "empty flow"),
            ('a: "unterminated', 1, 4, "unterminated"),
            ("a: 'unterminated", 1, 4, "unterminated"),
            ('a: "bad\\q"', 1, 8, "escape"),
            ('a: "bad\\u12xz"', 1, 8, "Unicode escape"),
            ('a: "bad\\uD800"', 1, 8, "surrogate"),
            ("a: [] trailing", 1, 7, "trailing content"),
            ("a: {} trailing", 1, 7, "trailing content"),
            ('a: "x" trailing', 1, 8, "trailing content"),
            ("1: x", 1, 1, "non-string mapping key"),
            ("true: x", 1, 1, "non-string mapping key"),
            ("null: x", 1, 1, "non-string mapping key"),
            ("{1: x}", 1, 2, "non-string mapping key"),
            ("{false: x}", 1, 2, "non-string mapping key"),
            ("{~: x}", 1, 2, "non-string mapping key"),
            ("a: first\n  second", 2, 3, "indentation"),
            ("a: \x01", 1, 4, "control"),
        ]
        for source, line, col, construct in cases:
            with self.subTest(source=source):
                with self.assertRaises(MiniYamlError) as caught:
                    loads(source)
                error = caught.exception
                self.assertEqual(error.line, line)
                self.assertGreaterEqual(error.col, 1)
                self.assertLessEqual(error.col, col)
                self.assertIn(construct, str(error))
                self.assertIn("line {}".format(line), str(error))

    def test_depth_limits(self):
        self.assertEqual(loads("[" * 64 + "0" + "]" * 64), self.nested_list(64))
        self.assertEqual(loads("-\n  0"), [0])
        cases = [("[" * 65 + "0" + "]" * 65, 1, 65),
                 ("{x: [" * 33 + "0" + "]}" * 33, 1, 161),
                 ("\n".join("  " * i + "x:" for i in range(65)), 65, 129),
                 ("\n".join("  " * i + "-" for i in range(65)), 65, 129),
                 ("- x: " + "[" * 63 + "0" + "]" * 63, 1, 68),
                 ("- " * 65 + "0", 1, 129)]
        for text, line, col in cases:
            with self.subTest(text=text[:70]):
                with self.assertRaisesRegex(MiniYamlError, "depth") as caught:
                    loads(text)
                self.assertEqual(caught.exception.line, line)
                self.assertGreaterEqual(caught.exception.col, 1)
                self.assertLessEqual(caught.exception.col, col)
        block = "\n".join("  " * i + "x:" for i in range(64))
        self.assertIsInstance(loads(block), dict)
        # An enormous depth still stops before Python's recursion limit.
        with self.assertRaisesRegex(MiniYamlError, "depth"):
            loads("[" * 10000)

    @staticmethod
    def nested_list(count):
        value = 0
        for _ in range(count):
            value = [value]
        return value

    def test_indentation_mutations(self):
        for path in sorted(FIXTURES.glob("*.yaml")):
            lines = path.read_text(encoding="utf-8").splitlines()
            original = load_path(path)
            for index, line in enumerate(lines):
                if not line.strip():
                    continue
                for shift in (-1, 1):
                    if shift == -1 and not line.startswith(" "):
                        continue  # Negative indentation cannot be represented.
                    mutated = list(lines)
                    mutated[index] = " " + line if shift == 1 else line[1:]
                    with self.subTest(fixture=path.name, line=index + 1, shift=shift):
                        try:
                            value = loads("\n".join(mutated))
                        except MiniYamlError:
                            continue
                        self.assertNotEqual(value, original, "unchanged value after indentation mutation")

    def test_comments_quotes_and_collections(self):
        self.assertEqual(loads("\n# heading\n\na: x # comment\nb: 'x # y'\nc: a#b\n"),
                         {"a": "x", "b": "x # y", "c": "a#b"})
        self.assertEqual(loads('a: a["foo # comment\nb: a{"foo # comment'),
                         {"a": 'a["foo', "b": 'a{"foo'})
        self.assertEqual(loads('{"a":"x # y", "b": "z"}'), {"a": "x # y", "b": "z"})
        self.assertEqual(loads("a: x\u00a0\nb: \u00a0#x"), {"a": "x\u00a0", "b": "\u00a0#x"})
        self.assertEqual(loads("- - a\n  - b\n- - name: c\n    other: d"),
                         [["a", "b"], [{"name": "c", "other": "d"}]])
        self.assertEqual(loads("- - 'key # literal': 'value # literal'\n    other: x"),
                         [[{"key # literal": "value # literal", "other": "x"}]])
        self.assertEqual(loads("a: don't # comment\nb: say\"hi # comment"),
                         {"a": "don't", "b": 'say"hi'})
        self.assertEqual(loads("a:\n- one\n- two\nb: 3"), {"a": ["one", "two"], "b": 3})
        self.assertEqual(loads("-    a: 1\n     b: 2\n- a:\n    - 3\n  b: 4"),
                         [{"a": 1, "b": 2}, {"a": [3], "b": 4}])
        self.assertEqual(loads("{\"a\":1, 'b': [2,], c:,}"), {"a": 1, "b": [2], "c": None})
        self.assertEqual(loads('"1": x\n"true": y\n"null": z'), {"1": "x", "true": "y", "null": "z"})
        self.assertEqual(loads("a:\n\nb: []\nc: {}"), {"a": None, "b": [], "c": {}})
        self.assertIsNone(loads("# only comments\n\n"))
        self.assertEqual(loads("a: 1\r\nb: 2\rc: 3"), {"a": 1, "b": 2, "c": 3})

    def test_load_path_bounds_and_encoding(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.yaml"
            path.write_bytes(b"\xef\xbb\xbfa: 1\n")
            self.assertEqual(load_path(path), {"a": 1})
            path.write_bytes(b"x" * (1024 * 1024))
            self.assertEqual(len(load_path(path)), 1024 * 1024)
            path.write_bytes(b"x" * (1024 * 1024 + 1))
            with self.assertRaisesRegex(MiniYamlError, "size"):
                load_path(path)
            path.write_bytes(b"a: 1\nb: \xff")
            with self.assertRaisesRegex(MiniYamlError, "UTF-8") as caught:
                load_path(path)
            self.assertEqual((caught.exception.line, caught.exception.col), (2, 4))
            for data, position in [(b"\xef\xbb\xbfa: \xff", (1, 4)),
                                   (b"\xef\xbb", (1, 1))]:
                path.write_bytes(data)
                with self.assertRaisesRegex(MiniYamlError, "UTF-8") as caught:
                    load_path(path)
                self.assertEqual((caught.exception.line, caught.exception.col), position)

    def test_adversarial_linear_time(self):
        # Long numeric-looking near matches exercise all numeric regex paths.
        text = "1" * (1024 * 1024 - 1) + "x"
        started = time.process_time()
        self.assertEqual(loads(text), text)
        with self.assertRaises(MiniYamlError):
            loads("[" * (1024 * 1024))
        self.assertLess(time.process_time() - started, scaled(2.0))
        with self.assertRaisesRegex(MiniYamlError, "integer too long"):
            loads("1" * 10000)

    def test_python39_syntax(self):
        source = Path(__file__).resolve().parents[2] / "plugins/orcexe/src/orcexe/miniyaml.py"
        ast.parse(source.read_text(encoding="utf-8"), feature_version=(3, 9))


if __name__ == "__main__":
    unittest.main()
