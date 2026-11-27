# Host Services are opened by a helper script that `asb service enable` runs inside the running Netguard

ADR-0013 says a Host Service is opened only by an explicit command, without restarting either container. Netguard's rules are applied once at start (ADR-0003), so the CLI needs a way to change them live. We decided: Netguard's image ships `asb-service` (`images/netguard/service.sh`, a POSIX shell script), and `asb service enable <name>` runs `docker exec <netguard> asb-service enable <name> <host> <port>`. The helper adds `-A NG_SERVICES -d <ip> -p tcp --dport <port> -j ACCEPT` (tagged with a comment naming the service) for each IPv4 address of the host, reads the chain back, and removes its own rules if anything fails. `NG_SERVICES` is consulted before the range drops, so only the listed port opens; every other port on the same host stays dropped. `disable <name>` removes that service's rules; `down` removes the whole Netguard, so nothing outlives the Session. No new capability, mount, port or environment variable is given to either container: the helper runs as the same uid 0 with only `NET_ADMIN` that ADR-0014 already accepted, and the Session still cannot reach it.

The helper does not trust its arguments even though the CLI has validated them. It accepts only a plain service name, a port from 1 to 65535 and a DNS name or IPv4 literal; it resolves the host itself and refuses to open any address that is loopback, unspecified, link-local (cloud metadata), multicast or reserved, and refuses to run before Netguard's ready file exists. Config validation applies the same host rules, so `network.host_services` cannot name `localhost`, `127.0.0.1`, `169.254.169.254` and the like.

## Considered Options

- **Re-run `iptables-restore` with the full rule set including Host Services**: rejected. The rules would have to be passed in again (a new `docker exec` with env anyway), and a half-applied restore is harder to reason about than one added rule that is read back.
- **Open Host Services at Netguard start from `ASB_NETGUARD_RULES`**: rejected. That is what ADR-0013 superseded; a config change would open a path on plain `asb up`.
- **Recreate Netguard and the Session with the extra rule**: rejected. ADR-0013 requires activation without restarting either container, and the Session would lose its processes.
- **Run `iptables` through `docker exec` directly from the CLI, without a helper**: rejected. The CLI would have to resolve names, validate the result and roll back on failure from outside, with several round trips and a window where only some addresses of a name are open.

## Consequences

- The host name is resolved inside Netguard when the service is enabled. If the host's address changes later, run `asb service disable` then `enable` again.
- `host.docker.internal` resolves to the Docker host gateway on Linux (`--add-host`, set on Netguard, ADR-0003) and to the VM's host address on Colima and Docker Desktop; the CLI never reads the host's own network.
- A host app that listens only on `127.0.0.1` on Linux is not reachable through the gateway; it must listen on the bridge address or `0.0.0.0`. `asb doctor` tells these cases apart by probing from the Session. Binding an app to `0.0.0.0` exposes it to the LAN; that is the user's choice.
- Rules are TCP only and IPv4 only (IPv6 is disabled). UDP Host Services are not supported.
- The Netguard image gains one script and relies on `getent` from musl; if `getent` cannot resolve a name, `enable` fails and opens nothing.
