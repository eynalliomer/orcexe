"""Observe every real child, including direct Git calls and app-server sessions."""

import ast
import os
import uuid

from tests.security import (ROOT, PrivateHomes, SecurityFixture, capture_children, dotted,
                            python_sources)


SECRET_NAMES = (
    "OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL", "CURSOR_API_KEY", "GITHUB_TOKEN", "GH_TOKEN",
    "AWS_SECRET_ACCESS_KEY", "NPM_TOKEN", "MY_SERVICE_TOKEN", "MY_SERVICE_SECRET",
    "MY_SERVICE_PASSWORD", "SECRET", "PASSWORD",
)
# Tool config locations are intentional passthroughs, never credentials. The
# wrapper records its incoming environment BEFORE adding fake-only controls.
ALLOWED_TOOL_ENV = frozenset((
    "PATH", "HOME", "USERPROFILE", "CODEX_HOME", "CLAUDE_CONFIG_DIR",
    "CURSOR_CONFIG_DIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "SystemRoot",
    "SYSTEMROOT", "WINDIR", "PATHEXT", "COMSPEC", "ComSpec", "LC_CTYPE",
    "__CF_USER_TEXT_ENCODING",  # macOS/Python injects this after exec.
))


def secret_values():
    # Deliberately no provider-shaped credential literals, even synthetic ones.
    return {name: "-".join(("synthetic", "security", uuid.uuid4().hex, str(index)))
            for index, name in enumerate(SECRET_NAMES)}


def identity_lookup(record):
    return any(path == "orcexe/do.py" and name == "_resolve_identity"
               for path, name, _ in record["frames"])


class ChildEnvironmentTests(SecurityFixture):
    def assert_no_secrets(self, records, secrets):
        self.assertTrue(records, "no child environments captured")
        violations = []
        for record in records:
            env = record["env"]
            location = "{} ({})".format(record["argv"], record.get("frames", record.get("tool")))
            if env is None:
                violations.append(location + ": inherited parent environment")
                continue
            # Search all values as well: renaming a credential is still a leak.
            for name, value in env.items():
                if name.upper() in SECRET_NAMES or any(secret in value for secret in secrets.values()):
                    violations.append(location + ": leaked " + name)
        self.assertEqual(violations, [], "\n".join(violations))

    def test_all_executor_and_support_paths_strip_parent_secrets(self):
        from orcexe import gitutil, worktree
        secrets = secret_values()
        os.environ.update(secrets)
        with capture_children() as records:
            self.run_flows()
            # Exercise the lifecycle's temporary-index, archive and removal
            # paths in addition to creation/status and gate project commands.
            wt = worktree.create(self.repo, worktree.Spec("security", "archive", "codex"))
            self.assertEqual(worktree.status(wt)["dirty"], [])
            self.assertTrue(worktree.remove(self.repo, wt, archive_root=self.root / "archive")["removed"])
            self.assertTrue(gitutil.is_repo(self.repo))
        self.assertTrue(any(identity_lookup(record) for record in records))
        self.assert_no_secrets(records, secrets)
        fakes = self.fake_records(environments=True)
        self.assert_no_secrets(fakes, secrets)
        for record in fakes:
            with self.subTest(tool=record["tool"], argv=record["argv"]):
                self.assertLessEqual(set(record["env"]), ALLOWED_TOOL_ENV)
                self.assertEqual(record["env"]["HOME"], str(self.home))
                config = {"codex": "CODEX_HOME", "claude-code": "CLAUDE_CONFIG_DIR",
                          "cursor": "CURSOR_CONFIG_DIR"}[record["tool"]]
                self.assertEqual(record["env"][config], self.env[config])
        # Coverage is based on actual call stacks, not mocked adapter success.
        observed = {path for record in records for path, _, _ in record["frames"]}
        for module in ("adapters/base", "adapters/codex", "adapters/codex_appserver",
                       "adapters/claude_code", "adapters/cursor", "gate_lite", "worktree",
                       "gitutil", "doctor", "do", "run"):
            self.assertIn("orcexe/" + module + ".py", observed)
        # trust.py itself has no subprocesses; its CLI performs Git discovery.
        self.assertTrue(any(name == "_trust_command" for r in records for _, name, _ in r["frames"]))
        self.assertTrue(any(name == "_project" for r in records for _, name, _ in r["frames"]),
                        "gate project command was not executed")

    def test_git_identity_lookup_strips_parent_secrets(self):
        from orcexe import do
        secrets = secret_values()
        os.environ.update(secrets)
        os.environ.update(USER="fixture", LOGNAME="fixture", LANG="C", LC_ALL="C",
                          LC_MESSAGES="C", GIT_AUTHOR_NAME="Fixture Author",
                          GIT_AUTHOR_EMAIL="author@example.invalid",
                          GIT_COMMITTER_NAME="Fixture Committer",
                          GIT_COMMITTER_EMAIL="committer@example.invalid",
                          GIT_AUTHOR_DATE="ignored", GIT_COMMITTER_DATE="ignored",
                          GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="user.name",
                          GIT_CONFIG_VALUE_0="ignored override")
        with capture_children() as records:
            self.assertIsNotNone(do._resolve_identity(self.repo))
        self.assertTrue(all(identity_lookup(record) for record in records))
        self.assert_no_secrets(records, secrets)
        allowed = {"PATH", "HOME", "USER", "LOGNAME", "LANG", "XDG_CONFIG_HOME",
                   "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_AUTHOR_NAME",
                   "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"}
        expected = {key: value for key, value in os.environ.items()
                    if key in allowed or key.startswith("LC_")}
        for record in records:
            self.assertEqual(record["env"], expected)
            self.assertEqual(record["argv"][:5],
                             ["git", "-c", "core.hooksPath=" + os.devnull, "config", "--get"])


class SubprocessSourceTests(PrivateHomes):
    def test_every_spawn_site_requires_an_explicit_environment(self):
        """New modules cannot silently introduce unobserved inherited-env calls."""
        violations = []
        checked = 0
        for path in python_sources():
            if ROOT / "scripts" in path.parents:
                # Repository maintenance scripts are not CLI subprocess paths;
                # the network-import guard still scans them without exception.
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            aliases = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    aliases.update((n.asname or n.name, n.name) for n in node.names)
                elif isinstance(node, ast.ImportFrom):
                    aliases.update((n.asname or n.name, (node.module or "") + "." + n.name)
                                   for n in node.names)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                root, dot, tail = dotted(node.func).partition(".")
                name = aliases.get(root, root) + (dot + tail if dot else "")
                location = "{}:{}".format(path.relative_to(ROOT), node.lineno)
                if name.startswith("subprocess.") and name.rsplit(".", 1)[-1] in (
                        "Popen", "run", "call", "check_call", "check_output", "getoutput", "getstatusoutput") or name.endswith("proc.run"):
                    checked += 1
                    env = next((kw.value for kw in node.keywords if kw.arg == "env"), None)
                    if env is None or isinstance(env, ast.Constant) and env.value is None:
                        violations.append(location + ": missing explicit env")
                elif name.startswith(("os.system", "os.popen", "os.spawn", "os.exec", "os.posix_spawn")):
                    violations.append(location + ": unaudited process-launch API " + name)
        self.assertGreater(checked, 0)
        self.assertEqual(violations, [], "\n".join(violations))
