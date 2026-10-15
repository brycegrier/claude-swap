"""Dashboard: every account's full card on top, a nested action menu below.

The accounts panel is the live monitor — every account full-size, always
on screen — so there is no separate watch or switch page; the arrow keys
drive the *menu*. Account actions are submenus, like the settings:

- ``s`` / menu "Switch account…" → a picker: the best pick (most room
  left) first, then each account with its usage; Enter switches.
- "Remove account" / "Disable / enable account" list the accounts.
- "Settings" lists the ``autoswitch`` keys of settings.json (the same ones
  ``cswap config`` edits): choices open a submenu, booleans toggle in place,
  numbers and text open an input modal.

No global command palette: actions live where their context is.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, ListView, Static

from claude_swap.exceptions import ConfigError
from claude_swap.poll_policy import binding_pct
from claude_swap.tui import data
from claude_swap.settings import (
    SETTING_SPECS,
    effective_settings,
    format_setting_value,
    load_settings,
    set_setting,
    unset_setting,
)
from claude_swap.tui.modals import RESET_TO_DEFAULT, SettingInputModal
from claude_swap.tui.widgets import AccountsPanel, MenuItem

if TYPE_CHECKING:
    from claude_swap.tui.app import CswapApp

MenuEntries = list[tuple[str, str]]  # (label, action_id)

_BACK = ("← back", "back")


class DashboardScreen(Screen):
    BINDINGS = [
        Binding("s", "open_switch", "Switch account"),
        Binding("escape,left", "menu_back", "Back", show=False),
        Binding("q", "app.quit", "Quit"),
        # Power shortcuts; the menu is the discoverable path.
        Binding("g", "app.open_auto", "Auto view", show=False),
        Binding("f", "app.refresh_full", "Refresh usage", show=False),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    app: "CswapApp"

    def __init__(self) -> None:
        super().__init__()
        # Stack of (title, entries); depth 1 = root menu.
        self._menu_stack: list[tuple[str, MenuEntries]] = []

    def compose(self) -> ComposeResult:
        # Every account full-size; scrolls past half the screen so the menu
        # below always keeps its room.
        with VerticalScroll(id="accounts-panel-scroll"):
            yield AccountsPanel(full=True, id="accounts-panel")
        yield Static("", id="menu-title")
        yield ListView(id="menu")
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#menu", ListView).focus()
        await self._push_menu("menu", self._root_entries())
        self.watch(self.app, "refresh_status", self._update_title)

    # -- menu plumbing --------------------------------------------------------

    def _root_entries(self) -> MenuEntries:
        # No "Refresh" entry: every view auto-refreshes, so a menu item would
        # wrongly imply the user has to. `f` stays as a hidden escape hatch.
        return [
            ("Switch account…", "switch-menu"),
            ("Auto-switch view", "auto"),
            ("Add account…", "add-menu"),
            ("Disable / enable account…", "disable-menu"),
            ("Remove account…", "remove-menu"),
            ("Settings…", "settings-menu"),
            ("Theme…", "theme-menu"),
            ("Quit", "quit"),
        ]

    def _switch_entries(self) -> MenuEntries:
        """The best pick first, then every account with its usage; the
        active one marked ● (selecting it is a no-op)."""
        snap = self.app.snapshot
        entries: MenuEntries = [("★ best pick (most room left)", "switch-best")]
        for acc in (snap.accounts if snap else ()):
            name = acc.alias or acc.email.split("@", 1)[0]
            if acc.usage.sentinel is not None:
                usage = data.sentinel_label(acc.usage.sentinel)
            else:
                pct = binding_pct(acc.usage.last_good)
                usage = "usage unknown" if pct is None else f"{pct:.0f}% used"
            mark = "●" if acc.is_active else " "
            state = "  (disabled)" if acc.disabled else ""
            entries.append(
                (f"{mark} {acc.number}  {name:<12} {usage}{state}", f"switch:{acc.number}")
            )
        entries.append(_BACK)
        return entries

    def _add_entries(self) -> MenuEntries:
        return [
            ("From current Claude Code login", "add-login"),
            ("From a setup-token / API key…", "add-token"),
            _BACK,
        ]

    def _remove_entries(self) -> MenuEntries:
        snap = self.app.snapshot
        entries: MenuEntries = [
            (
                f"{acc.number}  {f'{acc.alias} ({acc.email})' if acc.alias else acc.email}"
                f"  [{acc.display_tag}]",
                f"remove:{acc.number}",
            )
            for acc in (snap.accounts if snap else ())
        ]
        entries.append(_BACK)
        return entries

    def _disable_entries(self) -> MenuEntries:
        """One row per account, labelled with its current state and the action
        selecting it will take (enable a disabled one, disable an active one)."""
        snap = self.app.snapshot
        entries: MenuEntries = []
        for acc in (snap.accounts if snap else ()):
            name = f"{acc.alias} ({acc.email})" if acc.alias else acc.email
            action = "→ enable" if acc.disabled else "→ disable"
            state = "  (disabled)" if acc.disabled else ""
            entries.append(
                (f"{acc.number}  {name}{state}   {action}", f"disable:{acc.number}")
            )
        entries.append(_BACK)
        return entries

    def _theme_entries(self) -> MenuEntries:
        """dark / light / auto, with the active setting marked."""
        current = self.app._theme_name
        entries: MenuEntries = [
            (f"{'●' if name == current else ' '} {name}", f"theme:{name}")
            for name in ("dark", "light", "auto")
        ]
        entries.append(_BACK)
        return entries

    def _setting_rows(self) -> list:
        """(spec, value, is_set) for the autoswitch keys; ui.theme has its
        own menu."""
        return [
            row
            for row in effective_settings(self.app.switcher.backup_dir)
            if row[0].section == "autoswitch"
        ]

    def _settings_entries(self) -> MenuEntries:
        """One row per setting: key, effective value, and whether it is the
        default (absent from the file) — the same view as ``cswap config``."""
        rows = self._setting_rows()
        width = max(len(spec.json_key) for spec, _value, _set in rows)
        entries: MenuEntries = []
        for spec, value, is_set in rows:
            shown = format_setting_value(value)
            marker = "" if is_set else "  (default)"
            entries.append(
                (f"{spec.json_key:<{width}}  {shown}{marker}", f"setting:{spec.dotted}")
            )
        entries.append(_BACK)
        return entries

    def _choice_entries(self, dotted: str) -> MenuEntries:
        spec = SETTING_SPECS[dotted]
        current, is_set = next(
            (value, is_set)
            for s, value, is_set in self._setting_rows()
            if s.dotted == dotted
        )
        entries: MenuEntries = [
            (f"{'●' if choice == current else ' '} {choice}", f"set:{dotted}={choice}")
            for choice in spec.choices
        ]
        if is_set:
            entries.append(
                (
                    f"  reset to default ({format_setting_value(spec.default)})",
                    f"unset:{dotted}",
                )
            )
        entries.append(_BACK)
        return entries

    async def _push_menu(self, title: str, entries: MenuEntries) -> None:
        self._menu_stack.append((title, entries))
        await self._render_menu()

    async def _pop_menu(self) -> None:
        if len(self._menu_stack) > 1:
            self._menu_stack.pop()
            await self._render_menu()

    async def _refresh_settings_menu(self, dotted: str) -> None:
        """Re-render the settings list in place, cursor on ``dotted``'s row."""
        title, _entries = self._menu_stack[-1]
        entries = self._settings_entries()
        self._menu_stack[-1] = (title, entries)
        await self._render_menu()
        self.query_one("#menu", ListView).index = next(
            (i for i, (_l, a) in enumerate(entries) if a == f"setting:{dotted}"), 0
        )

    def _update_title(self, _status: str = "") -> None:
        """Breadcrumb plus the app's refresh status (snapshot age, a long
        refresh in flight) — what the old watch page's title carried."""
        if not self._menu_stack:
            return
        crumb = " › ".join(t for t, _ in self._menu_stack)
        status = self.app.refresh_status
        self.query_one("#menu-title", Static).update(
            f"{crumb}   ·   {status}" if status else crumb
        )

    async def _render_menu(self) -> None:
        title, entries = self._menu_stack[-1]
        self._update_title()
        menu = self.query_one("#menu", ListView)
        await menu.clear()
        await menu.extend(
            MenuItem(label, action_id, muted=(action_id == "back"))
            for label, action_id in entries
        )
        menu.index = 0

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, MenuItem):
            await self._dispatch(item.action_id)

    async def _dispatch(self, action_id: str) -> None:
        app = self.app
        actions: dict[str, Callable[[], None]] = {
            "auto": app.action_open_auto,
            "add-login": app.action_add_current,
            "add-token": app.action_add_token,
            "quit": app.exit,
        }
        if action_id == "back":
            await self._pop_menu()
        elif action_id == "switch-menu":
            await self.action_open_switch()
        elif action_id == "switch-best":
            app.action_switch_best()
            await self._pop_to_root()
        elif action_id.startswith("switch:"):
            number = action_id.split(":", 1)[1]
            snap = app.snapshot
            if snap is not None and snap.active_number == number:
                app.notify(f"Already on account {number}", timeout=2)
            else:
                app.do_switch(number)
            await self._pop_to_root()
        elif action_id == "add-menu":
            await self._push_menu("add account", self._add_entries())
        elif action_id == "remove-menu":
            await self._push_menu("remove account", self._remove_entries())
        elif action_id.startswith("remove:"):
            number = action_id.split(":", 1)[1]
            snap = app.snapshot
            email = next(
                (a.email for a in (snap.accounts if snap else ()) if a.number == number),
                "?",
            )
            app.confirm_remove(number, email)
        elif action_id == "theme-menu":
            await self._push_menu("theme", self._theme_entries())
        elif action_id.startswith("theme:"):
            name = action_id.split(":", 1)[1]
            app.apply_theme(name)
            app.notify(f"Theme: {name}")
            await self._pop_menu()
        elif action_id == "settings-menu":
            await self._push_menu("settings", self._settings_entries())
        elif action_id.startswith("setting:"):
            await self._edit_setting(action_id.split(":", 1)[1])
        elif action_id.startswith("set:"):
            dotted, value = action_id.split(":", 1)[1].split("=", 1)
            await self._pop_menu()
            await self._apply_setting(dotted, value)
        elif action_id.startswith("unset:"):
            await self._pop_menu()
            await self._apply_setting(action_id.split(":", 1)[1], None)
        elif action_id == "disable-menu":
            await self._push_menu("disable / enable", self._disable_entries())
        elif action_id.startswith("disable:"):
            number = action_id.split(":", 1)[1]
            app.do_toggle_disabled(number)
            await self._pop_menu()
        else:
            actions[action_id]()

    # -- settings ---------------------------------------------------------------

    async def _edit_setting(self, dotted: str) -> None:
        spec = SETTING_SPECS[dotted]
        if spec.kind == "choice":
            await self._push_menu(spec.json_key, self._choice_entries(dotted))
            return
        current = next(
            value for s, value, _set in self._setting_rows() if s.dotted == dotted
        )
        if spec.kind == "bool":
            await self._apply_setting(dotted, "false" if current else "true")
            return
        shown = "" if current is None else format_setting_value(current)

        async def on_result(result: object) -> None:
            if result is RESET_TO_DEFAULT:
                await self._apply_setting(dotted, None)
            elif isinstance(result, str):
                await self._apply_setting(dotted, result)

        self.app.push_screen(SettingInputModal(spec, shown), on_result)

    async def _apply_setting(self, dotted: str, raw: str | None) -> None:
        """Persist one key (``None`` = back to default) and re-render."""
        app = self.app
        backup_dir = app.switcher.backup_dir
        try:
            if raw is None:
                unset_setting(backup_dir, dotted)
            else:
                set_setting(backup_dir, dotted, raw)
        except ConfigError as exc:
            app.notify(str(exc), severity="error")
            return
        if dotted == "autoswitch.threshold":
            # The bar tick everywhere reads app.threshold_pct.
            app.threshold_pct = load_settings(backup_dir).threshold
            self.query_one("#accounts-panel", AccountsPanel).refresh()
        spec = SETTING_SPECS[dotted]
        value = next(
            value for s, value, _set in self._setting_rows() if s.dotted == dotted
        )
        app.notify(f"{spec.json_key}: {format_setting_value(value)}")
        await self._refresh_settings_menu(dotted)

    # -- actions ----------------------------------------------------------------

    async def action_open_switch(self) -> None:
        if self._menu_stack and self._menu_stack[-1][0] == "switch account":
            return  # already open
        await self._pop_to_root()
        await self._push_menu("switch account", self._switch_entries())
        # Cursor on the active account: the natural starting point.
        snap = self.app.snapshot
        if snap is not None:
            menu = self.query_one("#menu", ListView)
            ids = [a for _l, a in self._menu_stack[-1][1]]
            target = f"switch:{snap.active_number}"
            if target in ids:
                menu.index = ids.index(target)

    async def _pop_to_root(self) -> None:
        if len(self._menu_stack) > 1:
            del self._menu_stack[1:]
            await self._render_menu()

    async def action_menu_back(self) -> None:
        await self._pop_menu()

    def action_cursor_down(self) -> None:
        self.query_one("#menu", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#menu", ListView).action_cursor_up()
