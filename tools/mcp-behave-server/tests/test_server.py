import json
from pathlib import Path

from behave_mcp.adapters import (
    InMemoryJobRegistry,
    LocalArtifactStore,
    LocalFeatureFileReader,
)
from behave_mcp.config import Settings
from behave_mcp.ports import Job, LogFileOpenError, ProcessStartError
from behave_mcp.service import BehaveService


class FakeHandle:
    def __init__(self, returncode=None):
        self.returncode = returncode
        self.closed = False
        self.terminated = False

    def poll(self):
        return self.returncode

    def close(self):
        self.closed = True

    def terminate(self):
        self.terminated = True


class FakeLauncher:
    def __init__(self, handle=None, error=None):
        self.calls = []
        self._handle = handle if handle is not None else FakeHandle()
        self._error = error

    def launch(self, command, cwd, env, stdout_log_path):
        self.calls.append(
            {
                "command": command,
                "cwd": cwd,
                "env": env,
                "stdout_log_path": stdout_log_path,
            }
        )
        if self._error is not None:
            raise self._error
        return self._handle


class FakeWorkspace:
    def __init__(
        self,
        *,
        repo_root=None,
        log_dir=None,
        env=None,
        repo_root_error=None,
    ):
        self._repo_root = repo_root
        self._log_dir = log_dir
        self._env = env if env is not None else {}
        self._repo_root_error = repo_root_error

    def resolve_repo_root(self, override):
        if self._repo_root_error is not None:
            raise ValueError(self._repo_root_error)
        if override:
            return Path(override)
        return self._repo_root

    def resolve_log_dir(self, repo_root):
        return self._log_dir

    def subprocess_env(self):
        return dict(self._env)


def _settings(*, allow_cloud=False, max_parallel_jobs=1) -> Settings:
    return Settings(
        allow_cloud_machine_types=allow_cloud,
        max_parallel_jobs=max_parallel_jobs,
        transport="stdio",
    )


def _make_repo_with_feature(
    tmp_path, rel="features/cli/attach.feature"
) -> Path:
    repo_root = tmp_path / "repo"
    feature_path = repo_root / rel
    feature_path.parent.mkdir(parents=True, exist_ok=True)
    (repo_root / "tox.ini").write_text("[tox]\n", encoding="utf-8")
    feature_path.write_text("Feature: sample\n", encoding="utf-8")
    return repo_root


def _make_service(
    workspace,
    *,
    settings=None,
    launcher=None,
    registry=None,
    monotonic=None,
    sleep=None,
    now_utc=None,
    new_job_id=None,
) -> BehaveService:
    return BehaveService(
        workspace=workspace,
        settings=settings if settings is not None else _settings(),
        feature_reader=LocalFeatureFileReader(),
        artifact_store=LocalArtifactStore(),
        registry=registry if registry is not None else InMemoryJobRegistry(),
        launcher=launcher if launcher is not None else FakeLauncher(),
        monotonic=monotonic if monotonic is not None else (lambda: 0.0),
        sleep=sleep if sleep is not None else (lambda seconds: None),
        now_utc=now_utc if now_utc is not None else (lambda: "T0"),
        new_job_id=(
            new_job_id if new_job_id is not None else (lambda: "job0001")
        ),
    )


def test_list_features_returns_feature_files(tmp_path):
    repo_root = _make_repo_with_feature(tmp_path)
    service = _make_service(FakeWorkspace(repo_root=repo_root))

    result = service.list_features()

    assert result["ok"] is True
    assert "features/cli/attach.feature" in result["features"]


def test_list_features_uses_repo_root_override(tmp_path):
    repo_root = _make_repo_with_feature(
        tmp_path, "features/cli/sample.feature"
    )
    service = _make_service(FakeWorkspace(repo_root=None))

    result = service.list_features(repo_root=str(repo_root))

    assert result["ok"] is True
    assert result["repo_root"] == str(repo_root)
    assert result["features"] == ["features/cli/sample.feature"]


def test_list_features_rejects_invalid_repo_root():
    service = _make_service(
        FakeWorkspace(repo_root_error="Invalid repo_root: bad")
    )

    result = service.list_features(repo_root="/whatever")

    assert result["ok"] is False
    assert "Invalid repo_root" in result["error"]
    assert result["features"] == []


def test_start_scenario_rejects_unlisted_feature(tmp_path):
    repo_root = _make_repo_with_feature(tmp_path)
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, log_dir=tmp_path)
    )

    result = service.start_scenario(
        "features/cli/does-not-exist.feature",
        machine_types=["lxd-container"],
    )

    assert result["ok"] is False
    assert "Feature is not listed by list_features" in result["error"]


def test_start_scenario_accepts_normalized_listed_feature(tmp_path):
    repo_root = _make_repo_with_feature(tmp_path)
    launcher = FakeLauncher()
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, log_dir=tmp_path),
        launcher=launcher,
    )

    result = service.start_scenario(
        "features/cli/../cli/attach.feature",
        machine_types=["lxd-container"],
    )

    assert result["ok"] is True
    assert "artifacts" in result
    assert result["artifacts"]["metadata"].endswith("_meta.json")


def test_start_scenario_builds_command(tmp_path):
    repo_root = _make_repo_with_feature(tmp_path)
    launcher = FakeLauncher()
    service = _make_service(
        FakeWorkspace(
            repo_root=repo_root,
            log_dir=tmp_path,
            env={"UACLIENT_BEHAVE_CONTRACT_TOKEN": "token"},
        ),
        launcher=launcher,
    )

    result = service.start_scenario(
        "features/cli/attach.feature",
        machine_types=["lxd-container"],
        scenario_name="attach",
        releases=["resolute"],
    )

    assert result["ok"] is True
    assert "job_id" in result
    call = launcher.calls[0]
    assert call["command"][:5] == [
        "tox",
        "-e",
        "behave",
        "--",
        "features/cli/attach.feature",
    ]
    assert "--name" in call["command"]
    assert "-f" in call["command"]
    assert "json" in call["command"]
    assert call["cwd"] == str(repo_root)
    assert call["env"]["UACLIENT_BEHAVE_CONTRACT_TOKEN"] == "token"


def test_start_scenario_uses_repo_root_override(tmp_path):
    repo_root = _make_repo_with_feature(
        tmp_path, "features/cli/sample.feature"
    )
    launcher = FakeLauncher()
    service = _make_service(
        FakeWorkspace(repo_root=None, log_dir=tmp_path), launcher=launcher
    )

    result = service.start_scenario(
        "features/cli/sample.feature",
        machine_types=["lxd-container"],
        repo_root=str(repo_root),
    )

    assert result["ok"] is True
    assert launcher.calls[0]["cwd"] == str(repo_root)


def test_start_scenario_rejects_invalid_repo_root():
    service = _make_service(
        FakeWorkspace(repo_root_error="Invalid repo_root: bad")
    )

    result = service.start_scenario(
        "features/cli/attach.feature",
        machine_types=["lxd-container"],
        repo_root="/bad",
    )

    assert result["ok"] is False
    assert "Invalid repo_root" in result["error"]


def test_start_scenario_requires_machine_types(tmp_path):
    repo_root = _make_repo_with_feature(tmp_path)
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, log_dir=tmp_path)
    )

    result = service.start_scenario("features/cli/attach.feature", [])

    assert result["ok"] is False
    assert "machine_types is required" in result["error"]


def test_start_scenario_rejects_cloud_machine_type_by_default(tmp_path):
    repo_root = _make_repo_with_feature(tmp_path)
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, log_dir=tmp_path)
    )

    result = service.start_scenario(
        "features/cli/attach.feature", machine_types=["azure.generic"]
    )

    assert result["ok"] is False
    assert "Cloud machine_types are disabled by default" in result["error"]


def test_start_scenario_allows_cloud_machine_type_with_toggle(tmp_path):
    repo_root = _make_repo_with_feature(tmp_path)
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, log_dir=tmp_path),
        settings=_settings(allow_cloud=True),
    )

    result = service.start_scenario(
        "features/cli/attach.feature", machine_types=["azure.generic"]
    )

    assert result["ok"] is True


def test_start_scenario_fails_fast_when_capacity_reached(tmp_path):
    repo_root = _make_repo_with_feature(tmp_path)
    registry = InMemoryJobRegistry()
    ids = iter(["job1", "job2"])
    launcher = FakeLauncher(handle=FakeHandle(returncode=None))
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, log_dir=tmp_path),
        settings=_settings(max_parallel_jobs=1),
        launcher=launcher,
        registry=registry,
        new_job_id=lambda: next(ids),
    )

    first_result = service.start_scenario(
        "features/cli/attach.feature", machine_types=["lxd-container"]
    )
    assert first_result["ok"] is True

    second_result = service.start_scenario(
        "features/cli/attach.feature", machine_types=["lxd-container"]
    )
    assert second_result["ok"] is False
    assert second_result["status"] == "capacity_exceeded"
    assert second_result["capacity"]["max_parallel_jobs"] == 1
    assert second_result["capacity"]["running_jobs"] == 1


def test_start_scenario_releases_slot_when_process_start_fails(tmp_path):
    repo_root = _make_repo_with_feature(tmp_path)
    registry = InMemoryJobRegistry()
    launcher = FakeLauncher(error=ProcessStartError("boom"))
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, log_dir=tmp_path),
        launcher=launcher,
        registry=registry,
        new_job_id=lambda: "jobX",
    )

    result = service.start_scenario(
        "features/cli/attach.feature", machine_types=["lxd-container"]
    )

    assert result["ok"] is False
    assert "Failed to start behave scenario" in result["error"]
    assert registry.get("jobX") is None


def test_start_scenario_reports_log_open_failure(tmp_path):
    repo_root = _make_repo_with_feature(tmp_path)
    registry = InMemoryJobRegistry()
    launcher = FakeLauncher(error=LogFileOpenError("denied"))
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, log_dir=tmp_path),
        launcher=launcher,
        registry=registry,
        new_job_id=lambda: "jobLog",
    )

    result = service.start_scenario(
        "features/cli/attach.feature", machine_types=["lxd-container"]
    )

    assert result["ok"] is False
    assert "Failed to open log file for job_id jobLog" in result["error"]
    assert registry.get("jobLog") is None


def test_wait_for_completion_running_to_completed(tmp_path):
    registry = InMemoryJobRegistry()
    job_id = "job12345"
    stdout_log = tmp_path / f"{job_id}_stdout.log"
    report = tmp_path / f"{job_id}_report.json"
    metadata = tmp_path / f"{job_id}_meta.json"
    stdout_log.write_text("line1\nline2\n", encoding="utf-8")
    handle = FakeHandle(returncode=None)
    registry.register(
        job_id,
        Job(
            job_id=job_id,
            process_handle=handle,
            stdout_log=stdout_log,
            json_report=report,
            metadata=metadata,
        ),
    )

    def fake_sleep(seconds):
        report.write_text(
            json.dumps(
                [
                    {
                        "name": "feature",
                        "elements": [
                            {
                                "name": "scenario",
                                "steps": [
                                    {
                                        "name": "a step",
                                        "result": {
                                            "status": "failed",
                                            "error_message": "boom",
                                        },
                                    }
                                ],
                            }
                        ],
                    }
                ]
            ),
            encoding="utf-8",
        )
        handle.returncode = 1

    service = _make_service(
        FakeWorkspace(repo_root=tmp_path, log_dir=tmp_path),
        registry=registry,
        sleep=fake_sleep,
    )

    completed = service.wait_for_completion(
        job_id, max_wait_seconds=60, poll_interval_seconds=0.01
    )
    assert completed["status"] == "completed"
    assert completed["ok"] is False
    assert completed["summary"]["steps"]["failed"] == 1
    assert completed["failures"][0]["step"] == "a step"
    assert handle.closed is True


def test_wait_for_completion_missing_report_fallback(tmp_path):
    registry = InMemoryJobRegistry()
    job_id = "job54321"
    stdout_log = tmp_path / f"{job_id}_stdout.log"
    stdout_log.write_text("setup failed\n", encoding="utf-8")
    handle = FakeHandle(returncode=2)
    registry.register(
        job_id,
        Job(
            job_id=job_id,
            process_handle=handle,
            stdout_log=stdout_log,
            json_report=tmp_path / "missing.json",
            metadata=tmp_path / f"{job_id}_meta.json",
        ),
    )
    service = _make_service(
        FakeWorkspace(repo_root=tmp_path, log_dir=tmp_path),
        registry=registry,
    )

    completed = service.wait_for_completion(job_id)
    assert completed["status"] == "completed"
    assert completed["ok"] is False
    assert completed["summary"] is None
    assert "setup failed" in completed["recent_output"]


def test_wait_for_completion_timeout(tmp_path):
    registry = InMemoryJobRegistry()
    job_id = "jobtimeout"
    stdout_log = tmp_path / f"{job_id}_stdout.log"
    stdout_log.write_text("still running\n", encoding="utf-8")
    handle = FakeHandle(returncode=None)
    registry.register(
        job_id,
        Job(
            job_id=job_id,
            process_handle=handle,
            stdout_log=stdout_log,
            json_report=tmp_path / "missing.json",
            metadata=tmp_path / f"{job_id}_meta.json",
        ),
    )
    monotonic_values = iter([0.0, 0.1, 0.6, 1.1])
    service = _make_service(
        FakeWorkspace(repo_root=tmp_path, log_dir=tmp_path),
        registry=registry,
        monotonic=lambda: next(monotonic_values),
        sleep=lambda seconds: None,
    )

    timeout = service.wait_for_completion(
        job_id, max_wait_seconds=1, poll_interval_seconds=0.01
    )
    assert timeout["ok"] is False
    assert timeout["status"] == "timeout"
    assert timeout["last_status"] == "running"
    assert "still running" in timeout["recent_output"]


def test_completed_job_remains_in_registry_and_reemits_events(tmp_path):
    registry = InMemoryJobRegistry()
    job_id = "jobkeep"
    stdout_log = tmp_path / f"{job_id}_stdout.log"
    stdout_log.write_text("done\n", encoding="utf-8")
    handle = FakeHandle(returncode=0)
    registry.register(
        job_id,
        Job(
            job_id=job_id,
            process_handle=handle,
            stdout_log=stdout_log,
            json_report=tmp_path / f"{job_id}_report.json",
            metadata=tmp_path / f"{job_id}_meta.json",
        ),
    )
    service = _make_service(
        FakeWorkspace(repo_root=tmp_path, log_dir=tmp_path),
        registry=registry,
    )

    first = service.wait_for_completion(
        job_id, max_wait_seconds=5, poll_interval_seconds=0.01
    )
    assert first["status"] == "completed"
    assert registry.get(job_id) is not None

    service.wait_for_completion(
        job_id, max_wait_seconds=5, poll_interval_seconds=0.01
    )

    index_path = tmp_path / "index.jsonl"
    events = [
        json.loads(line)
        for line in index_path.read_text(encoding="utf-8").splitlines()
    ]
    completed_events = [e for e in events if e.get("event") == "completed"]
    assert len(completed_events) == 2


def test_get_logs_returns_tail(tmp_path):
    registry = InMemoryJobRegistry()
    job_id = "jobtail"
    stdout_log = tmp_path / f"{job_id}_stdout.log"
    stdout_log.write_text("l1\nl2\nl3\n", encoding="utf-8")
    registry.register(
        job_id,
        Job(
            job_id=job_id,
            process_handle=None,
            stdout_log=stdout_log,
            json_report=tmp_path / "none.json",
            metadata=tmp_path / f"{job_id}_meta.json",
        ),
    )
    service = _make_service(
        FakeWorkspace(repo_root=tmp_path, log_dir=tmp_path),
        registry=registry,
    )

    result = service.get_logs(job_id, lines=2)
    assert result["ok"] is True
    assert result["lines"] == 2
    assert result["output"] == "l2\nl3"
    assert result["output_lines"] == ["l2", "l3"]


def test_get_logs_rejects_invalid_repo_root():
    service = _make_service(
        FakeWorkspace(repo_root_error="Invalid repo_root: bad")
    )

    result = service.get_logs("missing-job", repo_root="/bad")

    assert result["ok"] is False
    assert "Invalid repo_root" in result["error"]


def test_get_artifacts_returns_paths_and_metadata(tmp_path):
    registry = InMemoryJobRegistry()
    job_id = "jobmeta01"
    stdout_log = tmp_path / f"{job_id}_stdout.log"
    json_report = tmp_path / f"{job_id}_report.json"
    metadata = tmp_path / f"{job_id}_meta.json"
    stdout_log.write_text("line\n", encoding="utf-8")
    json_report.write_text("[]\n", encoding="utf-8")
    metadata.write_text(
        json.dumps({"job_id": job_id, "status": "started"}),
        encoding="utf-8",
    )
    registry.register(
        job_id,
        Job(
            job_id=job_id,
            process_handle=None,
            stdout_log=stdout_log,
            json_report=json_report,
            metadata=metadata,
        ),
    )
    service = _make_service(
        FakeWorkspace(repo_root=tmp_path, log_dir=tmp_path),
        registry=registry,
    )

    result = service.get_artifacts(job_id)
    assert result["ok"] is True
    assert result["exists"]["stdout_log"] is True
    assert result["exists"]["json_report"] is True
    assert result["exists"]["metadata"] is True
    assert result["metadata"]["status"] == "started"


def test_get_artifacts_rejects_invalid_repo_root():
    service = _make_service(
        FakeWorkspace(repo_root_error="Invalid repo_root: bad")
    )

    result = service.get_artifacts("missing-job", repo_root="/bad")

    assert result["ok"] is False
    assert "Invalid repo_root" in result["error"]
