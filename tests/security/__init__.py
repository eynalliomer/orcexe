"""Offline security fixtures. All state and fake executors live in temp dirs."""

from contextlib import contextmanager, redirect_stderr, redirect_stdout
import ast
import inspect
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.unit._fixtures import git_environment, non_repository_root


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "plugins/orcexe/src"
FAKES = ROOT / "tests/fixtures/fake-cli"
sys.path.insert(0, str(SOURCE))

TOOLS = {"codex": ("codex", "gpt-6-luna", "low"),
         "claude-code": ("claude", "claude-sonnet-5-5", "medium"),
         "cursor": ("agent", "composer-2.5", "high")}


def python_sources():
    """Discover future files too, including extensionless Python launchers."""
    for root in (SOURCE, ROOT / "plugins/orcexe/bin", ROOT / "scripts"):
        for path in sorted(root.rglob("*")):
            if path.is_file() and (path.suffix == ".py" or
                    path.suffix == "" and b"python" in path.read_bytes().split(b"\n", 1)[0]):
                yield path


def dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return dotted(node.value) + "." + node.attr
    return ""


class PrivateHomes(unittest.TestCase):
    """Even source-only checks run with the brief's five private locations."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="orcexe-security-static-")
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name).resolve()
        env = {"HOME": str(home), "CODEX_HOME": str(home / ".codex"),
               "CLAUDE_CONFIG_DIR": str(home / ".claude"),
               "CURSOR_CONFIG_DIR": str(home / ".cursor"),
               "ORCEXE_STATE_DIR": str(home / ".cache/orcexe")}
        for value in env.values():
            Path(value).mkdir(parents=True, exist_ok=True)
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)


@contextmanager
def capture_children():
    """Observe the actual spawn boundary, including proc and app-server calls."""
    real = subprocess.Popen
    records = []

    def spawn(argv, *args, **kwargs):
        frames = []
        frame = inspect.currentframe().f_back
        try:
            while frame:
                path = Path(frame.f_code.co_filename)
                if SOURCE in path.parents:
                    frames.append((path.relative_to(SOURCE).as_posix(),
                                   frame.f_code.co_name, frame.f_lineno))
                frame = frame.f_back
        finally:
            del frame
        environment = kwargs.get("env")
        records.append(dict(argv=list(argv), env=None if environment is None else dict(environment),
                            frames=frames))
        return real(argv, *args, **kwargs)

    with mock.patch.object(subprocess, "Popen", side_effect=spawn):
        yield records


class SecurityFixture(unittest.TestCase):
    """Real local Git plus wrapped shipped fakes; no user configuration reused."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="orcexe-security-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        non_repository_root(self.root)
        self.home = self.root / "home"
        self.home.mkdir()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.calls = self.root / "calls.jsonl"
        self.child_env_log = self.root / "child-environments.jsonl"
        self.env = {name: os.environ[name] for name in
                    ("SystemRoot", "SYSTEMROOT", "WINDIR", "PATHEXT", "COMSPEC")
                    if name in os.environ}
        self.env.update(
            PATH=os.pathsep.join((str(Path(sys.executable).parent), os.defpath,
                                  os.environ.get("PATH", ""))),
            HOME=str(self.home), USERPROFILE=str(self.home),
            CODEX_HOME=str(self.home / ".codex"),
            CLAUDE_CONFIG_DIR=str(self.home / ".claude"),
            CURSOR_CONFIG_DIR=str(self.home / ".cursor"),
            ORCEXE_STATE_DIR=str(self.home / ".cache/orcexe"),
            XDG_CACHE_HOME=str(self.home / ".cache"),
            XDG_CONFIG_HOME=str(self.home / ".config"),
            PYTHONDONTWRITEBYTECODE="1", GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
            GIT_TERMINAL_PROMPT="0")
        self.git = git_environment(self.env, self.root)
        self.assertIsNotNone(self.git, "security tests require local Git")
        for name in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "CURSOR_CONFIG_DIR", "ORCEXE_STATE_DIR"):
            Path(self.env[name]).mkdir(parents=True, exist_ok=True)
        self.codex_original = b'# security sentinel\nmodel = "saved"\n'
        self.cursor_original = dict(model={"saved": True}, selectedModel="saved",
            modelSelectionHistory=["saved"], modelParameters={"saved": []},
            hasChangedDefaultModel=False, sentinel="untouched", fakeLastRun="updated")
        # fakeLastRun is already its fake's final value, so unrelated config
        # preservation does not require an exception to the home-write policy.
        (self.home / ".codex/config.toml").write_bytes(self.codex_original)
        (self.home / ".claude/settings.json").write_text('{"sentinel": "untouched"}\n')
        (self.home / ".cursor/cli-config.json").write_text(json.dumps(self.cursor_original) + "\n")
        (self.home / "keep.txt").write_text("outside tool configuration\n")
        for path in self.home.rglob("*"):
            if path.is_file():
                path.chmod(0o600)
        for tool, (binary, _, _) in TOOLS.items():
            wrapper = self.root / (binary + "-wrapper.py")
            controls = dict(ORCEXE_FAKE_CALLS=str(self.calls), ORCEXE_FAKE_SCENARIO="success",
                            ORCEXE_FAKE_EDIT_FILE="src/app.py",
                            ORCEXE_FAKE_EDIT_TEXT='\n# Synthetic documentation.\n')
            wrapper.write_text(
                "#!" + sys.executable + "\n"
                "import json, os, runpy, sys\n"
                "sys.dont_write_bytecode = True\n"
                "with open({!r}, 'a', encoding='utf-8') as log:\n"
                "    log.write(json.dumps(dict(tool={!r}, argv=sys.argv[1:], env=dict(os.environ))) + '\\n')\n"
                "os.environ.update({!r})\n"
                "sys.path.insert(0, {!r})\n"
                "runpy.run_path({!r}, run_name='__main__')\n".format(
                    str(self.child_env_log), tool, controls, str(FAKES), str(FAKES / binary)))
            wrapper.chmod(0o700)
            if os.name == "nt":
                command = wrapper.with_suffix(".cmd")
                command.write_text('@"{}" -B "{}" %*\n'.format(sys.executable, wrapper))
            else:
                command = wrapper
            self.env["ORCEXE_BIN_" + tool.upper().replace("-", "_")] = str(command)
        environment = mock.patch.dict(os.environ, self.env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        old_cwd = Path.cwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, old_cwd)
        self.git_run("init", "-q", "--template=")
        self.git_run("config", "user.name", "Security Fixture")
        self.git_run("config", "user.email", "fixture@example.invalid")
        self.git_run("config", "commit.gpgsign", "false")
        self.git_run("config", "core.hooksPath", str(self.root / "no-hooks"))
        (self.repo / "src").mkdir()
        (self.repo / "src/app.py").write_text('def app():\n    """Return nothing."""\n')
        (self.repo / "tests").mkdir()
        (self.repo / "tests/__init__.py").touch()
        (self.repo / "tests/test_app.py").write_text(
            "import unittest\nfrom src.app import app\n"
            "class AppTest(unittest.TestCase):\n"
            "    def test_app(self):\n        self.assertIsNone(app())\n")
        self.git_run("add", ".")
        # Only this disposable repository gets a fixture commit.
        self.git_run("commit", "-qm", "security fixture")
        self.brief = self.root / "brief.md"
        self.brief.write_text("Add a comment to src/app.py.\nORCEXE_EXIT: DONE\n")

    def git_run(self, *args):
        return subprocess.run([self.git, *args], cwd=str(self.repo), env=self.env,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True,
            timeout=5).stdout.strip()

    def invoke(self, *args, codes=(0,)):
        from orcexe import cli
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(list(args))
        self.assertIn(code, codes, (args, code, stdout.getvalue(), stderr.getvalue()))
        self.assertEqual(stderr.getvalue(), "", args)
        return stdout.getvalue().encode("utf-8")

    def fake_records(self, environments=False):
        path = self.child_env_log if environments else self.calls
        self.assertTrue(path.is_file(), "fake executors were never reached")
        return [json.loads(line) for line in path.read_text().splitlines()]

    def run_flows(self):
        """All requested user surfaces, with successful execution/artifact checks."""
        from orcexe import ledger
        outputs = {}
        for json_flag in ((), ("--json",)):
            label = "json" if json_flag else "human"
            outputs["doctor/" + label] = self.invoke("doctor", "--no-cache", *json_flag)
            for tool in TOOLS:
                for operation in ("detect", "auth", "models", "quota"):
                    outputs["probe/{}/{}/{}".format(tool, operation, label)] = self.invoke(
                        "probe", tool, operation, *json_flag,
                        codes=(1,) if tool == "claude-code" and operation == "models" else (0,))
        outputs["env.json"] = (self.home / ".cache/orcexe/env.json").read_bytes()
        self.trusted_workdirs = []
        for tool, (_, model, effort) in TOOLS.items():
            work = self.root / (tool + "-work")
            self.git_run("worktree", "add", "--quiet", "--detach", str(work), "HEAD")
            run_id = "security-" + tool
            outputs["run/" + tool] = self.invoke("run", "--tool", tool, "--model", model,
                "--effort", effort, "--brief", str(self.brief), "--workdir", str(work),
                "--profile", "write", "--run-id", run_id, "--json")
            result = json.loads(outputs["run/" + tool])
            self.assertEqual(result["status"], "completed", result)
            self.assertIn("src/app.py", result["diff_files"], result)
            directory = self.repo / ".orcexe/runs" / run_id
            outputs[run_id + "/result.json"] = (directory / "result.json").read_bytes()
            if tool == "codex":
                self.trusted_workdirs.append(str(work))
        outputs["do"] = self.invoke("do", "Add a docstring to src/app.py", "--repo", str(self.repo),
            "--model-constraint", '{"tools":["codex"]}', "--timeout", "5", "--json")
        summary = json.loads(outputs["do"])
        self.assertEqual(summary["verdict"], "accepted", summary)
        self.trusted_workdirs.append(summary["worktree"])
        directory = self.repo / ".orcexe/runs" / summary["run_id"]
        for name in ("status.md", "gate.json", "result.json"):
            outputs["do/" + name] = (directory / name).read_bytes()
        outputs["ledger"] = ledger.default_path(self.repo).read_bytes()
        outputs["trust/list"] = self.invoke("trust", "list", "--repo", str(self.repo), "--json")
        self.assertTrue(json.loads(outputs["trust/list"])["entries"])
        self.assertEqual({call["tool"] for call in self.fake_records(True)}, set(TOOLS))
        self.assertEqual(sum(call["model_call"] for call in self.fake_records()), 4)
        return outputs
