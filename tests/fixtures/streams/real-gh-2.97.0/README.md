Real `gh 2.97.0` (macOS, 2026-11-18) byte recordings for `gh issue list --json title,labels --limit 2`.
- noremote: repo with no git remotes (exit 1, stderr only)
- nongh: remote pointing to a non-GitHub host (exit 1, stderr only)
- public: `-R cli/cli --state open`, authenticated (exit 0; stdout is ONE line of JSON array, newline-terminated)
- noauth: empty GH_CONFIG_DIR, no token env (exit code in exit-codes.txt, stderr only)
Label objects carry id/name/description/color; only `name` may be used. Titles are untrusted text.
