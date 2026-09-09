# 03 — Architectural

**Predicted routing:** `critique`.
**Observed routing:** `cascade`. `mai-code-1.1-flash` drafted, `gpt-5.6-sol`
reviewed and returned `reject`, `gpt-5.6-sol` produced the final answer. Three phases,
14.72 AIU.

This is the prompt that makes the dashboard worth looking at. A comparable open-ended
design question about distributed cache eviction later routed to a `cascade` whose
reviewer **accepted** the cheap draft, for 2.39 AIU total. Same shape of task, opposite
outcome, which is the behaviour worth measuring at scale.

---

Design a retry and backoff policy for a flaky third-party payments API that is
not idempotent, can return 200 with a body that indicates failure, and
occasionally double-charges when retried. Cover idempotency key strategy,
jitter, budget exhaustion, circuit breaking, and reconciliation of ambiguous
outcomes. Justify each tradeoff and name what you would give up.
