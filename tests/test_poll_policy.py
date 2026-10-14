"""Unit tests for the shared usage-poll cadence policy."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from claude_swap import poll_policy

NOW = 1_000_000.0
HALF = lambda: 0.5  # noqa: E731 — rng midpoint: jitter factor exactly 1.0


def _usage(pct: float, resets_at: str | None = None) -> dict:
    window: dict = {"pct": pct}
    if resets_at:
        window["resets_at"] = resets_at
    return {"five_hour": window, "seven_day": {"pct": 0.0}}


def _plan(**overrides):
    kwargs = dict(
        prev_interval_s=None,
        prev_usage=None,
        new_usage=_usage(10),
        is_active=False,
        threshold=90.0,
        models=(),
        recent_429=False,
        now=NOW,
        rng=HALF,
    )
    kwargs.update(overrides)
    return poll_policy.plan_after_fetch(**kwargs)


class TestIntervalAdaptation:
    def test_first_fetch_uses_defaults(self):
        # Active cadence comes from room left: 10% used is far below the
        # ramp start, so the first plan is already the long wait.
        _, active = _plan(is_active=True)
        _, candidate = _plan(is_active=False)
        # 10% used at threshold 90: 80 points left → 800s.
        assert active == poll_policy.active_interval_for(10.0, 90.0) == 800.0
        assert candidate == poll_policy.CANDIDATE_DEFAULT_INTERVAL_S

    def test_unmoved_decays_toward_the_ceiling(self):
        _, interval = _plan(prev_interval_s=300.0, prev_usage=_usage(10))
        assert interval == 450.0
        _, capped = _plan(prev_interval_s=700.0, prev_usage=_usage(10))
        # Candidates now stretch as far as the active ramp's longest wait.
        assert capped == max(poll_policy.CANDIDATE_MAX_INTERVAL_S, poll_policy.ACTIVE_FAR_INTERVAL_S)
        # The active account does not decay at all — points left set it.
        _, active = _plan(
            prev_interval_s=250.0, prev_usage=_usage(10), is_active=True
        )
        assert active == poll_policy.active_interval_for(10.0, 90.0)

    def test_movement_halves_floored_at_min(self):
        _, interval = _plan(
            prev_interval_s=600.0, prev_usage=_usage(10), new_usage=_usage(15)
        )
        assert interval == 300.0
        _, floored = _plan(
            prev_interval_s=200.0, prev_usage=_usage(10), new_usage=_usage(15)
        )
        assert floored == poll_policy.MIN_INTERVAL_S

    def test_sub_delta_wiggle_is_not_movement(self):
        _, interval = _plan(
            prev_interval_s=300.0,
            prev_usage=_usage(10),
            new_usage=_usage(10.5),  # below MOVEMENT_DELTA_PCT
        )
        assert interval == 450.0

    def test_unknown_pct_uses_the_default(self):
        _, interval = _plan(prev_interval_s=600.0, new_usage=None)
        assert interval == poll_policy.CANDIDATE_DEFAULT_INTERVAL_S


class TestActiveCadence:
    """The active account waits in proportion to the points left before the
    threshold — room left only, no measured burn speed."""

    def test_table(self):
        f = poll_policy.active_interval_for
        # Defaults: 3%/min assumed burn, half-margin → 10s per point left.
        assert f(50.0, 99.0) == pytest.approx(490.0)
        assert f(76.0, 99.0) == pytest.approx(230.0)
        assert f(83.0, 99.0) == pytest.approx(160.0)
        assert f(90.0, 99.0) == pytest.approx(90.0)
        assert f(96.0, 99.0) == poll_policy.ACTIVE_MIN_INTERVAL_S  # 30s floor
        assert f(5.0, 99.0) == poll_policy.ACTIVE_FAR_INTERVAL_S   # 15m max
        assert f(99.5, 99.0) == poll_policy.ACTIVE_MIN_INTERVAL_S  # past it
        assert f(None, 99.0) == poll_policy.MIN_INTERVAL_S         # unknown

    def test_assumed_burn_scales_the_waits(self):
        f = poll_policy.active_interval_for
        assert f(83.0, 99.0, burn_pct_per_min=6.0) == pytest.approx(80.0)
        assert f(83.0, 99.0, burn_pct_per_min=1.5) == pytest.approx(320.0)

    def _active(self, pct: float, **kw):
        return _plan(
            is_active=True, threshold=99.0, prev_interval_s=180.0,
            prev_usage=_usage(pct), new_usage=_usage(pct), **kw,
        )[1]

    def test_movement_does_not_matter(self):
        moved = _plan(
            is_active=True, threshold=99.0, prev_usage=_usage(80),
            new_usage=_usage(90),
        )[1]
        assert moved == self._active(90.0)

    def test_budget_guard_stretches_fast_polls(self):
        # A rested bucket (one request: this one) allows the 30s floor.
        assert self._active(96.0, fetch_log=(NOW,)) == 30.0
        # Eight requests in the last 35s drained it: wait for a token, which
        # here is more than a full refill interval away.
        drained = tuple(NOW - 5.0 * k for k in range(8))
        assert self._active(96.0, fetch_log=drained) > poll_policy.MIN_INTERVAL_S
        # The bucket only ever lengthens a wait: far from the limit the 490s
        # plan stands unless the overdrawn bucket needs longer still.
        assert self._active(50.0, fetch_log=drained) == pytest.approx(
            max(490.0, poll_policy.next_token_at(drained) - NOW)
        )
        assert self._active(50.0, fetch_log=(NOW,)) == pytest.approx(490.0)

    def test_next_token_at(self):
        f = poll_policy.next_token_at
        assert f(()) is None
        assert f((100.0,)) == 100.0  # plenty of tokens left
        n = poll_policy.FAST_POLL_BURST
        burst = tuple(100.0 + k for k in range(n))
        # Drained to the refill earned during the burst ((n-1)s of 180s):
        # the next whole token is the rest of one refill interval away.
        refilled = (n - 1) / poll_policy.MIN_INTERVAL_S
        assert f(burst) == pytest.approx(
            burst[-1] + (1.0 - refilled) * poll_policy.MIN_INTERVAL_S
        )

    def test_recent_429_still_floors_the_cadence(self):
        assert self._active(96.0, recent_429=True) >= poll_policy.POST_429_MIN_INTERVAL_S

    def test_parked_near_the_limit_stays_under_the_hourly_cap(self):
        """Idle at 96% for two hours: fast polls until the budget is spent,
        then MIN_INTERVAL_S — never more than the measured ~28/hour."""
        t, polls = NOW, []
        while t < NOW + 7200.0:
            polls.append(t)
            log = tuple(p for p in polls if t - 3600.0 < p <= t)
            t += _plan(
                is_active=True, threshold=99.0, prev_usage=_usage(96),
                new_usage=_usage(96), now=t, fetch_log=log,
            )[1]
        window = max(
            sum(1 for p in polls if start <= p < start + 3600.0) for start in polls
        )
        # The bucket's own bound: burst + one per MIN_INTERVAL_S.
        bound = poll_policy.FAST_POLL_BURST + 3600.0 / poll_policy.MIN_INTERVAL_S
        assert window <= bound <= 26

    def test_the_measured_climb_is_caught_before_the_threshold(self):
        """Replays the reported episode: 83% → 96% in five minutes (2.6%/min).
        Each poll must land before the burn covers the points left."""
        t, pct, rate = 0.0, 83.0, 2.6 / 60.0
        while pct < 99.0:
            wait = self._active(pct)
            assert pct + rate * wait < 99.0 + rate * 30.0  # at most one floor poll over
            t += wait
            pct = min(99.0, pct + rate * wait)
            if 99.0 - pct < 0.5:
                break


class TestPost429Floor:
    def test_recent_429_floors_the_cadence(self):
        _, interval = _plan(recent_429=True, prev_usage=_usage(10))
        assert interval >= poll_policy.POST_429_MIN_INTERVAL_S

    def test_slower_learned_cadence_survives_the_floor(self):
        # A learned interval already above the floor is grown (×1.5), never
        # dropped back to the floor.
        _, interval = _plan(
            recent_429=True, prev_interval_s=590.0, prev_usage=_usage(10)
        )
        assert interval == pytest.approx(590.0 * poll_policy.POST_429_BACKOFF_MULT)
        assert interval > poll_policy.POST_429_MIN_INTERVAL_S


class TestPost429Aimd:
    """AIMD backoff on a contended token: while 429s recur, each successful
    poll multiplicatively increases the interval toward a wider 429 ceiling, so
    independent machines sharing one token each retreat and their combined poll
    rate converges under the endpoint budget (no cross-machine coordination)."""

    def test_recent_429_multiplicatively_increases_from_prev(self):
        # A prior 360s interval, still seeing 429s, is pushed up (×1.5), not
        # held flat at the floor.
        _, interval = _plan(
            recent_429=True,
            prev_interval_s=poll_policy.POST_429_MIN_INTERVAL_S,  # 360
            prev_usage=_usage(10),
        )
        assert interval > poll_policy.POST_429_MIN_INTERVAL_S
        assert interval == pytest.approx(
            poll_policy.POST_429_MIN_INTERVAL_S * poll_policy.POST_429_BACKOFF_MULT
        )

    def test_recent_429_ceiling_exceeds_normal_candidate_max(self):
        # The 429 ceiling is wider than the normal candidate ceiling so a
        # contended token can back off far enough for several machines to fit.
        assert (
            poll_policy.POST_429_MAX_INTERVAL_S
            > poll_policy.CANDIDATE_MAX_INTERVAL_S
        )
        _, interval = _plan(
            recent_429=True,
            prev_interval_s=poll_policy.POST_429_MAX_INTERVAL_S,  # already at ceiling
            prev_usage=_usage(10),
        )
        assert interval == poll_policy.POST_429_MAX_INTERVAL_S

    def test_no_429_uses_normal_ceiling(self):
        # Without recent 429s the wider ceiling never applies (normal cadence).
        _, interval = _plan(
            recent_429=False, prev_interval_s=700.0, prev_usage=_usage(10)
        )
        assert interval == max(poll_policy.CANDIDATE_MAX_INTERVAL_S, poll_policy.ACTIVE_FAR_INTERVAL_S)

    def _converge_trajectory(self, recent_429: bool, rounds: int = 12):
        # Deterministic evolution of the interval under a sustained contended
        # token: each successful poll re-plans with the same recent_429 flag and
        # unmoved usage (movement decay would only shorten it — the worst case
        # for convergence is an unmoving account that just keeps 429ing). rng at
        # the midpoint so jitter is exactly 1.0 and the trajectory is exact.
        prev = None
        traj = []
        for _ in range(rounds):
            _, interval = _plan(
                recent_429=recent_429,
                prev_interval_s=prev,
                prev_usage=_usage(10),
                new_usage=_usage(10),
            )
            traj.append(interval)
            prev = interval
        return traj

    def test_sustained_429_grows_the_interval_to_the_wide_ceiling(self):
        # THE convergence property: while 429s recur, the interval must keep
        # growing (×MULT) until it reaches POST_429_MAX_INTERVAL_S. This is what
        # lets N machines sharing one token each back off far enough that their
        # combined rate drops under the budget — the deadlock cannot clear
        # without it.
        traj = self._converge_trajectory(recent_429=True)
        # strictly increasing until it saturates at the wide ceiling
        assert traj[-1] == poll_policy.POST_429_MAX_INTERVAL_S
        assert traj == sorted(traj)  # monotonic non-decreasing
        # actually reaches the ceiling within the simulated rounds
        assert max(traj) == poll_policy.POST_429_MAX_INTERVAL_S
        # each pre-ceiling step grew by the multiplier (AIMD multiplicative incr)
        for a, b in zip(traj, traj[1:]):
            if b < poll_policy.POST_429_MAX_INTERVAL_S:
                assert b == pytest.approx(a * poll_policy.POST_429_BACKOFF_MULT)

    def test_without_recency_the_interval_is_capped_at_the_narrow_ceiling(self):
        # The failure mode the recency bug caused: if recent_429 is False on the
        # post-block success (the pre-fix behavior after an honored hour-scale
        # Retry-After), the interval can never exceed CANDIDATE_MAX_INTERVAL_S.
        # N machines then jam at 600s each and their combined rate can sit above
        # the budget forever — a permanent deadlock the AIMD is meant to break.
        traj = self._converge_trajectory(recent_429=False)
        assert max(traj) == max(poll_policy.CANDIDATE_MAX_INTERVAL_S, poll_policy.ACTIVE_FAR_INTERVAL_S)
        assert max(poll_policy.CANDIDATE_MAX_INTERVAL_S, poll_policy.ACTIVE_FAR_INTERVAL_S) < poll_policy.POST_429_MAX_INTERVAL_S


class TestResetCapping:
    def _iso(self, ts: float) -> str:
        return (
            datetime.fromtimestamp(ts, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )

    def test_poll_never_scheduled_past_a_future_reset(self):
        reset_ts = NOW + 90.0
        next_poll, interval = _plan(new_usage=_usage(40, self._iso(reset_ts)))
        assert next_poll == pytest.approx(reset_ts + poll_policy.RESET_SLACK_S)
        assert interval == poll_policy.CANDIDATE_DEFAULT_INTERVAL_S

    def test_at_limit_keeps_bounded_polling_before_distant_reset(self):
        reset_ts = NOW + 7_200.0
        next_poll, interval = _plan(new_usage=_usage(100, self._iso(reset_ts)))
        assert interval == poll_policy.EXHAUSTED_INTERVAL_S
        assert next_poll == pytest.approx(NOW + interval)
        assert next_poll < reset_ts

    def test_at_limit_poll_is_pulled_to_an_imminent_reset(self):
        reset_ts = NOW + 90.0
        next_poll, interval = _plan(new_usage=_usage(100, self._iso(reset_ts)))
        assert interval == poll_policy.EXHAUSTED_INTERVAL_S
        assert next_poll == pytest.approx(reset_ts + poll_policy.RESET_SLACK_S)

    @pytest.mark.parametrize("reset_ts", [NOW - 90.0, NOW])
    def test_at_limit_ignores_non_future_reset(self, reset_ts):
        next_poll, interval = _plan(new_usage=_usage(100, self._iso(reset_ts)))
        assert interval == poll_policy.EXHAUSTED_INTERVAL_S
        assert next_poll == pytest.approx(NOW + interval)

    def test_active_at_limit_uses_same_bounded_recovery_probe(self):
        reset_ts = NOW + 7_200.0
        next_poll, interval = _plan(
            new_usage=_usage(100, self._iso(reset_ts)), is_active=True
        )
        assert interval == poll_policy.EXHAUSTED_INTERVAL_S
        assert next_poll == pytest.approx(NOW + interval)


class TestJitter:
    def test_jitter_bounds(self, monkeypatch):
        monkeypatch.setattr("claude_swap.poll_policy.JITTER_FRAC", 0.1)
        early, _ = _plan(rng=lambda: 0.0)
        late, _ = _plan(rng=lambda: 1.0)
        interval = poll_policy.CANDIDATE_DEFAULT_INTERVAL_S
        assert early == pytest.approx(NOW + interval * 0.9)
        assert late == pytest.approx(NOW + interval * 1.1)


class TestBudgetInvariants:
    """Relationships the measured rate limit demands of the constants.

    Measured 2026-07-11 (probe3): a rolling ~60-minute window of ~28-30
    requests per token × UA-class — not a refilling bucket. Capacity returns
    only as old requests age out of the trailing hour, so a saturated window
    needs up to 60 minutes to recover. These invariants lean only on the
    robust parts of that measurement (a safe sustained cadence and an
    hour-scale recovery horizon), not on the exact server algorithm.
    """

    def test_sustained_floor_stays_under_the_hourly_cap(self):
        # 3600/180 = 20 requests/hour vs the measured ~28-30/hour window.
        assert poll_policy.MIN_INTERVAL_S >= 180.0
        assert poll_policy.SERVE_TTL_S >= 180.0

    def test_edge_backoff_probes_slower_than_capacity_frees(self):
        # While saturated, capacity returns at up to ~30/hour as the old
        # burst ages out; probing at ≥300 s (≤12/hour) lets recovery win.
        assert poll_policy.EDGE_BACKOFF_S >= 300.0

    def test_post_429_floor_covers_the_saturation_horizon(self):
        # A 429 means the trailing hour is full, and it takes up to 60
        # minutes for the spending burst to age out entirely.
        assert poll_policy.RECENT_429_WINDOW_S >= 3600.0
        assert poll_policy.POST_429_MIN_INTERVAL_S >= poll_policy.MIN_INTERVAL_S

    def test_urgent_episode_alone_fits_inside_the_window_cap(self):
        # Urgent mode is bounded by construction: each further urgent poll
        # needs ≥ MOVEMENT_DELTA_PCT of movement, so the slowest qualifying
        # burn crosses the escalation band in margin/delta polls — inside the
        # ~28-30 request rolling-hour window even before the post-429 floor
        # (which absorbs any overshoot) is considered.
        polls = poll_policy.ESCALATION_MARGIN_PCT / poll_policy.MOVEMENT_DELTA_PCT
        assert polls < 27
