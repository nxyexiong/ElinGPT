"""Payload accounting without recording game text, credentials or tool arguments."""

from __future__ import annotations

import json
from typing import Any, TextIO

from .observation import _size


class Metrics:
    def __init__(self, sink: TextIO | None = None):
        self.sink = sink
        self.tools: dict[str, dict[str, int]] = {}

    def record(self, name: str, value: dict[str, Any], *, elapsed_ms: int = 0,
               usage: dict[str, Any] | None = None) -> None:
        size = _size(value)
        totals = self.tools.setdefault(name, {"calls": 0, "bytes": 0, "max_bytes": 0})
        totals["calls"] += 1
        totals["bytes"] += size
        totals["max_bytes"] = max(totals["max_bytes"], size)
        if self.sink is None:
            return
        observation = value.get("observation", value)
        state = observation.get("state", observation.get("state_delta", {}))
        record = {"name": name, "bytes": size, "elapsed_ms": elapsed_ms,
                  "delta": observation.get("delta", False),
                  "section_bytes": {key: _size(item) for key, item in state.items()}}
        if usage is not None:
            record["usage"] = {key: usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                               if type(usage.get(key)) is int}
        self.sink.write(json.dumps(record, separators=(",", ":")) + "\n")
        self.sink.flush()
