# Spike: `asb update` and rollback (ticket 11, ADR-0012, ADR-0022)

**Status: UNVERIFIED.** Written on a host with no Docker daemon, so nothing below was run. Each item is an assumption, how the design fails closed if it is wrong, and the check that settles it: `uv run python -m pytest -m docker tests/test_e2e_docker.py -k update` on Linux Docker and on Colima. Replace each **Result:** line.

## 1. `docker tag` moves a tag and an id can be tagged

- Assumption: `docker tag <id> repo:previous` works for an image id read from `docker image ls`, and re-tagging `repo:latest` leaves the old image without a tag, which `docker rmi <id>` then removes.
- Fails closed: a failed tag undoes the promotion and removes the new builds; `latest` never ends up between two builds.
- Check: `test_update_image_then_rollback_moves_latest_and_spares_a_running_session`.
- Result: _not run_.

## 2. `docker rmi` without `-f` refuses an image a running Session uses

- Assumption: removing a dated tag whose image is used by a running container fails or only untags; either way the Session keeps running and `update` reports the tag it could not remove.
- Fails closed: pruning never forces. The worst case is one extra old image on disk.
- Check: same test (the Session stays up, `{{.Image}}` still the old id).
- Result: _not run_.

## 3. The one-off integrations container

- Assumption: `docker run --rm --read-only --network none --cap-drop ALL --user <uid>:<gid> --mount type=volume,source=agent-sandbox-hermes,target=/agents/hermes ... sh -c 'mkdir -p ... && herdr integration install hermes'` writes the integration files into the volume as the same uid the Session uses, and does not need the network.
- Fails closed: a failed install is a warning naming what herdr loses and exit code 1; nothing else changes. No volume is created: only volumes that already exist are used.
- If `herdr integration install` needs a writable `/tmp` or `$XDG_*` outside HOME, `--tmpfs /tmp` is already given; any other need must be added with a new ADR, not by relaxing the flags.
- Check: `test_update_integrations_keeps_the_agent_volume_contents`.
- Result: _not run_.

## 4. Hand-off to the upgraded CLI

- Assumption: after `uv tool upgrade` / `pipx upgrade`, `asb` on PATH is the new version, so `asb update --after-cli` builds the new Dockerfiles. If PATH still resolves to the old one, the images are built from the old Dockerfiles; a warning says so.
- Check: run `asb update` after bumping the package version; compare the Dockerfile that was built.
- Result: _not run_.
