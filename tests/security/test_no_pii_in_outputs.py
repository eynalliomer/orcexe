"""Synthetic identities from real fake responses must never reach reports."""

import json

from tests.security import FAKES, SecurityFixture


PRIVATE_KEYS = frozenset(("email", "userEmail", "userInfo", "orgId", "orgName",
                          "accountId", "userId", "firstName", "lastName"))


def pii_canaries():
    """Derive values from fixtures so replacing a synthetic identity stays covered."""
    values = set()

    def visit(value, private=False):
        if isinstance(value, dict):
            for key, item in value.items():
                visit(item, private or key in PRIVATE_KEYS or "workspace" in key.lower())
        elif isinstance(value, list):
            for item in value:
                visit(item, private)
        elif private and isinstance(value, str) and value:
            values.add(value)

    for path in sorted(FAKES.rglob("*")):
        if path.is_file() and (path.suffix == ".json" or path.name.endswith(".json.txt")):
            visit(json.loads(path.read_text(encoding="utf-8")))
        elif path.is_file() and path.suffix == ".jsonl":
            for line in path.read_text(encoding="utf-8").splitlines():
                visit(json.loads(line))
    # Claude's auth document is constructed in the executable, not a JSON fixture.
    import ast
    tree = ast.parse((FAKES / "claude").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg in PRIVATE_KEYS and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                values.add(node.value.value)
    return values


class OutputPrivacyTests(SecurityFixture):
    def test_all_user_facing_outputs_and_persisted_reports_exclude_pii(self):
        outputs = self.run_flows()
        canaries = pii_canaries()
        self.assertIn("fake-workspace", canaries)
        self.assertIn("fake-claude-org-id", canaries)
        self.assertIn("fake-cursor-firstName", canaries)
        for label, data in outputs.items():
            with self.subTest(output=label):
                leaks = [value for value in sorted(canaries | PRIVATE_KEYS)
                         if value.encode("utf-8") in data]
                self.assertEqual(leaks, [], label + ": leaked " + ", ".join(leaks))
