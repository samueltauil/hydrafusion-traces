# 01 — Trivial

**Predicted routing:** `single`, cheapest available model.
**Observed routing:** `cascade`. `gpt-5.6-luna` drafted, `gpt-5.6-sol` reviewed and
returned `reject`, `gpt-5.6-sol` produced the final version. 13.11 AIU for a two-file
rename.

Intended to establish the floor. It instead showed that our sense of "trivial" is not the
router's, and introduced a model we had not seen. Routing is served remotely and this was
a single observation. See docs/FINDINGS.md.

---

Rename the `updated` field on `TokenBucket` to `last_refill_at` throughout
`ratelimit.py` and `test_ratelimit.py`. Do not change any behaviour.
