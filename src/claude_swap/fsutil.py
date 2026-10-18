"""Filesystem primitives with no claude_swap dependencies.

Deliberately a leaf module: the atomic-write helpers here are needed by
settings, credentials, mappings and session, which sit on both sides of
the paths/models/usage_store import cycle.
"""

from __future__ import annotations

import errno
import os
import sys
import tempfile
import time
from pathlib import Path

# Windows error codes that usually mean "someone else has the file open right
# now": ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION and ERROR_LOCK_VIOLATION.
# AV/indexer contention clears within milliseconds; ERROR_ACCESS_DENIED can
# also be a persistent condition (ACLs, read-only attribute), which still
# surfaces once the bounded retries are exhausted.
_TRANSIENT_WIN_ERRORS = frozenset({5, 32, 33})


def read_text_with_retry(
    path: os.PathLike | str,
    *,
    attempts: int = 10,
    initial_delay: float = 0.002,
) -> str:
    """``Path.read_text``, retried past the same transient Windows failures.

    The read side of :func:`replace_with_retry`, and it needs the same
    treatment for the same reason: ``_write_json`` publishes the roster by
    renaming onto ``sequence.json``, so the file is freshly-modified exactly
    when AV/the indexer opens it opportunistically, and a reader arriving in
    that window gets ERROR_SHARING_VIOLATION as a ``PermissionError``.

    That window matters more on the read side than the write side, because a
    strict roster read raises ``ConfigError`` at ~59 call sites — including
    read-only ones (``list_accounts``, ``_account_kind``) and
    ``SwitchTransaction.rollback``, where turning a recoverable switch failure
    into a failed ROLLBACK is the worst outcome available.

    POSIX is untouched: there is no such window, and an ``EACCES`` there is a
    genuine, persistent permission problem that must surface at once rather
    than after a ~0.75 s stall.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    delay = initial_delay
    for attempt in range(attempts):
        try:
            return Path(path).read_text(encoding="utf-8")
        except OSError as e:
            transient = (
                sys.platform == "win32"
                and getattr(e, "winerror", None) in _TRANSIENT_WIN_ERRORS
            )
            if not transient or attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.25)
    raise AssertionError("unreachable")  # pragma: no cover - loop always exits


def replace_with_retry(
    src: os.PathLike | str,
    dst: os.PathLike | str,
    *,
    attempts: int = 10,
    initial_delay: float = 0.002,
) -> None:
    """``os.replace``, retried past transient Windows sharing failures.

    POSIX ``rename`` is genuinely atomic and never fails this way, but on
    Windows antivirus (Defender) and the search indexer open freshly-created
    files opportunistically. A replace onto a just-written target then fails
    with ERROR_ACCESS_DENIED or ERROR_SHARING_VIOLATION for a few milliseconds
    — measured at ~44% of replaces into a scanned temp directory, which made
    credential and usage-store writes fail intermittently for real users.

    Only the contention codes are retried; other errors (missing source,
    cross-device link) surface on the first attempt. A *persistent* access
    denial (ACLs, read-only target) matches the retried codes and so raises
    only after the attempt budget — a ~0.75 s delay in the worst case.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    delay = initial_delay
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except OSError as e:
            transient = (
                sys.platform == "win32"
                and getattr(e, "winerror", None) in _TRANSIENT_WIN_ERRORS
            )
            if not transient or attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.25)


# Filesystems that do not implement fsync answer one of these; the write goes
# ahead without the durability it would otherwise buy (Claude Code's own
# writer tolerates the same set). Anything else -- EIO above all -- raises.
_FSYNC_UNSUPPORTED = frozenset(
    code
    for code in (
        getattr(errno, name, None)
        for name in ("EINVAL", "ENOTSUP", "EOPNOTSUPP", "ENOSYS")
    )
    if code is not None
)


def atomic_write_bytes(
    path: os.PathLike | str, data: bytes, *, mode: int = 0o600
) -> None:
    """Publish ``data`` at ``path`` by temp file + rename.

    ``mkstemp`` creates the temp beside the target (0600 from creation, same
    filesystem, so the rename is atomic); the bytes are written, ``mode`` is
    set on the open descriptor, the content is fsynced, and the temp is
    renamed over the target. A reader sees the old file or the new one, never
    a truncated or partial one -- which is what ``Path.write_text`` (O_TRUNC,
    then write) cannot promise, and why the switch rollback must not use it:
    Claude Code watches ``~/.claude.json`` and treats a torn read as a
    corrupted config (#419).

    Precisely what is promised: a failure before the rename leaves the
    destination exactly as it was and removes the temp; no further ordinary
    fallible operation follows the rename (a signal can still interrupt the
    caller once the file is published). The content is fsynced before it is
    published, so a crash cannot leave an empty new file where an old one was.
    The durability of the rename itself is the directory's business and is
    not promised: a crash right after it can lose a newly created file or
    show the previous version of an existing one. Where the filesystem
    reports fsync as unsupported, the write proceeds without that guarantee.
    One cleanup gap is accepted: a signal landing inside ``mkstemp`` between
    the create and the return of its name strands one 0600 temp.

    Not done here on purpose: no ``mkdir`` of parents (callers that need it do
    it first), and no in-place fallback when the rename is refused -- a
    bind-mounted target answers EBUSY, the write fails, the destination is
    untouched. Claude Code's own writer does fall back to an in-place rewrite
    there (keeping its temp until the rewrite lands); that is the shape to copy
    if anyone ever asks for it, in this one place.
    """
    target = Path(path)
    fd, tmp = tempfile.mkstemp(
        dir=str(target.parent), prefix=f"{target.name}.", suffix=".tmp"
    )
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written == 0:
                raise OSError(errno.EIO, "write made no progress", tmp)
            view = view[written:]
        if sys.platform != "win32":
            os.fchmod(fd, mode)
        try:
            os.fsync(fd)
        except OSError as e:
            if e.errno not in _FSYNC_UNSUPPORTED:
                raise
        os.close(fd)
        fd = -1
        replace_with_retry(tmp, target)
    except BaseException:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(
    path: os.PathLike | str, text: str, *, mode: int = 0o600
) -> None:
    """:func:`atomic_write_bytes` for UTF-8 text."""
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)
