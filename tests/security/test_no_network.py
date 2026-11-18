"""Source-wide network capability guard, plus cold imports with sockets denied."""

import ast
import subprocess
import sys

from tests.security import ROOT, SOURCE, PrivateHomes, SecurityFixture, dotted, python_sources


NETWORK_MODULES = frozenset(("socket", "ssl", "urllib", "http", "ftplib", "smtplib",
                             "telnetlib", "xmlrpc", "webbrowser"))
STREAM_OPENERS = frozenset(("open_connection", "open_unix_connection", "start_server",
                            "start_unix_server", "create_connection", "create_server",
                            "create_unix_connection", "create_unix_server", "create_datagram_endpoint"))


def network_violations(source):
    tree = ast.parse(source)
    aliases = {}
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                aliases[name.asname or name.name.split(".")[0]] = name.name
        elif isinstance(node, ast.ImportFrom):
            for name in node.names:
                aliases[name.asname or name.name] = (node.module or "") + "." + name.name

    def resolve(name):
        root, dot, tail = name.partition(".")
        return aliases.get(root, root) + (dot + tail if dot else "")

    for node in ast.walk(tree):
        reason = None
        if isinstance(node, ast.Import):
            if any(name.name.split(".")[0] in NETWORK_MODULES for name in node.names):
                reason = "network import"
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in NETWORK_MODULES:
                reason = "network import"
            elif any(resolve(name.asname or name.name).startswith("asyncio.") and
                     name.name in STREAM_OPENERS for name in node.names):
                reason = "asyncio stream opener"
        elif isinstance(node, ast.Call):
            name = resolve(dotted(node.func))
            if name in ("__import__", "builtins.__import__", "importlib.import_module"):
                value = node.args[0] if node.args else next(
                    (kw.value for kw in node.keywords if kw.arg == "name"), None)
                if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                    reason = "non-literal dynamic import"
                elif value.value.lstrip(".").split(".")[0] in NETWORK_MODULES:
                    reason = "dynamic network import"
                elif value.value.startswith("asyncio.") and value.value.rsplit(".", 1)[-1] in STREAM_OPENERS:
                    reason = "dynamic asyncio stream opener"
            elif name.rsplit(".", 1)[-1] in STREAM_OPENERS:
                reason = "network stream opener"
        if reason:
            violations.append((node.lineno, reason))
    return violations


def source_violations():
    return [(path.relative_to(ROOT).as_posix(), line, reason)
            for path in python_sources()
            for line, reason in network_violations(path.read_text(encoding="utf-8"))]


class NetworkSourceTests(PrivateHomes):
    def test_source_tree_has_no_network_capabilities(self):
        found = source_violations()
        self.assertEqual(found, [], "\n".join("{}:{}: {}".format(*v) for v in found))

    def test_do_does_not_import_socket(self):
        self.assertEqual(network_violations((SOURCE / "orcexe/do.py").read_text(encoding="utf-8")), [])

    def test_scanner_rejects_aliases_dynamic_imports_and_streams(self):
        bad = ("import socket", "from urllib.request import urlopen", "import http.client as h",
               "from asyncio import open_connection as connect", "import asyncio as a; a.start_server(f)",
               "loop.create_connection(f)", "__import__('ssl')", "__import__(name)",
               "import importlib as i; i.import_module('xmlrpc.client')",
               "from importlib import import_module as load; load(name)")
        for source in bad:
            with self.subTest(source=source):
                self.assertTrue(network_violations(source))
        self.assertEqual(network_violations("import json; __import__('pathlib')"), [])


class ImportNetworkTests(SecurityFixture):
    def test_all_package_modules_cold_import_with_socket_disabled(self):
        modules = []
        for path in sorted((SOURCE / "orcexe").rglob("*.py")):
            if path.name == "__main__.py":
                continue  # Executable entry point, imported below via runpy.
            parts = path.relative_to(SOURCE).with_suffix("").parts
            modules.append(".".join(parts[:-1] if parts[-1] == "__init__" else parts))
        code = (
            "import importlib, runpy, socket, sys\n"
            "from unittest.mock import patch\n"
            "sys.path.insert(0, {!r})\n"
            "with patch.object(socket, 'socket', side_effect=AssertionError('network at import')):\n"
            "    for name in {!r}:\n        importlib.import_module(name)\n"
            "    runpy.run_module('orcexe.__main__', run_name='security_import')\n"
        ).format(str(SOURCE), modules)
        observed = subprocess.run([sys.executable, "-B", "-c", code], env=self.env,
            cwd=str(self.root), capture_output=True, text=True, timeout=5)
        self.assertEqual(observed.returncode, 0, observed.stderr)
