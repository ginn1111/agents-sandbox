# Block the host and private networks except listed Host Services; keep the public internet open

Docker's default bridge lets a container reach every port on the host and the company LAN, so a list of Host Services means nothing unless everything else is blocked. We decided: the sandbox blocks all traffic to the host and to private and link-local address ranges, except the `host:port` pairs listed as Host Services. Public internet stays open because the agents need their own APIs. If the rules can't be applied, the session doesn't start (fail closed).

## Considered Options

- **Open bridge, documented only**: rejected. The Host Service list would be decoration only, and the agent could reach internal databases and admin panels on the LAN.
- **Block everything except Host Services, including the internet**: rejected. Agents could no longer reach their own APIs unless every one went through a gateway on the host. Too heavy for a lightweight tool.

## Consequences

- This is not an exfiltration control: an agent can still send Workspace data to the public internet (consistent with ADR-0001).
- The rules must cover IPv4 and IPv6, and must work on Linux Docker and Colima.
