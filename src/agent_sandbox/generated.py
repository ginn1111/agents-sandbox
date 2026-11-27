"""`{generated}` Portal values (ADR-0010, ADR-0020).

A Portal env value of exactly ``{generated}`` is a random secret the CLI creates once per Workspace
and Portal, and keeps in its own state directory, outside the Workspace: project config and the repo
never hold it. The file is mode 0600 in a 0700 directory. A store that is unreadable, corrupt or
readable by others is refused rather than silently replaced, because replacing it would log the user
out and hide that something else could read the file.
"""
from __future__ import annotations

import json
import os
import secrets
import stat
from pathlib import Path

from .portals import GENERATED as MARKER, PortalError


def state_root() -> Path:
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "agent-sandbox" / "portal-secrets"


def store_path(session: str) -> Path:
    """One file per Session name, which already encodes the Workspace path."""
    return state_root() / f"{session}.json"


def names(env: dict[str, str]) -> list[str]:
    """The variables of a Portal whose value is generated."""
    return [k for k, v in env.items() if v == MARKER]


def _token() -> str:
    return secrets.token_urlsafe(32)  # 256 bits, URL-safe, no characters a shell or YAML would trip on


def _inside(path: Path, workspace: Path) -> bool:
    real, ws = Path(os.path.realpath(path)), Path(os.path.realpath(workspace))
    return real == ws or ws in real.parents


def _load(path: Path) -> dict:
    mode = stat.S_IMODE(path.lstat().st_mode)
    if mode & 0o077:
        raise PortalError(
            f"{path} has permissions {mode:o}: other users could read the Portal logins; "
            "run `chmod 600` on it or delete it to generate new ones")
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise PortalError(f"{path} is not valid: {e}; delete it to generate new Portal logins") from e
    if not isinstance(data, dict) or not all(isinstance(v, dict) for v in data.values()):
        raise PortalError(f"{path} is not valid: unexpected layout; delete it to generate new Portal logins")
    return data


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def resolve(session: str, workspace: Path, portal: str, env: dict[str, str]) -> dict[str, str]:
    """`env` with every ``{generated}`` replaced by its stored value, creating the missing ones."""
    wanted = names(env)
    if not wanted:
        return dict(env)
    path = store_path(session)
    if _inside(path.parent, workspace):
        raise PortalError(
            f"the Portal secret store ({path.parent}) is inside the Workspace; set XDG_STATE_HOME elsewhere")
    data = _load(path) if path.exists() else {}
    mine = data.setdefault(portal, {})
    missing = [k for k in wanted if not isinstance(mine.get(k), str) or not mine[k]]
    if missing:
        for k in missing:
            mine[k] = _token()
        _write(path, data)
    return {k: (mine[k] if v == MARKER else v) for k, v in env.items()}
