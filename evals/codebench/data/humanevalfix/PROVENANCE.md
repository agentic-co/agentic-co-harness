# HumanEvalFix — provenance

- Source: `bigcode/humanevalpack` on Hugging Face, `python` config, `test` split
- Dataset commit/sha: `9a41762f73a8cb23bb5811b73d5aab164efcf378`
- Licence: MIT (per HF dataset card `cardData.license`)
- Fetched: "2025-08-19T20:35:51.000Z"
- Tasks: 164
- Visible test = `example_test` field (1-2 assertions); hidden test = `test` field (the full held-out suite, 5-8+ assertions). Both fields ship in the dataset itself — this is not a synthesized split, unlike QuixBugs below.
- `import`/`test_setup` fields are empty for every Python row (checked all 164) — not used.
- `bug_type` is carried through (6 categories: value/operator/variable/function misuse, missing/excess logic). See `datasets.HARDER_BUG_TYPES` for the harder/easier split this build uses to build a harder task pool.
