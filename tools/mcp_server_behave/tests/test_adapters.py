import subprocess

import pytest
from conftest import make_repo_with_feature

import behave_mcp.adapters as adapters_module
from behave_mcp.adapters import (
    InMemoryJobRegistry,
    LocalArtifactStore,
    LocalFeatureFileReader,
    LocalWorkspace,
    PopenLauncher,
)
from behave_mcp.ports import Job, LogFileOpenError, ProcessStartError

# ---- LocalFeatureFileReader ----


def test_discover_feature_files_delegates(tmp_path):
    (tmp_path / "features").mkdir(parents=True)
    (tmp_path / "features" / "sample.feature").write_text("", encoding="utf-8")

    reader = LocalFeatureFileReader()

    assert reader.discover_feature_files(tmp_path) == [
        "features/sample.feature"
    ]


_SAMPLE_FEATURE = """\
@uses.config.contract_token
Feature: Sample feature

  Scenario Outline: Attach on a machine
    Given a `<release>` `<machine_type>` machine with \
ubuntu-advantage-tools installed
    When I attach

    Examples: ubuntu release
      | release  | machine_type  |
      | jammy    | lxd-container |
      | resolute | lxd-vm        |
"""


def test_discover_feature_details_delegates(tmp_path):
    (tmp_path / "features").mkdir(parents=True)
    (tmp_path / "features" / "sample.feature").write_text(
        _SAMPLE_FEATURE, encoding="utf-8"
    )

    reader = LocalFeatureFileReader()
    details = reader.discover_feature_details(tmp_path)

    assert len(details) == 1
    assert details[0].path == "features/sample.feature"
    assert details[0].title == "Sample feature"


# ---- LocalArtifactStore ----


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


# ---- LocalWorkspace ----


def test_resolve_repo_root_override(tmp_path):
    repo = make_repo_with_feature(tmp_path, rel=None)
    workspace = LocalWorkspace()
    assert workspace.resolve_repo_root(str(repo)) == repo.resolve()


def test_resolve_repo_root_env(tmp_path, monkeypatch):
    repo = make_repo_with_feature(tmp_path, rel=None)
    monkeypatch.setenv("UBUNTU_PRO_CLIENT_REPO", str(repo))
    workspace = LocalWorkspace()
    assert workspace.resolve_repo_root(None) == repo.resolve()


def test_resolve_repo_root_invalid(tmp_path):
    invalid = tmp_path / "invalid"
    invalid.mkdir()
    workspace = LocalWorkspace()
    with pytest.raises(ValueError, match="Invalid repo_root"):
        workspace.resolve_repo_root(str(invalid))


def test_detect_repo_root_walks_up_to_features_and_tox(tmp_path):
    repo = make_repo_with_feature(tmp_path, rel=None)
    nested = repo / "tools" / "mcp_server_behave" / "behave_mcp"
    nested.mkdir(parents=True)
    workspace = LocalWorkspace()
    assert (
        workspace._detect_repo_root(nested / "adapters.py") == repo.resolve()
    )


def test_detect_repo_root_returns_none_outside_a_checkout(tmp_path):
    isolated = tmp_path / "isolated" / "site-packages" / "behave_mcp"
    isolated.mkdir(parents=True)
    workspace = LocalWorkspace()
    assert workspace._detect_repo_root(isolated / "adapters.py") is None


def test_resolve_repo_root_raises_clear_error_when_undetectable(monkeypatch):
    workspace = LocalWorkspace()
    monkeypatch.setattr(workspace, "_detect_repo_root", lambda start: None)
    with pytest.raises(ValueError, match="UBUNTU_PRO_CLIENT_REPO"):
        workspace.resolve_repo_root(None)


def test_resolve_log_dir_env_and_default(tmp_path, monkeypatch):
    workspace = LocalWorkspace()

    custom = tmp_path / "logs"
    monkeypatch.setenv("MCP_LOG_DIR", str(custom))
    assert workspace.resolve_log_dir(tmp_path) == custom.resolve()
    assert custom.exists()

    monkeypatch.delenv("MCP_LOG_DIR", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    default = workspace.resolve_log_dir(repo)
    assert default == repo / ".mcp_behave_logs"
    assert default.exists()


def test_subprocess_env_forwards_all(monkeypatch):
    workspace = LocalWorkspace()
    monkeypatch.setenv("MCP_TEST_PASSTHROUGH", "carried")
    env = workspace.subprocess_env()
    assert env["MCP_TEST_PASSTHROUGH"] == "carried"


# ---- PopenLauncher ----


class _FakeProcess:
    def __init__(self):
        self.returncode = None
        self.terminated = False
        self.pid = 4321

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
    launcher = PopenLauncher()
    log_path = tmp_path / "out.log"

    handle = launcher.launch(["tox"], str(tmp_path), {"A": "B"}, log_path)

    assert calls["command"] == ["tox"]
    assert calls["cwd"] == str(tmp_path)
    assert calls["env"] == {"A": "B"}
    assert calls["stderr"] == subprocess.STDOUT
    assert calls["text"] is True
    assert handle.poll() is None
    assert handle.pid == 4321

    handle.close()
    assert calls["stdout"].closed is True
    handle.terminate()


def test_launcher_log_open_failure(tmp_path):
    launcher = PopenLauncher()
    missing_dir = tmp_path / "missing" / "out.log"
    with pytest.raises(LogFileOpenError):
        launcher.launch(["tox"], str(tmp_path), {}, missing_dir)


def test_launcher_process_start_failure(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("no exec")

    monkeypatch.setattr(adapters_module.subprocess, "Popen", boom)
    launcher = PopenLauncher()
    log_path = tmp_path / "out.log"

    with pytest.raises(ProcessStartError):
        launcher.launch(["tox"], str(tmp_path), {}, log_path)

    assert log_path.exists()


def test_is_pid_alive_true_then_false_after_exit():
    launcher = PopenLauncher()
    process = subprocess.Popen(["sleep", "5"])
    try:
        assert launcher.is_pid_alive(process.pid) is True
    finally:
        process.terminate()
        process.wait(timeout=5)
    assert launcher.is_pid_alive(process.pid) is False


def test_is_pid_alive_false_for_nonexistent_pid():
    launcher = PopenLauncher()
    # A PID this large should never be in use on Linux.
    assert launcher.is_pid_alive(2**30) is False


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


def test_registry_snapshot_returns_all_tracked_jobs(tmp_path):
    registry = InMemoryJobRegistry()
    registry.register("a", _job("a", tmp_path))
    registry.register("b", _job("b", tmp_path))

    snapshot = registry.snapshot()

    assert {job.job_id for job in snapshot} == {"a", "b"}


# ---- LocalArtifactStore.list_job_ids ----


def test_list_job_ids_globs_meta_files(tmp_path):
    store = LocalArtifactStore()
    (tmp_path / "job1_meta.json").write_text("{}", encoding="utf-8")
    (tmp_path / "job2_meta.json").write_text("{}", encoding="utf-8")
    (tmp_path / "job2_stdout.log").write_text("", encoding="utf-8")

    assert store.list_job_ids(tmp_path) == ["job1", "job2"]


def test_list_job_ids_missing_dir(tmp_path):
    store = LocalArtifactStore()
    assert store.list_job_ids(tmp_path / "missing") == []
