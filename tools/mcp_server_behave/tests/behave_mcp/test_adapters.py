import os
import signal
import subprocess
import sys
import time

import pytest

import behave_mcp.adapters as adapters_module
from behave_mcp.adapters import (
    FileLanePool,
    LocalFeatureFileReader,
    LocalJobResultStoreFactory,
    LocalWorkspace,
    LockedJobLauncher,
)
from behave_mcp.domain.pools import Pool
from behave_mcp.messages import JobExit, JobRecord
from tests.conftest import job_file, make_repo_with_feature

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


def test_read_record_missing_or_invalid(tmp_path):
    store = _store(tmp_path)
    assert store.read_record("missing") == JobRecord()

    (job_file(tmp_path, "bad", "job.json")).write_text(
        "not json", encoding="utf-8"
    )
    assert store.read_record("bad") == JobRecord()

    (job_file(tmp_path, "list", "job.json")).write_text("[]", encoding="utf-8")
    assert store.read_record("list") == JobRecord()


def test_write_and_read_record_roundtrip(tmp_path):
    store = _store(tmp_path)
    record = JobRecord(job_id="x", status="started", pid=42)
    store.write_record("jobx", record)
    assert store.read_record("jobx") == record


def test_log_tail_and_lines(tmp_path):
    store = _store(tmp_path)
    (job_file(tmp_path, "jobx", "stdout.log")).write_text(
        "a\nb\nc\n", encoding="utf-8"
    )
    assert store.log_tail("jobx", 2) == "b\nc"
    assert store.read_log_lines("jobx") == ["a", "b", "c"]


def test_log_tail_missing(tmp_path):
    store = _store(tmp_path)
    assert store.log_tail("nope", 5) == "Waiting for output..."
    assert store.read_log_lines("nope") == []


def test_read_report_missing_or_invalid(tmp_path):
    store = _store(tmp_path)
    assert store.read_report("missing") is None

    (job_file(tmp_path, "bad", "report.json")).write_text(
        "nope", encoding="utf-8"
    )
    assert store.read_report("bad") is None

    (job_file(tmp_path, "obj", "report.json")).write_text(
        "{}", encoding="utf-8"
    )
    assert store.read_report("obj") is None

    (job_file(tmp_path, "good", "report.json")).write_text(
        "[1, 2]", encoding="utf-8"
    )
    assert store.read_report("good") == [1, 2]


def test_exists_and_list_job_ids(tmp_path):
    store = _store(tmp_path)
    empty = store.exists("jobx")
    assert not empty.record
    assert not empty.stdout_log
    assert not empty.json_report

    (job_file(tmp_path, "jobx", "job.json")).write_text("{}", encoding="utf-8")
    (job_file(tmp_path, "jobx", "stdout.log")).write_text(
        "x", encoding="utf-8"
    )
    flags = store.exists("jobx")
    assert flags.record is True
    assert flags.status is False
    assert flags.stdout_log is True
    assert flags.json_report is False
    assert store.list_job_ids() == ["jobx"]


def test_write_targets_and_artifacts_naming(tmp_path):
    store = _store(tmp_path)
    targets = store.write_targets("jobx")
    assert targets.stdout_log == job_file(tmp_path, "jobx", "stdout.log")
    assert targets.json_report == job_file(tmp_path, "jobx", "report.json")

    artifacts = store.artifacts("jobx")
    assert artifacts.job_dir == str(tmp_path / "jobx")
    assert artifacts.record == str(tmp_path / "jobx" / "job.json")
    assert artifacts.status == str(tmp_path / "jobx" / "status.json")


def test_a_job_directory_without_a_record_is_not_listed(tmp_path):
    store = _store(tmp_path)
    store.write_targets("refused")

    assert store.list_job_ids() == []


def test_the_first_exit_written_wins(tmp_path):
    store = _store(tmp_path)
    first = JobExit(completed_at="T1", returncode=0, ok=True)
    later = JobExit(completed_at="T2", returncode=1, ok=False)
    store.write_exit("jobx", first)

    kept = store.write_exit("jobx", later)

    assert kept == first
    assert store.read_exit("jobx") == first


def test_no_exit_reads_as_none(tmp_path):
    assert _store(tmp_path).read_exit("jobx") is None


def test_an_interrupted_write_leaves_no_partial_file(tmp_path, monkeypatch):
    store = _store(tmp_path)

    def crash(source, target):
        raise OSError("disk went away")

    monkeypatch.setattr(adapters_module.os, "replace", crash)

    store.write_exit("jobx", JobExit(completed_at="T1", returncode=0))

    # Neither the status file nor a temporary file is left behind.
    assert list((tmp_path / "jobx").iterdir()) == []


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


def test_state_defaults_to_one_directory_in_the_servers_checkout(
    tmp_path, monkeypatch
):
    repo = make_repo_with_feature(tmp_path)
    monkeypatch.delenv("MCP_STATE_DIR", raising=False)
    monkeypatch.setenv("UBUNTU_PRO_CLIENT_REPO", str(repo))
    workspace = LocalWorkspace()

    state = repo.resolve() / ".mcp_server_behave"

    assert workspace.resolve_state_dir() == state
    assert workspace.resolve_log_dir() == state / "jobs"
    assert workspace.resolve_campaign_dir() == state / "campaigns"


def test_each_kind_of_artifact_gets_its_own_subdirectory(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MCP_STATE_DIR", str(tmp_path / "state"))
    workspace = LocalWorkspace()

    jobs = workspace.resolve_log_dir()
    campaigns = workspace.resolve_campaign_dir()

    # Created on demand, and distinct, so job output cannot land among
    # campaign records.
    assert jobs.is_dir() and campaigns.is_dir()
    assert jobs != campaigns
    assert jobs.parent == campaigns.parent


def test_the_state_directory_can_be_moved_wholesale(tmp_path, monkeypatch):
    custom = tmp_path / "elsewhere"
    monkeypatch.setenv("MCP_STATE_DIR", str(custom))
    workspace = LocalWorkspace()

    # One variable relocates every kind of artifact together, and needs no
    # checkout to resolve.
    assert workspace.resolve_state_dir() == custom.resolve()
    assert workspace.resolve_log_dir() == custom.resolve() / "jobs"
    assert workspace.resolve_campaign_dir() == custom.resolve() / "campaigns"


def test_subprocess_env_forwards_all(monkeypatch):
    workspace = LocalWorkspace()
    monkeypatch.setenv("MCP_TEST_PASSTHROUGH", "carried")
    env = workspace.subprocess_env()
    assert env["MCP_TEST_PASSTHROUGH"] == "carried"


def _init_git_repo(repo_root):
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True)
    subprocess.run(
        ["git", "-C", str(repo_root), "config", "user.email", "t@t.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_root), "config", "user.name", "t"],
        check=True,
    )
    (repo_root / "committed.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_root), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo_root), "commit", "-q", "-m", "initial"],
        check=True,
    )


def test_repo_state_reports_commit_branch_and_clean(tmp_path):
    repo = make_repo_with_feature(tmp_path, rel=None)
    _init_git_repo(repo)
    workspace = LocalWorkspace()

    state = workspace.repo_state(repo)

    assert state.commit
    assert state.branch
    assert state.dirty is False


def test_repo_state_detects_dirty_worktree(tmp_path):
    repo = make_repo_with_feature(tmp_path, rel=None)
    _init_git_repo(repo)
    (repo / "committed.txt").write_text("changed", encoding="utf-8")
    workspace = LocalWorkspace()

    state = workspace.repo_state(repo)

    assert state.dirty is True


def test_repo_state_returns_all_none_outside_a_git_checkout(tmp_path):
    not_a_repo = make_repo_with_feature(tmp_path, rel=None)
    workspace = LocalWorkspace()

    state = workspace.repo_state(not_a_repo)

    assert state.commit is None
    assert state.branch is None
    assert state.dirty is None


# ---- lanes, job locks and the wrapper: real processes ----


PATH_ONLY = {"PATH": "/usr/bin:/bin"}


def _eventually(condition, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture
def lanes(tmp_path):
    return FileLanePool(lambda: tmp_path / "lanes")


@pytest.fixture
def launcher(tmp_path):
    return LockedJobLauncher(lambda: tmp_path / "jobs")


def _launch(tmp_path, lanes, launcher, job_id, command):
    (tmp_path / "jobs" / job_id).mkdir(parents=True)
    lease = lanes.take(Pool.LXD, 1)
    assert lease is not None, "no free lane"
    launcher.launch(job_id, command, str(tmp_path), PATH_ONLY, lease)


def _exit(tmp_path, job_id):
    return (
        LocalJobResultStoreFactory().bind(tmp_path / "jobs").read_exit(job_id)
    )


class TestFileLanePool:
    def test_each_lane_is_taken_once(self, lanes):
        first = lanes.take(Pool.LXD, 2)
        second = lanes.take(Pool.LXD, 2)

        third = lanes.take(Pool.LXD, 2)

        assert first is not None and second is not None
        assert third is None
        assert lanes.busy(Pool.LXD, 2) == 2

    def test_closing_a_lease_frees_its_lane(self, lanes):
        lease = lanes.take(Pool.LXD, 1)
        lease.close()

        assert lanes.busy(Pool.LXD, 1) == 0
        assert lanes.take(Pool.LXD, 1) is not None

    def test_pools_are_separate(self, lanes):
        lanes.take(Pool.LXD, 1)

        assert lanes.take(Pool.WSL, 1) is not None

    def test_another_process_holding_a_lane_counts(self, tmp_path, lanes):
        # A lane held by another process: it locks lane 0 and waits.
        lane = tmp_path / "lanes" / "lxd" / "0.lock"
        lane.parent.mkdir(parents=True)
        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import fcntl, sys, time\n"
                "f = open(sys.argv[1], 'w')\n"
                "fcntl.flock(f, fcntl.LOCK_EX)\n"
                "print('held', flush=True)\n"
                "time.sleep(60)\n",
                str(lane),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert holder.stdout.readline().strip() == "held"

            assert lanes.take(Pool.LXD, 1) is None
            assert lanes.busy(Pool.LXD, 1) == 1
        finally:
            holder.kill()
            holder.wait()

        assert lanes.take(Pool.LXD, 1) is not None


class TestLockedJobLauncher:
    def test_a_job_runs_to_a_recorded_exit(self, tmp_path, lanes, launcher):
        _launch(tmp_path, lanes, launcher, "job", ["sh", "-c", "exit 3"])

        assert _eventually(lambda: _exit(tmp_path, "job") is not None)
        exit = _exit(tmp_path, "job")
        assert (exit.returncode, exit.ok, exit.killed) == (3, False, False)

    def test_the_job_output_is_its_log(self, tmp_path, lanes, launcher):
        _launch(tmp_path, lanes, launcher, "job", ["sh", "-c", "echo hello"])

        assert _eventually(lambda: _exit(tmp_path, "job") is not None)
        log = tmp_path / "jobs" / "job" / "stdout.log"
        assert log.read_text().strip() == "hello"

    def test_a_job_reads_no_stdin_from_the_server(
        self, tmp_path, lanes, launcher
    ):
        _launch(
            tmp_path,
            lanes,
            launcher,
            "job",
            ["sh", "-c", "read line; echo got=$line"],
        )

        assert _eventually(lambda: _exit(tmp_path, "job") is not None)
        log = tmp_path / "jobs" / "job" / "stdout.log"
        assert log.read_text().strip() == "got="

    def test_the_wrapper_holds_the_lane_and_lock_until_it_exits(
        self, tmp_path, lanes, launcher
    ):
        _launch(tmp_path, lanes, launcher, "job", ["sleep", "1"])

        # The launcher has let go of both; only the wrapper holds them.
        assert launcher.is_running("job")
        assert lanes.busy(Pool.LXD, 1) == 1

        assert _eventually(lambda: not launcher.is_running("job"))
        assert lanes.busy(Pool.LXD, 1) == 0
        assert _exit(tmp_path, "job").ok is True

    def test_a_signal_stops_the_job_and_records_a_kill(
        self, tmp_path, lanes, launcher
    ):
        _launch(tmp_path, lanes, launcher, "job", ["sleep", "30"])

        signalled = launcher.signal("job")

        assert signalled is True
        assert _eventually(lambda: _exit(tmp_path, "job") is not None)
        exit = _exit(tmp_path, "job")
        assert (exit.killed, exit.ok) == (True, False)

    def test_a_kill_reaches_every_process_the_job_started(
        self, tmp_path, lanes, launcher
    ):
        pid_file = tmp_path / "child.pid"
        _launch(
            tmp_path,
            lanes,
            launcher,
            "job",
            ["sh", "-c", "sleep 30 & echo $! > {}; wait".format(pid_file)],
        )
        assert _eventually(
            lambda: pid_file.exists() and pid_file.read_text().strip()
        )
        child = int(pid_file.read_text())

        launcher.signal("job")

        assert _eventually(lambda: not _pid_alive(child))

    def test_a_finished_job_cannot_be_signalled(
        self, tmp_path, lanes, launcher
    ):
        _launch(tmp_path, lanes, launcher, "job", ["true"])
        assert _eventually(lambda: not launcher.is_running("job"))

        assert launcher.signal("job") is False

    def test_a_dead_wrapper_leaves_no_exit_and_takes_its_job_with_it(
        self, tmp_path, lanes, launcher
    ):
        pid_file = tmp_path / "child.pid"
        _launch(
            tmp_path,
            lanes,
            launcher,
            "job",
            ["sh", "-c", "echo $$ > {}; exec sleep 30".format(pid_file)],
        )
        assert _eventually(
            lambda: pid_file.exists() and pid_file.read_text().strip()
        )
        child = int(pid_file.read_text())
        wrapper = int((tmp_path / "jobs" / "job" / "job.lock").read_text())

        os.kill(wrapper, signal.SIGKILL)

        assert _eventually(lambda: not launcher.is_running("job"))
        assert _exit(tmp_path, "job") is None
        assert lanes.busy(Pool.LXD, 1) == 0
        # The parent-death signal stops the job when its wrapper dies.
        assert _eventually(lambda: not _pid_alive(child))

    def test_a_kill_before_the_wrapper_is_ready_still_lands(
        self, tmp_path, lanes, launcher
    ):
        # The marker a kill leaves, as if it came before the wrapper had
        # published its PID.
        marker = tmp_path / "jobs" / "job" / "kill"
        marker.parent.mkdir(parents=True)
        marker.touch()
        lease = lanes.take(Pool.LXD, 1)

        launcher.launch(
            "job", ["sleep", "30"], str(tmp_path), PATH_ONLY, lease
        )

        assert _eventually(lambda: _exit(tmp_path, "job") is not None)
        exit = _exit(tmp_path, "job")
        assert (exit.killed, exit.returncode) == (True, None)

    def test_a_command_that_cannot_start_records_why(
        self, tmp_path, lanes, launcher
    ):
        _launch(tmp_path, lanes, launcher, "job", ["/no/such/command"])

        assert _eventually(lambda: _exit(tmp_path, "job") is not None)
        exit = _exit(tmp_path, "job")
        assert exit.returncode is None
        assert "No such file" in exit.error


def test_list_job_ids_globs_meta_files(tmp_path):
    store = LocalJobResultStoreFactory().bind(tmp_path)
    (job_file(tmp_path, "job1", "job.json")).write_text("{}", encoding="utf-8")
    (job_file(tmp_path, "job2", "job.json")).write_text("{}", encoding="utf-8")
    (job_file(tmp_path, "job2", "stdout.log")).write_text("", encoding="utf-8")

    assert store.list_job_ids() == ["job1", "job2"]


def test_list_job_ids_missing_dir(tmp_path):
    store = LocalJobResultStoreFactory().bind(tmp_path / "missing")
    assert store.list_job_ids() == []
