"""Cadence policy for the ``/api/oauth/usage`` endpoint — every number in one place.

The endpoint enforces a budget on non-first-party clients: a **~60-minute
window of ~28-30 requests per identity × UA-class** (measured 2026-07-11,
probe3, two runs: a rested identity admitted 30 requests before the first
429; the post-drain 429 oscillation ended exactly when the drain burst aged
60 minutes; steady 1/180 s polling then ran 96 minutes from a rested window
with zero 429s). It is NOT a bucket with a refill rate: capacity returns
only as old requests age out of the trailing hour, so a burst saturates the
identity for up to a full hour — pausing does not restore headroom early,
and earlier "refill rate" estimates were artifacts of measuring while
saturated.

What that identity is depends on which 429 regime the org is on — the two
regimes coexist across orgs (see the Retry-After discussion in
``usage_store``). Under the fixed-deadline regime it is the **account/org**
(measured 2026-07-28: a freshly minted token was blocked 135 s after issue,
which a per-token counter cannot produce). Under the saturated-edge
(``Retry-After: 0``) regime it is the **access token** (measured 2026-07-29,
probe4: at saturation, a freshly minted token of the same lineage was
admitted while the old token stayed blocked, requests interleaved). Plan for
the account-scoped case — it is the conservative one: re-authenticating
cannot be relied on to clear a block, and two machines holding different
tokens for one account may share one budget, which is what
``POST_429_BACKOFF_MULT`` below exists to converge.

Error bars: the horizon is bracketed to ~55-64 minutes from a single
transition event, the exact edge algorithm (likely a Cloudflare
sliding-window approximation) is undocumented, and Anthropic can retune it
any day — so the constants below lean only on the robust parts: a sustained
rate safely under the cap, and an ~hour recovery horizon. The budget target
is an **average of at most ~1 request / 3 minutes** (20/hour vs the ~28-30/
hour cap), leaving ~8-10 requests/hour of headroom for manual commands,
wake-from-sleep catch-up, and the bounded urgent mode below.

Health invariant to watch in the logs: steady state shows zero http-429.
A post-burst 429 does NOT reliably clear at its stated horizon — measured
over one machine's full log (re-measured 2026-08-03; re-derive rather than
trust this verbatim, it ages as the log grows — method recorded next to
``usage_store.RETRY_AFTER_MARGIN_S``), 20 of 35 lapsed blocks re-blocked
within 900s of their own deadline (+2s..+887s), each for a fresh full hour
("of 35", not 38 raw gaps: 3 are negative — NOT a uniform mechanism (one has
no within-block revision at all, one is revised BACKWARD mid-block, one is
unchanged — see usage_store.RETRY_AFTER_MARGIN_S's comment for the per-gap
detail) — excluded from both numerator and denominator; round 8 switched
from the prior "21 of 36"/"2 of 38" figures here to these, on the OTHER of
two equally-reproducing readings — see usage_store.RETRY_AFTER_MARGIN_S's
comment for which reading and why).
The prior "10 of 23" figure here did not reproduce under any of 40 method
variants swept and is superseded. That is why the wait is Retry-After plus
``usage_store.RETRY_AFTER_MARGIN_S`` and not Retry-After alone. What would
mean this model needs revisiting is a 429 episode at modest rates that
outlasts an hour *past* that margin.

Plans computed here are persisted per account in the usage store
(``nextPollAt``/``pollIntervalS``) by whichever collector fetched, so every
surface — ``cswap list``, the TUI, the menu bar, the auto engine — inherits
the same cadence no matter how often it repaints.

If a future probe revises the measured shape, adjust the constants in this
module only.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from claude_swap import oauth

# Freshness floor shared by every collector: an entry younger than this is
# served from the store without any fetch, so the maximum sustained rate on
# one token is 1/SERVE_TTL_S regardless of how many surfaces are open.
SERVE_TTL_S = 180.0

# Normal cadence floor — movement can halve an interval down to this, never
# below.
MIN_INTERVAL_S = 180.0

# The ACTIVE account's wait is proportional to the POINTS LEFT before the
# switch threshold: sized so that even a fast burn (ASSUMED_BURN_PCT_PER_MIN)
# covers at most WAIT_SAFETY of the remaining room before the next poll.
# Room left only — no measured speed (usage is bursty; a measured rate is
# stale the moment it is taken). At the defaults each point of room is worth
# 10s: 49 points → ~8 min, 16 → ~2.7 min, 3 → the 30s floor. Measured on the
# user's account: +13 points in 5 minutes (~2.6%/min), which the old fixed
# 50%→90% line missed by waiting 5 minutes at 83%. Settings
# autoswitch.pollAssumedBurnPctPerMin / pollMinIntervalSeconds /
# pollMaxIntervalSeconds; these are the defaults.
ASSUMED_BURN_PCT_PER_MIN = 3.0
WAIT_SAFETY = 0.5
ACTIVE_MIN_INTERVAL_S = 30.0
ACTIVE_FAR_INTERVAL_S = 900.0

# Budget guard: a token bucket over the account's request log (fetchLog —
# every surface on this machine). Capacity FAST_POLL_BURST, refilled at one
# token per MIN_INTERVAL_S. A poll may be planned only when a token is
# available, so any trailing hour holds at most FAST_POLL_BURST + 3600 /
# MIN_INTERVAL_S = 26 requests, under the measured ~28-30/hour block. Far
# from the limit the bucket refills; near it, fast polls spend the bank and
# the cadence settles at MIN_INTERVAL_S rather than going blind. (A plain
# "N in the last hour" count cannot do this: the steady polls after a burst
# land ON TOP of it — measured in simulation, 22 + 16 = 38 in one hour.)
FAST_POLL_BURST = 6

# Decay ceilings for an idle alternate (candidates still adapt on movement).
# The ceiling actually used is max(CANDIDATE_MAX_INTERVAL_S, far interval):
# the engine refetches every candidate before any switch decision anyway.
CANDIDATE_DEFAULT_INTERVAL_S = 300.0
CANDIDATE_MAX_INTERVAL_S = 600.0

# An exhausted account with a known reset is PARKED until that reset (+
# slack): usage cannot fall before its window rolls over, and the reset time
# is already known, so probing in between spends budget to learn nothing.
# Its stored reading stays decision-trusted until the reset (usage_store), so
# it keeps reading as exhausted, not unknown. The trade: an early provider-
# side quota grant goes unnoticed until the advertised reset. Only when no
# reset is known does it fall back to a bounded ten-minute probe. A reset
# farther out than PARK_MAX_S is treated as malformed and not parked on.
EXHAUSTED_INTERVAL_S = 600.0
PARK_MAX_S = 8 * 86400.0

# A window whose binding pct moved at least this much between polls is being
# consumed somewhere (this machine, another PC, session mode) → tighten; an
# unmoved one backs off toward its ceiling.
MOVEMENT_DELTA_PCT = 1.0

# ±fraction applied to each scheduled interval so independent processes
# (watch + menu bar + auto) drift apart instead of fetching in lockstep.
JITTER_FRAC = 0.1

# Reaction to a 429 with ``Retry-After: 0`` (the saturated-window edge):
# probe at most every 5 minutes (≤12/hour) so aging-out — up to ~30/hour —
# outpaces the probing (used by the usage store's failure backoff)...
EDGE_BACKOFF_S = 300.0
# ...and while any 429 was seen on the token within this window, floor the
# planned cadence here so freed capacity accumulates instead of being
# re-spent. The window matches the saturation horizon: a full trailing hour
# takes up to 60 minutes to age out.
POST_429_MIN_INTERVAL_S = 360.0
RECENT_429_WINDOW_S = 3600.0

# AIMD on a contended budget. The budget is shared across every machine
# polling the same account (under the account-scoped regime a machine with its
# own token is no less a competitor — see the module docstring on scope),
# none of them can see the others, and the endpoint
# exposes no remaining-request count — only a Retry-After once already
# blocked. So while 429s recur, each successful poll multiplicatively grows the
# interval (×POST_429_BACKOFF_MULT) toward POST_429_MAX_INTERVAL_S — wider than
# the normal candidate ceiling so several machines can each back off far enough
# that their combined rate fits under the budget. Movement (a real success run
# with no recent 429) decays it back down. This is TCP-style congestion control:
# the budget gets fair-shared by reaction alone, with no machine count or
# shared state to configure.
POST_429_BACKOFF_MULT = 1.5
POST_429_MAX_INTERVAL_S = 1800.0

# The engine escalates to a full candidate refresh when the active account is
# within this margin of the threshold (decision policy, kept beside the
# cadence numbers it interacts with).
ESCALATION_MARGIN_PCT = 15.0

# Never schedule a poll later than a known window reset (+ slack): stored
# usage is obsolete the moment the window rolls over.
RESET_SLACK_S = 60.0


@dataclass(frozen=True)
class PollInputs:
    """Everything poll planning keys on, from the effective settings: the
    switch threshold, the scoped models, and the active-cadence knobs."""

    threshold: float
    models: tuple[str, ...] = ()
    burn_pct_per_min: float = ASSUMED_BURN_PCT_PER_MIN
    min_interval_s: float = ACTIVE_MIN_INTERVAL_S
    far_interval_s: float = ACTIVE_FAR_INTERVAL_S
    # Claude Code's status-line feed is live for the active account (see
    # live_feed): its API poll is only a cross-check, so it waits the max.
    feed_live: bool = False

    @classmethod
    def from_settings(
        cls, settings, models: tuple[str, ...], *, feed_live: bool = False
    ) -> "PollInputs":
        """From an ``AutoSwitchSettings`` (duck-typed: no settings import)."""
        return cls(
            threshold=settings.threshold,
            models=models,
            burn_pct_per_min=settings.poll_assumed_burn_pct_per_min,
            min_interval_s=settings.poll_min_interval_seconds,
            far_interval_s=settings.poll_max_interval_seconds,
            feed_live=feed_live,
        )

    def ramp_kwargs(self) -> dict:
        """The ``active_interval_for`` keyword arguments."""
        return {
            "burn_pct_per_min": self.burn_pct_per_min,
            "min_interval_s": self.min_interval_s,
            "far_interval_s": self.far_interval_s,
        }


def binding_pct(usage: dict | None, models: tuple[str, ...] = ()) -> float | None:
    """Utilization of the binding (worst) relevant window, or None."""
    headroom = oauth.account_headroom(usage, models)
    return None if headroom is None else 100.0 - headroom


def limiting_reset_ts(
    usage: dict | None, models: tuple[str, ...] = ()
) -> float | None:
    """Epoch when the last of the ≥100% relevant windows resets (account
    usable again)."""
    latest: float | None = None
    for _, pct, resets_at in oauth.relevant_windows(usage, models):
        if pct < 100.0:
            continue
        ts = parse_reset_ts(resets_at)
        if ts is not None and (latest is None or ts > latest):
            latest = ts
    return latest


def earliest_future_reset_ts(
    usage: dict | None, now: float, models: tuple[str, ...] = ()
) -> float | None:
    """Epoch of the next relevant-window reset ahead of ``now``, any
    utilization."""
    earliest: float | None = None
    for _, _, resets_at in oauth.relevant_windows(usage, models):
        ts = parse_reset_ts(resets_at)
        if ts is not None and ts > now and (earliest is None or ts < earliest):
            earliest = ts
    return earliest


def active_interval_for(
    pct: float | None,
    threshold: float,
    *,
    burn_pct_per_min: float = ASSUMED_BURN_PCT_PER_MIN,
    min_interval_s: float = ACTIVE_MIN_INTERVAL_S,
    far_interval_s: float = ACTIVE_FAR_INTERVAL_S,
) -> float:
    """The active account's poll interval at ``pct`` used, before the budget
    guard: ``WAIT_SAFETY`` of the time a ``burn_pct_per_min`` burn would take
    to cover the points left, clamped to ``[min_interval_s,
    far_interval_s]``. At/over the threshold → the floor. Unknown
    utilization takes ``MIN_INTERVAL_S`` — not knowing is no reason to wait
    long, nor to poll fast.
    """
    if pct is None:
        return MIN_INTERVAL_S
    floor = min(min_interval_s, far_interval_s)
    points_left = threshold - pct
    if points_left <= 0:
        return floor
    wait = WAIT_SAFETY * points_left / max(burn_pct_per_min, 0.01) * 60.0
    return min(far_interval_s, max(floor, wait))


def next_token_at(fetch_log: tuple[float, ...] | list[float]) -> float | None:
    """When the request bucket next holds a whole token, given every request
    in the trailing hour (including the one just made), or None when the log
    is empty. Replays the log from a full bucket — the worst case for the
    window, so the bound above holds for any trailing hour."""
    tokens = float(FAST_POLL_BURST)
    last: float | None = None
    for t in sorted(fetch_log):
        if last is not None:
            tokens = min(float(FAST_POLL_BURST), tokens + (t - last) / MIN_INTERVAL_S)
        tokens -= 1.0
        last = t
    if last is None:
        return None
    if tokens >= 1.0:
        return last
    return last + (1.0 - tokens) * MIN_INTERVAL_S


def parked_until(
    usage: dict | None, now: float, models: tuple[str, ...] = ()
) -> float | None:
    """When an exhausted account's blocking windows have all rolled over
    (+ ``RESET_SLACK_S``), or None if it isn't exhausted, the reset is
    unknown/past, or implausibly far out (``PARK_MAX_S``)."""
    headroom = oauth.account_headroom(usage, models)
    if headroom is None or headroom > 0:
        return None
    reset_ts = limiting_reset_ts(usage, models)
    if reset_ts is None or reset_ts <= now or reset_ts - now > PARK_MAX_S:
        return None
    return reset_ts + RESET_SLACK_S


def parse_reset_ts(resets_at: str | None) -> float | None:
    if not resets_at:
        return None
    try:
        return datetime.fromisoformat(
            str(resets_at).replace("Z", "+00:00")
        ).timestamp()
    except ValueError:
        return None


def plan_after_fetch(
    *,
    prev_interval_s: float | None,
    prev_usage: dict | None,
    new_usage: dict | None,
    is_active: bool,
    threshold: float,
    models: tuple[str, ...],
    recent_429: bool,
    now: float,
    rng: Callable[[], float] = random.random,
    burn_pct_per_min: float = ASSUMED_BURN_PCT_PER_MIN,
    min_interval_s: float = ACTIVE_MIN_INTERVAL_S,
    far_interval_s: float = ACTIVE_FAR_INTERVAL_S,
    fetch_log: tuple[float, ...] | None = None,
    feed_live: bool = False,
) -> tuple[float, float]:
    """``(next_poll_at, interval_s)`` for an account just fetched successfully.

    The ACTIVE account's interval is proportional to the points left
    (:func:`active_interval_for`), and never sooner than the request bucket
    allows (:func:`next_token_at` over ``fetch_log``, the trailing-hour
    request times including this fetch). A
    CANDIDATE adapts on movement: movement halves the interval (floored at
    ``MIN_INTERVAL_S``), no movement backs off ×1.5 toward
    ``max(CANDIDATE_MAX_INTERVAL_S, far_interval_s)``; unknown utilization
    uses the default. A recent 429 on this token floors the cadence at
    ``POST_429_MIN_INTERVAL_S`` until ``RECENT_429_WINDOW_S`` has passed. The scheduled time gets
    ``JITTER_FRAC`` noise, is never later than the account's next window
    reset (+ ``RESET_SLACK_S``). An at-limit account keeps a bounded slow
    poll instead of sleeping until that reset, so an early provider-side
    quota grant is observed and its decision-grade status stays current.
    """
    default = MIN_INTERVAL_S if is_active else CANDIDATE_DEFAULT_INTERVAL_S
    ceiling = max(CANDIDATE_MAX_INTERVAL_S, far_interval_s)
    base = prev_interval_s or default
    prev_pct = binding_pct(prev_usage, models)
    new_pct = binding_pct(new_usage, models)
    moving = (
        prev_pct is not None
        and new_pct is not None
        and abs(new_pct - prev_pct) >= MOVEMENT_DELTA_PCT
    )
    if is_active and feed_live:
        # The status-line feed reports this account live and free; the API
        # poll is a cross-check, so it waits the longest and keeps the bucket
        # full for when the feed goes quiet.
        interval = far_interval_s
    elif is_active:
        # Points left decide, not movement or the previous interval.
        interval = active_interval_for(
            new_pct,
            threshold,
            burn_pct_per_min=burn_pct_per_min,
            min_interval_s=min_interval_s,
            far_interval_s=far_interval_s,
        )
        ready = next_token_at(fetch_log) if fetch_log else None
        if ready is not None:
            interval = max(interval, ready - now)  # the hour's budget
    elif prev_pct is None or new_pct is None:
        interval = default
    elif moving:
        interval = max(MIN_INTERVAL_S, base / 2)
    else:
        interval = min(ceiling, max(MIN_INTERVAL_S, base * 1.5))
    if recent_429:
        # AIMD additive-increase: grow the interval multiplicatively from the
        # last one toward the wider 429 ceiling, so machines sharing a
        # contended token each retreat until their combined rate fits the
        # budget. Floored at POST_429_MIN_INTERVAL_S for the first 429.
        increased = max(base * POST_429_BACKOFF_MULT, POST_429_MIN_INTERVAL_S)
        interval = min(POST_429_MAX_INTERVAL_S, max(interval, increased))

    headroom = oauth.account_headroom(new_usage, models)
    if headroom is not None and headroom <= 0:
        parked = parked_until(new_usage, now, models)
        if parked is not None:
            # 60-120s past the reset, spread so machines don't hit it at once.
            # The interval matches the deadline, so the store's obsolete-plan
            # guard (a far deadline beside a short interval) never fires.
            next_poll = parked + RESET_SLACK_S * rng()
            return next_poll, next_poll - now
        # No reset known: a bounded probe, keeping any wider post-429 interval.
        interval = max(interval, EXHAUSTED_INTERVAL_S)

    next_poll = now + interval * (1.0 + JITTER_FRAC * (2.0 * rng() - 1.0))
    if headroom is not None and headroom <= 0:
        reset_ts = limiting_reset_ts(new_usage, models)
        if reset_ts is not None and reset_ts > now:
            next_poll = min(next_poll, reset_ts + RESET_SLACK_S)
    else:
        reset_ts = earliest_future_reset_ts(new_usage, now, models)
        if reset_ts is not None:
            next_poll = min(next_poll, reset_ts + RESET_SLACK_S)
    return next_poll, interval
