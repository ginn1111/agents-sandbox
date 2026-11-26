"""Seam 1: the image side of the Host Browser. The real `asb-chrome-mcp` wrapper against stubbed
`getent`, `curl` and `chrome-devtools-mcp`, and what the agent Dockerfile installs. No Docker.

What a real Chrome then accepts (Host header, loopback bind) is the Seam 2 test and
docs/spikes/host-browser.md (UNVERIFIED). Stubs are scripts, so these skip without an exec-able dir.
"""
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest

from agent_sandbox import host_browser
from test_netguard_entrypoint import EXEC_DIR  # tests/ is on sys.path (no package)

IMAGE = Path(__file__).parents[1] / "src" / "agent_sandbox" / "images" / "agent"
WRAPPER = IMAGE / host_browser.MCP_COMMAND
DOCKERFILE = IMAGE / "Dockerfile"

STUBS = {
    "getent": """#!/bin/sh
[ "$1" = ahostsv4 ] || exit 2
case "$2" in
    host.docker.internal) [ -z "$STUB_NO_HOST" ] || exit 2; echo "${STUB_HOST_ADDR:-172.17.0.1}  STREAM $2" ;;
    *) exit 2 ;;
esac
""",
    # prints what it was asked, including the Host header curl would send (curl sends the URL's host)
    "curl": """#!/bin/sh
printf '%s\\n' "$*" >> "$STUB_DIR/curl"
[ -z "$STUB_CURL_FAILS" ] || exit 7
echo '{"Browser": "Chrome/130.0.0.0", "webSocketDebuggerUrl": "ws://x/devtools/browser/abc"}'
""",
    "chrome-devtools-mcp": """#!/bin/sh
printf '%s\\n' "$*" > "$STUB_DIR/mcp"
""",
}

needs_exec = pytest.mark.skipif(EXEC_DIR is None, reason="no directory that allows executing stub scripts")


@pytest.fixture
def env(tmp_path):
    bin_dir = Path(tempfile.mkdtemp(dir=EXEC_DIR, prefix="asb-stubs-")) if EXEC_DIR else tmp_path
    for name, body in STUBS.items():
        f = bin_dir / name
        f.write_text(body)
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    yield {"PATH": f"{bin_dir}:/usr/bin:/bin", "STUB_DIR": str(tmp_path)}
    if EXEC_DIR:
        shutil.rmtree(bin_dir, ignore_errors=True)


def wrapper(env, *args, **extra):
    return subprocess.run(["sh", str(WRAPPER), *args], env={**env, **extra}, capture_output=True, text=True)


def logged(env, name):
    f = Path(env["STUB_DIR"]) / name
    return f.read_text().strip() if f.exists() else None


@needs_exec
def test_the_mcp_connects_to_the_gateway_by_ip_address_never_by_name(env):
    r = wrapper(env)
    assert r.returncode == 0, r.stderr
    assert logged(env, "mcp") == "--browser-url=http://172.17.0.1:9222"  # an IP: Chrome checks the Host header


@needs_exec
def test_extra_arguments_reach_the_mcp(env):
    assert wrapper(env, "--headless=false").returncode == 0
    assert logged(env, "mcp") == "--browser-url=http://172.17.0.1:9222 --headless=false"


@needs_exec
def test_probe_asks_chrome_for_its_version_by_ip_and_does_not_start_the_mcp(env):
    r = wrapper(env, "--probe")
    assert r.returncode == 0 and "Chrome/130" in r.stdout
    assert "http://172.17.0.1:9222/json/version" in logged(env, "curl")
    assert logged(env, "mcp") is None


@needs_exec
def test_probe_fails_when_chrome_does_not_answer(env):
    r = wrapper(env, "--probe", STUB_CURL_FAILS="1")
    assert r.returncode != 0 and "does not answer" in r.stderr and "172.17.0.1:9222" in r.stderr


@needs_exec
def test_it_fails_closed_when_the_host_name_does_not_resolve(env):
    r = wrapper(env, STUB_NO_HOST="1")
    assert r.returncode != 0 and "cannot resolve" in r.stderr and logged(env, "mcp") is None


@needs_exec
@pytest.mark.parametrize("addr", ["127.0.0.1", "0.0.0.0", "169.254.169.254", "224.0.0.1", "999.1.1.1"])
def test_it_refuses_an_address_that_is_not_the_host_gateway(env, addr):
    r = wrapper(env, STUB_HOST_ADDR=addr)
    assert r.returncode != 0 and logged(env, "mcp") is None


@needs_exec
def test_it_never_takes_the_target_from_its_arguments_or_environment(env):
    r = wrapper(env, "--probe", "http://evil.example:9222", HOST_BROWSER_URL="http://evil.example")
    assert "evil" not in (logged(env, "curl") or "")
    assert r.returncode == 0


# -- the image ----------------------------------------------------------------------------------------


def test_the_dockerfile_installs_chrome_devtools_mcp_and_the_wrapper_but_no_chrome():
    text = DOCKERFILE.read_text()
    assert "chrome-devtools-mcp" in text
    assert f"COPY {host_browser.MCP_COMMAND} " in text and f"/usr/local/bin/{host_browser.MCP_COMMAND}" in text
    lowered = text.lower()
    for chrome in ("google-chrome", "chromium", "chrome-stable", "--no-sandbox"):
        assert chrome not in lowered.replace("chrome-devtools-mcp", "")  # ADR-0008: Chrome stays on the host


def test_the_mcp_package_is_installed_with_the_other_npm_tools_on_the_private_node():
    text = DOCKERFILE.read_text()
    npm = text[text.index("npm install -g"):text.index("wrap-node-bins /opt/agents/bin")]
    assert "chrome-devtools-mcp" in npm  # same prefix, so wrap-node-bins pins its node shebang


def test_the_wrapper_has_no_host_or_port_input_other_than_the_constant():
    text = WRAPPER.read_text()
    assert "host.docker.internal" in text and "9222" in text
    assert "HOST_BROWSER" not in text and "$HOST" not in text and "${2" not in text


def test_chrome_devtools_mcp_is_pinned_to_one_exact_version():
    # The wrapper relies on --browser-url; an unpinned `latest` could change that flag under us.
    import re
    text = DOCKERFILE.read_text()
    m = re.search(r"^ARG CHROME_DEVTOOLS_MCP_VERSION=(\d+\.\d+\.\d+)$", text, re.M)
    assert m, "chrome-devtools-mcp must be pinned with ARG CHROME_DEVTOOLS_MCP_VERSION=<x.y.z>"
    npm = text[text.index("npm install -g"):text.index("wrap-node-bins /opt/agents/bin")]
    assert "chrome-devtools-mcp@${CHROME_DEVTOOLS_MCP_VERSION}" in npm
    assert re.search(r"chrome-devtools-mcp(?!@)\s", npm) is None  # no unpinned copy alongside
