"""Live usage from Claude Code's status line — free, real-time, no API call.

Claude Code hands its status-line command a JSON object on every new
assistant message (debounced ~300ms). For claude.ai Pro/Max logins it
carries ``rate_limits.five_hour`` / ``rate_limits.seven_day``, each with
``used_percentage`` (0-100) and ``resets_at`` (Unix epoch seconds), derived
from the rate-limit headers on Claude Code's own API responses. A window may
be absent, and Claude Code drops one once its reset passes.
(https://code.claude.com/docs/en/statusline)

Nothing persists that object, so the user's status-line script writes it to
``<backup_root>/live-usage.json``::

    {"writtenAt": <epoch seconds>, "rate_limits": <the rate_limits object>}

(snippets in the README). This module reads it back as a usage dict in the
store's shape, so the auto engine can decide on it exactly like a fetched
reading.

The feed names no account. Attribution is the caller's job: a reading
belongs to the account that was active when it was written, so the engine
only accepts readings written after the active account last changed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

LIVE_USAGE_FILENAME = "live-usage.json"

# A reading this recent means the feed is LIVE: Claude Code is producing
# messages, so the engine re-reads every 10s and lets the active account's
# API polls relax. The status line only refreshes on new messages.
FEED_FRESH_S = 90.0

# An older reading is still a valid lower bound — usage only rises within a
# window — until one of its windows resets, so the engine keeps deciding on
# it (instead of falling back to an older API reading) for up to this long.
FEED_HOLD_S = 3600.0

_WINDOWS = (("five_hour", "five_hour"), ("seven_day", "seven_day"))


@dataclass(frozen=True)
class LiveReading:
    """One status-line reading, in the usage store's ``last_good`` shape."""

    written_at: float
    usage: dict  # {"five_hour": {"pct", "resets_at"}, "seven_day": {...}}


def live_usage_path(backup_root: Path) -> Path:
    return backup_root / LIVE_USAGE_FILENAME


def _iso(epoch: float) -> str:
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _num(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def parse_rate_limits(rate_limits) -> dict:
    """Map a status-line ``rate_limits`` object to the store's usage shape.

    Only windows with a numeric ``used_percentage`` are kept; ``resets_at``
    (epoch seconds) becomes the ISO string the rest of cswap parses.
    """
    usage: dict = {}
    if not isinstance(rate_limits, dict):
        return usage
    for src, dst in _WINDOWS:
        window = rate_limits.get(src)
        if not isinstance(window, dict):
            continue
        pct = _num(window.get("used_percentage"))
        if pct is None:
            continue
        entry: dict = {"pct": pct}
        resets = _num(window.get("resets_at"))
        if resets is not None and resets > 0:
            entry["resets_at"] = _iso(resets)
        usage[dst] = entry
    return usage


def read_live_usage(
    backup_root: Path, now: float, *, fresh_s: float = FEED_FRESH_S
) -> LiveReading | None:
    """The current status-line reading, or None when there is none to trust:
    no file, unreadable or malformed JSON (a writer mid-replace on a
    filesystem without atomic rename), no usable window, or older than
    ``fresh_s``. Never raises."""
    try:
        raw = json.loads(live_usage_path(backup_root).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    written = _num(raw.get("writtenAt"))
    if written is None or now - written > fresh_s or written > now + 60.0:
        return None
    usage = parse_rate_limits(raw.get("rate_limits"))
    if not usage:
        return None
    if any(
        _reset_passed(window.get("resets_at"), now) for window in usage.values()
    ):
        return None  # a window rolled over since: the reading is obsolete
    return LiveReading(written_at=written, usage=usage)


def _reset_passed(resets_at: str | None, now: float) -> bool:
    if not resets_at:
        return False
    try:
        ts = datetime.fromisoformat(resets_at.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return False
    return ts <= now
