"""Shared HTTP + helpers with polite rate limiting and retries.

Boot Barn sits behind bot protection that blocks plain `requests` (TLS/JA3
fingerprinting -> "Access denied"). We use curl_cffi, which impersonates a real
Chrome TLS fingerprint and sails through. Same idea as a headless browser but
far lighter and schedulable.
"""
from __future__ import annotations

import random
import sys
import time
from pathlib import Path

from curl_cffi import requests as creq

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402

# curl_cffi rotates through a few recent Chrome fingerprints on session refresh.
IMPERSONATE_POOL = ["chrome", "chrome124", "chrome123", "chrome120"]

# Markers of a bot-wall challenge page (served as HTTP 200 with HTML).
_BLOCK_MARKERS = (
    b"Access to this page has been denied",
    b"Pardon Our Interruption",
    b"px-captcha",
    b"/_Incapsula_",
)


def _looks_blocked(r) -> bool:
    body = r.content[:4000]
    return any(m in body for m in _BLOCK_MARKERS)


class PoliteSession:
    """curl_cffi session that rate-limits, retries, and recovers from bot walls.

    Boot Barn's protection (PerimeterX / Yottaa) intermittently serves a
    challenge page as HTTP 200 after sustained crawling. We detect that, refresh
    the session (new cookies + rotated fingerprint), back off, and retry -- and
    we proactively rotate the session every `refresh_every` requests.
    """

    def __init__(self, delay: float = config.REQUEST_DELAY_SEC,
                 jitter: float = config.REQUEST_JITTER, refresh_every: int = 60):
        self.delay = delay
        self.jitter = jitter
        self.refresh_every = refresh_every
        self.proxies = ({"http": config.PROXY_URL, "https": config.PROXY_URL}
                        if config.PROXY_URL else None)
        self._last = 0.0
        self._count = 0
        self._imp_i = 0
        self._new_session()

    def _new_session(self) -> None:
        imp = IMPERSONATE_POOL[self._imp_i % len(IMPERSONATE_POOL)]
        self._imp_i += 1
        self.s = creq.Session(impersonate=imp, proxies=self.proxies)
        self._warm_up()

    def _warm_up(self) -> None:
        """Hit the homepage to acquire bot-protection cookies before crawling.

        A brand-new session with no cookies is challenged on its first hit, so
        we prime it here and ignore the result (the challenge sets the cookies
        we need for subsequent requests)."""
        try:
            self.s.get(config.BASE_URL, timeout=config.REQUEST_TIMEOUT)
        except Exception:
            pass

    def _throttle(self) -> None:
        # Randomize the gap so we don't hit the site on a robotic fixed cadence.
        target = self.delay * (1.0 + random.uniform(0.0, self.jitter))
        wait = target - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def get(self, url: str, **kw):
        kw.setdefault("timeout", config.REQUEST_TIMEOUT)
        self._count += 1
        if self._count % self.refresh_every == 0:
            self._new_session()
        for attempt in range(1, config.MAX_RETRIES + 1):
            self._throttle()
            try:
                r = self.s.get(url, **kw)
                if r.status_code == 429 or r.status_code >= 500 or _looks_blocked(r):
                    # transient error or bot-wall challenge -> refresh + back off
                    self._new_session()
                    time.sleep(min(2 ** attempt + attempt, 20))
                    continue
                return r
            except Exception as e:  # curl_cffi raises its own error types
                if attempt == config.MAX_RETRIES:
                    print(f"  ! request failed {url}: {e}", file=sys.stderr)
                    return None
                self._new_session()
                time.sleep(min(2 ** attempt, 15))
        return None


def log(msg: str) -> None:
    print(msg, flush=True)
