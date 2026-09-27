"""Paired exact McNemar + Holm-Bonferroni, matching
evals/sopbench-bank-asop/score_asop_arms.py's `mcnemar_exact`/`paired` bodies
(kept as a fresh copy for the same reason that file gives for not importing
`score_ladder.py`: one function per comparison type, no import-time coupling
to that script's cached DOMAIN/MODEL/SUBSET globals — see scoring.py's
docstring).
"""

from __future__ import annotations

from math import comb


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact binomial p for discordant pairs b (A-only) vs c (B-only)."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(comb(n, i) for i in range(0, k + 1)) / (2**n)
    return min(1.0, 2 * tail)


def paired_delta(a_success: dict[int, bool], b_success: dict[int, bool], keys: list[int]) -> dict:
    """`a` vs `b` on `success`, paired exact McNemar with a 95% Wald CI on the delta."""
    b = sum(1 for k in keys if a_success[k] and not b_success[k])
    c = sum(1 for k in keys if b_success[k] and not a_success[k])
    n = len(keys)
    d = (sum(a_success[k] for k in keys) - sum(b_success[k] for k in keys)) / n
    se = ((b + c - (b - c) ** 2 / n) ** 0.5) / n if (b + c) else 0.0
    return {
        "delta": d,
        "lo": d - 1.96 * se,
        "hi": d + 1.96 * se,
        "gained": b,
        "lost": c,
        "p": mcnemar_exact(b, c),
        "n": n,
    }


def holm_bonferroni(named_pvalues: list[tuple[str, float]], alpha: float = 0.05) -> dict[str, dict]:
    """Holm's step-down correction. Returns {name: {"p": ..., "rank": ...,
    "threshold": ..., "reject": bool}}, `reject` computed with Holm's
    step-down rule (once a hypothesis fails to clear its threshold, every
    hypothesis ranked below it is also not rejected, even if its own raw p
    would have cleared a later, larger threshold).
    """
    m = len(named_pvalues)
    ordered = sorted(named_pvalues, key=lambda kv: kv[1])
    out: dict[str, dict] = {}
    blocked = False
    for rank, (name, p) in enumerate(ordered, start=1):
        threshold = alpha / (m - rank + 1)
        reject = (not blocked) and (p <= threshold)
        if not reject:
            blocked = True
        out[name] = {"p": p, "rank": rank, "threshold": threshold, "reject": reject}
    return out
