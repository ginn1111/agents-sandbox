#!/bin/sh
# Netguard entrypoint (ADR-0002, ADR-0003). Fail closed: any error removes the ready file and exits
# non-zero, and every chain already defaults to DROP, so a half-applied rule set never opens the network.
#
# 1. apply the rule set in $ASB_NETGUARD_RULES (';'-separated lines) in one atomic iptables-restore
# 2. allow DNS only to the resolvers in resolv.conf (NG_DNS)
# 3. drop this network's own gateway and subnet, even if Docker picked a public-looking range (NG_LOCAL)
# 4. read the rules back and check them, check that IPv6 is disabled
# 5. only then write the ready file; the CLI starts the Session after it appears
# Host Services are not opened here: that is a later, explicit command (ADR-0013).
set -eu

RUN_DIR=${ASB_NETGUARD_RUN_DIR:-/run/netguard}
READY="$RUN_DIR/ready"
RESOLV=${ASB_NETGUARD_RESOLV_CONF:-/etc/resolv.conf}
IPV6_FLAG=${ASB_NETGUARD_IPV6_FLAG:-/proc/sys/net/ipv6/conf/all/disable_ipv6}

fail() {
    echo "netguard: $*" >&2
    rm -f "$READY"
    exit 1
}

mkdir -p "$RUN_DIR" || fail "cannot create $RUN_DIR"
rm -f "$READY"

for tool in iptables iptables-restore iptables-save ip awk grep sleep; do
    command -v "$tool" >/dev/null 2>&1 || fail "required tool missing: $tool"
done
[ -n "${ASB_NETGUARD_RULES:-}" ] || fail "ASB_NETGUARD_RULES is empty; refusing to start without rules"

RULES=$(printf '%s\n' "$ASB_NETGUARD_RULES" | awk 'BEGIN { RS = ";" } NF { print }')

# 1. atomic rule set
printf '%s\n' "$RULES" | iptables-restore || fail "iptables-restore failed"

# 2. DNS: only the configured IPv4 resolvers (loopback ones never leave the namespace)
resolvers=$(awk '$1 == "nameserver" && $2 ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/ && $2 !~ /^127\./ { print $2 }' "$RESOLV") \
    || fail "cannot read $RESOLV"
[ -n "$resolvers" ] || echo "netguard: warning: no IPv4 resolver in $RESOLV; DNS will not work" >&2
for ns in $resolvers; do
    for proto in udp tcp; do
        iptables -A NG_DNS -d "$ns" -p "$proto" --dport 53 -j ACCEPT || fail "cannot allow DNS to $ns"
    done
done

# 3. this network's own gateway (the host) and subnet
gateway=$(ip -4 route show default | awk '$1 == "default" { print $3; exit }') || fail "cannot read the default route"
subnet=$(ip -4 -o addr show scope global | awk '{ for (i = 1; i < NF; i++) if ($i == "inet") { print $(i + 1); exit } }') \
    || fail "cannot read the network address"
[ -n "$gateway" ] || fail "no default gateway found"
[ -n "$subnet" ] || fail "no network address found"
iptables -A NG_LOCAL -d "$gateway" -j DROP || fail "cannot block the gateway $gateway"
iptables -A NG_LOCAL -d "$subnet" -j DROP || fail "cannot block the subnet $subnet"

# 4. verify what is actually applied
saved=$(iptables-save) || fail "cannot verify the rules: iptables-save failed"
# iptables-save may list conntrack states in either order; compare in one order
saved=$(printf '%s\n' "$saved" | sed 's/ESTABLISHED,RELATED/RELATED,ESTABLISHED/')
for policy in ":INPUT DROP" ":FORWARD DROP" ":OUTPUT DROP"; do
    printf '%s\n' "$saved" | grep -q -- "^$policy " || fail "verify: policy '$policy' is not applied"
done
printf '%s\n' "$RULES" | grep '^-A ' | while IFS= read -r rule; do
    printf '%s\n' "$saved" | grep -qxF -- "$rule" || { echo "netguard: verify: rule not applied: $rule" >&2; exit 1; }
done || fail "verify: the applied rules differ from the generated rules"
[ "$(cat "$IPV6_FLAG" 2>/dev/null || echo unknown)" = "1" ] || fail "IPv6 is not disabled in this network"

# 5. ready, then idle
: > "$READY" || fail "cannot write $READY"
trap 'exit 0' TERM INT
while :; do
    sleep 3600 &
    wait $! || true
done
