from ratelimit import SlidingWindowLimiter, TokenBucket


def test_sliding_window_allows_up_to_limit():
    limiter = SlidingWindowLimiter(max_events=3, window_seconds=1.0)
    assert limiter.allow(now=0.0)
    assert limiter.allow(now=0.1)
    assert limiter.allow(now=0.2)


def test_sliding_window_rejects_over_limit():
    limiter = SlidingWindowLimiter(max_events=3, window_seconds=1.0)
    limiter.allow(now=0.0)
    limiter.allow(now=0.1)
    limiter.allow(now=0.2)
    assert not limiter.allow(now=0.3)


def test_sliding_window_recovers_after_window():
    limiter = SlidingWindowLimiter(max_events=3, window_seconds=1.0)
    for t in (0.0, 0.1, 0.2):
        limiter.allow(now=t)
    assert limiter.allow(now=1.5)


def test_token_bucket_refills():
    bucket = TokenBucket(capacity=2, refill_per_second=1.0, now=0.0)
    assert bucket.consume(now=0.0)
    assert bucket.consume(now=0.0)
    assert not bucket.consume(now=0.0)
    assert bucket.consume(now=1.0)
