"""CLI glue for Portals: `asb portal start|stop|restart|status`, and the lines `up`, `ls` and `check` print."""
from __future__ import annotations

from typing import TextIO

from .plan import SessionPlan
from .portal_runtime import PortalController, active_urls, render_status
from .runtime import Docker

ACTIONS = ("start", "stop", "restart", "status")


def add_parser(sub) -> None:
    s = sub.add_parser("portal", help="start, stop, restart or show one Portal of the running Session")
    s.add_argument("action", choices=ACTIONS)
    s.add_argument("name", metavar="NAME")
    s.add_argument("dir", nargs="?", default=None, metavar="DIR")


def run_portal(args, docker: Docker, plan: SessionPlan, out: TextIO) -> int:
    ctl = PortalController(docker, plan)
    if args.action in ("start", "restart"):
        url = getattr(ctl, args.action)(args.name)
        entry = next(e for e in plan.portals if e["name"] == args.name)
        print(f"{args.name} → {url}", file=out)
        print("  Open it in your browser; asb never opens one. It answers on 127.0.0.1 only.", file=out)
        login = ctl.login(entry)
        if login:
            print(f"  Login:    username: {login['username']}  password: {login['password']}", file=out)
            print(f"  Generated once for this Workspace and kept in {login['store']} (outside the Workspace).", file=out)
        if entry["started_by"] == "agent":
            print("  The agent starts this service itself; asb only publishes the port.", file=out)
    elif args.action == "stop":
        was = ctl.stop(args.name)
        print(f"Portal {args.name} stopped" if was else f"Portal {args.name} not published", file=out)
    else:
        print(render_status(ctl.status(args.name)), file=out)
    return 0


def print_available(plan: SessionPlan, out: TextIO) -> None:
    """What `up` says about Portals: configured, not started (ADR-0013)."""
    if not plan.portals:
        return
    names = ", ".join(e["name"] for e in plan.portals)
    print(f"Portals available, none started: {names}; run `asb portal start <name>`", file=out)


def print_active(docker: Docker, session: str, out: TextIO) -> None:
    for name, url in active_urls(docker, session):
        print(f"  {name} → {url}", file=out)
