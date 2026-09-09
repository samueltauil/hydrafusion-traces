# 02 — Subtle bug

**Predicted routing:** `cascade` with a rejecting judge.
**Observed routing:** `single`. See docs/FINDINGS.md.

Kept precisely because the prediction was wrong. A boundary bug where the obvious fix is
the wrong one looks like the textbook case for a review step to catch a weak draft, and
the router went straight to the strongest model — arguably the better call. Across 20
turns no simple rule emerged for what triggers a compound pattern.

---

`test_sliding_window_rejects_over_limit` fails in `ratelimit.py`. Diagnose the
off-by-one, decide whether the boundary belongs in the eviction cutoff or the
capacity check, fix it, and explain why the other candidate fix would be wrong.
Then run the tests to prove it.
