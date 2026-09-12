# Contributing

## The one rule that is enforced mechanically

**Nothing in this repository names a real person, a real company, or a real
deployment.** Not in code, not in comments, not in tests, not in docs.

This is not a style preference. This runtime was extracted from a private
deployment, and the extraction did not take the first time: names, a day-job
employer, and one operator's live work state reached the public history before
anyone noticed. `tools/leakguard/` exists because of that, and CI runs it on
every push and pull request.

Install the hook so you find out before a commit exists rather than after:

```
cp tools/leakguard/pre-commit .git/hooks/pre-commit
cp tools/leakguard/leakguard.toml.example tools/leakguard/leakguard.toml
# then fill in the names you personally must not publish; the file is gitignored
```

CI runs base rules (emails, cloud GUIDs, home paths, internal hosts). Names need
a denylist, and a denylist of real names is itself a list of real names, so it
never lives in the tree.

## Tests

`uv run pytest -q`. A fix gets a test that **fails against the commit before
it** — several bugs here were invisible to a suite that only asserted success,
and one security fix was itself forgeable because its test picked the easy case.

Assert on the artefact, not the call. `rc=0` has lied more than once.

## Commit messages

Say what changed and why it was wrong before, in prose. The reasoning is the
part that survives; a bullet list of file names is already in the diff.

## Scope

The gate is the contract. `Beads.update()` is the single point where work
reaches `done`; do not add a second way. If you think you need one, that is
worth a conversation first.
