"""Seam 1: `network.host_services` entries in config (ADR-0002, ADR-0013). No Docker."""
import textwrap

import pytest

from agent_sandbox.config import ConfigError, load_config


@pytest.fixture
def env(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    gpath = tmp_path / "global.yaml"

    def write(path, text):
        path.write_text(textwrap.dedent(text))

    return ws, gpath, write


def services(ws, gpath):
    return [(s["name"], s["host"], s["port"]) for s in load_config(ws, global_path=gpath).host_services]


def test_no_host_service_is_configured_by_default(env):
    ws, gpath, _ = env
    assert services(ws, gpath) == []


def test_entries_have_a_name_a_host_and_a_port(env):
    ws, gpath, write = env
    write(gpath, """\
        version: 1
        network:
          host_services:
            - {name: dbx-web, host: host.docker.internal, port: 4224}
            - {name: llm-gateway, host: host.docker.internal, port: 20128}
        """)
    assert services(ws, gpath) == [
        ("dbx-web", "host.docker.internal", 4224), ("llm-gateway", "host.docker.internal", 20128)]


def test_project_config_may_list_host_services_and_check_records_where_they_came_from(env):
    ws, gpath, write = env
    write(ws / ".agent-sandbox.yaml", """\
        version: 1
        network: {host_services: [{name: dbx-web, host: host.docker.internal, port: 4224}]}
        """)
    cfg = load_config(ws, global_path=gpath)
    assert ("network.host_services", "dbx-web host.docker.internal:4224") in cfg.project_rules


def test_a_later_layer_replaces_a_service_of_the_same_name(env):
    ws, gpath, write = env
    write(gpath, "version: 1\nnetwork: {host_services: [{name: a, host: host.docker.internal, port: 1}]}\n")
    write(ws / ".agent-sandbox.yaml",
          "version: 1\nnetwork: {host_services: [{name: a, host: host.docker.internal, port: 2}]}\n")
    assert services(ws, gpath) == [("a", "host.docker.internal", 2)]


@pytest.mark.parametrize("host", [
    "localhost", "LocalHost", "127.0.0.1", "127.1.2.3", "0.0.0.0", "::1", "[::1]", "fe80::1",
    "", " ", "bad host", "host:4224", "http://host", "host/path", "host;rm", "-leading-dash",
    "224.0.0.1", "169.254.169.254", "255.255.255.255",
])
def test_hosts_that_are_not_a_name_or_a_usable_ipv4_address_are_rejected(env, host):
    ws, gpath, write = env
    gpath.write_text("version: 1\nnetwork:\n  host_services:\n    - {name: a, host: %r, port: 80}\n" % host)
    with pytest.raises(ConfigError, match="host"):
        load_config(ws, global_path=gpath)


@pytest.mark.parametrize("host", ["host.docker.internal", "192.168.5.2", "172.17.0.1", "dbx.internal"])
def test_host_names_and_ipv4_addresses_are_accepted(env, host):
    ws, gpath, write = env
    gpath.write_text("version: 1\nnetwork:\n  host_services:\n    - {name: a, host: %s, port: 80}\n" % host)
    assert services(ws, gpath) == [("a", host, 80)]


@pytest.mark.parametrize("name", ["A", "has space", "-x", "x" * 40, "a/b", "a;b", ""])
def test_service_names_must_be_plain_because_they_label_firewall_rules(env, name):
    ws, gpath, _ = env
    gpath.write_text("version: 1\nnetwork:\n  host_services:\n    - {name: %r, host: h.internal, port: 80}\n" % name)
    with pytest.raises(ConfigError, match="name"):
        load_config(ws, global_path=gpath)


def test_the_same_name_twice_in_one_file_is_rejected(env):
    ws, gpath, write = env
    write(gpath, """\
        version: 1
        network:
          host_services:
            - {name: a, host: h.internal, port: 1}
            - {name: a, host: h.internal, port: 2}
        """)
    with pytest.raises(ConfigError, match="more than once"):
        load_config(ws, global_path=gpath)
