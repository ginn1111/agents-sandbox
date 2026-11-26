# Portals are published live through one publisher container per Portal

ADR-0013 requires a Portal to be started in a running Session without restarting the Session or Netguard. ADR-0003 and ADR-0010 put `127.0.0.1:<random>:<port>` on the Netguard `docker run`, which Docker only allows at creation. So the CLI gives each started Portal its own small **publisher**: a container from a dedicated image (`agent-sandbox-portal`, Alpine and `socat`) with every capability dropped, a read-only root, uid 65534, no mounts and no environment. It publishes `127.0.0.1::8000` (Docker picks the host port) and forwards to the Portal port on Netguard's address on a per-Portal `--internal` Docker network. `asb portal start` creates that network, connects Netguard to it, adds a Netguard rule that drops the network's subnet for anything the Session starts (`NG_LOCAL`), starts the publisher, adds one `INPUT` rule that accepts only that publisher to that one port, and reads the host binding back from Docker, refusing anything that is not `127.0.0.1`. Any failed step removes everything the start created, so a failed Portal is never left published. `stop` and `down` remove the publisher and the network, which releases the host port. The Session and Netguard keep running and are not replaced; the Session never gets a published port, so nothing is published by the Session, and ADR-0003's "set on Netguard" rule is replaced for the live case by "set on a publisher".

## Considered Options

- **Publish on the Netguard `docker run`, as ADR-0010 says:** rejected. It publishes at creation, so `up` would activate every Portal (against ADR-0013), and starting one later would mean replacing Netguard and so the Session.
- **Edit Docker's own DNAT rules or run a host proxy:** rejected. The first needs root on the host and is different on Colima; the second makes the CLI start a host program (AGENTS.md hard rule).
- **One publisher for all Portals:** rejected. A single publisher would be one more shared thing, and stopping one Portal would touch the others.
- **Give the Session a second network interface:** rejected. The Session must stay on Netguard's network only.

## Consequences

- A Netguard `INPUT` rule is the only inbound path. The Session cannot reach a publisher: Netguard drops the Portal network's subnet for new outbound connections (and the private ranges already drop it), and the network is `--internal`, so the publisher has no route out either. Traffic starts on the host and goes in; nothing in the Session can use a Portal to reach the host.
- The `127.0.0.1` binding is checked, not assumed: `parse_published` accepts only loopback bindings, and a start that cannot prove it is undone.
- Cost: one extra tiny image to build (`asb build` does it; `doctor` warns when it is missing) and one container per started Portal.
- A Portal published before the service listens just refuses connections. `status` shows publication and service separately (ADR-0013).
- Unverified, needs a spike (docs/spikes/portals.md): connecting Netguard to a second network while its network namespace is shared with the Session, that the Session sees the new interface but cannot use it, and Docker's loopback publish on Colima.
- `{generated}` secrets and the Hermes dashboard entry are not part of this decision (ticket 09).
