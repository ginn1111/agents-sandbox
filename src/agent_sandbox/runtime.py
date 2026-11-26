"""Executes plans with the `docker` CLI. Argument lists only, never shell strings (ADR-0011)."""
from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .netguard import READY_FILE
from .masks import MaskError, check_originals_exist, prepare_mask_sources, verify_session_masks
from .plan import IMAGE, SessionPlan


READY_TIMEOUT = 20.0  # seconds Netguard gets to apply and verify its rules
READY_POLL = 0.2


class DockerError(RuntimeError):
    """Docker is missing, unreachable, or refused a command."""


@dataclass
class Result:
    code: int
    out: str
    err: str


class Docker:
    def __init__(self, command: list[str] | None = None):
        self.command = list(command) if command else ["docker"]

    def run(self, *args: str, check: bool = True, interactive: bool = False,
            env: dict[str, str] | None = None) -> Result:
        """`env` is added to the docker client's own environment, for `docker exec -e NAME` (a bare
        name takes its value from there), so a secret never appears on a command line."""
        argv = [*self.command, *args]
        client_env = {**os.environ, **env} if env else None
        try:
            if interactive:
                return Result(subprocess.call(argv), "", "")
            p = subprocess.run(argv, capture_output=True, text=True, env=client_env)
        except FileNotFoundError as e:
            raise DockerError(f"docker not found ({self.command[0]}); install Docker, Colima or Docker Desktop") from e
        if check and p.returncode != 0:
            raise DockerError(p.stderr.strip() or f"docker {args[0]} failed with exit code {p.returncode}")
        return Result(p.returncode, p.stdout, p.stderr)

    # -- checks
    def require_daemon(self) -> None:
        r = self.run("info", check=False)
        if r.code != 0:
            raise DockerError("cannot reach the Docker daemon; is Docker (or Colima) running?")

    def has_image(self, image: str = IMAGE) -> bool:
        return self.run("image", "inspect", image, check=False).code == 0

    # -- sessions
    def running_sessions(self, name: str | None = None) -> list[tuple[str, str]]:
        filters = ["--filter", "label=agent-sandbox=session"]
        if name:
            filters += ["--filter", f"name=^{name}$"]
        r = self.run("ps", *filters, "--format", '{{.Names}}\t{{.Label "agent-sandbox.workspace"}}')
        rows = []
        for line in r.out.splitlines():
            n, _, ws = line.partition("\t")
            if n:
                rows.append((n, ws))
        return rows

    def is_running(self, name: str) -> bool:
        return bool(self.running_sessions(name))

    def container_running(self, name: str) -> bool:
        r = self.run("ps", "--filter", f"name=^{name}$", "--format", "{{.Names}}")
        return name in r.out.split()

    def _wait_netguard_ready(self, name: str) -> None:
        """Block until Netguard has applied and verified its rules. Fail closed on exit or timeout."""
        deadline = time.monotonic() + READY_TIMEOUT
        while True:
            if self.run("exec", name, "test", "-f", READY_FILE, check=False).code == 0:
                return
            state = self.run("inspect", "--format", "{{.State.Running}}", name, check=False)
            if state.code != 0 or state.out.strip() != "true":
                log = self.run("logs", "--tail", "20", name, check=False)
                detail = (log.err or log.out).strip()
                raise DockerError(f"Netguard {name} exited before it was ready" + (f": {detail}" if detail else ""))
            if time.monotonic() >= deadline:
                raise DockerError(f"Netguard {name} was not ready after {READY_TIMEOUT:g}s; the Session was not started")
            time.sleep(READY_POLL)

    def start(self, plan: SessionPlan) -> None:
        """Start Netguard, wait until it is ready, then the Session. Fails closed: a partial start is removed.

        The Session joins Netguard's network, so it never runs without Netguard's rules in place.
        A blocked plan, a missing image or a missing Secret file stops everything before anything starts.
        """
        if plan.errors:
            raise MaskError("; ".join(plan.errors))
        self.require_daemon()
        if plan.netguard_run:
            daemon_os = self.run("version", "--format", "{{.Server.Os}}", check=False)
            if daemon_os.code != 0 or daemon_os.out.strip() != "linux":
                raise DockerError("Netguard requires a Linux Docker daemon; use a Linux Docker context or --offline")
        wanted = [plan.image] + ([plan.netguard_image] if plan.netguard_run else [])
        for image in wanted:
            if not self.has_image(image):
                raise DockerError(f"image {image} not found; run `asb build` first")
        check_originals_exist(plan)
        prepare_mask_sources(plan)
        # Volumes before anything starts, so a failure leaves no half-started Session. Never removed by the CLI.
        for volume in plan.volumes:
            self.run("volume", "create", "--label", "agent-sandbox=volume", volume)
        try:
            if plan.netguard_run:
                self.run("rm", "-f", plan.netguard_name, check=False)  # a leftover from a crashed run
                self.run(*plan.netguard_run)
                self._wait_netguard_ready(plan.netguard_name)
            self.run("rm", "-f", plan.container_name, check=False)
            self.run(*plan.session_run)
            check_originals_exist(plan)
            if any(e.status == "MASKED" for e in plan.entries):
                r = self.run("inspect", "--format", "{{json .Mounts}}", plan.container_name)
                verify_session_masks(plan, r.out)
        except (DockerError, MaskError):
            self.stop(plan)
            raise

    def stop(self, plan: SessionPlan) -> None:
        """Remove the Session first, then its Netguard."""
        self.run("rm", "-f", plan.container_name, check=False)
        if plan.netguard_name:
            self.run("rm", "-f", plan.netguard_name, check=False)

    def attach(self, name: str) -> int:
        """Open the Session's own herdr (ADR-0009). No host herdr socket or environment is involved."""
        return self.run("exec", "-it", "-w", "/workspace", name, "herdr", interactive=True).code

    def build(self, context: Path, image: str = IMAGE) -> None:
        self.require_daemon()
        self.run("build", "-t", image, str(context))
