"""Executes plans with the `docker` CLI. Argument lists only, never shell strings (ADR-0011)."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .plan import IMAGE, SessionPlan


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

    def run(self, *args: str, check: bool = True, interactive: bool = False) -> Result:
        argv = [*self.command, *args]
        try:
            if interactive:
                return Result(subprocess.call(argv), "", "")
            p = subprocess.run(argv, capture_output=True, text=True)
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

    def start(self, plan: SessionPlan) -> None:
        """Start the Session. Fails closed: a partial start is removed."""
        self.require_daemon()
        if not self.has_image(plan.image):
            raise DockerError(f"image {plan.image} not found; run `asb build` first")
        # Volumes first, so a failure leaves no half-started Session. Never removed by the CLI.
        for volume in plan.volumes:
            self.run("volume", "create", "--label", "agent-sandbox=volume", volume)
        try:
            self.run(*plan.session_run)
        except DockerError:
            self.run("rm", "-f", plan.container_name, check=False)
            raise

    def stop(self, name: str) -> None:
        self.run("rm", "-f", name)

    def attach(self, name: str) -> int:
        return self.run("exec", "-it", "-w", "/workspace", name, "bash", interactive=True).code

    def build(self, context: Path, image: str = IMAGE) -> None:
        self.require_daemon()
        self.run("build", "-t", image, str(context))
