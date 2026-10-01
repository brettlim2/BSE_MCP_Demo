"""Shared HTTP helpers: tiny in-memory TTL cache and value coercion.

The cache is process-local and volatile — it only collapses duplicate request
bursts within a few seconds. Nothing is written to disk. It is NOT a data store.
"""

from __future__ import annotations

import time
from typing import Any


class MicroCache:
    """A minimal TTL cache held entirely in memory."""

    def __init__(self, ttl: float) -> None:
        self._ttl = ttl
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        hit = self._store.get(key)
        if not hit:
            return None
        expires, value = hit
        if time.monotonic() > expires:
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        if self._ttl <= 0:
            return
        self._store[key] = (time.monotonic() + self._ttl, value)


def to_float(value: Any) -> float | None:
    """Coerce a possibly-string, possibly-comma-grouped number to float."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if text in ("", "-", "NA", "N.A.", "null", "None"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def to_int(value: Any) -> int | None:
    f = to_float(value)
    return int(f) if f is not None else None


def find_value(data: Any, *candidate_keys: str) -> Any:
    """Breadth-first search a nested dict/list for the first scalar whose key
    case-insensitively matches one of candidate_keys.

    BSE's JSON shapes drift and nest unpredictably, so we locate fields by name
    rather than by a fixed path.
    """
    wanted = {k.lower() for k in candidate_keys}
    queue: list[Any] = [data]
    while queue:
        node = queue.pop(0)
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str) and key.lower() in wanted:
                    if value is not None and not isinstance(value, (dict, list)):
                        text = str(value).strip()
                        if text != "":
                            return value
                if isinstance(value, (dict, list)):
                    queue.append(value)
        elif isinstance(node, list):
            queue.extend(node)
    return None
