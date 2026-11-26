"""The Session plan: a pure function from (workspace, config) to a SessionPlan.

No Docker calls and no writes happen here. ``asb check`` renders the plan,
``asb check --json`` serialises it and the runtime executes it.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import Config

IMAGE = "agent-sandbox-agent:latest"
FALLBACK_UID = 1000

# Agent volumes (ADR-0006): one per agent, named after the agent only, so the same
# volume is reused by every Workspace and never by another agent.
AGENT_HOME_ROOT = "/agents"
MISE_VOLUME = "agent-sandbox-mise"
MISE_DIR = "/mise"
# Agents the image ships and pre-creates a writable home for (see images/agent/Dockerfile).
IMAGE_AGENTS = frozenset({"hermes", "omp", "claude", "codex"})


def agent_volume(name: str) -> str:
    return f"agent-sandbox-{name}"


def agent_home(name: str) -> str:
    return f"{AGENT_HOME_ROOT}/{name}"

# SPEC section 11. Shown by `check` and kept in sync with the README.
LIMITS = [
    "Masking is by filename only; secrets inside other files are not detected.",
    "Secrets committed to git history stay readable.",
    "Files created after `up`, or under Skip patterns, are not masked.",
    "The agent can change Workspace files that later run on the host (Makefile, package.json, .git/hooks).",
    "Host Services can return sensitive data; the Host Browser, when enabled, exposes the host's whole network view and host files.",
    "Containers share the host kernel.",
    "Agent credentials in Agent volumes are readable by the agent and by anything it runs.",
    "The inner herdr can set the terminal title and request clipboard writes (OSC 52); turn off clipboard writes from programs in your terminal.",
]


@dataclass
class SessionPlan:
    workspace: str
    container_name: str
    image: str
    config_files: list[str]
    project_rules: list[tuple[str, str]]
    session_run: list[str]
    warnings: list[str] = field(default_factory=list)
    # One entry per enabled agent: name, command, env, volume, home and `launch`,
    # the argv to run inside the Session (`docker exec <session> <launch...>`).
    agents: list[dict] = field(default_factory=list)
    # Named volumes the Session mounts; `up` creates them and `down` never removes them.
    volumes: list[str] = field(default_factory=list)
    limits: list[str] = field(default_factory=lambda: list(LIMITS))
    result: str = "Ready"

    def to_dict(self) -> dict:
        return asdict(self)


def session_name(workspace: Path) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", workspace.name.lower()).strip("-") or "workspace"
    digest = hashlib.sha256(str(workspace).encode()).hexdigest()[:8]
    return f"asb-{slug[:30]}-{digest}"


def _mount_arg(source: Path, target: str) -> str:
    quoted = str(source).replace('"', '""')
    return f'type=bind,"source={quoted}",target={target}'


def _volume_mount_arg(volume: str, target: str) -> str:
    return f"type=volume,source={volume},target={target}"


def _agent_entry(name: str, command: list[str], env: dict[str, str]) -> dict:
    home = agent_home(name)
    # `env` sets only what the config names: nothing is inherited from the host.
    launch = ["env", f"HOME={home}", *(f"{k}={v}" for k, v in sorted(env.items())), *command]
    return {
        "name": name, "command": list(command), "env": dict(env),
        "volume": agent_volume(name), "home": home, "launch": launch,
    }


def _external_git_warnings(workspace: Path) -> list[str]:
    git = workspace / ".git"
    if not git.is_file():
        return []
    try:
        first = git.read_text(errors="replace").splitlines()[0]
    except (OSError, IndexError):
        first = ""
    if first.startswith("gitdir:"):
        target = (workspace / first.split(":", 1)[1].strip()).resolve()
        if workspace not in (target, *target.parents):
            return [
                f".git points to {target}, outside the Workspace; it is not mounted, "
                "so git will not work inside the Session"
            ]
    return []


def plan(workspace: Path, config: Config, *, uid: int | None = None, gid: int | None = None) -> SessionPlan:
    workspace = Path(workspace).resolve()
    uid = os.getuid() if uid is None else uid
    gid = os.getgid() if gid is None else gid
    if uid == 0:  # never run the agent as root, even when the host user is root
        uid, gid = FALLBACK_UID, FALLBACK_UID
    name = session_name(workspace)

    agents = [_agent_entry(n, config.agents[n], config.agent_env.get(n, {})) for n in sorted(config.agents)]
    volumes = sorted({a["volume"] for a in agents} | {MISE_VOLUME})
    volume_mounts: list[str] = []
    for a in agents:
        volume_mounts += ["--mount", _volume_mount_arg(a["volume"], a["home"])]
    volume_mounts += ["--mount", _volume_mount_arg(MISE_VOLUME, MISE_DIR)]

    args = [
        "run", "-d",
        "--name", name,
        "--hostname", "asb",
        "--label", "agent-sandbox=session",
        "--label", f"agent-sandbox.workspace={workspace}",
        "--read-only",
        "--tmpfs", "/tmp", "--tmpfs", "/run",
        "--user", f"{uid}:{gid}",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--cpus", str(config.resources["cpus"]),
        "--memory", str(config.resources["memory"]),
        "--pids-limit", str(config.resources["pids_limit"]),
        "--network", "none",
        "--env", "HOME=/tmp",
        "--mount", _mount_arg(workspace, "/workspace"),
        *volume_mounts,
        "-w", "/workspace",
        IMAGE,
    ]

    warnings = [
        "No Netguard yet: the Session has no network at all (fail closed).",
        "Secret files are not masked yet: every file in the Workspace, including .env and "
        ".git/config, is readable and writable by the agent.",
    ]
    warnings += _external_git_warnings(workspace)
    for a in agents:
        if a["name"] not in IMAGE_AGENTS:
            warnings.append(
                f"agent '{a['name']}' is not installed in the image, and Docker creates its volume "
                f"{a['volume']} root-owned, so {a['home']} may not be writable; add the agent to the "
                "image Dockerfile (ADR-0007)"
            )

    return SessionPlan(
        workspace=str(workspace),
        container_name=name,
        image=IMAGE,
        config_files=list(config.loaded_files),
        project_rules=list(config.project_rules),
        session_run=args,
        warnings=warnings,
        agents=agents,
        volumes=volumes,
    )
