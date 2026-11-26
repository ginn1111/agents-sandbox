"""Workspace resolution and validation (SPEC section 4)."""
from __future__ import annotations

from pathlib import Path


class WorkspaceError(ValueError):
    """The requested Workspace is unsafe or unusable."""


def resolve_workspace(path: str | Path | None, *, home: Path | None = None) -> Path:
    """Return the canonical absolute Workspace path, or raise WorkspaceError.

    Refuses the filesystem root, the home directory itself and any path above it.
    Symlinks are resolved first so a link cannot smuggle in a forbidden path.
    """
    home_dir = (home if home is not None else Path.home()).resolve()
    raw = Path.cwd() if path is None else Path(path).expanduser()
    if not raw.exists():
        raise WorkspaceError(f"Workspace {raw} does not exist")
    resolved = raw.resolve()
    if not resolved.is_dir():
        raise WorkspaceError(f"Workspace {resolved} is not a directory")
    if resolved == Path(resolved.anchor):
        raise WorkspaceError("refusing the filesystem root as a Workspace")
    if resolved == home_dir:
        raise WorkspaceError(f"refusing your home directory ({resolved}) as a Workspace")
    if resolved in home_dir.parents:
        raise WorkspaceError(f"refusing {resolved}: it is above your home directory")
    return resolved
