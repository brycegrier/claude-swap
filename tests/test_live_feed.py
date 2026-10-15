"""Tests for the status-line live usage feed (live_feed.py)."""

from __future__ import annotations

import json
from pathlib import Path

from claude_swap import live_feed
from claude_swap.live_feed import parse_rate_limits, read_live_usage

NOW = 1_800_000_000.0


def _write(tmp_path: Path, payload) -> None:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    live_feed.live_usage_path(tmp_path).write_text(text, encoding="utf-8")


def _feed(written_at: float = NOW, **windows) -> dict:
    return {"writtenAt": written_at, "rate_limits": windows}


class TestParseRateLimits:
    def test_maps_windows_and_converts_epoch_resets(self):
        usage = parse_rate_limits({
            "five_hour": {"used_percentage": 23.5, "resets_at": 1738425600},
            "seven_day": {"used_percentage": 41},
        })
        assert usage == {
            "five_hour": {"pct": 23.5, "resets_at": "2025-02-01T16:00:00Z"},
            "seven_day": {"pct": 41.0},
        }

    def test_absent_and_malformed_windows_are_skipped(self):
        assert parse_rate_limits({"five_hour": None}) == {}
        assert parse_rate_limits({"five_hour": {"used_percentage": "high"}}) == {}
        assert parse_rate_limits({"spend_limit": {"used_percentage": 50}}) == {}
        assert parse_rate_limits(None) == {}


class TestReadLiveUsage:
    def test_fresh_reading(self, tmp_path):
        _write(tmp_path, _feed(NOW - 10, five_hour={"used_percentage": 91}))
        reading = read_live_usage(tmp_path, NOW)
        assert reading is not None
        assert reading.written_at == NOW - 10
        assert reading.usage == {"five_hour": {"pct": 91.0}}

    def test_stale_reading_is_ignored(self, tmp_path):
        _write(tmp_path, _feed(
            NOW - live_feed.FEED_FRESH_S - 1, five_hour={"used_percentage": 91}
        ))
        assert read_live_usage(tmp_path, NOW) is None

    def test_missing_malformed_or_empty_is_none(self, tmp_path):
        assert read_live_usage(tmp_path, NOW) is None  # no file
        _write(tmp_path, "{not json")
        assert read_live_usage(tmp_path, NOW) is None  # torn write
        _write(tmp_path, [1, 2])
        assert read_live_usage(tmp_path, NOW) is None
        _write(tmp_path, _feed(NOW))  # no usable window (API-key login)
        assert read_live_usage(tmp_path, NOW) is None
        _write(tmp_path, {"rate_limits": {"five_hour": {"used_percentage": 5}}})
        assert read_live_usage(tmp_path, NOW) is None  # no writtenAt

    def test_a_far_future_stamp_is_not_trusted(self, tmp_path):
        _write(tmp_path, _feed(NOW + 3600, five_hour={"used_percentage": 5}))
        assert read_live_usage(tmp_path, NOW) is None

    def test_tolerates_a_utf8_bom(self, tmp_path):
        # Windows PowerShell 5's Set-Content -Encoding UTF8 writes a BOM.
        live_feed.live_usage_path(tmp_path).write_text(
            "﻿" + json.dumps(_feed(NOW, five_hour={"used_percentage": 7})),
            encoding="utf-8",
        )
        assert read_live_usage(tmp_path, NOW).usage["five_hour"]["pct"] == 7.0
