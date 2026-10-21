from collections.abc import Iterable
from contextlib import asynccontextmanager

from fastapi import Request

from src.auth.keys import AuthContext
from src.common.errors import ModelBusy, RateLimited
from src.ratelimit import RateLimiter

# What an admin can set on an API key, and what a model can set on its route. Absent = the Router's default, 0 = unlimited.
KEY_FIELDS = ("rpm", "poll_rpm", "jobs_per_day", "max_outstanding_jobs")
MODEL_FIELDS = ("rpm_per_key", "rpm_total", "max_concurrency")


def parse_limits(raw, fields: Iterable[str]) -> dict[str, int]:
    """Keeps the known fields whose value is a non-negative integer. Anything else is dropped, never an error: a bad
    record must fall back to the defaults, not lock a client or a model out."""
    if not isinstance(raw, dict):
        return {}
    return {
        field: raw[field]
        for field in fields
        if isinstance(raw.get(field), int) and not isinstance(raw[field], bool) and raw[field] >= 0
    }


class ConcurrencyGate:
    """Counts requests in flight per model in this process and refuses instead of queueing, so one slow model cannot
    pile up waiting requests. Each Router replica has its own count."""

    def __init__(self):
        self._inflight: dict[str, int] = {}

    @asynccontextmanager
    async def slot(self, model: str, limit: int):
        count = self._inflight.get(model, 0)
        if limit > 0 and count >= limit:
            raise ModelBusy("Model is busy; retry shortly", headers={"Retry-After": "1"})
        self._inflight[model] = count + 1
        try:
            yield
        finally:
            remaining = self._inflight[model] - 1
            if remaining > 0:
                self._inflight[model] = remaining
            else:
                del self._inflight[model]


async def enforce_model_limits(request: Request, auth: AuthContext, model: str, route) -> None:
    """The model's own request budgets, on top of the caller's key budget. Called once the caller is known to be allowed
    to use the model, so requests for models they cannot see never spend it."""
    settings = request.app.state.settings
    limiter = RateLimiter(request.app.state.redis)
    budgets = (
        ("model-key", f"{model}:{auth.key_id}", route.limits.get("rpm_per_key", settings.model_rpm_per_key)),
        ("model-total", model, route.limits.get("rpm_total", settings.model_rpm_total)),
    )
    for scope, subject, limit in budgets:
        retry_after = await limiter.hit(scope, subject, limit=limit, window=60)
        if retry_after is not None:
            raise RateLimited("Rate limit exceeded", headers={"Retry-After": str(retry_after)})
