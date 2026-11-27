# Spike: Netguard on Linux Docker and Colima

**Status: UNVERIFIED.** This was written on a host with no Docker daemon, so nothing below was run. Each item is an assumption the implementation relies on, the way the design fails closed if it is wrong, and the check that settles it. Run the Seam 2 tests (`uv run pytest -m docker tests/test_e2e_docker.py`) on Linux Docker and on Colima, then replace each **Result:** line with what you saw.

## 1. A container joined with `--network container:<netguard>` loses its network when Netguard dies

- Assumption: when Netguard stops, Docker tears down Netguard's veth. The Session keeps the namespace alive with only `lo`.
- Fails closed anyway: the filter table in that namespace keeps its default-DROP policies as long as the namespace exists.
- Check: `test_killing_netguard_leaves_the_session_with_no_network`, `test_concurrent_sessions_have_their_own_netguard`.
- Result: _not run_.

## 2. DNS resolver in Netguard's network

- Assumption: Netguard runs on the default bridge, so `/etc/resolv.conf` holds the resolvers Docker copied from the host (for example the upstream of `systemd-resolved`, not `127.0.0.53`). Netguard allows `udp/tcp 53` to each IPv4 resolver before the range drops, so a resolver on the LAN keeps working and nothing else on the LAN does.
- Colima/Lima: the resolver is likely a `192.168.5.x` address, which sits inside a blocked range; the DNS rule handles that, the range drop does not hide it.
- Unknown: whether any setup gives an `127.0.0.11` (embedded DNS) or IPv6-only resolver. Loopback resolvers are skipped (the script doesn't open them). If DNS doesn't work there, the entrypoint prints a warning and the e2e DNS test fails; it never opens more than port 53 to a listed IPv4 resolver.
- Result: _not run_.

## 3. Host gateway and `host.docker.internal`

- Assumption (Linux): `--add-host host.docker.internal:host-gateway` on Netguard maps the name to the docker0 gateway (a 172.16/12 address, dropped). The name alone grants nothing.
- Assumption (Colima, Docker Desktop): the name already resolves inside the VM to an address in a private range (for example `192.168.5.2`), so the plan doesn't add `--add-host` when not on Linux. It is added based on the host Python's `sys.platform`, which is right for Linux Docker and Colima/Docker Desktop on macOS but wrong for a Linux client talking to a remote or Colima Docker context. If that matters, move the check to `docker info` (`OperatingSystem`).
- The gateway and the container's own subnet are dropped by address at start (the `NG_LOCAL` chain), so the host stays blocked even if Docker is configured with a public-looking address pool.
- Check: `test_host_gateway_and_host_addresses_are_unreachable`.
- Result: _not run_.

## 4. Docker accepts the flag split between Netguard and the Session

- Assumption: Docker rejects `--hostname`, `--add-host`, `--dns`, `--publish` and `--sysctl` network settings on a container that joins another's network namespace, so the plan puts them on Netguard only. (`--hostname` is left off the Session when it is joined; offline Sessions keep it.)
- Check: `asb up` on any machine; a rejected flag shows as a failing `docker run` and `up` cleans up and exits non-zero.
- Result: _not run_.

## 5. IPv6 sysctls

- Assumption: `--sysctl net.ipv6.conf.all.disable_ipv6=1` (and `default`) is accepted on the default bridge, and `/proc/sys/net/ipv6/conf/all/disable_ipv6` reads `1` inside Netguard. On a host with IPv6 disabled in the kernel the file may be missing; the entrypoint then refuses to become ready (fail closed) and `up` fails with "IPv6 is not disabled".
- Check: `test_ipv6_is_disabled_in_the_session_network`.
- Result: _not run_.

## 6. `iptables` backend and verification

- Assumption: Alpine's `iptables` (nft backend) works in the container on Linux and in the Colima VM. The script verifies by reading back `iptables-save` and comparing each generated `-A` line; the format of `-m conntrack --ctstate` output (state order, extra spaces) and of `-d` network values (e.g. `10.0.0.0/8`) is assumed to match the input. If it doesn't, **every** start fails verification; that's a closed failure, not an open one. Fix by loosening the comparison, not by skipping it.
- Check: `asb up` prints the first unmatched rule on failure.
- Result: _not run_.

## 7. Readiness handshake

- Assumption: `docker exec <netguard> test -f /run/netguard/ready` succeeds only after the script has applied and verified the rules, and `/run` as tmpfs is writable by uid 0 with all capabilities except `NET_ADMIN` dropped.
- Check: `test_netguard_reports_ready_through_its_ready_file`, `test_netguard_without_net_admin_never_becomes_ready`.
- Result: _not run_.
