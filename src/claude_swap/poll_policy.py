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

# Burst ("urgent") mode: the ACTIVE account at/above BURST_START_PCT used,
# with movement observed this poll (i.e. actually burning toward the limit).
# Bounded by construction, not by speed: each burst poll needs a fresh
# MOVEMENT_DELTA_PCT rise, so an episode costs about (threshold - burst
# start) / delta polls plus a MIN_INTERVAL_S poll whenever usage pauses —
# 90→99 is ~9 burst polls — inside the measured ~28-30 request rolling-hour
# window; overshoot on top of steady traffic is absorbed by the post-429
# floor below. Both are settings (autoswitch.pollBurstStartPct /
# pollBurstIntervalSeconds); these are the defaults.
URGENT_INTERVAL_S = 60.0
BURST_START_PCT = 75.0

# The active account's not-moving cadence ramps on ROOM LEFT: at or below
# RAMP_START_PCT used it waits ACTIVE_FAR_INTERVAL_S, shortening linearly to
# MIN_INTERVAL_S at the burst start (see active_interval_for). Settings
# autoswitch.pollRampStartPct / pollMaxIntervalSeconds; these are defaults.
RAMP_START_PCT = 50.0
ACTIVE_FAR_INTERVAL_S = 900.0

# Decay ceilings for an idle alternate (candidates still adapt on movement).
# The ceiling actually used is max(CANDIDATE_MAX_INTERVAL_S, far interval):
# the engine refetches every candidate before any switch decision anyway.
CANDIDATE_DEFAULT_INTERVAL_S = 300.0
CANDIDATE_MAX_INTERVAL_S = 600.0

# Exhaustion is stable enough to poll slowly, but not to stop polling until a
# reported reset. Quota grants and provider-side corrections can make an
# account usable before that timestamp, and decision-grade status must not age
# into "unavailable" while the scheduler is deliberately waiting. Ten-minute
# polling (six requests/hour) stays below the measured budget and
# detects recovery promptly; a nearer reported reset still pulls the next poll
# forward.
EXHAUSTED_INTERVAL_S = 600.0

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
# within this margin of the threshold (decision policy, but the urgent-mode
# cadence keys on the same band, so it lives with the cadence numbers).
ESCALATION_MARGIN_PCT = 15.0

# Never schedule a poll later than a known window reset (+ slack): stored
# usage is obsolete the moment the window rolls over.
RESET_SLACK_S = 60.0


@dataclass(frozen=True)
class PollInputs:
    """Everything poll planning keys on, from the effective settings: the
    switch threshold, the scoped models, and the ramp/burst knobs."""

    threshold: float
    models: tuple[str, ...] = ()
    ramp_start_pct: float = RAMP_START_PCT
    far_interval_s: float = ACTIVE_FAR_INTERVAL_S
    burst_start_pct: float = BURST_START_PCT
    burst_interval_s: float = URGENT_INTERVAL_S

    @classmethod
    def from_settings(cls, settings, models: tuple[str, ...]) -> "PollInputs":
        """From an ``AutoSwitchSettings`` (duck-typed: no settings import)."""
        return cls(
            threshold=settings.threshold,
            models=models,
            ramp_start_pct=settings.poll_ramp_start_pct,
            far_interval_s=settings.poll_max_interval_seconds,
            burst_start_pct=settings.poll_burst_start_pct,
            burst_interval_s=settings.poll_burst_interval_seconds,
        )

    def ramp_kwargs(self) -> dict:
        """The ``active_interval_for`` keyword arguments."""
        return {
            "ramp_start_pct": self.ramp_start_pct,
            "far_interval_s": self.far_interval_s,
            "burst_start_pct": self.burst_start_pct,
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
    ramp_start_pct: float = RAMP_START_PCT,
    far_interval_s: float = ACTIVE_FAR_INTERVAL_S,
    burst_start_pct: float = BURST_START_PCT,
) -> float:
    """The active account's not-moving poll interval at ``pct`` used.

    ``far_interval_s`` at or below the ramp start, ``MIN_INTERVAL_S`` at or
    above the burst start (capped at the threshold: there is no burst zone
    past the switch point), a straight line in between. Unknown utilization
    takes ``MIN_INTERVAL_S`` — not knowing is no reason to wait long.
    """
    if pct is None:
        return MIN_INTERVAL_S
    far = max(far_interval_s, MIN_INTERVAL_S)
    end = min(burst_start_pct, threshold)
    start = min(ramp_start_pct, end)
    if pct >= end:
        return MIN_INTERVAL_S
    if pct <= start:
        return far
    frac = (pct - start) / (end - start)
    return far - frac * (far - MIN_INTERVAL_S)


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
    ramp_start_pct: float = RAMP_START_PCT,
    far_interval_s: float = ACTIVE_FAR_INTERVAL_S,
    burst_start_pct: float = BURST_START_PCT,
    burst_interval_s: float = URGENT_INTERVAL_S,
) -> tuple[float, float]:
    """``(next_poll_at, interval_s)`` for an account just fetched successfully.

    The ACTIVE account's interval comes from room left
    (:func:`active_interval_for`), dropping to ``burst_interval_s`` when it
    is at/above the burst start and moved ≥ ``MOVEMENT_DELTA_PCT`` since the
    previous poll. A CANDIDATE adapts on movement: movement halves the
    interval (floored at ``MIN_INTERVAL_S``), no movement backs off ×1.5
    toward ``max(CANDIDATE_MAX_INTERVAL_S, far_interval_s)``; unknown
    utilization uses the default. A recent 429 on this token floors
    the cadence at ``POST_429_MIN_INTERVAL_S`` (and suppresses urgent mode)
    until ``RECENT_429_WINDOW_S`` has passed. The scheduled time gets
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
    if is_active:
        # Room left decides, not movement. The ramp ignores `base`, so a
        # burst interval snaps straight back once movement stops.
        interval = active_interval_for(
            new_pct,
            threshold,
            ramp_start_pct=ramp_start_pct,
            far_interval_s=far_interval_s,
            burst_start_pct=burst_start_pct,
        )
    elif prev_pct is None or new_pct is None:
        interval = default
    elif moving:
        interval = max(MIN_INTERVAL_S, base / 2)
    else:
        interval = min(ceiling, max(MIN_INTERVAL_S, base * 1.5))
    if (
        is_active
        and moving
        and not recent_429
        and new_pct is not None
        and new_pct >= min(burst_start_pct, threshold)
    ):
        interval = burst_interval_s
    if recent_429:
        # AIMD additive-increase: grow the interval multiplicatively from the
        # last one toward the wider 429 ceiling, so machines sharing a
        # contended token each retreat until their combined rate fits the
        # budget. Floored at POST_429_MIN_INTERVAL_S for the first 429.
        increased = max(base * POST_429_BACKOFF_MULT, POST_429_MIN_INTERVAL_S)
        interval = min(POST_429_MAX_INTERVAL_S, max(interval, increased))

    headroom = oauth.account_headroom(new_usage, models)
    if headroom is not None and headroom <= 0:
        # Keep probing exhausted accounts: Anthropic can grant/reset quota
        # before the previously advertised timestamp. Preserve a wider
        # post-429 interval if congestion control already selected one.
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
