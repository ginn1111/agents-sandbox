# Spike: masking aliases (symlinks, hard links) and attach warning (ticket #4)

**Status: UNVERIFIED.** Written on a host with no Docker daemon; nothing here has been run. Run the steps on Linux
Docker and on Colima, record results, then update this note. The implementation fails closed whichever way each
question below turns out.

## Question 1: does Docker resolve a symlinked mount destination to the real file?

ADR-0004 assumes it does. We do not rely on it: discovery resolves a Secret-named symlink itself
(`os.path.realpath`, inside the Workspace only) and mounts the empty file at the **real** Workspace-relative path.
A symlink is therefore never a mount destination, and the symlink inside the Session reads the masked real file.

Two situations could still put a symlink on a mount destination path:

1. A directory **on the way** to a masked path is a symlink. Discovery never descends into symlinked directories and
   masks only real paths, so this can only arise if the Workspace changes between `check` and `up`. `up` now
   re-checks every parent directory of every masked path with `lstat` and refuses with a clear message
   (`masks._refuse_symlinked_parents`), before any container is created.
2. The masked path itself is a symlink at `up` time. Already refused by `check_originals_exist` ("changed since the plan").

A TOCTOU window remains between that check and `docker run` (another host process could swap a path). The
post-start `docker inspect` mount verification (ticket #3) catches a missing or wrong-source mount; it cannot prove
the destination resolved to the intended file. Recorded as a known limit.

### Steps to verify

```bash
tmp=$(mktemp -d) && cd "$tmp" && echo SECRET > real && ln -s real .env
: > /tmp/empty && chmod 444 /tmp/empty
# (a) mount onto the symlink itself: what does Docker do?
docker run --rm --mount type=bind,source="$tmp",target=/workspace \
  --mount type=bind,source=/tmp/empty,target=/workspace/.env,readonly debian:bookworm-slim \
  sh -c 'ls -l /workspace; cat /workspace/real; cat /workspace/.env'
# (b) mount onto the real target (what we do): both paths must read empty
docker run --rm --mount type=bind,source="$tmp",target=/workspace \
  --mount type=bind,source=/tmp/empty,target=/workspace/real,readonly debian:bookworm-slim \
  sh -c 'wc -c < /workspace/real; wc -c < /workspace/.env'
# (c) a directory symlink on the way: does the mount land in the symlink's target?
mkdir d2 && ln -s d2 d1 && echo S > d2/f
# same docker run as (b) with target=/workspace/d1/f ; then `cat d2/f` on the host must be unchanged
```

Record: (a) whether Docker replaces the symlink, follows it, or errors; (c) whether it follows, and whether a
missing destination under a symlink makes Docker create files in the Workspace. Repeat on Colima/virtiofs.

## Question 2: hard links

Each hard link is a separate directory entry, so each gets its own mount of the empty file (same mechanism as any
Secret file). Verify with `ln .env notes.txt`: both read empty in the Session and the host inode/nlink/mtime are
unchanged. Seam 2 covers this (`test_content_is_unreadable_through_every_alias`).

Known limit (unchanged, SPEC section 11): hard links under Skip patterns or outside the Workspace are not found.

## Question 3: attach warning

`asb attach` on an already running Session re-plans the Workspace and compares every path the plan would mask with
the Session's real mounts (`docker inspect --format '{{json .Mounts}}'`). Paths with no mask mount are listed on
stderr; the shell still opens. If `inspect` fails or returns garbage it warns that the check could not run, rather
than staying silent. When `attach` itself starts the Session, nothing is checked (masks were just verified).

Not detected: files under Skip patterns (not masked by design), and a file created and deleted between two attaches.
Verify with `tests/test_e2e_docker.py::test_attach_warns_about_a_secret_file_created_after_up`.
