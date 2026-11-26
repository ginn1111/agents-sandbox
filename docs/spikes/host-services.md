# Spike: Host Services through `asb service enable` (ticket #6)

**Status: UNVERIFIED.** Written on a host with no Docker daemon, so nothing below was run against real Docker. Each item is an assumption, how the design fails closed if it is wrong, and the check that settles it. Run `uv run python -m pytest -m docker tests/test_e2e_docker.py -k host_service` on Linux Docker and on Colima (and Docker Desktop if available), then replace each **Result:** line.

## 1. The `NG_SERVICES` rule opens the listed port and nothing else

- Assumption: `OUTPUT` jumps to `NG_DNS`, `NG_SERVICES`, `NG_LOCAL` in that order, before the range drops, so an ACCEPT in `NG_SERVICES` wins over the drop of the gateway (172.16/12 on Linux, 192.168/16 on Colima). A different port on the same address still reaches the drop.
- Fails closed: if the rule does not apply, the helper rolls back and exits non-zero; `asb service enable` reports an error; the port stays dropped.
- Check: `test_a_listed_host_port_is_reachable_and_an_unlisted_port_on_the_same_host_is_not`.
- Note: `NG_LOCAL` drops the gateway address by itself and is consulted **after** `NG_SERVICES` (see `netguard.EXCEPTION_CHAINS`), so listing the gateway address explicitly works as well.
- Result: _not run_.

## 2. `docker exec` into Netguard can run `iptables` as the helper

- Assumption: `docker exec` joins Netguard with its bounding set (`NET_ADMIN` only, ADR-0014) and runs as the container user (uid 0), so the helper has the same rights as the entrypoint. The capability is already effective for uid 0, so `no-new-privileges` does not interfere.
- Fails closed: if `iptables` is refused, `enable` fails and nothing opens.
- Check: `test_a_listed_host_port_is_reachable...`, `test_disable_closes_the_host_service_again`.
- Result: _not run_.

## 3. `iptables -S NG_SERVICES` format matches what the helper reads back

- Assumption: Alpine's iptables (nft backend) prints `-A NG_SERVICES -d 1.2.3.4/32 -p tcp -m tcp --dport 80 -m comment --comment asb-svc:name -j ACCEPT`: `/32` appended, `-m tcp` inserted, the comment unquoted (no spaces in the tag). `list`, `disable` and the verification parse exactly that. Needs the `comment` match module in Alpine's iptables (`xt_comment` in the host kernel).
- Fails closed: if the format differs or `comment` is missing, `enable` fails verification and rolls back (a closed failure, every time). Loosen the comparison in `service.sh`; do not skip verification.
- Check: any `service enable` run. The stub-based Seam 1 tests prove the script only against this assumed format.
- Result: _not run_.

## 4. Name resolution inside Netguard

- Assumption: Alpine's musl `getent` supports `ahostsv4` (fallback `hosts`) and resolves `host.docker.internal` from `/etc/hosts` (Linux `--add-host`) or Docker's DNS (Colima, Docker Desktop). Resolution happens before the rule is added, so DNS in Netguard only needs `NG_DNS`.
- Fails closed: no address means `enable` fails.
- Unknown: on Colima the name may resolve to an address in a range Netguard drops by default (for example `192.168.5.2`); the `NG_SERVICES` ACCEPT is consulted before that drop, so it is expected to work.
- Result: _not run_.

## 5. Host app bound to 127.0.0.1 on Linux

- Assumption: through the Docker bridge gateway the Session reaches only apps listening on `0.0.0.0` or on the bridge address. A loopback-only app is not reachable. This is a Docker property; the CLI must not widen anything on the host.
- `asb doctor` probes from the Session for an enabled service ("does not answer from the Session"). With no Session running it can only probe the host (127.0.0.1), which cannot see this problem.
- Result: _not run_.

## 6. Rules end with the Session

- Assumption: the rules live in Netguard's network namespace; `down` removes Netguard, so a new `up` starts with `NG_SERVICES` empty.
- Check: `test_the_host_service_rules_end_with_the_session`.
- Result: _not run_.

## 7. The Session cannot open a Host Service itself

- Assumption: the Session holds no capabilities and the agent image has no `iptables`, so it cannot add a rule to the shared network namespace.
- Check: `test_the_session_cannot_open_a_host_service_itself`.
- Result: _not run_.
