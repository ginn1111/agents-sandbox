"""A stand-in `docker` for Portal tests. It answers the network, port, iptables and in-Session
`exec` commands the Portal controller uses and hands everything else to fake_docker.py (same JSON
state file, so containers started by `asb up` are visible here).

Knobs, set through PortalDocker.set(): publish_on (the host address Docker reports for a published
port), iptables_fails, process_dies, portal_listening.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).with_name("fake_docker.py")
args = sys.argv[1:]
path = os.environ["FAKE_DOCKER_STATE"]
cmd = args[0] if args else ""
try:
    s = json.load(open(path))
except FileNotFoundError:
    s = {"calls": [], "containers": {}, "images": [], "daemon": True}


def opt(flag):
    return [args[i + 1] for i, a in enumerate(args) if a == flag and i + 1 < len(args)]


def finish(code=0, err=None):
    json.dump(s, open(path, "w"))
    if err:
        print(err, file=sys.stderr)
    sys.exit(code)


nets = s.setdefault("networks", {})
rules = s.setdefault("iptables", {})
procs = s.setdefault("procs", {})
fmt = opt("--format")
ours = (
    cmd in ("network", "port")
    or (cmd == "ps" and fmt and "agent-sandbox.portal" in fmt[0])
    or (cmd == "inspect" and fmt and "NetworkSettings" in fmt[0])
    or (cmd == "exec" and "iptables" in args)
    or (cmd == "exec" and "kanban" in args)
    or (cmd == "exec" and ("sh" in args or "bash" in args) and "-c" in args)
)
if not ours:
    json.dump(s, open(path, "w"))
    sys.exit(subprocess.call([sys.executable, str(HERE), *args]))

s["calls"].append(args)


def ip_of(net, ctr):
    return f"172.30.{list(nets).index(net) + 1}.{2 + nets[net]['members'].index(ctr)}"


if cmd == "ps":
    want = [f.split("=", 2)[2] for f in opt("--filter") if f.startswith("label=agent-sandbox.session=")]
    for name, c in s["containers"].items():
        lab = c["labels"]
        if c.get("exited") or lab.get("agent-sandbox") != "portal":
            continue
        if want and lab.get("agent-sandbox.session") != want[0]:
            continue
        print(f"{name}\t{lab.get('agent-sandbox.portal', '')}\t{lab.get('agent-sandbox.open', '')}")
elif cmd == "network":
    sub = args[1]
    if sub == "create":
        if args[-1] in nets:
            finish(1, f"network {args[-1]} already exists")
        nets[args[-1]] = {"members": [], "labels": opt("--label"), "internal": "--internal" in args}
    elif sub == "connect":
        net, ctr = args[-2], args[-1]
        if net not in nets or ctr not in s["containers"]:
            finish(1, "no such network or container")
        nets[net]["members"].append(ctr)
    elif sub == "disconnect":
        net, ctr = args[-2], args[-1]
        if ctr in nets.get(net, {}).get("members", []):
            nets[net]["members"].remove(ctr)
    elif sub == "rm":
        if s.get("network_rm_fails"):
            finish(1, "network removal failed")
        n = args[-1]
        if n not in nets:
            finish(1, f"no such network {n}")
        if any(m in s["containers"] for m in nets[n]["members"]):
            finish(1, f"network {n} has active endpoints")
        del nets[n]
    elif sub == "inspect":
        if args[-1] not in nets:
            finish(1, f"no such network {args[-1]}")
        print(f"172.30.{list(nets).index(args[-1]) + 1}.0/24")
    elif sub == "ls":
        want = [f.split("=", 2)[2] for f in opt("--filter") if f.startswith("label=agent-sandbox.session=")]
        exact = [f[6:-1] for f in opt("--filter") if f.startswith("name=^")]
        for n, v in nets.items():
            if (not exact or n in exact) and (not want or f"agent-sandbox.session={want[0]}" in v["labels"]):
                print(n)
elif cmd == "inspect":
    ctr = args[-1]
    if ctr not in s["containers"]:
        finish(1, "no such container")
    print(json.dumps({n: {"IPAddress": ip_of(n, ctr)} for n, v in nets.items() if ctr in v["members"]}))
elif cmd == "port":
    c = s["containers"].get(args[1])
    if c is None or c.get("exited") or "--publish" not in c["args"]:
        finish(1, "no public port")
    if "host_port" not in c:
        s["port_counter"] = s.get("port_counter", 49152) + 1
        c["host_port"] = s["port_counter"]
    bind = s.get("publish_on") or c["args"][c["args"].index("--publish") + 1].split(":")[0]
    print(f"{bind}:{c['host_port']}")
elif "kanban" in args:  # `exec <session> hermes kanban boards create <slug> --name <name>`
    if s.get("kanban_fails"):
        finish(1, "hermes: kanban failed")
elif "iptables" in args:
    if s.get("iptables_fails") and "-A" in args:
        finish(4, "iptables: Failed to apply the rule")
    rest = args[args.index("iptables") + 1:]
    op = next(a for a in rest if a in ("-A", "-C", "-D", "-S"))
    chain = rest[rest.index(op) + 1]
    cur = rules.setdefault(f"{args[1]}/{chain}", [])
    if op == "-S":
        for spec in cur:
            print(f"-A {chain} {spec}")
        finish(0)
    spec = " ".join(rest[rest.index(op) + 2:])
    if op == "-A":
        cur.append(spec)
    elif spec not in cur:
        finish(1, "iptables: Bad rule (does a matching rule exist in that chain?)")
    elif op == "-D":
        cur.remove(spec)
else:  # `exec [-d] [-e K=V ...] <session> sh -c <script> <$0> [<$@>...]`
    pos = args.index("-c")
    ctr = next(a for a in args[1:pos] if a in s["containers"])
    script, rest = args[pos + 1], args[pos + 2:]
    p = procs.setdefault(ctr, {})
    if "-d" in args:
        if s.get("process_dies"):
            finish(1, "exec: command not found")
        # a bare `-e NAME` takes its value from docker's own environment: that is how secrets stay off the command line
        env = [e if "=" in e else f"{e}={os.environ.get(e, '')}" for e in opt("-e")]
        p[rest[0]] = {"env": env, "argv": rest[1:], "alive": True}
    elif "/dev/tcp/" in script:
        finish(0 if s.get("portal_listening", True) else 1)
    elif "kill -0" in script:
        finish(0 if p.get(rest[0], {}).get("alive") else 1)
    elif "kill " in script:
        if rest[0] in p:
            p[rest[0]]["alive"] = False
finish()
