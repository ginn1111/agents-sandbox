import json
import sys
from pathlib import Path

import pytest

FAKE = Path(__file__).with_name("fake_docker.py")


@pytest.fixture(autouse=True)
def isolated_user_env(tmp_path, monkeypatch):
    """Never read the developer's real home or global config."""
    home = tmp_path / "home" / "user"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    return home


@pytest.fixture
def home(isolated_user_env):
    return isolated_user_env


@pytest.fixture
def workspace(home):
    ws = home / "work" / "my api"  # a space on purpose
    ws.mkdir(parents=True)
    return ws


class FakeDocker:
    def __init__(self, state_file: Path):
        self.state_file = state_file
        self.command = [sys.executable, str(FAKE)]

    def _state(self):
        if self.state_file.exists():
            return json.loads(self.state_file.read_text())
        return {"calls": [], "containers": {}, "images": [], "daemon": True}

    def _write(self, state):
        self.state_file.write_text(json.dumps(state))

    @property
    def calls(self):
        return self._state()["calls"]

    @property
    def containers(self):
        return self._state()["containers"]

    def has_image(self, *names):
        """Make the images exist: the agent and Netguard images by default."""
        s = self._state()
        s["images"] += list(names) or ["agent-sandbox-agent:latest", "agent-sandbox-netguard:latest"]
        self._write(s)

    def set(self, **flags):
        """netguard_ready=False, netguard_exits=True, fail_run=[container names to refuse]: how the fake behaves."""
        s = self._state()
        s.update(flags)
        self._write(s)

    def kill(self, name):
        """The container stops but stays known to `ps -a`, like a killed Netguard."""
        s = self._state()
        s["containers"][name]["exited"] = True
        self._write(s)

    def set(self, **knobs):
        """Failure injection for the fake: fail_run, drop_mounts, writable_mounts, wrong_source_mounts."""
        s = self._state()
        s.update(knobs)
        self._write(s)

    def stop_daemon(self):
        s = self._state()
        s["daemon"] = False
        self._write(s)


@pytest.fixture
def docker(tmp_path, monkeypatch):
    state = tmp_path / "docker-state.json"
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(state))
    return FakeDocker(state)
