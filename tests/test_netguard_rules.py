"""Seam 1: the generated Netguard rule set (ADR-0002, ADR-0003). No Docker."""
import ipaddress
import re

from agent_sandbox.netguard import BLOCKED_RANGES, rules


def _drops(lines):
    return [m.group(1) for l in lines if (m := re.fullmatch(r"-A OUTPUT -d (\S+) -j DROP", l))]


def test_rules_are_one_atomic_filter_table_restore_input():
    lines = rules()
    assert lines[0] == "*filter" and lines[-1] == "COMMIT"
    assert lines.count("COMMIT") == 1


def test_every_chain_defaults_to_drop_so_a_missing_rule_fails_closed():
    lines = rules()
    for chain in ("INPUT", "FORWARD", "OUTPUT"):
        assert f":{chain} DROP [0:0]" in lines


def test_host_lan_link_local_and_cgnat_ranges_are_dropped():
    drops = [ipaddress.ip_network(d) for d in _drops(rules())]
    for probe in ("10.1.2.3", "172.17.0.1", "172.31.255.255", "192.168.1.10", "192.168.5.2",
                  "169.254.169.254", "100.64.0.1", "100.127.255.254", "0.0.0.1", "224.0.0.251",
                  "255.255.255.255"):
        assert any(ipaddress.ip_address(probe) in n for n in drops), probe


def test_public_addresses_are_not_dropped():
    drops = [ipaddress.ip_network(d) for d in _drops(rules())]
    for probe in ("1.1.1.1", "8.8.8.8", "140.82.112.3", "172.15.0.1", "172.32.0.1", "100.63.255.255",
                  "100.128.0.1", "192.169.0.1"):
        assert not any(ipaddress.ip_address(probe) in n for n in drops), probe


def test_blocked_ranges_constant_matches_the_rules():
    assert _drops(rules()) == list(BLOCKED_RANGES)


def test_public_internet_is_open_only_after_every_drop():
    lines = rules()
    accept_all = lines.index("-A OUTPUT -j ACCEPT")
    assert all(lines.index(f"-A OUTPUT -d {r} -j DROP") < accept_all for r in BLOCKED_RANGES)
    assert lines[accept_all + 1] == "COMMIT"


def test_dns_services_and_local_chains_are_consulted_before_the_range_drops():
    lines = rules()
    first_drop = lines.index(f"-A OUTPUT -d {BLOCKED_RANGES[0]} -j DROP")
    jumps = [lines.index(f"-A OUTPUT -j {c}") for c in ("NG_DNS", "NG_SERVICES", "NG_LOCAL")]
    assert jumps == sorted(jumps) and jumps[-1] < first_drop
    # The chains start empty. Netguard fills NG_DNS (the configured resolver) and NG_LOCAL (drops for
    # its own gateway and subnet) at start; NG_SERVICES stays empty until a later explicit
    # `asb service enable` (ADR-0013).
    for c in ("NG_DNS", "NG_SERVICES", "NG_LOCAL"):
        assert f":{c} - [0:0]" in lines
    assert not [l for l in lines if l.startswith("-A NG_")]


def test_loopback_and_replies_are_allowed_but_nothing_new_comes_in():
    lines = rules()
    assert "-A INPUT -i lo -j ACCEPT" in lines
    assert "-A INPUT -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT" in lines
    assert "-A OUTPUT -o lo -j ACCEPT" in lines
    assert "-A OUTPUT -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT" in lines
    inbound = [l for l in lines if l.startswith("-A INPUT")]
    assert len(inbound) == 2  # no inbound port is open (Portals are a later, explicit step)


def test_no_host_service_is_open_by_default():
    # ADR-0013: Host Services are opened only by an explicit command, never by `up`
    assert not [l for l in rules() if "--dport" in l]


def test_conntrack_states_are_written_in_the_order_iptables_save_prints_them():
    # The entrypoint verifies by comparing lines with `iptables-save` output, which lists the
    # conntrack states as RELATED,ESTABLISHED (UNVERIFIED here: see docs/spikes/netguard.md; the
    # script also normalises the order, so a mismatch cannot fail the start by itself).
    for line in rules():
        if "--ctstate" in line:
            assert "--ctstate RELATED,ESTABLISHED " in line
