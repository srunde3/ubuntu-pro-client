import subprocess
from pathlib import Path

import behave_mcp.adapters as adapters_module
import pytest
from behave_mcp.adapters import (
    EnvConfig,
    InMemoryJobRegistry,
    LocalArtifactStore,
    SubprocessProcessLauncher,
)
from behave_mcp.ports import Job, LogFileOpenError, ProcessStartError

# ---- LocalArtifactStore ----


def test_discover_feature_files_sorted(tmp_path):
    (tmp_path / "features" / "cli").mkdir(parents=True)
    (tmp_path / "features" / "b.feature").write_text("", encoding="utf-8")
    (tmp_path / "features" / "cli" / "a.feature").write_text(
        "", encoding="utf-8"
    )
    (tmp_path / "features" / "notes.txt").write_text("", encoding="utf-8")

    store = LocalArtifactStore()

    assert store.discover_feature_files(tmp_path) == [
        "features/b.feature",
        "features/cli/a.feature",
    ]


def test_discover_feature_files_missing_dir(tmp_path):
    store = LocalArtifactStore()
    assert store.discover_feature_files(tmp_path) == []


def test_read_metadata_missing_or_invalid(tmp_path):
    store = LocalArtifactStore()
    assert store.read_metadata(tmp_path / "missing.json") == {}

    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    assert store.read_metadata(bad) == {}

    non_dict = tmp_path / "list.json"
    non_dict.write_text("[]", encoding="utf-8")
    assert store.read_metadata(non_dict) == {}


def test_write_and_read_metadata_roundtrip(tmp_path):
    store = LocalArtifactStore()
    path = tmp_path / "m.json"
    store.write_metadata(path, {"job_id": "x", "status": "started"})
    assert store.read_metadata(path) == {"job_id": "x", "status": "started"}


def test_tail_file_and_lines(tmp_path):
    store = LocalArtifactStore()
    log = tmp_path / "log.txt"
    log.write_text("a\nb\nc\n", encoding="utf-8")
    assert store.tail_file(log, 2) == "b\nc"
    assert store.tail_lines(log, 2) == ["b", "c"]


def test_tail_file_missing(tmp_path):
    store = LocalArtifactStore()
    assert store.tail_file(tmp_path / "nope.txt", 5) == "Waiting for output..."
    assert store.tail_lines(tmp_path / "nope.txt", 5) == []


def test_read_report_json(tmp_path):
    store = LocalArtifactStore()
    assert store.read_report_json(tmp_path / "missing.json") is None

    bad = tmp_path / "bad.json"
    bad.write_text("nope", encoding="utf-8")
    assert store.read_report_json(bad) is None

    obj = tmp_path / "obj.json"
    obj.write_text("{}", encoding="utf-8")
    assert store.read_report_json(obj) is None

    good = tmp_path / "good.json"
    good.write_text("[1, 2]", encoding="utf-8")
    assert store.read_report_json(good) == [1, 2]


# ---- EnvConfig ----


def _make_valid_repo(base: Path) -> Path:
    (base / "features").mkdir(parents=True)
    (base / "tox.ini").write_text("", encoding="utf-8")
    return base


def test_resolve_repo_root_override(tmp_path):
    repo = _make_valid_repo(tmp_path / "repo")
    config = EnvConfig()
    assert config.resolve_repo_root(str(repo)) == repo.resolve()


def test_resolve_repo_root_env(tmp_path, monkeypatch):
    repo = _make_valid_repo(tmp_path / "repo")
    monkeypatch.setenv("UBUNTU_PRO_CLIENT_REPO", str(repo))
    config = EnvConfig()
    assert config.resolve_repo_root(None) == repo.resolve()


def test_resolve_repo_root_invalid(tmp_path):
    invalid = tmp_path / "invalid"
    invalid.mkdir()
    config = EnvConfig()
    with pytest.raises(ValueError, match="Invalid repo_root"):
        config.resolve_repo_root(str(invalid))


def test_resolve_log_dir_env_and_default(tmp_path, monkeypatch):
    config = EnvConfig()

    custom = tmp_path / "logs"
    monkeypatch.setenv("MCP_LOG_DIR", str(custom))
    assert config.resolve_log_dir(tmp_path) == custom.resolve()
    assert custom.exists()

    monkeypatch.delenv("MCP_LOG_DIR", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    default = config.resolve_log_dir(repo)
    assert default == repo / ".mcp_behave_logs"
    assert default.exists()


def test_allow_cloud_toggle(monkeypatch):
    config = EnvConfig()
    monkeypatch.delenv("MCP_ALLOW_CLOUD_MACHINE_TYPES", raising=False)
    assert config.allow_cloud_machine_types() is False
    monkeypatch.setenv("MCP_ALLOW_CLOUD_MACHINE_TYPES", "yes")
    assert config.allow_cloud_machine_types() is True


def test_max_parallel_jobs(monkeypatch):
    config = EnvConfig()

    monkeypatch.delenv("MCP_MAX_PARALLEL_JOBS", raising=False)
    assert config.max_parallel_jobs() == (1, None)

    monkeypatch.setenv("MCP_MAX_PARALLEL_JOBS", "3")
    assert config.max_parallel_jobs() == (3, None)

    monkeypatch.setenv("MCP_MAX_PARALLEL_JOBS", "0")
    value, error = config.max_parallel_jobs()
    assert value is None
    assert "positive integer" in error

    monkeypatch.setenv("MCP_MAX_PARALLEL_JOBS", "abc")
    value, error = config.max_parallel_jobs()
    assert value is None
    assert "positive integer" in error


def test_subprocess_env_forwards_all(monkeypatch):
    config = EnvConfig()
    monkeypatch.setenv("MCP_TEST_PASSTHROUGH", "carried")
    env = config.subprocess_env()
    assert env["MCP_TEST_PASSTHROUGH"] == "carried"


def test_transport_default_and_override(monkeypatch):
    config = EnvConfig()
    monkeypatch.delenv("MCP_TRANSPORT", raising=False)
    assert config.transport() == "stdio"
    monkeypatch.setenv("MCP_TRANSPORT", "sse")
    assert config.transport() == "sse"


# ---- SubprocessProcessLauncher ----


class _FakeProcess:
    def __init__(self):
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True


def test_launcher_success(tmp_path, monkeypatch):
    calls = {}

    def fake_popen(command, cwd, env, stdout, stderr, text):
        calls["command"] = command
        calls["cwd"] = cwd
        calls["env"] = env
        calls["stdout"] = stdout
        calls["stderr"] = stderr
        calls["text"] = text
        return _FakeProcess()

    monkeypatch.setattr(adapters_module.subprocess, "Popen", fake_popen)
    launcher = SubprocessProcessLauncher()
    log_path = tmp_path / "out.log"

    handle = launcher.launch(["tox"], str(tmp_path), {"A": "B"}, log_path)

    assert calls["command"] == ["tox"]
    assert calls["cwd"] == str(tmp_path)
    assert calls["env"] == {"A": "B"}
    assert calls["stderr"] == subprocess.STDOUT
    assert calls["text"] is True
    assert handle.poll() is None

    handle.close()
    assert calls["stdout"].closed is True
    handle.terminate()


def test_launcher_log_open_failure(tmp_path):
    launcher = SubprocessProcessLauncher()
    missing_dir = tmp_path / "missing" / "out.log"
    with pytest.raises(LogFileOpenError):
        launcher.launch(["tox"], str(tmp_path), {}, missing_dir)


def test_launcher_process_start_failure(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("no exec")

    monkeypatch.setattr(adapters_module.subprocess, "Popen", boom)
    launcher = SubprocessProcessLauncher()
    log_path = tmp_path / "out.log"

    with pytest.raises(ProcessStartError):
        launcher.launch(["tox"], str(tmp_path), {}, log_path)

    assert log_path.exists()


# ---- InMemoryJobRegistry ----


class _Handle:
    def __init__(self, returncode):
        self._returncode = returncode

    def poll(self):
        return self._returncode

    def close(self):
        pass

    def terminate(self):
        pass


def _job(job_id, tmp_path, handle=None, reserved=False) -> Job:
    return Job(
        job_id=job_id,
        process_handle=handle,
        stdout_log=tmp_path / f"{job_id}.log",
        json_report=tmp_path / f"{job_id}.json",
        metadata=tmp_path / f"{job_id}_meta.json",
        reserved=reserved,
    )


def test_registry_try_reserve_respects_capacity(tmp_path):
    registry = InMemoryJobRegistry()

    first = registry.try_reserve(_job("a", tmp_path, reserved=True), 1)
    assert first.reserved is True
    assert first.running_jobs == 0

    second = registry.try_reserve(_job("b", tmp_path, reserved=True), 1)
    assert second.reserved is False
    assert second.running_jobs == 1
    assert second.max_parallel == 1


def test_registry_counts_running_processes(tmp_path):
    registry = InMemoryJobRegistry()
    registry.register("a", _job("a", tmp_path, handle=_Handle(None)))

    result = registry.try_reserve(_job("b", tmp_path, reserved=True), 1)
    assert result.reserved is False
    assert result.running_jobs == 1


def test_registry_ignores_completed_processes(tmp_path):
    registry = InMemoryJobRegistry()
    registry.register("a", _job("a", tmp_path, handle=_Handle(0)))

    result = registry.try_reserve(_job("b", tmp_path, reserved=True), 1)
    assert result.reserved is True
    assert result.running_jobs == 0


def test_registry_release_and_clear(tmp_path):
    registry = InMemoryJobRegistry()
    registry.register("a", _job("a", tmp_path))
    assert registry.get("a") is not None

    registry.release("a")
    assert registry.get("a") is None

    registry.register("b", _job("b", tmp_path))
    registry.clear()
    assert registry.get("b") is None
