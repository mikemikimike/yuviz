# Review: 03-tasks.md
VERDICT: GREEN

1. [minor] T4's RuntimeError sub-case will never reach `parse_graph` if it reuses the RuntimeConfig from the broken-JSON sub-case in the same test, because that path already `_cache_put` its fallback (runner.py:290-295) and the next non-draft call returns the cached graph at :274-276, so no `exc_info` record appears — T4 (Test plan 3) — fix: have the RuntimeError sub-case use a distinct agent id/slug, or clear `_GRAPH_CACHE` between the two sub-cases.
