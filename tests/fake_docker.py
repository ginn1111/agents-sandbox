"""A stand-in `docker` for tests: records every call, keeps container state in a JSON file.

Invoked as `python fake_docker.py <docker args...>` with FAKE_DOCKER_STATE pointing at the state file.
"""
import json
import os
import sys

state_path = os.environ["FAKE_DOCKER_STATE"]
try:
    state = json.load(open(state_path))
except FileNotFoundError:
    state = {"calls": [], "containers": {}, "images": [], "daemon": True}

args = sys.argv[1:]
state["calls"].append(args)


def save():
    json.dump(state, open(state_path, "w"))


def opt(flag):
    return [args[i + 1] for i, a in enumerate(args) if a == flag and i + 1 < len(args)]


cmd = args[0] if args else ""
if cmd == "version":
    print("27.0.0")
elif cmd == "info":
    if not state["daemon"]:
        print("Cannot connect to the Docker daemon", file=sys.stderr)
        save()
        sys.exit(1)
    print("ok")
elif cmd == "image" and args[1] == "inspect":
    if args[2] not in state["images"]:
        save()
        sys.exit(1)
elif cmd == "build":
    state["images"].append(opt("-t")[0])
elif cmd == "run":
    name = opt("--name")[0]
    labels = dict(l.split("=", 1) for l in opt("--label"))
    state["containers"][name] = {"labels": labels, "args": args}
    print("deadbeef")
elif cmd == "rm":
    state["containers"].pop(args[-1], None)
elif cmd == "ps":
    for name, c in state["containers"].items():
        ok = True
        for f in opt("--filter"):
            key, _, val = f.partition("=")
            if key == "label":
                lk, _, lv = val.partition("=")
                ok &= lk in c["labels"] and (not lv or c["labels"][lk] == lv)
            elif key == "name":
                ok &= val.strip("^$") == name
        if ok:
            print(f"{name}\t{c['labels'].get('agent-sandbox.workspace', '')}")
elif cmd == "volume" and args[1] == "create":
    if os.environ.get("FAKE_DOCKER_FAIL_VOLUME") == args[-1]:
        print(f"cannot create volume {args[-1]}", file=sys.stderr)
        save()
        sys.exit(1)
    state.setdefault("volumes", [])
    if args[-1] not in state["volumes"]:
        state["volumes"].append(args[-1])
    print(args[-1])
elif cmd == "exec":
    pass
save()
