# Spike: Portals (ticket 08, ADR-0010, ADR-0013, ADR-0018)

**Status: UNVERIFIED.** Written on a host with no Docker daemon, so nothing below was run. Each item is an assumption, how the design fails closed if it is wrong, and the check that settles it. Run `uv run python -m pytest -m docker tests/test_e2e_portals.py` on Linux Docker and on Colima, then replace each **Result:** line.

## 1. Netguard can join a second network while the Session shares its namespace

- Assumption: `docker network connect <portal-net> <netguard>` works on a running container whose network namespace is also used by the Session (`--network container:`), and the new interface appears in the shared namespace.
- Fails closed: if the connect is refused, `start` fails and removes the network; nothing is published.
- Check: `test_a_started_portal_answers_on_127_0_0_1...`, `test_start_does_not_restart_the_session_or_netguard`.
- Result: _not run_.

## 2. The Session sees the new interface but cannot use it

- Assumption: Netguard's `OUTPUT` chain applies to the Session's traffic too (same namespace), so `NG_LOCAL -d <subnet> -j DROP` stops the Session starting a connection to the publisher, and the `--internal` network gives the publisher no route out. The drop rule is added before the publisher starts.
- Fails closed: if the drop rule cannot be applied or read back (`iptables -C`), `start` fails and is undone.
- Check: `test_portals_are_one_way_nothing_in_the_session_reaches_the_publisher`, `test_the_session_still_has_no_route_to_the_host_while_a_portal_is_published`.
- Result: _not run_.

## 3. `INPUT` rule for the publisher's address

- Assumption: the publisher's connection to Netguard's address on the Portal network arrives on `INPUT` in the shared namespace (the Portal service listens in the Session, which shares it), and replies leave through the existing `ESTABLISHED` rule. `iptables -w` waits for the xtables lock; Alpine's `iptables` supports `-C`.
- Fails closed: without the rule nothing reaches the service; the Portal is published but refuses connections.
- Check: `test_a_started_portal_answers...`.
- Result: _not run_.

## 4. Loopback-only publication

- Assumption: `--publish 127.0.0.1::8000` binds only `127.0.0.1` on Linux Docker and on Colima/Docker Desktop. `docker port` prints the binding; `parse_published` refuses anything else and `start` undoes itself.
- Colima: forwarding from the VM to macOS is done by Lima, which may bind differently from Docker's own report; `docker port` does not cover that. Check by connecting from another machine.
- Check: `test_a_portal_is_not_reachable_from_other_machines` (skips the LAN probe when the host has no non-loopback address).
- Result: _not run_.

## 5. Plannotator inside the Session

- Assumption: `curl -fsSL https://plannotator.ai/install.sh | bash -s -- --minimal` with `HOME=/opt/plannotator` puts the binary in `/opt/plannotator/.local/bin` and writes nothing else. With `PLANNOTATOR_REMOTE=1` it listens on `0.0.0.0:<PLANNOTATOR_PORT>` (upstream `packages/server/remote.ts`, `getServerHostname`) and does not try to open a browser.
- Assumption: Claude Code reads `/etc/claude-code/managed-settings.json` (Linux managed settings path) and runs the `ExitPlanMode` `PermissionRequest` hook from it. Plannotator documents the same hook for user settings, not the managed path. If wrong, the hook goes into the Claude Agent volume instead.
- Unknown: whether Plannotator's pages and links work when the browser uses `127.0.0.1:<random>` instead of `:19432`. If it advertises a fixed port, set `PLANNOTATOR_URL_HOST` in the Portal entry; that is config, not code.
- Check: `test_plannotator_is_in_the_image_and_configured_for_remote_use`, then by hand: start `claude` in plan mode, `asb portal start plannotator`, open the URL, approve or deny, and see the feedback reach the agent. This is the acceptance criterion "plan review completes in the host browser" and is not proven.
- Result: _not run_.

## 6. The Portal command

- Assumption: `docker exec -d -e K=V -w /workspace <session> sh -c 'echo $$ > "$0" && exec "$@"' /tmp/asb-portal-<name>.pid <command...>` starts the command as the Session's user, writes its pid to the Session's `/tmp` (tmpfs, shared between `exec` calls) and keeps running after `exec` returns. `stop` runs `kill` on that pid. The listen probe uses bash's `/dev/tcp`.
- Fails closed: a command that does not listen within 15 s fails the start and is killed; nothing is published.
- Check: `test_a_command_portal_is_started_by_asb_and_stopped_with_it`.
- Result: _not run_.
