"""A stand-in `docker` for `asb update` tests. It models image tags and ids, `docker tag`, `docker rmi`,
`docker image ls`, `docker volume ls` and the one-off `docker run --rm` the integrations step uses, and
hands everything else to fake_docker.py (same JSON state file, so Sessions started by `asb up` are visible).

State keys it adds: image_ids (tag -> id), image_seq, volumes (also kept by fake_docker.py).
Knobs, set through UpdateDocker.set(): build_fails (True, or a list of repositories), rmi_fails (list of
references), tag_fails (list of references), fail_integration (list of agent names).
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


ids = s.setdefault("image_ids", {})
images = s.setdefault("images", [])


def id_of(tag):
    if tag not in ids:  # an image `has_image()` made without a build
        s["image_seq"] = s.get("image_seq", 0) + 1
        ids[tag] = f"sha256:{s['image_seq']:012d}"
    return ids[tag]


def repo_of(ref):
    return ref.rsplit(":", 1)[0]


sub = args[1] if len(args) > 1 else ""
ours = (
    cmd in ("build", "tag", "rmi")
    or (cmd == "image" and sub in ("ls", "inspect"))
    or (cmd == "volume" and sub == "ls")
    or (cmd == "run" and "--rm" in args)
)
if not ours:
    json.dump(s, open(path, "w"))
    sys.exit(subprocess.call([sys.executable, str(HERE), *args]))

s["calls"].append(args)

if cmd == "build":
    refs = opt("-t")
    fail = s.get("build_fails")
    if fail is True or (isinstance(fail, list) and any(repo_of(r) in fail for r in refs)):
        finish(1, "build failed: the package index could not be reached")
    s["image_seq"] = s.get("image_seq", 0) + 1
    new_id = f"sha256:{s['image_seq']:012d}"
    for r in refs:
        ids[r] = new_id
        if r not in images:
            images.append(r)
elif cmd == "image" and sub == "inspect":  # by tag, or by id for an image left without a tag
    if args[2] not in images and args[2] not in s.setdefault("dangling", []):
        finish(1, f"Error: No such image: {args[2]}")
elif cmd == "image":  # image ls <repo> --format '{{.Tag}}\t{{.ID}}'
    repo = args[2]
    for ref in list(images):
        if repo_of(ref) == repo:
            print(f"{ref.rsplit(':', 1)[1]}\t{id_of(ref)}")
elif cmd == "tag":  # the source is a tag or an image id
    src, dst = args[1], args[2]
    dangling = s.setdefault("dangling", [])
    if dst in s.get("tag_fails", []):
        finish(1, f"Error response from daemon: cannot tag {dst}")
    if src in images:
        new = id_of(src)
    elif src in ids.values() or src in dangling:
        new = src
    else:
        finish(1, f"Error response from daemon: No such image: {src}")
    old = ids.get(dst)
    ids[dst] = new
    if new in dangling:
        dangling.remove(new)
    if dst not in images:
        images.append(dst)
    if old and old != new and old not in ids.values():  # the tag moved away and left the image without one
        dangling.append(old)
elif cmd == "rmi":
    ref = args[-1]
    dangling = s.setdefault("dangling", [])
    if ref in s.get("rmi_fails", []):
        finish(1, f"Error response from daemon: conflict: unable to remove repository reference {ref}")
    if ref.startswith("sha256:"):  # by id: only an image without any tag can go (no -f in this CLI)
        if ref in ids.values():
            finish(1, f"Error response from daemon: conflict: unable to delete {ref} (must be forced)")
        if ref not in dangling:
            finish(1, f"Error response from daemon: No such image: {ref}")
        dangling.remove(ref)
        finish()
    if ref not in images:
        finish(1, f"Error response from daemon: No such image: {ref}")
    images.remove(ref)
    old = ids.pop(ref, None)  # the last tag of an image deletes it

elif cmd == "volume":  # volume ls --format {{.Name}}
    for v in s.get("volumes", []):
        print(v)
else:  # run --rm: the integrations step
    image_pos = max(i for i, a in enumerate(args) if a.startswith("agent-sandbox-agent:"))
    if args[image_pos] not in images:
        finish(125, f"Unable to find image '{args[image_pos]}' locally")
    if args[-1] in s.get("fail_integration", []):
        finish(1, f"herdr: integration install {args[-1]} failed")
finish()
