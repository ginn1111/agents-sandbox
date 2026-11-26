"""Command line entry point for `agent-sandbox` and `asb`."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import host_services
from .config import ConfigError, load_config
from .netguard import IMAGE as NETGUARD_IMAGE
from .masks import MaskError
from .plan import IMAGE, plan
from .render import render
from .runtime import Docker, DockerError
from .workspace import WorkspaceError, resolve_workspace

IMAGE_CONTEXT = Path(__file__).parent / "images" / "agent"
NETGUARD_CONTEXT = Path(__file__).parent / "images" / "netguard"


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
    d = sub.add_parser("doctor", help="check the local environment and whether each Host Service answers")
    d.add_argument("dir", nargs="?", default=None, metavar="DIR")
    sv = sub.add_parser("service", help="enable or disable one configured Host Service in the running Session")
    sv.add_argument("action", choices=("enable", "disable"))
    sv.add_argument("name")
    sv.add_argument("dir", nargs="?", default=None, metavar="DIR")
    sub.add_parser("build", help="build the agent image")
    return p


def _plan_for(args):
    ws = resolve_workspace(args.dir)
    cli = {"offline": True} if getattr(args, "offline", False) else None
    return plan(ws, load_config(ws, cli=cli))


def _ensure_up(docker: Docker, p, out) -> bool:
    if p.errors:  # blocked plan (e.g. a Secret file that cannot be masked): never touch Docker
        raise MaskError("; ".join(p.errors))
    if docker.is_running(p.container_name):
        if p.netguard_name and not docker.container_running(p.netguard_name):
            raise DockerError(
                f"Session {p.container_name} is running but its Netguard {p.netguard_name} has stopped, "
                "so the Session has no network; run `asb down` and then `asb up`")
        print(f"Session already running: {p.container_name} ({p.workspace})", file=out)
        return False
    docker.start(p)
    print(f"Session started: {p.container_name} ({p.workspace})", file=out)
    return True


def _doctor_services(docker: Docker, directory: str | None) -> None:
    """Report whether each configured Host Service answers. Needs a Workspace; skipped without one."""
    try:
        p = _plan_for(argparse.Namespace(dir=directory))
    except (WorkspaceError, ConfigError) as e:
        if directory is not None:  # an explicit DIR that cannot be used is worth saying
            print(f"FAIL Host Services: {e}")
        return
    for level, text in host_services.doctor_lines(docker, p):
        print(f"{level:<4} {text}")


def _service(docker: Docker, args) -> None:
    p = _plan_for(args)
    if args.action == "enable":
        e = host_services.enable(docker, p, args.name)
        print(f"Host Service enabled: {e['name']} {e['host']}:{e['port']} "
              f"(until `asb service disable {e['name']}` or `asb down`)")
    else:
        host_services.disable(docker, p, args.name)
        print(f"Host Service disabled: {args.name}")


def _doctor(docker: Docker, directory: str | None = None) -> int:
    ok = True
    v = sys.version_info
    py_ok = v >= (3, 11)
    print(f"{'ok  ' if py_ok else 'FAIL'} Python {v.major}.{v.minor}.{v.micro} (3.11+ required)")
    ok &= py_ok
    try:
        docker.run("version", "--format", "{{.Server.Version}}")
        docker.require_daemon()
        print("ok   Docker daemon reachable")
        for label, image in (("agent", IMAGE), ("Netguard", NETGUARD_IMAGE)):
            has_image = docker.has_image(image)
            print(f"{'ok  ' if has_image else 'FAIL'} {label} image {image}" + ("" if has_image else " missing; run `asb build`"))
            ok &= has_image
    except DockerError as e:
        print(f"FAIL Docker: {e}")
        ok = False
    _doctor_services(docker, directory)
    return 0 if ok else 1


def main(argv: list[str] | None = None, *, docker: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    d = Docker(docker)
    try:
        if args.command == "check":
            p = _plan_for(args)
            print(json.dumps(p.to_dict(), indent=2) if args.json else render(p))
            return 0 if p.result == "Ready" else 1
        elif args.command == "up":
            _ensure_up(d, _plan_for(args), sys.stdout)
        elif args.command == "attach":
            p = _plan_for(args)
            _ensure_up(d, p, sys.stdout)
            return d.attach(p.container_name)
        elif args.command == "down":
            p = _plan_for(args)
            was_running = d.is_running(p.container_name)
            d.stop(p)  # the Session, then its Netguard (also removes a leftover Netguard)
            print(f"Session removed: {p.container_name}" if was_running else f"Session not running: {p.container_name}")
        elif args.command == "ls":
            rows = d.running_sessions()
            if not rows:
                print("No running Sessions")
            for name, ws in rows:
                print(f"{name}\t{ws}")
        elif args.command == "doctor":
            return _doctor(d, args.dir)
        elif args.command == "service":
            _service(d, args)
        elif args.command == "build":
            d.build(IMAGE_CONTEXT)
            print(f"Built {IMAGE}")
            d.build(NETGUARD_CONTEXT, NETGUARD_IMAGE)
            print(f"Built {NETGUARD_IMAGE}")
    except (WorkspaceError, ConfigError, DockerError, MaskError, host_services.ServiceError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
