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

    def has_image(self, name="agent-sandbox-agent:latest"):
        s = self._state()
        s["images"].append(name)
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
