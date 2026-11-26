"""Seam 2: the Host Browser on a real Docker daemon (ADR-0008, ADR-0013, ADR-0021). Skipped without one.

UNVERIFIED: written on a host with no Docker daemon, so none of this has run. The assumptions behind it
are in docs/spikes/host-browser.md. Run `uv run python -m pytest -m docker tests/test_e2e_host_browser.py`
on Linux Docker and on Colima, then replace each **Result:** line in the spike.

The host side is a stand-in for Chrome's DevTools HTTP endpoint that, like Chrome, answers only when the
Host header is `localhost` or an IP address. It listens on 0.0.0.0, which is what the relay on the Docker
gateway amounts to. The test with a real Chrome runs only when `google-chrome` and `socat` are on the host.
"""
import http.server
import json
import shutil
import socket
import subprocess
import threading

import pytest

from agent_sandbox import host_browser
from agent_sandbox.cli import main
from agent_sandbox.plan import session_name
from agent_sandbox.runtime import Docker

pytestmark = pytest.mark.docker


def _docker_ok():
    return bool(shutil.which("docker")) and subprocess.run(["docker", "info"], capture_output=True).returncode == 0


if not _docker_ok():
    pytest.skip("no reachable Docker daemon", allow_module_level=True)


def sh(name, script):
    return subprocess.run(["docker", "exec", name, "sh", "-c", script], capture_output=True, text=True)


def reaches(name, host, port, seconds=3):
    script = f"timeout {seconds} bash -c 'exec 3<>/dev/tcp/{host}/{port}'"
    return sh(name, script).returncode == 0


def probe(name):
    return subprocess.run(["docker", "exec", name, host_browser.MCP_COMMAND, "--probe"], capture_output=True, text=True)


class FakeChrome(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if not (host == "localhost" or host.replace(".", "").isdigit()):  # Chrome's own rule
            self.send_error(500, "Host header is specified and is not an IP address or localhost")
            return
        body = json.dumps({"Browser": "FakeChrome/1", "webSocketDebuggerUrl": f"ws://{host}:9222/devtools/browser/x"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def fake_chrome():
    """Listens on host port 9222 on every interface (what the gateway relay amounts to); skips if taken."""
    try:
        server = http.server.ThreadingHTTPServer(("0.0.0.0", host_browser.PORT), FakeChrome)
    except OSError:
        pytest.skip(f"host port {host_browser.PORT} is in use (a real Chrome?)")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield
    server.shutdown()
    server.server_close()


@pytest.fixture
def other_host_port():
    server = socket.socket()
    server.bind(("0.0.0.0", 0))
    server.listen(16)
    yield server.getsockname()[1]
    server.close()


@pytest.fixture
def browser_session(workspace, home):
    cfg = home / ".config" / "agent-sandbox" / "config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("version: 1\nnetwork:\n  host_browser: true\n")
    assert main(["build"]) == 0
    assert main(["up", str(workspace)]) == 0
    yield session_name(workspace.resolve())
    main(["down", str(workspace)])


def test_the_host_browser_port_is_closed_until_it_is_enabled(browser_session, fake_chrome):
    assert not reaches(browser_session, "host.docker.internal", host_browser.PORT)  # `up` opens nothing (ADR-0013)
    assert probe(browser_session).returncode != 0


def test_enabling_the_host_browser_opens_9222_and_no_other_host_port(
        browser_session, workspace, fake_chrome, other_host_port):
    assert main(["browser", "enable", str(workspace)]) == 0
    assert reaches(browser_session, "host.docker.internal", host_browser.PORT)
    assert not reaches(browser_session, "host.docker.internal", other_host_port)


def test_disable_closes_the_host_browser_again(browser_session, workspace, fake_chrome):
    assert main(["browser", "enable", str(workspace)]) == 0
    assert main(["browser", "disable", str(workspace)]) == 0
    assert not reaches(browser_session, "host.docker.internal", host_browser.PORT)


def test_the_wrapper_probe_gets_chrome_s_version_by_ip(browser_session, workspace, fake_chrome):
    assert main(["browser", "enable", str(workspace)]) == 0
    r = probe(browser_session)
    assert r.returncode == 0 and "FakeChrome" in r.stdout, r.stderr
    # by name the Host header check refuses: the reason the wrapper connects by IP (spike item 2)
    by_name = f"curl -s -o /dev/null -w '%{{http_code}}' http://host.docker.internal:{host_browser.PORT}/json/version"
    assert sh(browser_session, by_name).stdout.strip() == "500"


def test_the_session_cannot_open_the_host_browser_itself(browser_session, fake_chrome):
    assert sh(browser_session, f"iptables -A NG_SERVICES -p tcp --dport {host_browser.PORT} -j ACCEPT").returncode != 0
    assert not reaches(browser_session, "host.docker.internal", host_browser.PORT)


def test_the_image_has_chrome_devtools_mcp_and_no_chrome(browser_session):
    assert sh(browser_session, "command -v chrome-devtools-mcp").returncode == 0
    for chrome in ("google-chrome", "chromium", "chrome"):
        assert sh(browser_session, f"command -v {chrome}").returncode != 0


def test_doctor_reports_that_chrome_answers_from_the_session(browser_session, workspace, fake_chrome, capsys):
    assert main(["browser", "enable", str(workspace)]) == 0
    capsys.readouterr()
    main(["doctor", str(workspace)])
    assert "Chrome answers from the Session" in capsys.readouterr().out


@pytest.mark.skipif(not (shutil.which("google-chrome") and shutil.which("socat")),
                    reason="needs google-chrome and socat on the host")
def test_an_agent_can_open_a_page_in_host_chrome_through_the_mcp(browser_session, workspace, tmp_path):
    """Real Chrome and the gateway relay, started here the way `asb doctor` tells the human to (the CLI
    itself never starts either). Speaks MCP over stdio to chrome-devtools-mcp and asks it to open a page.
    """
    relay_argv = host_browser.relay_command(host_browser.bridge_gateway(Docker()))
    assert relay_argv, "no usable Docker gateway address"
    chrome = subprocess.Popen(["google-chrome", "--headless=new", f"--remote-debugging-port={host_browser.PORT}",
                               f"--user-data-dir={tmp_path / 'profile'}", "--no-first-run", "about:blank"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    relay = subprocess.Popen(relay_argv.split(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert main(["browser", "enable", str(workspace)]) == 0
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "asb-test", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                "name": "new_page", "arguments": {"url": "about:blank"}}},
        ]
        r = subprocess.run(["docker", "exec", "-i", browser_session, host_browser.MCP_COMMAND],
                           input="\n".join(json.dumps(m) for m in msgs) + "\n", capture_output=True, text=True, timeout=60)
        out = r.stdout.replace(" ", "")
        assert '"id":2' in out and '"isError":true' not in out, r.stdout + r.stderr
    finally:
        relay.terminate()
        chrome.terminate()
