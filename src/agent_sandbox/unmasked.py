"""Warn about Secret files that appeared after `up` (SPEC section 11: files created after `up` are not masked).

Masks are mount points fixed when the Session starts. On `attach` the Workspace is planned again and every path the
new plan would Mask is compared with the mounts the running Session actually has. Names and mounts only: no file is
read. Failing to inspect the Session is reported as a warning, never as silence.
"""
from __future__ import annotations

import json

from .plan import SessionPlan


def new_unmasked_paths(plan: SessionPlan, mounts_json: str) -> list[str]:
    """Workspace-relative paths the current plan would mask that the running Session has no mask mount for."""
    mounts = json.loads(mounts_json)
    if not isinstance(mounts, list):
        raise ValueError("unexpected format")
    dests = {m.get("Destination") for m in mounts if isinstance(m, dict)}
    return sorted(e.path for e in plan.entries if e.status == "MASKED" and "/workspace/" + e.path not in dests)


def attach_warnings(docker, plan: SessionPlan) -> list[str]:
    r = docker.run("inspect", "--format", "{{json .Mounts}}", plan.container_name, check=False)
    try:
        if r.code != 0:
            raise ValueError(r.err.strip() or "docker inspect failed")
        paths = new_unmasked_paths(plan, r.out)
    except ValueError as e:
        return [f"cannot check the running Session for new Secret files ({e}); files created after `up` are not masked"]
    if not paths:
        return []
    listed = ", ".join(paths)
    return [
        f"{len(paths)} file(s) matching Secret patterns appeared since `up` and are NOT masked in this Session: {listed}. "
        "Run `asb down` and `asb up` to mask them."
    ]
