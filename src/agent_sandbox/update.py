"""`asb update` and `asb update --rollback` (ADR-0012).

Three steps, in order: try to upgrade the CLI, rebuild the agent and Netguard images without cache
and with fresh base image pulls, re-install the herdr integrations in existing Agent volumes that
support them. Running Sessions are never stopped; they are listed with how to restart them.

Image tags per repository (`agent-sandbox-agent`, `agent-sandbox-netguard`):

* ``latest``        the build new Sessions use;
* ``YYYY-MM-DD``    the build's date (``YYYY-MM-DD-2`` for a second build on the same day);
* ``previous``      the one build kept for `--rollback`.

A build is tagged by date first, and ``latest`` moves only after *both* images built, so a failed
build changes nothing. Dated tags that are neither ``latest`` nor ``previous`` are pruned. Nothing
here uses `docker rmi -f`, so an image a running Session still needs is kept and reported.

Host programs (uv, pipx, the upgraded asb) are run through an injected *runner* with an argument
list, never a shell string. Agent volumes are never created, removed or emptied: the integrations
step only runs `herdr integration install` in a throwaway container that mounts them.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Callable, TextIO

from . import herdr
from .netguard import IMAGE as NETGUARD_IMAGE
from .plan import FALLBACK_UID, IMAGE as AGENT_IMAGE, agent_home, agent_volume
from .runtime import Docker, DockerError, Result

PACKAGE = "agent-sandbox"
STEPS = ("cli", "image", "integrations")
IMAGES_DIR = Path(__file__).parent / "images"
# (repository, build context) of every image `update` rebuilds. The Portal publisher image is a
# pinned Alpine + socat helper that `asb build` makes; ADR-0012 names only these two.
BUILDS = (
    (AGENT_IMAGE.rsplit(":", 1)[0], IMAGES_DIR / "agent"),
    (NETGUARD_IMAGE.rsplit(":", 1)[0], IMAGES_DIR / "netguard"),
)
DATED = re.compile(r"^\d{4}-\d{2}-\d{2}(-\d+)?$")
RESTART_HINT = "restart to apply: asb down && asb up"

Runner = Callable[..., Result]


class UpdateError(RuntimeError):
    """An update step failed in a way the rest of the update cannot go on from."""


def today() -> str:
    return date.today().isoformat()


def default_runner(argv: list[str], passthrough: bool = False) -> Result:
    """Run a host program with an argument list. `passthrough` hands it our terminal."""
    try:
        if passthrough:
            return Result(subprocess.call(argv), "", "")
        p = subprocess.run(argv, capture_output=True, text=True)
    except FileNotFoundError:
        return Result(127, "", f"{argv[0]}: not found")
    return Result(p.returncode, p.stdout, p.stderr)


def find_cli(tool: str, runner: Runner) -> str | None:
    """Find the upgraded entry point inside the selected installer's environment, not on PATH."""
    query = ["uv", "tool", "dir"] if tool == "uv" else ["pipx", "environment", "--value", "PIPX_LOCAL_VENVS"]
    result = runner(query)
    root = Path(result.out.strip())
    if result.code != 0 or not root.is_absolute():
        return None
    bin_dir = root / PACKAGE / "bin"
    # Do not use the installer's shared bin directory: another install may own those links.
    return shutil.which(str(bin_dir / "asb")) or shutil.which(str(bin_dir / PACKAGE))


# -- the command line

def add_parser(sub) -> None:
    s = sub.add_parser("update", help="try to upgrade the CLI, rebuild the agent and Netguard images, re-install herdr integrations")
    s.add_argument("--only", choices=STEPS, help="run one step only")
    s.add_argument("--rollback", action="store_true", help="point `latest` back at the previous build")
    s.add_argument("--after-cli", action="store_true", help=argparse.SUPPRESS)  # the upgraded asb runs the rest


def check_args(parser: argparse.ArgumentParser, args) -> None:
    if args.rollback and (args.only or args.after_cli):
        parser.error("update: --rollback cannot be combined with --only or --after-cli")
    if args.after_cli and args.only:
        parser.error("update: --after-cli cannot be combined with --only")


# -- step 1: the CLI

def _installer(runner: Runner) -> str | None:
    listed = runner(["uv", "tool", "list"])
    if listed.code == 0 and any(line.split()[:1] == [PACKAGE] for line in listed.out.splitlines()):
        return "uv"
    listed = runner(["pipx", "list", "--short"])
    if listed.code == 0 and any(line.split()[:1] == [PACKAGE] for line in listed.out.splitlines()):
        return "pipx"
    return None


def upgrade_cli(tool: str | None, runner: Runner, out: TextIO, err: TextIO) -> str:
    """Upgrade the CLI itself. Returns "upgraded", "unmanaged" or "failed"."""
    if tool is None:
        print(f"warning: {PACKAGE} was not installed with `uv tool install` or `pipx install`, so it was not "
              "upgraded; upgrade it with the tool you installed it with", file=err)
        return "unmanaged"
    argv = ["uv", "tool", "upgrade", PACKAGE] if tool == "uv" else ["pipx", "upgrade", PACKAGE]
    r = runner(argv)
    if r.code != 0:
        detail = (r.err or r.out).strip()
        print(f"warning: `{' '.join(argv)}` failed" + (f": {detail}" if detail else ""), file=err)
        return "failed"
    print(f"CLI upgraded ({' '.join(argv)})", file=out)
    return "upgraded"


# -- step 2: the images

def _tags(docker: Docker, repo: str) -> dict[str, str]:
    """tag -> image id of every tag of the repository."""
    r = docker.run("image", "ls", repo, "--format", "{{.Tag}}\t{{.ID}}")
    tags = {}
    for line in r.out.splitlines():
        tag, _, image_id = line.partition("\t")
        if tag and image_id:
            tags[tag] = image_id
    return tags


def _new_tag(day: str, existing: dict[str, str]) -> str:
    """The date, or date-2, date-3 ... so a second build on one day never overwrites the first."""
    tag, n = day, 1
    while tag in existing:
        n += 1
        tag = f"{day}-{n}"
    return tag


def _tag(docker: Docker, source: str, target: str) -> None:
    r = docker.run("tag", source, target, check=False)
    if r.code != 0:
        raise DockerError(f"could not tag {target}: {(r.err or r.out).strip()}")


def _rmi(docker: Docker, ref: str, err: TextIO) -> bool:
    r = docker.run("rmi", ref, check=False)  # never forced: an image in use stays
    if r.code != 0:
        print(f"warning: {ref} could not be removed (in use by a running Session?); "
              f"it is retried on the next update: {(r.err or r.out).strip()}", file=err)
    return r.code == 0


def _quiet_rmi(docker: Docker, ref: str) -> None:
    docker.run("rmi", ref, check=False)


def update_images(docker: Docker, out: TextIO, err: TextIO) -> None:
    """Rebuild agent and Netguard without cache, then move `latest` after both builds succeed."""
    docker.require_daemon()
    day = today()
    before = {repo: _tags(docker, repo) for repo, _ in BUILDS}
    built: list[str] = []
    try:
        for repo, context in BUILDS:
            tag = _new_tag(day, before[repo])
            print(f"Building {repo}:{tag} (no cache, pull base images) ...", file=out)
            docker.run("build", "--no-cache", "--pull", "-t", f"{repo}:{tag}", str(context))
            built.append(f"{repo}:{tag}")
    except DockerError:
        for ref in built:  # `latest` was never moved
            _quiet_rmi(docker, ref)
        raise

    # Promote: previous <- latest, latest <- the new build. Undone in full if any tag fails.
    undo: list[tuple[str, str | None]] = []  # (ref, id it pointed at before, or None)
    try:
        for (repo, _), new_ref in zip(BUILDS, built):
            for ref in (f"{repo}:previous", f"{repo}:latest"):
                undo.append((ref, before[repo].get(ref.rsplit(":", 1)[1])))
            if "latest" in before[repo]:
                _tag(docker, before[repo]["latest"], f"{repo}:previous")
            _tag(docker, new_ref, f"{repo}:latest")
    except DockerError:
        for ref, old_id in reversed(undo):
            if old_id is not None:
                docker.run("tag", old_id, ref, check=False)
            else:
                _quiet_rmi(docker, ref)
        for ref in built:
            _quiet_rmi(docker, ref)
        raise

    for (repo, _), new_ref in zip(BUILDS, built):
        after = _tags(docker, repo)
        keep = {after[t] for t in ("latest", "previous") if t in after}
        for tag, image_id in sorted(after.items()):  # dated builds older than the previous one
            if DATED.match(tag) and image_id not in keep:
                _rmi(docker, f"{repo}:{tag}", err)
        old_previous = before[repo].get("previous")
        if old_previous and old_previous not in keep and old_previous not in _tags(docker, repo).values():
            _rmi(docker, old_previous, err)  # the build `previous` moved away from, if it has no tag left
        print(f"Built {new_ref}; {repo}:latest points at it", file=out)


def rollback_images(docker: Docker, out: TextIO, err: TextIO) -> None:
    """Point `latest` back at the previous build of every image, or change nothing."""
    docker.require_daemon()
    tags = {repo: _tags(docker, repo) for repo, _ in BUILDS}
    missing = [repo for repo, _ in BUILDS if "previous" not in tags[repo]]
    if missing:
        raise UpdateError(f"no previous build of {', '.join(missing)} to roll back to")
    undo: list[tuple[str, str | None]] = []
    try:
        for repo, _ in BUILDS:
            undo.append((f"{repo}:latest", tags[repo].get("latest")))
            _tag(docker, tags[repo]["previous"], f"{repo}:latest")
    except DockerError:
        for ref, old_id in reversed(undo):
            if old_id is not None:
                docker.run("tag", old_id, ref, check=False)
        raise
    for repo, _ in BUILDS:
        _quiet_rmi(docker, f"{repo}:previous")  # one step back only; the next update prunes the build rolled back from
        dated = sorted(t for t, i in tags[repo].items() if DATED.match(t) and i == tags[repo]["previous"])
        print(f"Rolled back {repo}:latest to {dated[-1] if dated else tags[repo]['previous']}", file=out)


# -- step 3: the herdr integrations

def _integration_volumes(docker: Docker) -> list[str]:
    have = set(docker.run("volume", "ls", "--format", "{{.Name}}").out.split())
    return [a for a in herdr.INTEGRATION_DIRS if agent_volume(a) in have]


def _integration_run(agent: str, uid: int, gid: int) -> list[str]:
    home = agent_home(agent)
    return [
        "run", "--rm",
        "--read-only", "--network", "none",
        "--user", f"{uid}:{gid}",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--tmpfs", "/tmp",
        "--memory", "512m", "--pids-limit", "128",
        "--env", f"HOME={home}",
        "--mount", f"type=volume,source={agent_volume(agent)},target={home}",
        AGENT_IMAGE,
        "sh", "-c", 'mkdir -p "$1" && herdr integration install "$2"', "sh",
        f"{home}/{herdr.INTEGRATION_DIRS[agent]}", agent,
    ]


def update_integrations(docker: Docker, out: TextIO, err: TextIO) -> list[str]:
    """Re-run `herdr integration install` for every existing Agent volume. Returns the agents that failed.

    A one-off container with no network, no capabilities and only that Agent volume mounted; the
    volume is never created, removed or reset, so logins and settings stay as they are.
    """
    docker.require_daemon()
    agents = _integration_volumes(docker)
    if not agents:
        print("No Agent volumes with a herdr integration yet; nothing to re-install", file=out)
        return []
    if not docker.has_image(AGENT_IMAGE):
        raise DockerError(f"image {AGENT_IMAGE} not found; run `asb build` first")
    uid, gid = os.getuid(), os.getgid()
    if uid == 0:  # the Session never runs the agent as root; match the files it wrote
        uid = gid = FALLBACK_UID
    failed = []
    for agent in agents:
        r = docker.run(*_integration_run(agent, uid, gid), check=False)
        if r.code != 0:
            detail = (r.err or r.out).strip()
            print(f"warning: herdr integration for {agent} was not re-installed in {agent_volume(agent)}: "
                  f"{herdr.LOSS[agent]}" + (f" ({detail})" if detail else ""), file=err)
            failed.append(agent)
        else:
            print(f"herdr integration re-installed for {agent} ({agent_volume(agent)})", file=out)
    return failed


# -- running Sessions

def list_running(docker: Docker, out: TextIO) -> None:
    try:
        rows = docker.running_sessions()
    except DockerError:
        return
    if not rows:
        return
    print(f"Running Sessions keep the image they started with and were not stopped; {RESTART_HINT}", file=out)
    for name, workspace in rows:
        print(f"  {name}\t{workspace}", file=out)


# -- the command

def run_update(args, docker: Docker, runner: Runner | None, out: TextIO | None = None,
               err: TextIO | None = None) -> int:
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    runner = runner or default_runner
    if args.rollback:
        rollback_images(docker, out, err)
        list_running(docker, out)
        return 0

    only = args.only
    steps = [only] if only else (["image", "integrations"] if args.after_cli else list(STEPS))
    code = 0
    if "cli" in steps:
        tool = _installer(runner)
        result = upgrade_cli(tool, runner, out, err)
        if result == "unmanaged" and only:
            return 1
        if result == "failed":
            code = 1
        if result == "upgraded" and tool is not None and not only:
            # The upgraded CLI ships the Dockerfiles for the remaining steps.
            new_cli = find_cli(tool, runner)
            if new_cli:
                return runner([new_cli, "update", "--after-cli"], passthrough=True).code
            print(f"warning: could not locate the upgraded CLI in the {tool} tool environment; "
                  "continuing with the current CLI", file=err)
    if "image" in steps:
        update_images(docker, out, err)  # a failed build raises: nothing after it runs
    if "integrations" in steps:
        if update_integrations(docker, out, err):
            code = 1
    if "image" in steps or "integrations" in steps:
        list_running(docker, out)
    return code
