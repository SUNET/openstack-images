"""Rate limiting for OpenStack API calls."""

import logging
import os
import threading
import time
from contextlib import contextmanager
from typing import Generator

from metrics import RATE_LIMIT_WAIT_SECONDS

logger = logging.getLogger(__name__)


class RateLimiter:
    """Thread-safe rate limiter using semaphore and token bucket.

    Limits both concurrent requests and requests per second to prevent
    overwhelming OpenStack APIs.
    """

    def __init__(
        self,
        max_concurrent: int = 10,
        requests_per_second: float = 20.0,
    ) -> None:
        """Initialize rate limiter.

        Args:
            max_concurrent: Maximum number of concurrent API calls
            requests_per_second: Maximum requests per second (averaged)
        """
        self._semaphore = threading.Semaphore(max_concurrent)
        self._min_interval = 1.0 / requests_per_second if requests_per_second > 0 else 0
        self._last_call_time = 0.0
        self._lock = threading.Lock()
        self._max_concurrent = max_concurrent
        self._requests_per_second = requests_per_second

        logger.info(
            "Rate limiter initialized: max_concurrent=%d, requests_per_second=%.1f",
            max_concurrent,
            requests_per_second,
        )

    @contextmanager
    def acquire(self) -> Generator[None, None, None]:
        """Acquire rate limit slot (context manager).

        Usage:
            with rate_limiter.acquire():
                # make API call
        """
        wait_start = time.monotonic()
        self._semaphore.acquire()
        try:
            # Enforce minimum interval between requests
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_call_time
                interval_wait = self._min_interval - elapsed

                if interval_wait > 0:
                    time.sleep(interval_wait)

                self._last_call_time = time.monotonic()

            # Record total wait time (semaphore + interval)
            total_wait = time.monotonic() - wait_start
            if total_wait > 0.001:  # Only record waits > 1ms
                RATE_LIMIT_WAIT_SECONDS.observe(total_wait)

            yield
        finally:
            self._semaphore.release()

    def __repr__(self) -> str:
        return (
            f"RateLimiter(max_concurrent={self._max_concurrent}, "
            f"requests_per_second={self._requests_per_second})"
        )


# Global rate limiter instance (initialized lazily)
_rate_limiter: RateLimiter | None = None
_rate_limiter_lock = threading.Lock()


def get_rate_limiter() -> RateLimiter:
    """Get or create the global rate limiter.

    Configuration via environment variables:
        OPENSTACK_MAX_CONCURRENT_CALLS: Max concurrent API calls (default: 10)
        OPENSTACK_REQUESTS_PER_SECOND: Max requests/second (default: 20)
    """
    global _rate_limiter

    if _rate_limiter is None:
        with _rate_limiter_lock:
            # Double-check after acquiring lock
            if _rate_limiter is None:
                max_concurrent = int(
                    os.environ.get("OPENSTACK_MAX_CONCURRENT_CALLS", "10")
                )
                requests_per_second = float(
                    os.environ.get("OPENSTACK_REQUESTS_PER_SECOND", "20")
                )
                _rate_limiter = RateLimiter(
                    max_concurrent=max_concurrent,
                    requests_per_second=requests_per_second,
                )

    return _rate_limiter
