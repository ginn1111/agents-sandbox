"""herdr inside the Session (ADR-0009): the Watcher's integrations, installed in the Agent volumes.

herdr's own socket lives inside the Session. Nothing here mounts, relays or reads a host herdr.
"""
from __future__ import annotations

import sys

from .plan import SessionPlan
from .runtime import Docker

# Agent -> directory that must exist in the agent's HOME before `herdr integration install` writes
# into it ("the Hermes config directory must already exist"; OMP: its agent directory).
INTEGRATION_DIRS = {
    "hermes": ".hermes",
    "omp": ".omp/agent",
}

# What the user loses when an install fails, for the warning.
LOSS = {
    "hermes": "herdr will not report Hermes session ids, so a Hermes pane cannot be resumed after a herdr restart",
    "omp": "herdr will not show working/idle/blocked state for OMP panes or resume them",
}


def integration_steps(plan: SessionPlan) -> list[dict]:
    """One step per enabled agent with an official herdr integration: how to prepare and install it."""
    steps = []
    for a in plan.agents:
        sub = INTEGRATION_DIRS.get(a["name"])
        if sub is None:
            continue
        steps.append({
            "agent": a["name"],
            "prepare": ["mkdir", "-p", f"{a['home']}/{sub}"],
            # HOME is the agent's own Agent volume, so the files land where the agent reads them.
            "install": ["env", f"HOME={a['home']}", "herdr", "integration", "install", a["name"]],
        })
    return steps


def install_integrations(docker: Docker, plan: SessionPlan, err=None) -> list[str]:
    """Install the herdr integrations inside the running Session. Returns the agents that failed.

    A failure costs only that agent's herdr status, never a security control, so it is reported
    and the Session is kept.
    """
    err = sys.stderr if err is None else err
    failed = []
    for step in integration_steps(plan):
        name = step["agent"]
        for argv in (step["prepare"], step["install"]):
            r = docker.run("exec", plan.container_name, *argv, check=False)
            if r.code != 0:
                detail = (r.err or r.out).strip()
                print(f"warning: herdr integration for {name} was not installed: {LOSS[name]}"
                      + (f" ({detail})" if detail else ""), file=err)
                failed.append(name)
                break
    return failed
