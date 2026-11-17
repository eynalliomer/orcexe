#!/usr/bin/env python3
"""Validate the Claude Code marketplace, plugin manifest, skills and commands.

Usage: python3 scripts/check_marketplace.py [--root REPO]

Exits 0 when everything is consistent, 1 with one message per problem.
"""

import argparse
import ast
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
MAX_SKILL_LINES = 160
MARKETPLACE_KEYS = ("name", "owner", "plugins")
ENTRY_KEYS = ("name", "source", "description", "version")
PLUGIN_KEYS = ("name", "version", "description")


def front_matter(text):
    """Return top-level `key: value` fields of a `---` block, or None if absent/invalid.

    Indented lines continue the previous value (YAML plain-scalar folding); a value
    wrapped in matching quotes is unquoted.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    fields, key = {}, None
    for line in lines[1:]:
        if line.strip() == "---":
            return {name: _unquote(value) for name, value in fields.items()}
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[:1] in (" ", "\t"):
            if key is None:
                return None
            fields[key] += " " + line.strip()
            continue
        name, separator, value = line.partition(":")
        if not separator or not name.strip():
            return None
        key = name.strip()
        fields[key] = value.strip()
    return None


def _unquote(value):
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return json.loads(value)
        except ValueError:
            return value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value


def _load_json(path, errors):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        errors.append("{}: cannot read JSON ({})".format(path, type(error).__name__))
        return None
    if not isinstance(data, dict):
        errors.append("{}: top level must be a JSON object".format(path))
        return None
    return data


def _missing(data, keys, label, errors):
    for key in keys:
        value = data.get(key)
        if value is None or value == "" or value == [] or value == {}:
            errors.append("{}: missing required key '{}'".format(label, key))


def _package_version(plugin):
    """Return orcexe.__version__ from source without importing it, or None if undefined."""
    path = plugin / "src" / "orcexe" / "__init__.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__version__"
                                                 for t in node.targets)
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            return node.value.value
    return None


def _launcher_version(plugin, errors):
    launcher = plugin / "bin" / "orcexe"
    if not launcher.is_file():
        errors.append("{}: launcher missing".format(launcher))
        return None
    try:
        result = subprocess.run([sys.executable, "-B", str(launcher), "version", "--json"],
                                cwd=str(plugin), stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise ValueError("exit " + str(result.returncode))
        return json.loads(result.stdout)["version"]
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        errors.append("bin/orcexe version --json failed ({})".format(error))
        return None


def _check_versions(marketplace, plugin_manifest, plugin, errors):
    versions = {}
    if isinstance(marketplace.get("metadata"), dict) and "version" in marketplace["metadata"]:
        versions["marketplace metadata.version"] = marketplace["metadata"]["version"]
    for entry in marketplace.get("plugins") or []:
        if isinstance(entry, dict) and entry.get("name") == plugin_manifest.get("name"):
            versions["marketplace plugins[{}].version".format(entry["name"])] = entry.get("version")
    versions["plugin.json version"] = plugin_manifest.get("version")
    try:
        package = _package_version(plugin)
    except (OSError, SyntaxError, ValueError) as error:
        errors.append("orcexe/__init__.py: cannot parse ({})".format(type(error).__name__))
        package = None
    if package is not None:
        versions["orcexe.__version__"] = package
    launcher = _launcher_version(plugin, errors)
    if launcher is not None:
        versions["bin/orcexe version"] = launcher
    if len(set(map(str, versions.values()))) > 1:
        errors.append("version mismatch: " + ", ".join(
            "{}={}".format(label, value) for label, value in versions.items()))
    return plugin_manifest.get("version")


def _check_marketplace(root, plugin, errors):
    path = root / ".claude-plugin" / "marketplace.json"
    marketplace = _load_json(path, errors)
    if marketplace is None:
        return None
    _missing(marketplace, MARKETPLACE_KEYS, "marketplace.json", errors)
    owner = marketplace.get("owner")
    if owner is not None and not (isinstance(owner, dict) and owner.get("name")):
        errors.append("marketplace.json: owner must be an object with a 'name'")
    entries = marketplace.get("plugins")
    if entries is not None and not isinstance(entries, list):
        errors.append("marketplace.json: 'plugins' must be a list")
        return marketplace
    found = False
    for index, entry in enumerate(entries or []):
        label = "marketplace.json plugins[{}]".format(index)
        if not isinstance(entry, dict):
            errors.append(label + ": must be an object")
            continue
        _missing(entry, ENTRY_KEYS, label, errors)
        source = entry.get("source")
        if isinstance(source, str) and source:
            target = (root / source).resolve()
            if not source.startswith("./") or not target.is_dir():
                errors.append("{}: source '{}' is not an existing ./ relative directory".format(label, source))
            elif target == plugin.resolve():
                found = True
    if not found:
        errors.append("marketplace.json: no plugin entry has source ./plugins/orcexe")
    return marketplace


def _read_text(path, label, errors):
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError) as error:
        errors.append("{}: cannot read ({})".format(label, type(error).__name__))
        return None


def _check_skills(plugin, errors):
    skills = sorted((plugin / "skills").glob("*/SKILL.md"))
    if not skills:
        errors.append("{}: no skills/*/SKILL.md found".format(plugin / "skills"))
    for path in skills:
        label = str(path.relative_to(plugin.parent.parent))
        text = _read_text(path, label, errors)
        if text is None:
            continue
        count = len(text.splitlines())
        if count > MAX_SKILL_LINES:
            errors.append("{}: {} lines (max {})".format(label, count, MAX_SKILL_LINES))
        fields = front_matter(text)
        if fields is None:
            errors.append(label + ": missing or invalid front matter")
            continue
        if not fields.get("name"):
            errors.append(label + ": front matter lacks 'name'")
        if not fields.get("description"):
            errors.append(label + ": front matter lacks 'description'")
        elif not fields["description"].startswith("Use when"):
            errors.append(label + ": description must start with 'Use when'")
    return len(skills)


def _check_commands(plugin, errors):
    commands = sorted((plugin / "commands").glob("*.md"))
    for path in commands:
        label = str(path.relative_to(plugin.parent.parent))
        text = _read_text(path, label, errors)
        if text is None:
            continue
        fields = front_matter(text)
        if fields is None:
            errors.append(label + ": missing or invalid front matter")
        elif not fields.get("description"):
            errors.append(label + ": front matter lacks 'description'")
    return len(commands)


def check(root=ROOT):
    """Return (errors, summary) for the repository at root."""
    root = Path(root)
    plugin = root / "plugins" / "orcexe"
    errors = []
    marketplace = _check_marketplace(root, plugin, errors)
    manifest_path = plugin / ".claude-plugin" / "plugin.json"
    manifest = _load_json(manifest_path, errors)
    version = None
    if manifest is not None:
        _missing(manifest, PLUGIN_KEYS, "plugin.json", errors)
        if marketplace is not None:
            version = _check_versions(marketplace, manifest, plugin, errors)
    skills = _check_skills(plugin, errors)
    commands = _check_commands(plugin, errors)
    summary = "plugin orcexe {}: {} skill(s), {} command(s)".format(version, skills, commands)
    return errors, summary


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate the orcexe Claude Code marketplace and plugin.")
    parser.add_argument("--root", default=str(ROOT), help="Repository root (default: this checkout)")
    args = parser.parse_args(argv)
    errors, summary = check(Path(args.root))
    for error in errors:
        print("FAIL  " + error)
    if errors:
        print("{} problem(s) found.".format(len(errors)))
        return 1
    print("OK    " + summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
