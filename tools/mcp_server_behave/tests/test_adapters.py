import subprocess

import pytest
from conftest import FakeProcessHandle, make_repo_with_feature

import behave_mcp.adapters as adapters_module
from behave_mcp.adapters import (
    InMemoryJobRegistry,
    LocalFeatureFileReader,
    LocalJobResultStoreFactory,
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


# ---- LocalJobResultStore ----


def _store(tmp_path):
    return LocalJobResultStoreFactory().bind(tmp_path)


def test_read_metadata_missing_or_invalid(tmp_path):
    store = _store(tmp_path)
    assert store.read_metadata("missing") == {}

    (tmp_path / "bad_meta.json").write_text("not json", encoding="utf-8")
    assert store.read_metadata("bad") == {}

    (tmp_path / "list_meta.json").write_text("[]", encoding="utf-8")
    assert store.read_metadata("list") == {}


def test_write_and_read_metadata_roundtrip(tmp_path):
    store = _store(tmp_path)
    store.write_metadata("jobx", {"job_id": "x", "status": "started"})
    assert store.read_metadata("jobx") == {"job_id": "x", "status": "started"}


def test_log_tail_and_lines(tmp_path):
    store = _store(tmp_path)
    (tmp_path / "jobx_stdout.log").write_text("a\nb\nc\n", encoding="utf-8")
    assert store.log_tail("jobx", 2) == "b\nc"
    assert store.log_tail_lines("jobx", 2) == ["b", "c"]


def test_log_tail_missing(tmp_path):
    store = _store(tmp_path)
    assert store.log_tail("nope", 5) == "Waiting for output..."
    assert store.log_tail_lines("nope", 5) == []


def test_read_report_missing_or_invalid(tmp_path):
    store = _store(tmp_path)
    assert store.read_report("missing") is None

    (tmp_path / "bad_report.json").write_text("nope", encoding="utf-8")
    assert store.read_report("bad") is None

    (tmp_path / "obj_report.json").write_text("{}", encoding="utf-8")
    assert store.read_report("obj") is None

    (tmp_path / "good_report.json").write_text("[1, 2]", encoding="utf-8")
    assert store.read_report("good") == [1, 2]


def test_exists_and_list_job_ids(tmp_path):
    store = _store(tmp_path)
    empty = store.exists("jobx")
    assert not empty.stdout_log
    assert not empty.json_report
    assert not empty.metadata

    (tmp_path / "jobx_meta.json").write_text("{}", encoding="utf-8")
    (tmp_path / "jobx_stdout.log").write_text("x", encoding="utf-8")
    flags = store.exists("jobx")
    assert flags.metadata is True
    assert flags.stdout_log is True
    assert flags.json_report is False
    assert store.list_job_ids() == ["jobx"]


def test_write_targets_and_artifacts_naming(tmp_path):
    store = _store(tmp_path)
    targets = store.write_targets("jobx")
    assert targets.stdout_log == tmp_path / "jobx_stdout.log"
    assert targets.json_report == tmp_path / "jobx_report.json"

    artifacts = store.artifacts("jobx")
    assert artifacts.log_dir == str(tmp_path)
    assert artifacts.metadata == str(tmp_path / "jobx_meta.json")


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


def test_launcher_success(tmp_path, monkeypatch):
    calls = {}

    def fake_popen(command, cwd, env, stdout, stderr, text):
        calls["command"] = command
        calls["cwd"] = cwd
        calls["env"] = env
        calls["stdout"] = stdout
        calls["stderr"] = stderr
        calls["text"] = text
        return FakeProcessHandle(pid=4321)

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


def _job(job_id, tmp_path, handle=None, reserved=False) -> Job:
    return Job(
        job_id=job_id,
        process_handle=handle,
        log_dir=tmp_path,
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
    registry.register(
        "a", _job("a", tmp_path, handle=FakeProcessHandle(returncode=None))
    )

    result = registry.try_reserve(_job("b", tmp_path, reserved=True), 1)
    assert result.reserved is False
    assert result.running_jobs == 1


def test_registry_ignores_completed_processes(tmp_path):
    registry = InMemoryJobRegistry()
    registry.register(
        "a", _job("a", tmp_path, handle=FakeProcessHandle(returncode=0))
    )

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


# ---- LocalJobResultStore.list_job_ids ----


def test_list_job_ids_globs_meta_files(tmp_path):
    store = LocalJobResultStoreFactory().bind(tmp_path)
    (tmp_path / "job1_meta.json").write_text("{}", encoding="utf-8")
    (tmp_path / "job2_meta.json").write_text("{}", encoding="utf-8")
    (tmp_path / "job2_stdout.log").write_text("", encoding="utf-8")

    assert store.list_job_ids() == ["job1", "job2"]


def test_list_job_ids_missing_dir(tmp_path):
    store = LocalJobResultStoreFactory().bind(tmp_path / "missing")
    assert store.list_job_ids() == []
