"""codebench — a reproduce/propose/validate coding bug-fix benchmark.

Tests an ASOP (an explicit, gated repro -> fix -> validate procedure) against
a bare baseline and two intermediate scaffolds, on real bug-fix datasets
(HumanEvalFix, QuixBugs), against any OpenAI-compatible chat-completions
endpoint with tool calling (z.ai/GLM today, a local LM Studio model later).

See `evals/codebench/CODEBENCH.md` for the pre-registered design and
hypotheses, and `evals/codebench/data/*/PROVENANCE.md` for dataset sourcing.
"""
