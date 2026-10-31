# Review: 03-tasks.md
VERDICT: GREEN

1. [minor] T1's done-when requires every new Test plan 1 test to "fail on base", but it also includes the raising control, which by design (Test plan 1, AC5/22) passes on base, so the criterion cannot be met as written — T1 (03-tasks.md:11) — fix: scope "fail on base" to the zero-chunk and only-`b""` cases and require the raising control to pass on both base and head.
