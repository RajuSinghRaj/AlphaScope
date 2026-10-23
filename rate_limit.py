"""Bounded, process-local fixed-window rate limiting for public HTTP routes."""
from __future__ import annotations

import ipaddress
import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable


@dataclass
class _Window:
    expires_at: float
    count: int


class ProcessRateLimiter:
    """Thread-safe limiter; overflow clients share a bounded per-route bucket."""

    def __init__(self, max_keys: int = 4096, clock: Callable[[], float] = time.monotonic):
        self.max_keys = max(1, int(max_keys))
        self.clock = clock
        self._entries: OrderedDict[tuple[str, str], _Window] = OrderedDict()
        self._overflow: dict[str, _Window] = {}
        self._lock = threading.Lock()

    @staticmethod
    def client_key(address: object) -> str:
        if not isinstance(address, str) or not address or len(address) > 64:
            return "unknown"
        try:
            return ipaddress.ip_address(address).compressed
        except ValueError:
            return "unknown"

    def _cleanup(self, now: float) -> None:
        expired = [key for key, window in self._entries.items() if window.expires_at <= now]
        for key in expired:
            self._entries.pop(key, None)
        for route in [route for route, window in self._overflow.items() if window.expires_at <= now]:
            self._overflow.pop(route, None)

    def check(self, route: str, address: object, limit: int, window_seconds: int) -> tuple[bool, int]:
        """Return (allowed, retry_after_seconds); retry is zero when allowed."""
        now = self.clock()
        limit = max(1, int(limit))
        window_seconds = max(1, int(window_seconds))
        client = self.client_key(address)
        key = (route, client)
        with self._lock:
            self._cleanup(now)
            bucket = self._entries.get(key)
            if bucket is None and len(self._entries) >= self.max_keys:
                bucket = self._overflow.get(route)
                if bucket is None:
                    bucket = _Window(now + window_seconds, 0)
                    self._overflow[route] = bucket
            elif bucket is None:
                bucket = _Window(now + window_seconds, 0)
                self._entries[key] = bucket
            else:
                self._entries.move_to_end(key)

            if bucket.count >= limit:
                return False, max(1, math.ceil(bucket.expires_at - now))
            bucket.count += 1
            return True, 0
