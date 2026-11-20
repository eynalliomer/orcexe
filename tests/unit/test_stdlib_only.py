"""Reject non-standard-library imports in the bundled Python package."""

import ast
from pathlib import Path
import sys
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[2] / "plugins" / "orcexe" / "src"
LAUNCHER = SOURCE.parent / "bin" / "orcexe"
OPTIONAL_TOMLLIB_GUARD = ast.dump(ast.parse(
    "try:\n    import tomllib\nexcept ImportError:\n    tomllib = None\n").body[0])
# Python 3.9 has no sys.stdlib_module_names. Include public stdlib roots across
# supported platforms, including modules planned for subsequent tasks.
PY39_STDLIB = frozenset("""
    __future__ __main__ _thread abc aifc argparse array ast asynchat asyncio
    asyncore atexit audioop base64 bdb binascii binhex bisect builtins bz2 cProfile
    calendar cgi cgitb chunk cmath cmd code codecs codeop collections colorsys
    compileall concurrent configparser contextlib contextvars copy copyreg crypt
    csv ctypes curses dataclasses datetime dbm decimal difflib dis distutils
    doctest email encodings ensurepip enum errno faulthandler fcntl filecmp
    fileinput fnmatch formatter fractions ftplib functools gc genericpath getopt
    getpass gettext glob graphlib grp gzip hashlib heapq hmac html http idlelib
    imaplib imghdr imp importlib inspect io ipaddress itertools json keyword
    lib2to3 linecache locale logging lzma mailbox mailcap marshal math mimetypes
    mmap modulefinder msilib msvcrt multiprocessing netrc nis nntplib nt ntpath
    nturl2path numbers operator optparse os ossaudiodev pathlib pdb pickle
    pickletools pipes pkgutil platform plistlib poplib posix posixpath pprint
    profile pstats pty pwd py_compile pyclbr pydoc pydoc_data pyexpat queue quopri
    random re readline reprlib resource rlcompleter runpy sched secrets select
    selectors shelve shlex shutil signal site smtpd smtplib sndhdr socket
    socketserver spwd sqlite3 sre_compile sre_constants sre_parse ssl stat
    statistics string stringprep struct subprocess sunau symbol symtable sys
    sysconfig syslog tabnanny tarfile telnetlib tempfile termios textwrap threading
    time timeit tkinter token tokenize trace traceback tracemalloc tty turtle
    turtledemo types typing unicodedata unittest urllib uu uuid venv warnings
    wave weakref webbrowser winreg winsound wsgiref xdrlib xml xmlrpc zipapp
    zipfile zipimport zlib zoneinfo
""".split())


def python_sources():
    """Include the Python launcher even though it has no .py extension."""
    return sorted(SOURCE.rglob("*.py")) + [LAUNCHER]


def optional_tomllib_imports(tree):
    """Accept only the exact ImportError guard whose fallback intake tests force."""
    return {node.body[0] for node in ast.walk(tree)
            if isinstance(node, ast.Try) and ast.dump(node) == OPTIONAL_TOMLLIB_GUARD}


class StdlibOnlyTests(unittest.TestCase):
    def test_optional_tomllib_rule_is_narrow(self):
        guarded = "try:\n    import tomllib\nexcept ImportError:\n    tomllib = None\n"
        self.assertEqual(len(optional_tomllib_imports(ast.parse(guarded))), 1)
        for source in ("import tomllib", "from tomllib import loads",
                       guarded.replace("tomllib", "third_party"),
                       guarded.replace("except ImportError:", "except Exception:"),
                       guarded.replace("except ImportError:", "except:"),
                       guarded.replace("tomllib = None", "pass"),
                       guarded.replace("import tomllib", "import tomllib as parser"),
                       guarded.replace("import tomllib", "import tomllib, third_party"),
                       guarded + "else:\n    import third_party\n"):
            with self.subTest(source=source):
                self.assertEqual(optional_tomllib_imports(ast.parse(source)), set())

    def test_launcher_is_included(self):
        files = python_sources()
        self.assertIn(LAUNCHER, files)
        self.assertEqual(files.count(LAUNCHER), 1)
        self.assertTrue(all(path.is_file() for path in files))
        # Exercise the scanner, so dropping the extensionless launcher fails.
        with mock.patch.object(ast, "parse", wraps=ast.parse) as parsed:
            self.test_imports_are_stdlib()
        scanned = [call.kwargs["filename"] for call in parsed.call_args_list]
        self.assertIn(str(LAUNCHER), scanned)

    def test_imports_are_stdlib(self):
        allowed = set(getattr(sys, "stdlib_module_names", PY39_STDLIB)) | {"orcexe"}
        files = python_sources()
        self.assertTrue(files, "No package Python files found")
        for path in files:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            optional = optional_tomllib_imports(tree)
            for node in ast.walk(tree):
                if node in optional:
                    continue
                modules = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    modules = [node.module]
                for module in modules:
                    with self.subTest(file=str(path), line=node.lineno, module=module):
                        self.assertIn(
                            module.split(".")[0], allowed,
                            "{}:{}: non-stdlib import {}".format(path, node.lineno, module),
                        )
