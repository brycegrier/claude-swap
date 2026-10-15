"""The "cswap-dark" / "cswap-light" Textual themes and shared colors.

"Signal": cool slate neutrals, a periwinkle accent kept apart from the
usage colors, and a blue → gold → vermilion severity ramp drawn from the
Okabe–Ito colorblind-safe set. No red/green pairing: each step also changes
lightness, so the bars read with any color vision. Every text color meets
WCAG AA (≥ 4.5:1) on both the background and the panel (the worst-case row
background) in each theme.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from textual.theme import Theme

# Core palette (single source of truth — widgets import these for rich
# renderables, the Theme below maps them onto Textual's design tokens).
ACCENT = "#9db1ff"  # periwinkle
FOREGROUND = "#e3e7ee"  # cool off-white
MUTED = "#8b95a5"  # secondary text (slate)
BACKGROUND = "#13161c"
SURFACE = "#1b1f27"
PANEL = "#232a35"

# Usage severity ramp — blue, gold, vermilion (Okabe–Ito family).
SEV_OK = "#56b4e9"  # sky blue: plenty of headroom
SEV_WARN = "#f0c43c"  # gold: climbing (>= 70%)
SEV_CRIT = "#ee7438"  # vermilion: near the limit (>= 90%)
TRACK = "#2f3744"  # unfilled bar track

# Severity band edges. WARN mirrors where a user starts caring; CRIT mirrors
# the auto-switch default threshold so bar color and switch behavior agree.
WARN_PCT = 70.0
CRIT_PCT = 90.0


@dataclass(frozen=True)
class Palette:
    """Resolved colors for Rich renderables, keyed to a Textual theme.

    Rich renderables bake color into styles at render time, so they can't read
    Textual's ``$variables`` the way the .tcss layer does. A Palette carries the
    active theme's colors so the same render code paints correctly in either
    theme. Resolve from the Theme object (never App.theme_variables, which lags
    the deferred CSS refresh)."""

    accent: str
    foreground: str
    muted: str
    sev_ok: str
    sev_warn: str
    sev_crit: str
    track: str

    DARK: ClassVar["Palette"]

    def severity(self, pct: float | None) -> str:
        if pct is None:
            return self.muted
        if pct >= CRIT_PCT:
            return self.sev_crit
        if pct >= WARN_PCT:
            return self.sev_warn
        return self.sev_ok

    @classmethod
    def from_theme(cls, theme: Theme) -> "Palette":
        return cls(
            accent=theme.primary,
            foreground=theme.foreground,
            muted=theme.secondary,
            sev_ok=theme.success,
            sev_warn=theme.warning,
            sev_crit=theme.error,
            track=theme.variables.get("track", TRACK),
        )


CSWAP_DARK = Theme(
    name="cswap-dark",
    primary=ACCENT,
    secondary=MUTED,
    accent=ACCENT,
    foreground=FOREGROUND,
    background=BACKGROUND,
    surface=SURFACE,
    panel=PANEL,
    success=SEV_OK,
    warning=SEV_WARN,
    error=SEV_CRIT,
    dark=True,
    variables={
        # Footer keys pick up the accent instead of the default blue.
        "footer-key-foreground": ACCENT,
        "block-cursor-background": PANEL,
        "block-cursor-foreground": FOREGROUND,
        "block-cursor-text-style": "none",
        "track": TRACK,
    },
)

# Light companion palette (same intent on a cool near-white base). The
# severity colors are deepened so text meets AA on the panel (worst-case
# row background).
ACCENT_LIGHT = "#3d55c4"  # deep periwinkle
FOREGROUND_LIGHT = "#1e2430"
MUTED_LIGHT = "#566072"
BACKGROUND_LIGHT = "#f6f8fb"
SURFACE_LIGHT = "#eaeef4"
PANEL_LIGHT = "#dfe5ee"  # most-elevated = darkest (inverted from dark)
SEV_OK_LIGHT = "#1a6496"  # deep blue
SEV_WARN_LIGHT = "#7f5800"  # deep gold
SEV_CRIT_LIGHT = "#a63d0c"  # deep vermilion
TRACK_LIGHT = "#c9d1dd"

CSWAP_LIGHT = Theme(
    name="cswap-light",
    primary=ACCENT_LIGHT,
    secondary=MUTED_LIGHT,
    accent=ACCENT_LIGHT,
    foreground=FOREGROUND_LIGHT,
    background=BACKGROUND_LIGHT,
    surface=SURFACE_LIGHT,
    panel=PANEL_LIGHT,
    success=SEV_OK_LIGHT,
    warning=SEV_WARN_LIGHT,
    error=SEV_CRIT_LIGHT,
    dark=False,
    variables={
        "footer-key-foreground": ACCENT_LIGHT,
        "block-cursor-background": PANEL_LIGHT,
        "block-cursor-foreground": FOREGROUND_LIGHT,
        "block-cursor-text-style": "none",
        "track": TRACK_LIGHT,
    },
)

Palette.DARK = Palette(
    accent=ACCENT, foreground=FOREGROUND, muted=MUTED,
    sev_ok=SEV_OK, sev_warn=SEV_WARN, sev_crit=SEV_CRIT, track=TRACK,
)
