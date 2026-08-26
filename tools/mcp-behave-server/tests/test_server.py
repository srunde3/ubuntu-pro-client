import json
from pathlib import Path

import pytest

from behave_mcp import domain
from behave_mcp.adapters import (
    InMemoryJobRegistry,
    LocalArtifactStore,
    LocalFeatureCatalog,
    LocalFeatureFileReader,
)
from behave_mcp.config import Settings
from behave_mcp.ports import Job, LogFileOpenError, ProcessStartError
from behave_mcp.service import BehaveService, BehaveServiceError


class FakeHandle:
    def __init__(self, returncode=None, pid=4242):
        self.returncode = returncode
        self.pid = pid
        self.closed = False
        self.terminated = False

    def poll(self):
        return self.returncode

    def close(self):
        self.closed = True

    def terminate(self):
        self.terminated = True


class FakeLauncher:
    def __init__(self, handle=None, error=None, alive_pids=None):
        self.calls = []
        self._handle = handle if handle is not None else FakeHandle()
        self._error = error
        self._alive_pids = set(alive_pids) if alive_pids else set()

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

    def is_pid_alive(self, pid):
        return pid in self._alive_pids


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
        host="127.0.0.1",
        port=8000,
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
        feature_catalog=LocalFeatureCatalog(),
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

    result = service.list_features().model_dump(mode="json")

    paths = [feature["path"] for feature in result["features"]]
    assert "features/cli/attach.feature" in paths


def test_list_features_uses_repo_root_override(tmp_path):
    repo_root = _make_repo_with_feature(
        tmp_path, "features/cli/sample.feature"
    )
    service = _make_service(FakeWorkspace(repo_root=None))

    result = service.list_features(repo_root=str(repo_root)).model_dump(
        mode="json"
    )

    assert result["repo_root"] == str(repo_root)
    assert [feature["path"] for feature in result["features"]] == [
        "features/cli/sample.feature"
    ]


def test_list_features_rejects_invalid_repo_root():
    service = _make_service(
        FakeWorkspace(repo_root_error="Invalid repo_root: bad")
    )

    with pytest.raises(BehaveServiceError, match="Invalid repo_root"):
        service.list_features(repo_root="/whatever")


_OUTLINE_FEATURE = """\
@uses.config.contract_token
Feature: Attach things

  Scenario Outline: Attach on a machine
    Given a `<release>` `<machine_type>` machine with ubuntu-advantage-tools installed
    When I attach

    Examples: ubuntu release
      | release  | machine_type  |
      | jammy    | lxd-container |
      | resolute | lxd-vm        |

  @arm64
  Scenario Outline: Attach invalid token
    Given a `<release>` `<machine_type>` machine with ubuntu-advantage-tools installed
    When I attach INVALID

    Examples: ubuntu release
      | release | machine_type  |
      | jammy   | lxd-container |
"""


def _make_repo_with_outline(tmp_path) -> Path:
    repo_root = tmp_path / "repo"
    feature_path = repo_root / "features" / "cli" / "attach.feature"
    feature_path.parent.mkdir(parents=True, exist_ok=True)
    (repo_root / "tox.ini").write_text("[tox]\n", encoding="utf-8")
    feature_path.write_text(_OUTLINE_FEATURE, encoding="utf-8")
    return repo_root


def test_list_features_returns_catalog_entry(tmp_path):
    repo_root = _make_repo_with_outline(tmp_path)
    service = _make_service(FakeWorkspace(repo_root=repo_root))

    result = service.list_features().model_dump(mode="json")

    entry = result["features"][0]
    assert entry["path"] == "features/cli/attach.feature"
    assert entry["title"] == "Attach things"
    assert entry["scenario_count"] == 2
    assert entry["requires_config"] == ["contract_token"]
    assert entry["releases"] == ["jammy", "resolute"]
    assert entry["machine_types"] == ["lxd-container", "lxd-vm"]


def test_list_features_filters_by_release_and_machine_type(tmp_path):
    repo_root = _make_repo_with_outline(tmp_path)
    service = _make_service(FakeWorkspace(repo_root=repo_root))

    match = service.list_features(
        release="resolute", machine_type="lxd-vm"
    ).model_dump(mode="json")
    assert len(match["features"]) == 1

    no_match = service.list_features(
        release="resolute", machine_type="lxd-container"
    ).model_dump(mode="json")
    assert no_match["features"] == []


def test_describe_feature_returns_scenarios(tmp_path):
    repo_root = _make_repo_with_outline(tmp_path)
    service = _make_service(FakeWorkspace(repo_root=repo_root))

    result = service.describe_feature(
        "features/cli/attach.feature"
    ).model_dump(mode="json")

    assert result["requires_config"] == ["contract_token"]
    scenario = result["scenarios"][0]
    assert scenario["name"] == "Attach on a machine"
    assert scenario["type"] == "scenario_outline"
    assert scenario["example_columns"] == ["release", "machine_type"]
    assert scenario["combos"] == [
        {"release": "jammy", "machine_type": "lxd-container"},
        {"release": "resolute", "machine_type": "lxd-vm"},
    ]


def test_describe_feature_rejects_unlisted_feature(tmp_path):
    repo_root = _make_repo_with_outline(tmp_path)
    service = _make_service(FakeWorkspace(repo_root=repo_root))

    with pytest.raises(
        BehaveServiceError, match="Feature is not listed by list_features"
    ):
        service.describe_feature("features/cli/missing.feature")


def test_list_dimensions_counts_scenarios(tmp_path):
    repo_root = _make_repo_with_outline(tmp_path)
    service = _make_service(FakeWorkspace(repo_root=repo_root))

    result = service.list_dimensions().model_dump(mode="json")

    assert result["releases"] == [
        {"name": "jammy", "scenario_count": 2},
        {"name": "resolute", "scenario_count": 1},
    ]
    assert result["machine_types"] == [
        {"name": "lxd-container", "scenario_count": 2},
        {"name": "lxd-vm", "scenario_count": 1},
    ]


def test_find_scenarios_filters_by_tag(tmp_path):
    repo_root = _make_repo_with_outline(tmp_path)
    service = _make_service(FakeWorkspace(repo_root=repo_root))

    result = service.find_scenarios(tag="arm64").model_dump(mode="json")

    assert len(result["matches"]) == 1
    match = result["matches"][0]
    assert match["scenario_name"] == "Attach invalid token"
    assert match["feature_file"] == "features/cli/attach.feature"


def test_find_scenarios_filters_combos_by_machine_type(tmp_path):
    repo_root = _make_repo_with_outline(tmp_path)
    service = _make_service(FakeWorkspace(repo_root=repo_root))

    result = service.find_scenarios(machine_type="lxd-vm").model_dump(
        mode="json"
    )

    assert len(result["matches"]) == 1
    assert result["matches"][0]["combos"] == [
        {"release": "resolute", "machine_type": "lxd-vm"}
    ]


def test_start_scenario_rejects_unlisted_feature(tmp_path):
    repo_root = _make_repo_with_feature(tmp_path)
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, log_dir=tmp_path)
    )

    with pytest.raises(
        BehaveServiceError, match="Feature is not listed by list_features"
    ):
        service.start_scenario(
            "features/cli/does-not-exist.feature",
            machine_types=["lxd-container"],
        )


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
    ).model_dump(mode="json")

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
    ).model_dump(mode="json")

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
    assert "features.behave_combo_formatter:ComboFormatter" in call["command"]
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
    ).model_dump(mode="json")

    assert result["ok"] is True
    assert launcher.calls[0]["cwd"] == str(repo_root)


def test_start_scenario_writes_combo_report_path(tmp_path):
    repo_root = _make_repo_with_outline(tmp_path)
    launcher = FakeLauncher()
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, log_dir=tmp_path),
        launcher=launcher,
    )

    result = service.start_scenario(
        "features/cli/attach.feature",
        machine_types=["lxd-container", "lxd-vm"],
    ).model_dump(mode="json")

    metadata_path = Path(result["artifacts"]["metadata"])
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

    assert metadata["combo_report"].endswith("_combo.jsonl")
    call = launcher.calls[0]
    assert call["command"][-6:-2] == [
        "-f",
        "features.behave_combo_formatter:ComboFormatter",
        "-o",
        metadata["combo_report"],
    ]
    assert call["command"][-2:] == ["-f", "plain"]


def test_start_scenario_rejects_invalid_repo_root():
    service = _make_service(
        FakeWorkspace(repo_root_error="Invalid repo_root: bad")
    )

    with pytest.raises(BehaveServiceError, match="Invalid repo_root"):
        service.start_scenario(
            "features/cli/attach.feature",
            machine_types=["lxd-container"],
            repo_root="/bad",
        )


def test_start_scenario_requires_machine_types(tmp_path):
    repo_root = _make_repo_with_feature(tmp_path)
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, log_dir=tmp_path)
    )

    with pytest.raises(BehaveServiceError, match="machine_types is required"):
        service.start_scenario("features/cli/attach.feature", [])


def test_start_scenario_rejects_cloud_machine_type_by_default(tmp_path):
    repo_root = _make_repo_with_feature(tmp_path)
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, log_dir=tmp_path)
    )

    with pytest.raises(
        BehaveServiceError,
        match="Cloud machine_types are disabled by default",
    ):
        service.start_scenario(
            "features/cli/attach.feature", machine_types=["azure.generic"]
        )


def test_start_scenario_allows_cloud_machine_type_with_toggle(tmp_path):
    repo_root = _make_repo_with_feature(tmp_path)
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, log_dir=tmp_path),
        settings=_settings(allow_cloud=True),
    )

    result = service.start_scenario(
        "features/cli/attach.feature", machine_types=["azure.generic"]
    ).model_dump(mode="json")

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
    ).model_dump(mode="json")
    assert first_result["ok"] is True

    second_result = service.start_scenario(
        "features/cli/attach.feature", machine_types=["lxd-container"]
    ).model_dump(mode="json")
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

    with pytest.raises(
        BehaveServiceError, match="Failed to start behave scenario"
    ):
        service.start_scenario(
            "features/cli/attach.feature", machine_types=["lxd-container"]
        )
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

    with pytest.raises(
        BehaveServiceError,
        match="Failed to open log file for job_id jobLog",
    ):
        service.start_scenario(
            "features/cli/attach.feature", machine_types=["lxd-container"]
        )
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
    ).model_dump(mode="json")
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

    completed = service.wait_for_completion(job_id).model_dump(mode="json")
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
    ).model_dump(mode="json")
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
    ).model_dump(mode="json")
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

    result = service.get_logs(job_id, lines=2).model_dump(mode="json")
    assert result["lines"] == 2
    assert result["output"] == "l2\nl3"
    assert result["output_lines"] == ["l2", "l3"]


def test_get_logs_rejects_invalid_repo_root():
    service = _make_service(
        FakeWorkspace(repo_root_error="Invalid repo_root: bad")
    )

    with pytest.raises(BehaveServiceError, match="Invalid repo_root"):
        service.get_logs("missing-job", repo_root="/bad")


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

    result = service.get_artifacts(job_id).model_dump(mode="json")
    assert result["exists"]["stdout_log"] is True
    assert result["exists"]["json_report"] is True
    assert result["exists"]["metadata"] is True
    assert result["metadata"]["status"] == "started"


def test_get_artifacts_rejects_invalid_repo_root():
    service = _make_service(
        FakeWorkspace(repo_root_error="Invalid repo_root: bad")
    )

    with pytest.raises(BehaveServiceError, match="Invalid repo_root"):
        service.get_artifacts("missing-job", repo_root="/bad")


# ---- Reattach after restart (recovered jobs, no live handle) ----


def test_wait_for_completion_recovers_running_job_via_pid_liveness(tmp_path):
    job_id = "jobrecoveredalive"
    stdout_log = tmp_path / f"{job_id}_stdout.log"
    stdout_log.write_text("still going\n", encoding="utf-8")
    metadata = tmp_path / f"{job_id}_meta.json"
    metadata.write_text(json.dumps({"pid": 999}), encoding="utf-8")

    launcher = FakeLauncher(alive_pids={999})
    monotonic_values = iter([0.0, 0.1, 1.1])
    service = _make_service(
        FakeWorkspace(repo_root=tmp_path, log_dir=tmp_path),
        launcher=launcher,
        monotonic=lambda: next(monotonic_values),
        sleep=lambda seconds: None,
    )

    timeout = service.wait_for_completion(
        job_id, max_wait_seconds=1, poll_interval_seconds=0.01
    ).model_dump(mode="json")

    assert timeout["status"] == "timeout"
    assert timeout["last_status"] == "running"


def test_recovered_job_is_cached_in_registry(tmp_path):
    """A recovered job is registered so polls don't re-recover/re-log."""
    job_id = "jobrecoveredonce"
    stdout_log = tmp_path / f"{job_id}_stdout.log"
    stdout_log.write_text("still going\n", encoding="utf-8")
    metadata = tmp_path / f"{job_id}_meta.json"
    metadata.write_text(json.dumps({"pid": 999}), encoding="utf-8")

    registry = InMemoryJobRegistry()
    launcher = FakeLauncher(alive_pids={999})
    service = _make_service(
        FakeWorkspace(repo_root=tmp_path, log_dir=tmp_path),
        launcher=launcher,
        registry=registry,
    )

    assert registry.get(job_id) is None

    service.get_logs(job_id)

    cached = registry.get(job_id)
    assert cached is not None
    assert cached.process_handle is None
    assert cached.pid == 999


def test_wait_for_completion_recovers_dead_job_without_report_as_not_ok(
    tmp_path,
):
    job_id = "jobrecovereddead"
    stdout_log = tmp_path / f"{job_id}_stdout.log"
    stdout_log.write_text("crashed before report\n", encoding="utf-8")
    metadata = tmp_path / f"{job_id}_meta.json"
    metadata.write_text(json.dumps({"pid": 999}), encoding="utf-8")

    launcher = FakeLauncher(alive_pids=set())
    service = _make_service(
        FakeWorkspace(repo_root=tmp_path, log_dir=tmp_path),
        launcher=launcher,
    )

    completed = service.wait_for_completion(job_id).model_dump(mode="json")

    assert completed["status"] == "completed"
    assert completed["ok"] is False
    assert completed["returncode"] is None


def test_wait_for_completion_recovers_completed_job_ok_from_report(tmp_path):
    job_id = "jobrecoveredreport"
    stdout_log = tmp_path / f"{job_id}_stdout.log"
    stdout_log.write_text("done\n", encoding="utf-8")
    report = tmp_path / f"{job_id}_report.json"
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
                                    "result": {"status": "passed"},
                                }
                            ],
                        }
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    metadata = tmp_path / f"{job_id}_meta.json"
    metadata.write_text(json.dumps({"pid": 999}), encoding="utf-8")

    # pid is dead and returncode is unknown, but the report proves success --
    # ok must come from the report, not from a (missing) returncode.
    launcher = FakeLauncher(alive_pids=set())
    service = _make_service(
        FakeWorkspace(repo_root=tmp_path, log_dir=tmp_path),
        launcher=launcher,
    )

    completed = service.wait_for_completion(job_id).model_dump(mode="json")

    assert completed["status"] == "completed"
    assert completed["ok"] is True
    assert completed["summary"]["steps"]["passed"] == 1


# ---- list_jobs ----


def test_list_jobs_merges_in_memory_and_disk_only(tmp_path):
    registry = InMemoryJobRegistry()

    running_job_id = "jobrunning"
    stdout_running = tmp_path / f"{running_job_id}_stdout.log"
    stdout_running.write_text("running\n", encoding="utf-8")
    (tmp_path / f"{running_job_id}_meta.json").write_text(
        json.dumps({"feature_file": "features/a.feature", "started_at": "T1"}),
        encoding="utf-8",
    )
    registry.register(
        running_job_id,
        Job(
            job_id=running_job_id,
            process_handle=FakeHandle(returncode=None, pid=111),
            stdout_log=stdout_running,
            json_report=tmp_path / f"{running_job_id}_report.json",
            metadata=tmp_path / f"{running_job_id}_meta.json",
            pid=111,
        ),
    )

    disk_alive_id = "jobdiskalive"
    (tmp_path / f"{disk_alive_id}_stdout.log").write_text(
        "x\n", encoding="utf-8"
    )
    (tmp_path / f"{disk_alive_id}_meta.json").write_text(
        json.dumps({"pid": 222, "started_at": "T2"}), encoding="utf-8"
    )

    disk_dead_id = "jobdiskdead"
    (tmp_path / f"{disk_dead_id}_stdout.log").write_text(
        "y\n", encoding="utf-8"
    )
    (tmp_path / f"{disk_dead_id}_meta.json").write_text(
        json.dumps({"pid": 333, "started_at": "T3"}), encoding="utf-8"
    )

    launcher = FakeLauncher(alive_pids={222})
    service = _make_service(
        FakeWorkspace(repo_root=tmp_path, log_dir=tmp_path),
        registry=registry,
        launcher=launcher,
    )

    result = service.list_jobs().model_dump(mode="json")
    jobs_by_id = {job["job_id"]: job for job in result["jobs"]}

    assert jobs_by_id[running_job_id]["status"] == "running"
    assert jobs_by_id[disk_alive_id]["status"] == "running"
    assert jobs_by_id[disk_dead_id]["status"] == "unknown"
    assert jobs_by_id[disk_dead_id]["ok"] is False


def test_list_jobs_caps_completed_history(tmp_path):
    total = domain._DEFAULT_JOB_LIST_LIMIT + 5
    for i in range(total):
        job_id = f"jobold{i:03d}"
        (tmp_path / f"{job_id}_meta.json").write_text(
            json.dumps({"started_at": f"T{i:03d}"}), encoding="utf-8"
        )

    service = _make_service(
        FakeWorkspace(repo_root=tmp_path, log_dir=tmp_path),
    )

    result = service.list_jobs().model_dump(mode="json")

    assert len(result["jobs"]) == domain._DEFAULT_JOB_LIST_LIMIT
    kept_ids = {job["job_id"] for job in result["jobs"]}
    assert f"jobold{total - 1:03d}" in kept_ids
    assert "jobold000" not in kept_ids


def test_list_jobs_rejects_invalid_repo_root():
    service = _make_service(
        FakeWorkspace(repo_root_error="Invalid repo_root: bad")
    )

    with pytest.raises(BehaveServiceError, match="Invalid repo_root"):
        service.list_jobs(repo_root="/bad")


# ---- summarize_scenario_results ----


def test_summarize_scenario_results_groups_by_release_and_machine_type(
    tmp_path,
):
    repo_root = _make_repo_with_outline(tmp_path)
    handle = FakeHandle(returncode=0)
    launcher = FakeLauncher(handle=handle)
    registry = InMemoryJobRegistry()
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, log_dir=tmp_path),
        launcher=launcher,
        registry=registry,
    )

    start_result = service.start_scenario(
        "features/cli/attach.feature",
        machine_types=["lxd-container", "lxd-vm"],
    ).model_dump(mode="json")
    job_id = start_result["job_id"]
    metadata_path = Path(start_result["artifacts"]["metadata"])
    combo_report_path = Path(
        json.loads(metadata_path.read_text(encoding="utf-8"))["combo_report"]
    )

    jammy_location = "features/cli/attach.feature:10"
    resolute_location = "features/cli/attach.feature:11"
    combo_report_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "location": jammy_location,
                        "status": "passed",
                        "release": "jammy",
                        "machine_type": "lxd-container",
                    }
                ),
                json.dumps(
                    {
                        "location": resolute_location,
                        "status": "failed",
                        "release": "resolute",
                        "machine_type": "lxd-vm",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    report_path = Path(start_result["artifacts"]["json_report"])
    report_path.write_text(
        json.dumps(
            [
                {
                    "name": "Attach things",
                    "elements": [
                        {
                            "name": "Attach on a machine -- @1.1",
                            "location": jammy_location,
                            "steps": [
                                {
                                    "name": "step1",
                                    "result": {"status": "passed"},
                                }
                            ],
                        },
                        {
                            "name": "Attach on a machine -- @1.2",
                            "location": resolute_location,
                            "steps": [
                                {
                                    "name": "step2",
                                    "result": {
                                        "status": "failed",
                                        "error_message": "boom",
                                    },
                                }
                            ],
                        },
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )

    result = service.summarize_scenario_results(job_ids=[job_id]).model_dump(
        mode="json"
    )

    assert result["job_counts"] == {
        "total": 1,
        "running": 0,
        "completed_passed": 1,
        "completed_failed": 0,
        "unknown": 0,
    }
    by_release = {g["name"]: g for g in result["by_release"]}
    assert by_release["jammy"]["passed"] == 1
    assert by_release["jammy"]["precise"] is True
    assert by_release["resolute"]["failed"] == 1
    by_machine_type = {g["name"]: g for g in result["by_machine_type"]}
    assert by_machine_type["lxd-container"]["passed"] == 1
    assert by_machine_type["lxd-vm"]["failed"] == 1
    assert len(result["failures"]) == 1
    failure = result["failures"][0]
    assert failure["job_id"] == job_id
    assert failure["releases"] == ["resolute"]
    assert failure["machine_types"] == ["lxd-vm"]
    assert failure["precise"] is True
    assert result["matched_job_ids"] == [job_id]
    assert result["truncated"] is False


def test_summarize_scenario_results_filters_by_feature_file(tmp_path):
    (tmp_path / "jobmatch_meta.json").write_text(
        json.dumps(
            {
                "feature_file": "features/a.feature",
                "started_at": "T1",
                "releases": [],
                "machine_types": [],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "jobother_meta.json").write_text(
        json.dumps(
            {
                "feature_file": "features/b.feature",
                "started_at": "T2",
                "releases": [],
                "machine_types": [],
            }
        ),
        encoding="utf-8",
    )

    service = _make_service(
        FakeWorkspace(repo_root=tmp_path, log_dir=tmp_path),
    )

    result = service.summarize_scenario_results(
        feature_file="features/a.feature"
    ).model_dump(mode="json")

    assert result["matched_job_ids"] == ["jobmatch"]


def test_summarize_scenario_results_falls_back_for_legacy_jobs(
    tmp_path,
):
    job_id = "joblegacy"
    (tmp_path / f"{job_id}_meta.json").write_text(
        json.dumps(
            {
                "feature_file": "features/a.feature",
                "started_at": "T1",
                "releases": ["jammy"],
                "machine_types": ["lxd-container"],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / f"{job_id}_report.json").write_text(
        json.dumps(
            [
                {
                    "name": "feature",
                    "elements": [
                        {
                            "name": "scenario",
                            "location": "features/a.feature:99",
                            "steps": [
                                {
                                    "name": "step",
                                    "result": {"status": "passed"},
                                }
                            ],
                        }
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )

    service = _make_service(
        FakeWorkspace(repo_root=tmp_path, log_dir=tmp_path),
    )

    result = service.summarize_scenario_results().model_dump(mode="json")

    by_release = {g["name"]: g for g in result["by_release"]}
    assert by_release["jammy"]["passed"] == 1
    assert by_release["jammy"]["precise"] is False


def test_summarize_scenario_results_rejects_invalid_status(tmp_path):
    service = _make_service(
        FakeWorkspace(repo_root=tmp_path, log_dir=tmp_path),
    )

    with pytest.raises(BehaveServiceError, match="Invalid status filter"):
        service.summarize_scenario_results(status="bogus")


def test_summarize_scenario_results_truncates_failures(tmp_path):
    job_id = "jobtrunc"
    (tmp_path / f"{job_id}_meta.json").write_text(
        json.dumps(
            {
                "feature_file": "features/a.feature",
                "started_at": "T1",
                "releases": [],
                "machine_types": [],
            }
        ),
        encoding="utf-8",
    )
    elements = [
        {
            "name": f"scenario{i}",
            "location": f"features/a.feature:{i}",
            "steps": [
                {
                    "name": "step",
                    "result": {"status": "failed", "error_message": "boom"},
                }
            ],
        }
        for i in range(3)
    ]
    (tmp_path / f"{job_id}_report.json").write_text(
        json.dumps([{"name": "feature", "elements": elements}]),
        encoding="utf-8",
    )

    service = _make_service(
        FakeWorkspace(repo_root=tmp_path, log_dir=tmp_path),
    )

    result = service.summarize_scenario_results(limit=2).model_dump(
        mode="json"
    )

    assert len(result["failures"]) == 2
    assert result["truncated"] is True
