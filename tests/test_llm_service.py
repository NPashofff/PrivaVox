"""flow.llm_service: who owns the Ollama server, and who may stop it.

The ownership rule is the whole feature — an Ollama that was already up when
we booted must survive our quit — so that is what these lock down. No real
`ollama serve` is spawned; Popen and the /api/version probe are faked.
"""
import os
import subprocess

import pytest

from flow import llm_service
from flow.config import FlowConfig


class FakePopen:
    """Just enough Popen: alive until terminate/kill, records the signals."""

    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        self.pid = 4242
        self.returncode = None
        self.signals: list[str] = []

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired(self.argv, timeout)
        return self.returncode

    def terminate(self):
        self.signals.append("term")
        self.returncode = -15

    def kill(self):
        self.signals.append("kill")
        self.returncode = -9


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """No leaked ownership between tests, and never a real ollama binary."""
    monkeypatch.setattr(llm_service, "_proc", None)
    monkeypatch.setattr(llm_service, "find_binary", lambda: "/opt/homebrew/bin/ollama")
    monkeypatch.setattr(llm_service.atexit, "register", lambda *a, **k: None)
    yield
    monkeypatch.setattr(llm_service, "_proc", None)


def _spawns(monkeypatch, up_after: int):
    """Fake Popen + an is_up() that flips to True after `up_after` probes.

    Returns the list the spawned FakePopens land in (empty = nothing spawned).
    """
    spawned: list[FakePopen] = []
    probes = {"n": 0}

    def fake_popen(argv, **kwargs):
        p = FakePopen(argv, **kwargs)
        spawned.append(p)
        return p

    def fake_is_up(config, timeout=1.5):
        probes["n"] += 1
        return probes["n"] > up_after

    monkeypatch.setattr(llm_service.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(llm_service, "is_up", fake_is_up)
    monkeypatch.setattr(llm_service.time, "sleep", lambda _s: None)
    return spawned


def test_base_url_strips_the_openai_path():
    config = FlowConfig()
    assert llm_service.base_url(config) == "http://localhost:11434"


def test_running_ollama_is_not_ours_and_survives_stop(monkeypatch):
    spawned = _spawns(monkeypatch, up_after=0)  # up on the very first probe

    assert llm_service.ensure_running(FlowConfig()) is False
    assert spawned == [], "must not spawn a second server"
    assert llm_service.owns_server() is False

    llm_service.stop()  # the brew service keeps running — nothing to signal


def test_we_start_and_stop_our_own_server(monkeypatch):
    spawned = _spawns(monkeypatch, up_after=1)  # down at boot, up next probe
    killed: list[int] = []
    monkeypatch.setattr(llm_service.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(llm_service.os, "killpg",
                        lambda pgid, sig: (killed.append(pgid),
                                           setattr(spawned[0], "returncode", -15)))

    assert llm_service.ensure_running(FlowConfig()) is True
    assert spawned[0].argv == ["/opt/homebrew/bin/ollama", "serve"]
    # own session, so stop() can signal the runners ollama spawns too
    assert spawned[0].kwargs["start_new_session"] is True
    assert llm_service.owns_server() is True

    llm_service.stop()
    assert killed == [4242]
    assert llm_service.owns_server() is False


def test_stop_escalates_to_kill_when_term_is_ignored(monkeypatch):
    spawned = _spawns(monkeypatch, up_after=1)
    monkeypatch.setattr(llm_service.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(llm_service.os, "killpg", lambda pgid, sig: None)  # ignored

    llm_service.ensure_running(FlowConfig())
    llm_service.stop(timeout=0.01)
    assert spawned[0].signals == ["kill"]


def test_disabled_setting_leaves_ollama_alone(monkeypatch):
    spawned = _spawns(monkeypatch, up_after=99)  # nothing is running
    config = FlowConfig()
    config.manage_ollama = False

    assert llm_service.ensure_running(config) is False
    assert spawned == []


def test_disown_keeps_the_server_running_past_quit(monkeypatch):
    spawned = _spawns(monkeypatch, up_after=1)
    monkeypatch.setattr(llm_service.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(llm_service.os, "killpg",
                        lambda pgid, sig: pytest.fail("disowned server must not be signalled"))

    llm_service.ensure_running(FlowConfig())
    llm_service.disown()

    assert llm_service.owns_server() is False
    llm_service.stop()
    assert spawned[0].signals == []


def test_missing_binary_is_reported_not_raised(monkeypatch):
    _spawns(monkeypatch, up_after=99)
    monkeypatch.setattr(llm_service, "find_binary", lambda: None)

    assert llm_service.ensure_running(FlowConfig()) is False


def test_server_dying_at_startup_clears_ownership(monkeypatch):
    spawned = _spawns(monkeypatch, up_after=99)  # never comes up

    def fake_popen(argv, **kwargs):
        p = FakePopen(argv, **kwargs)
        p.returncode = 1  # exited immediately (port taken, bad install)
        spawned.append(p)
        return p

    monkeypatch.setattr(llm_service.subprocess, "Popen", fake_popen)

    assert llm_service.ensure_running(FlowConfig()) is False
    assert llm_service.owns_server() is False


def test_settings_round_trip_the_toggle(tmp_path):
    from flow.app import load_settings, save_settings

    path = str(tmp_path / "settings.json")
    config = FlowConfig()
    config.manage_ollama = False
    save_settings(config, path)

    loaded = FlowConfig()
    assert loaded.manage_ollama is True  # default: we own what we start
    load_settings(loaded, path)
    assert loaded.manage_ollama is False


# --------------------------------------------------------------------------
# Windows branches (exercised here on macOS: the flags and the kill call are
# what differ, and both are pure argument shaping)
# --------------------------------------------------------------------------

@pytest.fixture
def _as_windows(monkeypatch):
    monkeypatch.setattr(llm_service, "IS_WINDOWS", True)
    # constants that only exist in the win32 build of subprocess
    monkeypatch.setattr(llm_service.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200,
                        raising=False)
    monkeypatch.setattr(llm_service.subprocess, "CREATE_NO_WINDOW", 0x8000000,
                        raising=False)


def test_windows_spawns_without_a_console_and_in_its_own_group(monkeypatch, _as_windows):
    spawned = _spawns(monkeypatch, up_after=1)

    assert llm_service.ensure_running(FlowConfig()) is True
    kwargs = spawned[0].kwargs
    assert "start_new_session" not in kwargs, "POSIX-only; win32 ignores it silently"
    assert kwargs["creationflags"] == 0x200 | 0x8000000


def test_windows_stop_walks_the_process_tree(monkeypatch, _as_windows):
    spawned = _spawns(monkeypatch, up_after=1)
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        spawned[0].returncode = 1

    monkeypatch.setattr(llm_service.subprocess, "run", fake_run)
    monkeypatch.setattr(llm_service.os, "killpg",
                        lambda *a: pytest.fail("killpg does not exist on Windows"))

    llm_service.ensure_running(FlowConfig())
    llm_service.stop()
    # /T is the point: ollama's model runners are children of the server
    assert calls == [["taskkill", "/PID", "4242", "/T", "/F"]]


def test_windows_looks_for_ollama_under_localappdata(monkeypatch, _as_windows):
    # winget installs ollama per-user; the separator is the host's (this test
    # runs on macOS, so join() yields "/" — on Windows it yields "\\").
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\niki\AppData\Local")
    assert llm_service._bin_fallbacks() == (
        os.path.join(r"C:\Users\niki\AppData\Local", "Programs", "Ollama", "ollama.exe"),)
    monkeypatch.delenv("LOCALAPPDATA")
    assert llm_service._bin_fallbacks() == (), "no LOCALAPPDATA → no guesses"
