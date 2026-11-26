"""Command line entry point for `agent-sandbox` and `asb`."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import ConfigError, load_config
from .plan import IMAGE, plan
from .render import render
from .runtime import Docker, DockerError
from .workspace import WorkspaceError, resolve_workspace

IMAGE_CONTEXT = Path(__file__).parent / "images" / "agent"


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="asb", description="Run AI coding agents confined to one Workspace.")
    sub = p.add_subparsers(dest="command", required=True)
    for name, help_ in (("up", "start the Session for a Workspace"),
                        ("attach", "open a shell in the Session (starts it if needed)"),
                        ("down", "stop and remove the Session"),
                        ("check", "show the plan without starting anything")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("dir", nargs="?", default=None, metavar="DIR")
        if name == "check":
            s.add_argument("--json", action="store_true", help="emit the plan as JSON")
        if name in ("up", "attach", "check"):
            s.add_argument("--offline", action="store_true", help="no network at all")
    sub.add_parser("ls", help="list running Sessions")
    sub.add_parser("doctor", help="check the local environment")
    sub.add_parser("build", help="build the agent image")
    return p


def _plan_for(args):
    ws = resolve_workspace(args.dir)
    cli = {"offline": True} if getattr(args, "offline", False) else None
    return plan(ws, load_config(ws, cli=cli))


def _ensure_up(docker: Docker, p, out) -> bool:
    if docker.is_running(p.container_name):
        print(f"Session already running: {p.container_name} ({p.workspace})", file=out)
        return False
    docker.start(p)
    print(f"Session started: {p.container_name} ({p.workspace})", file=out)
    return True


def _doctor(docker: Docker) -> int:
    ok = True
    v = sys.version_info
    py_ok = v >= (3, 11)
    print(f"{'ok  ' if py_ok else 'FAIL'} Python {v.major}.{v.minor}.{v.micro} (3.11+ required)")
    ok &= py_ok
    try:
        docker.run("version", "--format", "{{.Server.Version}}")
        docker.require_daemon()
        print("ok   Docker daemon reachable")
        has_image = docker.has_image()
        print(f"{'ok  ' if has_image else 'FAIL'} agent image {IMAGE}" + ("" if has_image else " missing; run `asb build`"))
        ok &= has_image
    except DockerError as e:
        print(f"FAIL Docker: {e}")
        ok = False
    return 0 if ok else 1


def main(argv: list[str] | None = None, *, docker: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    d = Docker(docker)
    try:
        if args.command == "check":
            p = _plan_for(args)
            print(json.dumps(p.to_dict(), indent=2) if args.json else render(p))
        elif args.command == "up":
            _ensure_up(d, _plan_for(args), sys.stdout)
        elif args.command == "attach":
            p = _plan_for(args)
            _ensure_up(d, p, sys.stdout)
            return d.attach(p.container_name)
        elif args.command == "down":
            p = _plan_for(args)
            if d.is_running(p.container_name):
                d.stop(p.container_name)
                print(f"Session removed: {p.container_name}")
            else:
                print(f"Session not running: {p.container_name}")
        elif args.command == "ls":
            rows = d.running_sessions()
            if not rows:
                print("No running Sessions")
            for name, ws in rows:
                print(f"{name}\t{ws}")
        elif args.command == "doctor":
            return _doctor(d)
        elif args.command == "build":
            d.build(IMAGE_CONTEXT)
            print(f"Built {IMAGE}")
    except (WorkspaceError, ConfigError, DockerError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
