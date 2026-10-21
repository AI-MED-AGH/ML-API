class ResponseBudget:
    """Caps how many upstream response bytes one Router process holds in memory at once (0 = unlimited).

    No waiting: a request that does not fit is refused, so a few huge replies cannot queue up memory behind each other."""

    def __init__(self, limit: int):
        self._limit = limit
        self.in_use = 0

    def reserve(self, size: int) -> bool:
        if self._limit > 0 and self.in_use + size > self._limit:
            return False
        self.in_use += size
        return True

    def release(self, size: int) -> None:
        self.in_use = max(0, self.in_use - size)
