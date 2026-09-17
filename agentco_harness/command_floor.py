"""Predeclared command floor for agent execution.

WHY THIS EXISTS
---------------
ISC-4 states that every enforcement rule which today lives as a Claude Code hook
must be re-expressed as an ASOP gate check / identity-policy rule, verified by
the same rule producing an IDENTICAL REFUSAL CODE under two different executor
adapters.

The original `CommandFloorGuard.hook.ts` applies a set of baseline safety rules
regardless of which agent is running, which vendor it routes to, or which
permission flags it was launched with. It acts as a safety net against
catastrophic mistakes (e.g., force-pushing over shared history) in unsupervised
runs where recovery is difficult or impossible.

This module ports the `force-push-protected` rule.

HOW IT WORKS
------------
We inspect the command string to determine if it is a destructive force push.
The source TypeScript hook relied on pure string matching and only blocked
if the protected branch name (main or master) explicitly appeared in the command,
treating `--force-with-lease` as safe.

This Python implementation makes the following decisions:
1. `--force-with-lease` is ALLOWED. We preserve the source hook's logic because
   force-with-lease fails on the server side if the remote ref has moved since
   the last fetch. It is not a blind overwrite, so it does not carry the same
   "rewrites shared history without knowing" risk.
2. Implicit pushes FAIL CLOSED. If a user runs `git push --force` without specifying
   a refspec, the source hook silently allowed it (unless they were literally on
   a branch named main, wait, no, the hook required the word 'main' or 'master'
   in the command, so implicit pushes passed silently). We reverse this.
   If no explicit branch is found in the command, we check the `current_branch`
   parameter. If `current_branch` is not supplied, we lack the context to know
   what is being pushed and fail closed, raising a refusal. This matches this
   repository's fail-closed policy. If `current_branch` is supplied and is
   protected, we refuse.
"""

from __future__ import annotations

import shlex

COMMAND_FLOOR_FORCE_PUSH_PROTECTED = "command_floor:force_push_protected"

COMMAND_FLOOR_CODES = frozenset({
    COMMAND_FLOOR_FORCE_PUSH_PROTECTED,
})


class _CodedRefusal(Exception):
    """A refusal that carries a machine code alongside its prose.

    The code identifies the RULE, never the request — that is the whole point.
    Two agents denied by the same rule carry the same code and different
    messages, which is what makes the code branchable and the message useful.
    """
    default_code = "command_floor:unclassified"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code or self.default_code


class CommandFloorViolation(_CodedRefusal):
    """A command violated the predeclared command floor."""
    default_code = COMMAND_FLOOR_FORCE_PUSH_PROTECTED


def check_command(
    command: str,
    agent: str,
    *,
    protected_branches: frozenset[str] = frozenset({"main", "master"}),
    current_branch: str | None = None,
) -> None:
    """Check a command against the command floor rules.
    
    Raises CommandFloorViolation if a rule is violated, returns None otherwise.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        # Fallback for malformed shell strings (e.g. unclosed quotes)
        tokens = command.split()
        
    # Strip basic quotes from tokens to match how Claude Code normalizes
    tokens = [t.strip("'\"") for t in tokens]

    # 1. Is this a git push?
    if "git" not in tokens:
        return None
        
    git_indices = [i for i, t in enumerate(tokens) if t == "git"]
    push_idx = -1
    for gi in git_indices:
        try:
            push_idx = tokens.index("push", gi)
            break
        except ValueError:
            pass
            
    if push_idx == -1:
        return None

    # The window this ONE `git push` invocation actually owns: from its first
    # argument up to the next shell operator (&&, ||, ;, |, a redirect).
    # EVERY rule below reads from `cmd_args`, never raw `tokens` — scanning
    # outside this window is how a command chained before or after the push
    # leaks into the decision. This was computed AFTER the force check in an
    # earlier draft, and that ordering was itself a bug: `grep -f
    # patterns.txt && git push origin main` was refused as a force-push,
    # when the actual `git push` has no force flag at all — the `-f` an
    # unrelated PRECEDING command's flag. Confirmed empirically (2026-09-17)
    # by probing this exact function, the same way the branch-name false
    # positive below was found. Known remaining limitation, not fixed here
    # because it is a larger, separate redesign: a compound command with
    # TWO `git push` invocations only ever has the first evaluated — this
    # function returns as soon as it decides the first one is safe, so
    # `git push origin safe && git push -f origin main` would not be caught.
    args_after = tokens[push_idx + 1:]
    shell_ops = {"&&", "||", ";", "|", ">", "<", ">>"}
    cmd_args = []
    for arg in args_after:
        if arg in shell_ops:
            break
        cmd_args.append(arg)

    # 2. Is it a force push?
    # We must find --force or -f, but NOT --force-with-lease.
    has_force = "-f" in cmd_args or "--force" in cmd_args
    if not has_force:
        # Check combined short flags like -uf (though git push doesn't typically use this, we handle it defensively)
        for t in cmd_args:
            if t.startswith("-") and not t.startswith("--") and "f" in t:
                has_force = True
                break

    if not has_force:
        return None

    # 3. Detect explicitly named protected branches.
    # Scoped to `cmd_args` (this push's own window) — never the whole command.
    # A false positive was found and confirmed empirically (2026-09-17) by
    # probing this exact function: `git commit -m "update main docs" &&
    # git push -f origin feature-branch` was refused, because the word "main"
    # in the UNRELATED, unquoted commit message BEFORE the push matched this
    # loop when it scanned every token in the string. The push target was
    # `feature-branch` — a genuinely safe push — and the rule blocked it
    # anyway. (The original CommandFloorGuard.hook.ts carries the identical
    # flaw; this port does not need to repeat it to be faithful, and a
    # security floor that refuses unrelated safe commands is exactly the kind
    # of over-triggering this project's own evidence programme has spent this
    # session arguing against — N15's "a gate that fires on everything is
    # indistinguishable from always-refuse" applies here as much as it does
    # to an ASOP gate. `push_idx` alone was not enough: an UNRELATED command
    # chained AFTER this push, e.g. `git push -f origin safe && git commit -m
    # "fix main docs"`, would leak in from the other direction without also
    # stopping at the same shell-operator boundary rule 4 already computes.)
    for p_branch in protected_branches:
        for t in cmd_args:
            # Handle refs/heads/main, HEAD:main, HEAD:refs/heads/main
            parts = t.replace(':', '/').split('/')
            if p_branch in parts:
                raise CommandFloorViolation(
                    f"[CommandFloorGuard] DENIED by the predeclared command floor.\n\n"
                    f"  rule    force-push-protected\n"
                    f"  what    force-push to main/master\n"
                    f"  why     rewrites shared history; recovery depends on someone still holding the objects\n"
                    f"  command {command[:200] + '...' if len(command) > 200 else command}\n\n"
                    f"This floor applies to every agent, vendor, and permission mode.\n"
                    f"Agent {agent!r} attempted to force-push an explicitly named protected branch ({p_branch}).",
                    code=COMMAND_FLOOR_FORCE_PUSH_PROTECTED
                )

    # 4. Check if it's an explicit push to a safe branch.
    non_flags = [a for a in cmd_args if not a.startswith('-')]
    
    # A refspec is provided if there are at least two non-flags (remote + refspec)
    # or if the single non-flag contains a colon (like HEAD:some-branch).
    has_explicit_refspec = len(non_flags) >= 2 or (len(non_flags) == 1 and ":" in non_flags[0])
    
    if has_explicit_refspec:
        # They specified a refspec, and it wasn't a protected branch. Safe to push.
        return None

    # 5. Implicit target branch.
    # We couldn't find an explicit safe refspec. They might be doing `git push -f`
    # We must fail closed unless we know the current branch is safe.
    if current_branch is not None:
        if current_branch in protected_branches:
            raise CommandFloorViolation(
                f"[CommandFloorGuard] DENIED by the predeclared command floor.\n\n"
                f"  rule    force-push-protected\n"
                f"  what    force-push to main/master\n"
                f"  why     rewrites shared history; recovery depends on someone still holding the objects\n"
                f"  command {command[:200] + '...' if len(command) > 200 else command}\n\n"
                f"This floor applies to every agent, vendor, and permission mode.\n"
                f"Agent {agent!r} attempted an implicit force-push while the current branch is {current_branch!r}.",
                code=COMMAND_FLOOR_FORCE_PUSH_PROTECTED
            )
        else:
            return None
    else:
        raise CommandFloorViolation(
            f"[CommandFloorGuard] DENIED by the predeclared command floor.\n\n"
            f"  rule    force-push-protected\n"
            f"  what    force-push to main/master\n"
            f"  why     rewrites shared history; recovery depends on someone still holding the objects\n"
            f"  command {command[:200] + '...' if len(command) > 200 else command}\n\n"
            f"This floor applies to every agent, vendor, and permission mode.\n"
            f"Agent {agent!r} attempted an implicit force-push, and `current_branch` was not provided to verify safety. "
            f"This is denied by default (fail-closed). Specify a branch or use --force-with-lease.",
            code=COMMAND_FLOOR_FORCE_PUSH_PROTECTED
        )
