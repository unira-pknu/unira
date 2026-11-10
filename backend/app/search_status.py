"""Per-request search health, separate from the existence of matching evidence."""

from __future__ import annotations

from typing import Iterable


class SearchResults(list[dict]):
    """List-compatible hits with health that survives even an empty result."""

    def __init__(self, rows: Iterable[dict] = (), *, channels: Iterable[dict] = (),
                 failed_queries: Iterable[str] = ()) -> None:
        super().__init__(rows)
        self.channels = list(channels)
        self.failed_queries = list(failed_queries)

    @property
    def degraded(self) -> bool:
        return bool(self.failed_queries) or any(c["status"] == "error" for c in self.channels)

    def diagnostics(self) -> dict:
        return {"status": "partial" if self.degraded else "complete",
                "channels": self.channels, "failed_queries": self.failed_queries}


class SearchUnavailable(RuntimeError):
    """No usable evidence while one or more primary channels failed."""

    def __init__(self, channels: Iterable[dict] = ()) -> None:
        super().__init__("search unavailable")
        self.channels = list(channels)
