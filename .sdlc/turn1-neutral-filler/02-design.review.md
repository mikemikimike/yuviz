# Review: 02-design.md (turn1-neutral-filler)
VERDICT: GREEN

1. [minor] Test plan 4 tells the implementer to patch only `_TABLES`/`_SHORTEST_FIRST_TURN`, but the no-pool path it asserts goes through `shortest_tool_filler`, which reads `_SHORTEST`; left unpatched, `_SHORTEST.get("xx", _SHORTEST["en"])` returns "One moment." and the test fails for the wrong reason — Test plan item 4 — fix: list `_SHORTEST` among the patched dicts, built the same way as `test_i18n.py:137-139`.
