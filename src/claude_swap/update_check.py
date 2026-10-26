"""Check PyPI for newer versions of claude-swap."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
import urllib.request
from pathlib import Path
from typing import Any, NamedTuple

from claude_swap.cache import CACHE_DIR, MISSING, read_cache, write_cache

CACHE_PATH = CACHE_DIR / "update_check.json"
CACHE_TTL = 24 * 3600  # 24 hours
PYPI_URL = "https://pypi.org/pypi/claude-swap/json"

_VERSION_RE = re.compile(
    r"(\d+(?:\.\d+)*)(?:[-_.]?(alpha|beta|preview|pre|rc|a|b|c)[-_.]?(\d+)?)?",
    re.IGNORECASE,
)
_PRE_RANKS = {"alpha": 0, "a": 0, "beta": 1, "b": 1, "preview": 2, "pre": 2, "rc": 2, "c": 2}
_FINAL_RANK = 3


class _Version(NamedTuple):
    """A version as a sort key. NamedTuple so that comparing two of these
    compares the release first and only then the pre-release fields, which is
    what puts 0.27.0b1 below 0.27.0 and both below 0.28.0."""

    release: tuple[int, ...]
    pre_rank: int  # _FINAL_RANK when this is not a pre-release.
    pre_number: int


def _parse_version(v: str) -> _Version:
    """Parse a release number with an optional PEP 440 pre-release suffix.

    Every cycle of claude-swap ships as a pre-release first (0.27.0b1,
    0.26.0b1, ...), and a plain int() over the dotted parts raised ValueError
    on those, which check_for_update swallowed as "no update available".
    Development and post releases are not modeled — the project publishes
    none — so they collapse onto the release they belong to.
    """
    m = _VERSION_RE.match(v)
    if m is None:
        raise ValueError(f"unrecognized version: {v!r}")
    release = tuple(int(x) for x in m.group(1).split("."))
    # PEP 440 makes 0.27 and 0.27.0 the same release, and the pre-release rank
    # only means anything once the release segments line up.
    while len(release) > 1 and release[-1] == 0:
        release = release[:-1]
    if m.group(2) is None:
        return _Version(release, _FINAL_RANK, 0)
    return _Version(release, _PRE_RANKS[m.group(2).lower()], int(m.group(3) or 0))


def _is_newer(latest: str, current: str) -> bool:
    """Whether the latest release is an upgrade worth telling the user about."""
    latest_version = _parse_version(latest)
    current_version = _parse_version(current)
    # Nobody on a final release asked to be moved onto a pre-release, so we
    # stay quiet for them; people already running one still hear about later
    # pre-releases.
    if latest_version.pre_rank != _FINAL_RANK and current_version.pre_rank == _FINAL_RANK:
        return False
    return latest_version > current_version


def _detect_install_method() -> str | None:
    """Return 'uv', 'pipx', or None if we can't tell."""
    prefix = Path(sys.prefix)
    parts = tuple(p.lower() for p in prefix.parts)
    pairs = list(zip(parts, parts[1:]))

    if ("uv", "tools") in pairs:
        return "uv"
    if ("pipx", "venvs") in pairs:
        return "pipx"

    # Env-var override: only trust if sys.prefix is actually under it.
    for env_var, name in (("UV_TOOL_DIR", "uv"), ("PIPX_HOME", "pipx")):
        root = os.environ.get(env_var)
        if root:
            try:
                if prefix.is_relative_to(Path(root)):
                    return name
            except (ValueError, OSError):
                pass
    return None


class _Cooldown(NamedTuple):
    """An exclude-newer setting as uv spells it, and where the user set it."""

    value: str  # "P14D", "14 days", "2026-10-11T00:36:21Z"
    source: str  # 'uv-receipt.toml: exclude-newer-span = "P14D"'
    from_env: bool  # Set through UV_EXCLUDE_NEWER rather than the receipt.


def _read_receipt() -> dict[str, Any]:
    """The ``[tool]`` table of this install's uv tool receipt, or ``{}``."""
    try:
        with (Path(sys.prefix) / "uv-receipt.toml").open("rb") as fh:
            tool = tomllib.load(fh).get("tool", {})
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return tool if isinstance(tool, dict) else {}


def _uv_cooldown(receipt: dict[str, Any]) -> _Cooldown | None:
    """The release cooldown ``uv tool upgrade`` applies to this install.

    uv records ``--exclude-newer`` in the tool receipt and reapplies it on
    every upgrade. A relative setting ("14 days") is stored as
    ``exclude-newer-span`` next to the absolute cutoff of install time, and
    the span is what uv recomputes from. ``UV_EXCLUDE_NEWER`` in the
    environment overrides the receipt, as it does for uv itself.
    """
    env_value = os.environ.get("UV_EXCLUDE_NEWER", "").strip()
    if env_value:
        return _Cooldown(env_value, f"UV_EXCLUDE_NEWER={env_value}", from_env=True)
    options = receipt.get("options", {})
    if not isinstance(options, dict):
        return None
    for key in ("exclude-newer-span", "exclude-newer"):
        value = options.get(key)
        if isinstance(value, str):
            return _Cooldown(value, f'uv-receipt.toml: {key} = "{value}"', from_env=False)
    return None


_PLAIN_WORD_RE = re.compile(r"^[A-Za-z0-9._+=:/@-]+$")


def _shell_word(word: str) -> str:
    """Quote an argument for pasting into sh, cmd or PowerShell alike."""
    return word if _PLAIN_WORD_RE.match(word) else f'"{word}"'


def _requirement_spec(requirement: dict[str, Any], *, primary: bool) -> str | None:
    """A receipt requirement as the user would spell it on the command line.

    The primary requirement keeps only its extras: a version pin there would
    reinstall the release the user is trying to move off.
    """
    name = requirement.get("name")
    if not isinstance(name, str):
        return None
    extras = requirement.get("extras")
    spec = name
    if isinstance(extras, list) and extras:
        spec += "[" + ",".join(str(extra) for extra in extras) + "]"
    if primary:
        return spec
    specifier = requirement.get("specifier")
    if isinstance(specifier, str):
        spec += specifier
    marker = requirement.get("marker")
    if isinstance(marker, str):
        spec += f"; {marker}"
    return spec


def _reinstall_command(receipt: dict[str, Any]) -> str:
    """``uv tool install --reinstall`` with everything the receipt installed.

    A plain ``uv tool install claude-swap --reinstall`` would drop the
    ``[menubar]`` extra and every ``--with`` package along with their
    dependencies, so the command is rebuilt from the receipt's requirement
    list: the first entry is the tool itself and the rest were ``--with`` or
    ``--with-editable``.
    """
    requirements = receipt.get("requirements")
    if not isinstance(requirements, list):
        requirements = []
    args: list[str] = ["uv", "tool", "install", "--reinstall"]
    primary_seen = False
    for requirement in requirements:
        if not isinstance(requirement, dict):
            continue
        editable = requirement.get("editable")
        if isinstance(editable, str):
            args += ["--editable" if not primary_seen else "--with-editable", editable]
            primary_seen = True
            continue
        spec = _requirement_spec(requirement, primary=not primary_seen)
        if spec is None:
            continue
        args += [spec] if not primary_seen else ["--with", spec]
        primary_seen = True
    if not primary_seen:
        args.append("claude-swap")
    return " ".join(_shell_word(arg) for arg in args)


def _uv_latest_version(current_version: str, cooldown: _Cooldown | None) -> str | None:
    """The newest claude-swap release uv would install into this environment.

    ``uv pip list --outdated`` resolves against the index the way ``uv tool
    upgrade`` does once it is handed the same exclude-newer setting, so the
    version it reports is one uv can actually install. Returns the current
    version when nothing newer is visible and None when uv is missing or
    fails. The environment variable is dropped so that the cooldown passed
    here is the only one uv sees, and the command runs from the tool's own
    directory because uv reads the ``uv.toml`` or ``pyproject.toml`` of
    whatever project the shell happens to be in. ``uv tool upgrade`` does
    not, so a project's own cutoff must not hide releases from this check.
    """
    cmd = ["uv", "pip", "list", "--outdated", "--format", "json", "--python", sys.executable]
    if cooldown is not None:
        cmd += ["--exclude-newer", cooldown.value]
    env = {k: v for k, v in os.environ.items() if k != "UV_EXCLUDE_NEWER"}
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
            check=True,
            cwd=sys.prefix,
        )
        rows = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    for row in rows:
        if row.get("name") == "claude-swap":
            latest = row.get("latest_version")
            return latest if isinstance(latest, str) else None
    return current_version


def _pypi_latest_version() -> str | None:
    """PyPI's latest release, for installs that uv does not manage."""
    try:
        req = urllib.request.Request(PYPI_URL)
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read().decode())
        return data["info"]["version"]
    except Exception:
        return None


class _Lookup(NamedTuple):
    """A version lookup and whether its answer is worth keeping for a day."""

    latest: str | None
    cache: bool


def _latest_version(current_version: str, method: str | None) -> _Lookup:
    """The newest release the user's package manager will actually install.

    uv is often off PATH when cswap runs under cron or launchd, and a failed
    lookup cached for a day would keep the interactive runs that follow quiet
    too. Without a cooldown PyPI answers the same question, so it takes over.
    With one, PyPI would advertise releases the cooldown hides, so the run
    stays quiet and leaves the cache alone for the next run to retry.
    """
    if method != "uv":
        return _Lookup(_pypi_latest_version(), cache=True)
    cooldown = _uv_cooldown(_read_receipt())
    latest = _uv_latest_version(current_version, cooldown)
    if latest is not None:
        return _Lookup(latest, cache=True)
    if cooldown is None:
        return _Lookup(_pypi_latest_version(), cache=True)
    return _Lookup(None, cache=False)


def held_back_release_notice(current_version: str) -> str | None:
    """Explain a release that uv's cooldown keeps ``uv tool upgrade`` from seeing.

    uv prints "Nothing to upgrade" whether the install is current or the
    newest release is simply younger than the cooldown, and the user cannot
    tell the two apart. When a cooldown is set, this asks uv what it would
    install without one and names the setting that is in the way. The remedy
    is a reinstall that keeps the extras and ``--with`` packages, and when
    the cooldown comes from ``UV_EXCLUDE_NEWER`` the variable has to go
    first: uv writes it straight back into the fresh receipt otherwise.
    """
    receipt = _read_receipt()
    cooldown = _uv_cooldown(receipt)
    if cooldown is None:
        return None
    latest = _uv_latest_version(current_version, None)
    try:
        if latest is None or not _is_newer(latest, current_version):
            return None
    except ValueError:
        return None
    if cooldown.from_env:
        remedy = "To upgrade now and drop the cooldown, unset UV_EXCLUDE_NEWER and run:"
    else:
        remedy = "To upgrade now and drop the cooldown, run:"
    return (
        f"claude-swap {latest} is on PyPI, but this install has a release cooldown\n"
        f"  ({cooldown.source}) that hides it from uv tool upgrade.\n"
        f"  {remedy}\n"
        f"    {_reinstall_command(receipt)}"
    )


def check_for_update(current_version: str) -> str | None:
    """Return a notification string if a newer version exists, else None."""
    try:
        method = _detect_install_method()
        cached_data = read_cache(CACHE_PATH, CACHE_TTL)
        if cached_data is not MISSING and isinstance(cached_data, str | None):
            latest_version = cached_data
        else:
            lookup = _latest_version(current_version, method)
            latest_version = lookup.latest
            # A failed PyPI lookup is cached too, so a dead network costs
            # one timeout a day. A failed uv lookup under a cooldown is not.
            if lookup.cache:
                write_cache(CACHE_PATH, latest_version)

        if latest_version and _is_newer(latest_version, current_version):
            direct = {
                "uv": "uv tool upgrade claude-swap",
                "pipx": "pipx upgrade claude-swap",
            }.get(method or "")
            if direct and sys.platform != "win32":
                # cswap upgrade actually performs the upgrade here.
                hint = "Run `cswap upgrade` to update."
            elif direct:
                # Windows: cswap upgrade only prints, so point at the real command.
                hint = f"Run `{direct}` to update."
            else:
                # Unknown install method: cswap upgrade shows manual instructions.
                hint = "Run `cswap upgrade` for upgrade instructions."
            return (
                f"A newer version of claude-swap is available ({latest_version}). "
                f"You are using {current_version}. {hint}"
            )
        return None
    except Exception:
        return None


def run_self_upgrade(current_version: str | None = None) -> int:
    """Run the appropriate upgrade command for the current install method.

    Returns the subprocess exit code, or 1 if detection failed or the package
    manager is missing from PATH. A uv install with a release cooldown gets
    told about a release the cooldown hides, since uv itself only reports
    "Nothing to upgrade".
    """
    from claude_swap.printer import accent, error, warning

    if current_version is None:
        from claude_swap import __version__

        current_version = __version__

    method = _detect_install_method()
    commands = {
        "uv": ["uv", "tool", "upgrade", "claude-swap"],
        "pipx": ["pipx", "upgrade", "claude-swap"],
    }
    cmd = commands.get(method or "")
    if cmd is None:
        error(
            "Could not detect install method (looked for uv tool / pipx).\n"
            f"  sys.prefix:     {sys.prefix}\n"
            f"  sys.executable: {sys.executable}\n"
            "To upgrade manually, run one of:\n"
            "  uv tool upgrade claude-swap\n"
            "  pipx upgrade claude-swap\n"
            f"  {sys.executable} -m pip install --upgrade claude-swap\n"
            "If you installed with `pip install -e .`, use `git pull` instead."
        )
        return 1

    # Windows: the running cswap.exe launcher is locked, so an in-process
    # uv/pipx upgrade fails when it tries to replace the executable even
    # though the package itself updates. cswap exits right after this, which
    # releases the lock, so the user can just run the command themselves.
    if sys.platform == "win32":
        print(f"To upgrade claude-swap on Windows, run:\n  {accent(' '.join(cmd))}")
        if method == "uv":
            notice = held_back_release_notice(current_version)
            if notice:
                warning(notice)
        return 1

    try:
        result = subprocess.run(cmd, check=False)
    except FileNotFoundError:
        error(
            f"Detected {method} install but `{cmd[0]}` is not on PATH. "
            "Run the upgrade manually from a shell where it is available."
        )
        return 1
    if result.returncode == 0 and method == "uv":
        notice = held_back_release_notice(current_version)
        if notice:
            warning(notice)
    return result.returncode
