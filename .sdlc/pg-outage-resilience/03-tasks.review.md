# Review: 03-tasks.md
VERDICT: AMBER

1. [minor] T2's done-when drops the design Test plan 1 assertion that `reconcile_inactive_calls` still runs in the iteration where `reconcile_dead_nodes` raised (design Interfaces step 5, "always run"), so an implementation that skips step 5 after a step-4 failure passes every listed check — 03-tasks.md:6 (T2) — fix: add "the outage test asserts `reconcile_inactive_calls` is called in the same iteration where the injected `reconcile_dead_nodes` raise occurs" to T2's done-when.
2. [minor] T5's done-when drops two design Test plan items: the `_LOOKUP_TIMEOUT_S <= 3` tripwire (Test plan 3), and the requirement that the `only=None/[]/["x"]` narrowing holds on the in-window short-circuit call as well as on the fallback call (Test plan 4). Without the second, a backoff branch that skips `_narrow` passes — 03-tasks.md:12 (T5) — fix: add both to T5's done-when, naming the in-window narrowing assertion explicitly.
