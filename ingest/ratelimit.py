"""Token bucket for the Cerebras Gemma-4 limits (gemma-4-31b):
   100 req/min, 100k tokens/min (131k context, 144M tok/day).
Coarse pacing so the autonomous maintainer never trips the quota.
"""
from __future__ import annotations
import threading
import time

REQ_PER_MIN = 90      # headroom under 100
TOK_PER_MIN = 90_000  # headroom under 100k


class TokenBucket:
    def __init__(self, req_per_min: int = REQ_PER_MIN, tok_per_min: int = TOK_PER_MIN):
        self.req_cap = req_per_min
        self.tok_cap = tok_per_min
        self.req = req_per_min
        self.tok = tok_per_min
        self.ts = time.monotonic()
        self.lock = threading.Lock()

    def _refill(self):
        now = time.monotonic()
        dt = now - self.ts
        self.req = min(self.req_cap, self.req + dt * self.req_cap / 60.0)
        self.tok = min(self.tok_cap, self.tok + dt * self.tok_cap / 60.0)
        self.ts = now

    def acquire(self, est_tokens: int = 1500) -> None:
        """Block until 1 request + est_tokens are available."""
        est = min(est_tokens, self.tok_cap)
        while True:
            with self.lock:
                self._refill()
                if self.req >= 1 and self.tok >= est:
                    self.req -= 1
                    self.tok -= est
                    return
                need_tok = (est - self.tok) * 60.0 / self.tok_cap
                need_req = (1 - self.req) * 60.0 / self.req_cap
                wait = max(0.05, need_tok, need_req)
            time.sleep(min(wait, 5.0))


GLOBAL = TokenBucket()
