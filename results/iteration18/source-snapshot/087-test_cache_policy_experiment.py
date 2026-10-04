"""Pure guard and ownership checks: no model or server processes are started."""
from types import SimpleNamespace
import signal
import subprocess

import pytest

from scripts.cache_policy_experiment import flag, require_free, sha, stop, verify_hashes


def test_arm_changes_only_cache_entry_count():
    command = ["python", "-m", "mlx_lm", "server", "--prompt-cache-size", "1",
               "--prompt-cache-bytes", "268435456"]
    changed = flag(command, "--prompt-cache-size", 2)
    assert changed == command[:5] + ["2"] + command[6:]
    assert command[5] == "1"
    assert flag(changed, "--prompt-cache-bytes") == "268435456"


@pytest.mark.parametrize("command", [[], ["--model"], ["--model", "a", "--model", "b"]])
def test_ambiguous_launch_arguments_are_rejected(command):
    with pytest.raises(ValueError):
        flag(command, "--model")


def test_frozen_source_or_weight_change_is_rejected(tmp_path):
    path = tmp_path / "model.safetensors"
    path.write_bytes(b"original")
    expected = {str(path): sha(path)}
    verify_hashes(expected)
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="frozen file changed"):
        verify_hashes(expected)


def test_occupied_endpoint_refusal_never_signals_a_process(monkeypatch):
    class Probe:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def setsockopt(self, *args): pass
        def bind(self, address): raise OSError("address already in use")
    monkeypatch.setattr("scripts.cache_policy_experiment.socket.socket", Probe)
    monkeypatch.setattr("scripts.cache_policy_experiment.os.killpg", lambda *args: pytest.fail("must not kill listener"))
    with pytest.raises(OSError, match="already in use"):
        require_free("127.0.0.1", 8081)


def test_cleanup_escalates_only_its_owned_process_group(monkeypatch):
    signals = []
    process = SimpleNamespace(pid=4321, returncode=None, poll=lambda: None)
    def wait(timeout):
        if len(signals) == 1:
            raise subprocess.TimeoutExpired("owned child", timeout)
        process.returncode = -signal.SIGKILL
    process.wait = wait
    monkeypatch.setattr("scripts.cache_policy_experiment.os.killpg", lambda pid, sig: signals.append((pid, sig)))
    assert stop(process) == {"pid": 4321, "exit_code": -signal.SIGKILL, "forced_kill": True}
    assert signals == [(4321, signal.SIGTERM), (4321, signal.SIGKILL)]


def test_already_exited_child_is_not_signalled(monkeypatch):
    monkeypatch.setattr("scripts.cache_policy_experiment.os.killpg", lambda *args: pytest.fail("already exited"))
    process = SimpleNamespace(pid=4321, returncode=1, poll=lambda: 1)
    assert stop(process) == {"pid": 4321, "exit_code": 1, "forced_kill": False}
