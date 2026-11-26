# Spike: Secret masking by read-only bind mounts (ticket #3)

**Status: UNVERIFIED.** Written on a host with no Docker daemon. Nothing below has been run. Run the steps on Linux
Docker and on Colima, record the results here, and only then treat the assumptions as true. The implementation
is designed to fail closed if any of them is false (see "Fail-closed design").

## What the implementation assumes

1. `--mount type=bind,source=<empty file>,target=/workspace/<path>,readonly` placed **after** the `/workspace`
   mount overlays that one file, and the overlay is read-only and survives `rm`, `mv`, `truncate`, and writes by the
   non-root user (ADR-0004: "empty and read-only").
2. The same with an empty directory over a Secret directory.
3. Mounting over a path that exists in the Workspace does not create or modify anything on the host
   (the originals stay byte-identical, same inode, same mtime).
4. Docker resolves a mount destination through symlinks (ADR-0004). We avoid depending on this: discovery
   resolves symlinks itself and mounts at the **real** Workspace-relative path. The relative symlink then
   resolves to the masked file inside the Session. An absolute symlink target dangles inside the Session either way.
5. `docker inspect --format '{{json .Mounts}}'` reports every mask with `Destination`, `Source` and `RW: false`.
   `up` reads this after `docker run` and aborts if any mask is missing, writable or from the wrong source.
6. Local git (status, diff, branch, add, commit, log) works with `.git/config` read as an empty file
   (ADR-0004 says "expected to work, must be verified").
7. Colima/virtiofs and Docker Desktop honour nested single-file bind mounts the same way as Linux.

## Steps

```bash
tmp=$(mktemp -d) && cd "$tmp" && echo 'TOKEN=abc' > .env && ln -s .env link && mkdir .ssh && echo K > .ssh/id_rsa
stat -c '%i %Y %s' .env > before
mkdir -p ~/.local/state/agent-sandbox/masks && : > ~/.local/state/agent-sandbox/masks/empty-file
chmod 444 ~/.local/state/agent-sandbox/masks/empty-file
mkdir -p ~/.local/state/agent-sandbox/masks/empty-dir && chmod 555 ~/.local/state/agent-sandbox/masks/empty-dir
docker run --rm --read-only --cap-drop ALL --user "$(id -u):$(id -g)" \
  --mount type=bind,source="$tmp",target=/workspace \
  --mount type=bind,source=$HOME/.local/state/agent-sandbox/masks/empty-file,target=/workspace/.env,readonly \
  --mount type=bind,source=$HOME/.local/state/agent-sandbox/masks/empty-dir,target=/workspace/.ssh,readonly \
  debian:bookworm-slim sh -c '
    wc -c < /workspace/.env; ls -A /workspace/.ssh | wc -l
    echo x > /workspace/.env; rm /workspace/.env; mv /workspace/.env /workspace/e2
    touch /workspace/.ssh/n; rmdir /workspace/.ssh'
stat -c '%i %Y %s' .env | diff - before && cat .env
```

Expected: sizes `0` and `0`, every write/rm/mv/touch/rmdir fails (`Device or resource busy` / `Read-only file system`),
and the host `.env` is unchanged.

Also check:

- Hard-link alias: `ln .env notes.txt`, mount the empty file at both, `cat notes.txt` is empty.
- `docker inspect <ctr> --format '{{json .Mounts}}'` shows `RW: false` and the empty-file `Source` for each mask.
- Docker's behaviour when the mount **target does not exist** (it creates an empty file or directory in the
  Workspace on the host). `up` refuses before this (`check_originals_exist`), but record what Docker does.
- A symlinked destination: mount onto `/workspace/link` (a symlink to `.env`) and note whether Docker follows it. We never rely on it.
- Colima: repeat all of the above.
- `tests/test_e2e_docker.py::test_git_config_is_masked_and_local_git_still_works`.

## Fail-closed design

- `up` refuses (before touching Docker) when the plan has errors: an unreadable directory, a Secret symlink that
  resolves to the Workspace root, or mask sources that live inside the Workspace.
- Before `docker run`, `up` checks every masked path still exists with the expected type, so Docker cannot create a mount point in the Workspace.
- The empty sources are re-created if they are missing, non-empty, writable, symlinks or hard-linked. They live in
  `$XDG_STATE_HOME/agent-sandbox/masks` (default `~/.local/state/agent-sandbox/masks`), never under the
  Workspace, so the agent cannot fill the "empty" file through `/workspace`.
- After `docker run`, `up` inspects the Session's mounts. A missing, writable or wrong-source mask removes the
  Session (`docker rm -f`) and `up` exits non-zero.
- If an assumption above is false, the expected result is `up` failing, not a Session that starts with an exposed Secret file.

## Known gaps (not covered by ticket #3)

- Files created after `up` are not masked (stated by `check`). The `attach` warning is a later ticket.
- Mask sources are shared by all Sessions of one user. They are read-only and mounted read-only. If two users share one `XDG_STATE_HOME`, each needs their own.
- Hard links are found by scanning the Workspace outside Skip patterns only (stated by `check`).
