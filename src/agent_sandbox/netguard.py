"""Netguard (ADR-0002, ADR-0003): the rule set and the `docker run` arguments for one Session's Netguard.

Pure functions only. The rules are generated here, passed into the Netguard container in the
``ASB_NETGUARD_RULES`` variable and applied there by ``images/netguard/entrypoint.sh`` in one
atomic ``iptables-restore``. Every chain defaults to DROP, so a rule that is missing or fails to
apply leaves the network closed, never open.
"""
from __future__ import annotations

IMAGE = "agent-sandbox-netguard:latest"
RULES_ENV = "ASB_NETGUARD_RULES"
READY_FILE = "/run/netguard/ready"

# Never reachable from the Session (ADR-0002). Host Services are opened per Session by a separate,
# explicit command (ADR-0013) through the NG_SERVICES chain, which is consulted before these drops.
BLOCKED_RANGES = (
    "0.0.0.0/8",        # "this" network
    "10.0.0.0/8",       # RFC1918
    "100.64.0.0/10",    # CGNAT (also Tailscale and many VPNs)
    "169.254.0.0/16",   # link-local, cloud metadata services
    "172.16.0.0/12",    # RFC1918, includes the Docker bridge and the host gateway
    "192.0.0.0/24",     # IETF protocol assignments
    "192.168.0.0/16",   # RFC1918, includes Colima/Lima and Docker Desktop host addresses
    "198.18.0.0/15",    # benchmarking
    "224.0.0.0/3",      # multicast, reserved and broadcast
)

# Chains the entrypoint fills at start (NG_DNS, NG_LOCAL) or that stay empty until a later explicit
# command fills them (NG_SERVICES).
EXCEPTION_CHAINS = ("NG_DNS", "NG_SERVICES", "NG_LOCAL")


def rules() -> list[str]:
    """The iptables-restore input for the ``filter`` table. IPv6 is disabled instead of filtered."""
    lines = ["*filter", ":INPUT DROP [0:0]", ":FORWARD DROP [0:0]", ":OUTPUT DROP [0:0]"]
    lines += [f":{chain} - [0:0]" for chain in EXCEPTION_CHAINS]
    lines += [
        "-A INPUT -i lo -j ACCEPT",
        "-A INPUT -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT",
        "-A OUTPUT -o lo -j ACCEPT",
        "-A OUTPUT -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT",
    ]
    lines += [f"-A OUTPUT -j {chain}" for chain in EXCEPTION_CHAINS]
    lines += [f"-A OUTPUT -d {net} -j DROP" for net in BLOCKED_RANGES]
    lines += ["-A OUTPUT -j ACCEPT", "COMMIT"]
    return lines
