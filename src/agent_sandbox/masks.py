"""Empty read-only mask sources and the checks around applying them (ADR-0004).

A Secret file is Masked by bind-mounting an empty read-only file (or an empty
read-only directory) over it inside the Session. The originals are never read,
written or opened: the mounts are the only thing that touches them, and only
Docker does that. Every failure here must stop `up` (fail closed).
"""
from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path

from .plan import SessionPlan


class MaskError(RuntimeError):
    """A Secret file cannot be, or was not, Masked. `up` must fail."""


def _valid_file(path: str) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_size == 0 and st.st_mode & 0o222 == 0 and st.st_nlink == 1


def _valid_dir(path: str) -> bool:
    try:
        st = os.lstat(path)
        return stat.S_ISDIR(st.st_mode) and st.st_mode & 0o222 == 0 and not os.listdir(path)
    except OSError:
        return False


def _discard(path: str) -> None:
    """Delete a stale mask source (only ever inside our own state directory)."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode):
        for dirpath, _dirnames, _files in os.walk(path):  # make it deletable again
            os.chmod(dirpath, 0o700)
        shutil.rmtree(path)
    else:
        os.unlink(path)


def prepare_mask_sources(plan: SessionPlan) -> None:
    """(Re)create the empty read-only sources the plan's mounts point at."""
    try:
        for kind, path in plan.mask_sources.items():
            if (kind == "file" and _valid_file(path)) or (kind == "dir" and _valid_dir(path)):
                continue
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            _discard(path)
            if kind == "file":
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444)
                os.close(fd)
                os.chmod(path, 0o444)
            else:
                os.mkdir(path, 0o555)
                os.chmod(path, 0o555)
            ok = _valid_file(path) if kind == "file" else _valid_dir(path)
            if not ok:
                raise MaskError(f"mask source {path} is not an empty read-only {kind} after creation")
    except OSError as e:
        raise MaskError(f"cannot prepare mask sources: {e}") from e


def check_originals_exist(plan: SessionPlan) -> None:
    """Docker would create a missing mount point inside the Workspace; refuse instead."""
    ws = Path(plan.workspace)
    for e in plan.entries:
        if e.status != "MASKED":
            continue
        try:
            st = os.lstat(ws / e.path)
        except OSError as err:
            raise MaskError(f"cannot mask {e.path}: {err.strerror or err}") from err
        want_dir = e.kind == "dir"
        if stat.S_ISLNK(st.st_mode) or stat.S_ISDIR(st.st_mode) != want_dir:
            raise MaskError(f"cannot mask {e.path}: it changed since the plan was made; run `asb up` again")


def verify_session_masks(plan: SessionPlan, mounts_json: str) -> None:
    """Check the running Session really has every mask mounted read-only from the empty source."""
    try:
        mounts = json.loads(mounts_json)
    except ValueError as err:
        raise MaskError(f"cannot read the Session's mounts: {err}") from err
    if not isinstance(mounts, list):
        raise MaskError("cannot read the Session's mounts: unexpected format")
    by_dest = {m.get("Destination"): m for m in mounts if isinstance(m, dict)}
    for e in plan.entries:
        if e.status != "MASKED":
            continue
        dest = "/workspace/" + e.path
        m = by_dest.get(dest)
        if m is None:
            raise MaskError(f"{e.path} is not masked in the Session (no mount at {dest})")
        if m.get("RW") is not False:
            raise MaskError(f"mask of {e.path} is writable in the Session")
        want = plan.mask_sources[e.kind]
        got = m.get("Source", "")
        if got != want and os.path.realpath(got) != os.path.realpath(want):
            raise MaskError(f"mask of {e.path} comes from {got!r}, expected {want!r}")
