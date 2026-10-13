"""Usage-limit awareness and human notifications.

Claude reports usage-limit state on every agent session as RateLimitEvent messages
(status allowed / allowed_warning / rejected, utilization 0-1, and when the window
resets). Orchestra feeds every event into one shared UsageLimits tracker:

  - above `stop_new_at` utilization (with a warning) it stops starting new agents;
  - on `rejected` (or a rate-limit error) it pauses dispatch until the reset time,
    and the agent that was cut off is re-queued with its session kept, so it resumes
    where it stopped instead of starting over. A limit pause never counts as a failed attempt.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import time
from dataclasses import dataclass

from .config import Limits

LIMIT_MARKERS = ("rate limit", "rate_limit", "usage limit", "limit reached", "429", "overloaded_error")


@dataclass
class LimitSignal:
    status: str = "allowed"           # allowed | allowed_warning | rejected
    utilization: float | None = None
    resets_at: float | None = None    # unix seconds
    kind: str | None = None           # five_hour, seven_day, ...


def looks_like_limit(text: str) -> bool:
    t = (text or "").lower()
    return any(m in t for m in LIMIT_MARKERS)


class UsageLimits:
    def __init__(self, limits: Limits, log=print, clock=time.time):
        self.cfg, self.log, self.clock = limits, log, clock
        self.last = LimitSignal()
        self.paused_until: float | None = None
        self._first_pause_at: float | None = None

    def observe(self, sig: LimitSignal) -> None:
        self.last = sig
        if sig.status == "rejected":
            self.pause(sig.resets_at, sig.kind)

    def pause(self, resets_at: float | None, kind: str | None = None) -> None:
        now = self.clock()
        until = (resets_at + self.cfg.resume_buffer_s) if resets_at and resets_at > now \
            else now + self.cfg.fallback_wait_min * 60
        if self.paused_until is None or until > self.paused_until:
            self.paused_until = until
            self._first_pause_at = self._first_pause_at or now
            when = time.strftime("%H:%M", time.localtime(until))
            self.log(f"usage limit reached{f' ({kind})' if kind else ''}: pausing new work until ~{when}")

    @property
    def paused(self) -> bool:
        return self.paused_until is not None and self.clock() < self.paused_until

    def can_dispatch(self) -> bool:
        if self.paused:
            return False
        s = self.last
        return not (s.status == "allowed_warning" and s.utilization is not None
                    and s.utilization >= self.cfg.stop_new_at)

    def gave_up(self) -> bool:
        return (self._first_pause_at is not None and
                self.clock() - self._first_pause_at > self.cfg.max_wait_hours * 3600)

    async def wait(self, poll_s: float = 30.0) -> None:
        """Sleep until the pause ends (in short steps so Ctrl-C stays responsive)."""
        while self.paused:
            await asyncio.sleep(min(poll_s, max(self.paused_until - self.clock(), 0.05)))
        if self.paused_until is not None:
            self.log("usage window reset: resuming")
            self.paused_until = None
            self.last = LimitSignal()  # fresh window; the next event will tell us the truth


def notify(command: str, title: str, message: str) -> None:
    """Fire-and-forget user notification via the configured shell command."""
    if not command:
        return
    try:
        subprocess.Popen(command, shell=True, env={**os.environ, "ORCHESTRA_TITLE": title,
                                                   "ORCHESTRA_MESSAGE": message},
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass
