---
description: Run orcexe (doctor, do, run, trust) and summarise the result
argument-hint: "doctor | do \"<task>\" | run … | trust list|clean"
---

Run this command from the user's repository:

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/bin/orcexe" $ARGUMENTS
```

Then summarise the output briefly, following the rules in the `orcexe` skill: show `→ fix` lines verbatim, report the
`do` verdict and failed check, and never run a printed land command or `trust clean --apply` without the user's
explicit request.
