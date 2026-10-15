"""Snapshot source — the supported read path for dashboards and GUI shells.

Pacing is store-governed: the usage store's persisted poll plans plus its
freshness/backoff/claim gates (decided atomically in ``UsageStore.reserve``)
cap every surface at the same per-account cadence, so a dashboard repainting
every few seconds and a one-shot ``cswap list`` produce identical network
behavior. This class therefore just runs the same on-demand pass as ``cswap
list`` (``fetch=None``) each take — the store decides which accounts, if
any, may actually be fetched — and offers ``store_only`` for shells that
host an auto engine (which already collects on its own schedule).

``take()`` is blocking (file locks, keychain subprocesses, network): call it
from a background thread, never a UI event loop.
"""

from __future__ import annotations

import json
import threading
from dataclasses import replace

from claude_swap.json_output import USAGE_TOKEN_EXPIRED
from claude_swap.live_feed import (
    FEED_HOLD_S,
    attributable,
    live_usage_path,
    merged_usage,
    read_live_usage,
)
from claude_swap.models import AccountSnapshot, AccountsSnapshot
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.usage_store import UsageEntry


class SnapshotSource:
    """Takes one coherent snapshot per call; the store paces the network.

    ``full=True`` (the user's explicit refresh) is accepted for API
    stability but is no faster than a normal pass: even an explicit refresh
    is capped by the store's serve TTL and poll plans. ``store_only=True``
    reads the store without any network eligibility.
    """

    def __init__(self, switcher: ClaudeAccountSwitcher) -> None:
        self.switcher = switcher
        self._last: AccountsSnapshot | None = None
        self._lock = threading.Lock()
        # When the active account last changed under this source (manual
        # switches the auto engine never recorded) — feed attribution.
        self._active_seen: str | None = None
        self._active_since: float | None = None

    def take(
        self, *, full: bool = False, store_only: bool = False
    ) -> AccountsSnapshot:
        """Blocking snapshot pass; call from a thread worker."""
        fetch: set[str] | None = set() if store_only else None
        snap = self.switcher.accounts_snapshot(fetch=fetch)
        with self._lock:
            snap = self._reconcile(snap)
            snap = self._with_live_feed(snap)
            self._last = snap
            return snap

    def _with_live_feed(self, snap: AccountsSnapshot) -> AccountsSnapshot:
        """Show the active account's status-line reading when it is newer
        than the stored API one — the same reading the auto engine decides
        on, so cards and ticker agree. Display only: the store is untouched.
        Once applied it sticks via ``_reconcile`` (an older API row never
        replaces a newer reading) until a newer API fetch lands."""
        active = snap.active_number
        if active != self._active_seen:
            if self._active_seen is not None:
                self._active_since = snap.taken_at
            self._active_seen = active
        if active is None:
            return snap
        try:
            if not live_usage_path(self.switcher.backup_dir).exists():
                return snap
            reading = read_live_usage(
                self.switcher.backup_dir, snap.taken_at, fresh_s=FEED_HOLD_S
            )
        except Exception:
            return snap
        if reading is None:
            return snap
        since = [t for t in (self._last_switch_at(), self._active_since) if t is not None]
        if not attributable(reading, max(since) if since else None):
            return snap
        accounts = []
        for acc in snap.accounts:
            fetched = acc.usage.fetched_at
            if (
                acc.number == active
                and acc.usage.sentinel is None
                and (fetched is None or fetched < reading.written_at)
            ):
                usage = replace(
                    acc.usage,
                    last_good=merged_usage(acc.usage.last_good, reading),
                    fetched_at=reading.written_at,
                    age_s=max(0.0, snap.taken_at - reading.written_at),
                )
                acc = replace(acc, usage=usage, live_feed=True)
            accounts.append(acc)
        return replace(snap, accounts=tuple(accounts))

    def _last_switch_at(self) -> float | None:
        """The auto engine's last switch time (its state file), if any."""
        try:
            state = json.loads(
                (self.switcher.backup_dir / "autoswitch_state.json").read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, ValueError):
            return None
        last = state.get("lastSwitchAt") if isinstance(state, dict) else None
        return float(last) if isinstance(last, (int, float)) else None

    def _reconcile(self, snap: AccountsSnapshot) -> AccountsSnapshot:
        if self._last is None:
            return snap
        previous = {acc.number: acc for acc in self._last.accounts}
        accounts = tuple(
            self._reconcile_account(acc, previous.get(acc.number), snap.taken_at)
            for acc in snap.accounts
        )
        return replace(snap, accounts=accounts)

    def _reconcile_account(
        self,
        acc: AccountSnapshot,
        prev: AccountSnapshot | None,
        taken_at: float,
    ) -> AccountSnapshot:
        if prev is None or account_identity(acc) != account_identity(prev):
            return acc

        prev_fetched = prev.usage.fetched_at
        fetched = acc.usage.fetched_at
        if prev_fetched is not None and (fetched is None or fetched < prev_fetched):
            return replace(
                acc,
                usage=_with_current_age(prev.usage, taken_at),
                live_feed=prev.live_feed,
            )
        if acc.usage.sentinel is not None:
            return acc

        if (
            prev.usage.sentinel == USAGE_TOKEN_EXPIRED
            and fetched == prev_fetched
        ):
            return replace(acc, usage=replace(acc.usage, sentinel=USAGE_TOKEN_EXPIRED))
        return acc


def account_identity(acc: AccountSnapshot) -> tuple[str, str, str]:
    """The identity discriminator for a slot across snapshots.

    The single definition of "same account": snapshot reconciliation here and
    the TUI's snapshot merge must agree, or an identity change (e.g. a slot
    re-used by another login) is detected by one and missed by the other.
    """
    return (acc.email, acc.org_uuid, acc.kind)


def _with_current_age(usage: UsageEntry, taken_at: float) -> UsageEntry:
    fetched = usage.fetched_at
    if fetched is None:
        return usage
    return replace(usage, age_s=max(0.0, taken_at - fetched))
