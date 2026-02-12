"""Tests for rate limiting."""

import threading
import time

from ratelimit import RateLimiter


class TestRateLimiter:
    """Tests for RateLimiter class."""

    def test_allows_single_request(self):
        limiter = RateLimiter(max_concurrent=10, requests_per_second=100)

        with limiter.acquire():
            pass  # Should not block

    def test_enforces_concurrent_limit(self):
        limiter = RateLimiter(max_concurrent=2, requests_per_second=1000)
        active_count = 0
        max_active = 0
        lock = threading.Lock()

        def worker():
            nonlocal active_count, max_active
            with limiter.acquire():
                with lock:
                    active_count += 1
                    max_active = max(max_active, active_count)
                time.sleep(0.05)
                with lock:
                    active_count -= 1

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert max_active <= 2

    def test_enforces_rate_limit(self):
        # 10 requests per second = 100ms between requests
        limiter = RateLimiter(max_concurrent=10, requests_per_second=10)

        start = time.monotonic()
        for _ in range(3):
            with limiter.acquire():
                pass
        elapsed = time.monotonic() - start

        # 3 requests at 10/sec should take at least 200ms (2 intervals)
        assert elapsed >= 0.18  # Allow small tolerance

    def test_repr(self):
        limiter = RateLimiter(max_concurrent=5, requests_per_second=50)
        repr_str = repr(limiter)

        assert "max_concurrent=5" in repr_str
        assert "requests_per_second=50" in repr_str
