# A per-session Netguard container enforces network rules; IPv6 is disabled

The agent container has every Linux permission dropped, so it can't apply its own firewall and must never be able to change one. Each Session starts a small Netguard container first. Netguard alone holds `NET_ADMIN`, applies the ADR-0002 rules, signals ready, then idles. The agent container joins Netguard's network (`--network container:<netguard>`). Because of this, Portal ports and `--add-host` are set on Netguard, not on the agent. IPv6 is disabled in Netguard's network (`net.ipv6.conf.all.disable_ipv6=1`), so every rule only has to be written for IPv4. If Netguard fails to start, or stops during the Session, the agent doesn't run or loses its network. It never falls back to an open network.

## Considered Options

- **Rules on the host**: rejected. Needs sudo on Linux, and on Colima the rules would live inside the VM and need SSH, which breaks easily.
- **Firewall set inside the agent container by a root entrypoint**: rejected. The agent image would need `NET_ADMIN` at startup, and one script bug leaves the network open.
- **One shared Netguard for all Sessions**: rejected. Sessions would share a network, leaking one Session's Host Services and Portals to another.

## Consequences

- Estimated cost: about 0.2–0.5 s startup and about 10 MB per Session (not yet measured).
- IPv6-only Host Services can't be reached. Add `ip6tables` rules if that's ever needed.
