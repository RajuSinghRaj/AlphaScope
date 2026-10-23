import unittest
from concurrent.futures import ThreadPoolExecutor

from rate_limit import ProcessRateLimiter


class FakeClock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


class ProcessRateLimiterTests(unittest.TestCase):
    def test_under_and_over_limit_returns_retry_after(self):
        clock = FakeClock()
        limiter = ProcessRateLimiter(clock=clock)
        self.assertEqual(limiter.check("news", "192.0.2.1", 2, 60), (True, 0))
        self.assertEqual(limiter.check("news", "192.0.2.1", 2, 60), (True, 0))
        self.assertEqual(limiter.check("news", "192.0.2.1", 2, 60), (False, 60))

    def test_expired_entries_are_removed(self):
        clock = FakeClock()
        limiter = ProcessRateLimiter(max_keys=2, clock=clock)
        limiter.check("news", "192.0.2.1", 5, 10)
        clock.now += 10
        limiter.check("news", "192.0.2.2", 5, 10)
        self.assertEqual(list(limiter._entries), [("news", "192.0.2.2")])

    def test_key_map_is_bounded_and_overflow_clients_share_a_bucket(self):
        limiter = ProcessRateLimiter(max_keys=2, clock=FakeClock())
        self.assertEqual(limiter.check("news", "192.0.2.1", 5, 60), (True, 0))
        self.assertEqual(limiter.check("news", "192.0.2.2", 5, 60), (True, 0))
        self.assertEqual(limiter.check("news", "192.0.2.3", 1, 60), (True, 0))
        self.assertEqual(limiter.check("news", "192.0.2.4", 1, 60)[0], False)
        self.assertLessEqual(len(limiter._entries), 2)
        self.assertLessEqual(len(limiter._overflow), 1)

    def test_invalid_addresses_share_safe_unknown_identity(self):
        self.assertEqual(ProcessRateLimiter.client_key(None), "unknown")
        self.assertEqual(ProcessRateLimiter.client_key("not-an-ip"), "unknown")
        limiter = ProcessRateLimiter(clock=FakeClock())
        self.assertEqual(limiter.check("macro", None, 1, 60), (True, 0))
        self.assertEqual(limiter.check("macro", "not-an-ip", 1, 60)[0], False)

    def test_concurrent_requests_cannot_exceed_limit(self):
        limiter = ProcessRateLimiter(clock=FakeClock())
        with ThreadPoolExecutor(max_workers=24) as pool:
            results = list(pool.map(lambda _: limiter.check("snapshot", "198.51.100.7", 7, 60), range(100)))
        self.assertEqual(sum(allowed for allowed, _ in results), 7)
        self.assertTrue(all(retry == 60 for allowed, retry in results if not allowed))


if __name__ == "__main__":
    unittest.main()
