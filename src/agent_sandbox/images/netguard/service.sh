#!/bin/sh
# Netguard helper `asb-service` (ADR-0002, ADR-0013, ADR-0016): open or close ONE Host Service in the
# running Netguard, without restarting it. Run by `asb service enable|disable` through `docker exec`.
#
#   asb-service enable <name> <host> <port>   allow new TCP connections to <host>:<port>
#   asb-service disable <name>                remove every rule of that service
#   asb-service list                          names of the enabled services, one per line
#
# Rules go into NG_SERVICES, which the OUTPUT chain consults before the range drops (NG_LOCAL, the
# private ranges), so the listed port is reachable and every other port on the same host stays
# dropped. Each rule is tagged with a comment naming the service so disable can find it.
# Fail closed: invalid input, an unready Netguard, a failed `iptables` or a rule that cannot be
# read back leaves nothing open for that service (it is rolled back); the default is always DROP.
set -eu
set -f   # no globbing: rules are passed to iptables as split words

RUN_DIR=${ASB_NETGUARD_RUN_DIR:-/run/netguard}
CHAIN=NG_SERVICES

fail() {
    echo "asb-service: $*" >&2
    exit 1
}

usage() {
    fail "usage: asb-service enable <name> <host> <port> | disable <name> | list"
}

valid_name() {
    case "$1" in
        '' | -* | *[!a-z0-9-]*) return 1 ;;
        [a-z]*) [ "${#1}" -le 32 ] ;;
        *) return 1 ;;
    esac
}

valid_port() {
    case "$1" in
        '' | *[!0-9]* | 0*) return 1 ;;
    esac
    [ "${#1}" -le 5 ] && [ "$1" -le 65535 ]
}

# An IPv4 address the Session may be given a path to: never loopback, unspecified, link-local
# (cloud metadata), multicast, reserved or broadcast.
usable_ip() {
    printf '%s\n' "$1" | awk -F. '
        NF != 4 { exit 1 }
        { for (i = 1; i <= 4; i++) if ($i !~ /^[0-9]+$/ || $i > 255) exit 1 }
        $1 == 0 || $1 == 127 || $1 >= 224 { exit 1 }
        $1 == 169 && $2 == 254 { exit 1 }'
}

is_ipv4_literal() {
    printf '%s\n' "$1" | grep -Eq '^[0-9]+(\.[0-9]+){3}$'
}

# The IPv4 addresses of a host name or literal, one per line. IPv6 is disabled, so it is ignored.
resolve() {
    if is_ipv4_literal "$1"; then
        printf '%s\n' "$1"
        return 0
    fi
    out=$(getent ahostsv4 "$1" 2>/dev/null) || out=$(getent hosts "$1" 2>/dev/null) || out=""
    printf '%s\n' "$out" | awk '$1 ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/ { print $1 }' | sort -u
}

# Delete every rule tagged for service $1. Succeeds when there is none.
remove_rules() {
    tag="asb-svc:$1"
    rules=$(iptables -S "$CHAIN") || return 1
    printf '%s\n' "$rules" | awk -v tag="$tag" -v chain="$CHAIN" '
        $1 == "-A" && $2 == chain { for (i = 3; i < NF; i++) if ($i == "--comment" && $(i + 1) == tag) { print; next } }' \
        | sed 's/^-A /-D /' | while IFS= read -r rule; do
            iptables $rule || exit 1
        done
}

[ $# -ge 1 ] || usage
action=$1
shift
for tool in iptables awk grep sed sort; do
    command -v "$tool" >/dev/null 2>&1 || fail "required tool missing: $tool"
done

case "$action" in
    list)
        [ $# -eq 0 ] || usage
        iptables -S "$CHAIN" | sed -n 's/.* --comment asb-svc:\([a-z0-9-]*\) .*/\1/p' | sort -u
        ;;
    disable)
        [ $# -eq 1 ] || usage
        valid_name "$1" || fail "invalid service name '$1'"
        remove_rules "$1" || fail "cannot remove the rules of '$1'"
        if iptables -S "$CHAIN" | grep -q -- "--comment asb-svc:$1 "; then
            fail "verify: rules of '$1' are still present"
        fi
        ;;
    enable)
        [ $# -eq 3 ] || usage
        name=$1 host=$2 port=$3
        valid_name "$name" || fail "invalid service name '$name'"
        valid_port "$port" || fail "invalid port '$port'"
        case "$host" in
            '' | -* | *[!A-Za-z0-9.-]*) fail "invalid host '$host'" ;;
        esac
        [ -f "$RUN_DIR/ready" ] || fail "Netguard is not ready: its base rules are not verified"
        ips=$(resolve "$host")
        [ -n "$ips" ] || fail "cannot resolve '$host' to an IPv4 address"
        for ip in $ips; do
            usable_ip "$ip" || fail "'$host' resolves to $ip, which must never be opened"
        done
        remove_rules "$name" || fail "cannot replace the rules of '$name'"
        tag="asb-svc:$name"
        for ip in $ips; do
            if ! iptables -A "$CHAIN" -d "$ip" -p tcp --dport "$port" -m comment --comment "$tag" -j ACCEPT; then
                remove_rules "$name" || true
                fail "cannot allow $ip:$port"
            fi
        done
        saved=$(iptables -S "$CHAIN") || { remove_rules "$name" || true; fail "verify: cannot read back $CHAIN"; }
        for ip in $ips; do
            want="-A $CHAIN -d $ip/32 -p tcp -m tcp --dport $port -m comment --comment $tag -j ACCEPT"
            if ! printf '%s\n' "$saved" | grep -qxF -- "$want"; then
                remove_rules "$name" || true
                fail "verify: rule not applied: $want"
            fi
        done
        echo "$name: $host:$port open ($(echo $ips))"
        ;;
    *) usage ;;
esac
