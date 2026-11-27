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
    if "{{.Server.Os}}" in opt("--format"):
        print(state.get("server_os", "linux"))
    else:
        print(state.get("server_version", "28.0.0"))
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
    names = opt("--name")
    name = names[0] if names else None
    fail = state.get("fail_run")
    if fail is True or (name and name in (fail or [])):  # True: refuse every run; a list: refuse those names
        print("run refused: Error response from daemon: invalid mount config", file=sys.stderr)
        save()
        sys.exit(125)
    if name:
        labels = dict(l.split("=", 1) for l in opt("--label"))
        state["containers"][name] = {"labels": labels, "args": args}
        if name.endswith("-netguard") and state.get("netguard_exits"):
            state["containers"][name]["exited"] = True
    print("deadbeef")
elif cmd == "rm":
    state["containers"].pop(args[-1], None)
elif cmd == "inspect":
    name = args[-1]
    c = state["containers"].get(name)
    if c is None:
        save()
        sys.exit(1)
    if "{{.State.Running}}" in opt("--format"):  # Netguard readiness: is it still running?
        print("false" if c.get("exited") else "true")
    elif state.get("inspect_fails"):  # `docker inspect` of the mounts is broken (set after `up`)
        save()
        sys.exit(1)
    else:  # `{{json .Mounts}}`: what the mask check reads
        import csv
        mounts = []
        for v in [c["args"][n + 1] for n, a in enumerate(c["args"]) if a == "--mount"]:
            f = dict(x.partition("=")[::2] for x in next(csv.reader([v])))
            dest = f["target"]
            if dest in state.get("drop_mounts", []):
                continue
            src = "/wrong/source" if dest in state.get("wrong_source_mounts", []) else f["source"]
            rw = "readonly" not in f or dest in state.get("writable_mounts", [])
            mounts.append({"Type": f["type"], "Source": src, "Destination": dest, "RW": rw})
        print(json.dumps(mounts))
elif cmd == "logs":
    print("netguard: fake log line", file=sys.stderr)
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
        if c.get("exited") and "-a" not in args:
            ok = False
        if ok and "{{.Names}}" in opt("--format"):
            print(name)
        elif ok:
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
elif cmd == "exec" and "/usr/local/bin/asb-service" in args:
    # Host Service helper: `docker exec <netguard> asb-service enable <name> <host> <port> | disable <name> | list`
    i = args.index("/usr/local/bin/asb-service")
    action, rest = args[i + 1], args[i + 2:]
    open_ = state.setdefault("services", {}).setdefault(args[1], {})
    if state.get("service_fail") == action:
        print("asb-service: fake failure", file=sys.stderr)
        save()
        sys.exit(1)
    if action == "enable":
        open_[rest[0]] = f"{rest[1]}:{rest[2]}"
    elif action == "disable":
        open_.pop(rest[0], None)
    elif action == "list":
        print("\n".join(sorted(open_)))
elif cmd == "exec":
    # readiness probe: `docker exec <netguard> test -f /run/netguard/ready`
    c = state["containers"].get(args[1])
    if "integration" in args and args[-1] in state.get("fail_exec", []):
        print(f"integration install {args[-1]} failed", file=sys.stderr)
        save()
        sys.exit(1)
    if "test" in args and (c is None or c.get("exited") or not state.get("netguard_ready", True)):
        save()
        sys.exit(1)
save()
