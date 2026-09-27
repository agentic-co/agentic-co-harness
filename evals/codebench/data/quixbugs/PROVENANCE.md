# QuixBugs (Python, flat subset) — provenance

- Source: `jkoppel/QuixBugs` on GitHub
- Commit: `4257f44b0ff1181dedaedee6a447e133219fcebf`
- Licence: MIT (repo LICENSE, James Koppel, 2017-2019)
- Tasks: 31 of the 40 total programs
- **Scope decision**: the 9 graph-based programs (breadth_first_search, depth_first_search, detect_cycle, minimum_spanning_tree, reverse_linked_list, shortest_path_length, shortest_path_lengths, shortest_paths, topological_ordering) are EXCLUDED from v1. Their tests are hand-written functions over a shared `Node` helper, not parametrized [input, expected] pairs, so the visible/hidden split this script does by slicing a case list doesn't apply cleanly. Follow-up, not silently dropped.
- Visible/hidden split: first `round(0.3 * n)` cases (min 1, at least 1 held out) from `json_testcases/<name>.json` are visible; the rest are hidden. This split is OURS (QuixBugs ships one undifferentiated case list per program) — unlike HumanEvalFix, where visible/hidden are both authored upstream.
