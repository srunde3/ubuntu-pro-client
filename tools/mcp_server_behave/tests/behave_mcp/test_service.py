import json
from pathlib import Path

import pytest

from behave_mcp import domain
from behave_mcp.adapters import (
    InMemoryJobRegistry,
    LocalFeatureFileReader,
    LocalJobResultStoreFactory,
)
from behave_mcp.config import Settings
from behave_mcp.messages import RepoState
from behave_mcp.ports import Job, LogFileOpenError, ProcessStartError
from behave_mcp.service import (
    BehaveService,
    BehaveServiceError,
    UnknownJobError,
)
from tests.conftest import (
    FakeLauncher,
    FakeProcessHandle,
    FakeWorkspace,
    make_repo_with_feature,
)


def _settings(*, allow_cloud=False, max_parallel_jobs=1) -> Settings:
    return Settings(
        allow_cloud_machine_types=allow_cloud,
        max_parallel_jobs=max_parallel_jobs,
        campaign_poll_timeout=60,
        transport="stdio",
        host="127.0.0.1",
        port=8000,
    )


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
        results=LocalJobResultStoreFactory(),
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
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    service = _make_service(FakeWorkspace(repo_root=repo_root))

    result = service.list_features().model_dump(mode="json")

    paths = [feature["path"] for feature in result["features"]]
    assert "features/cli/attach.feature" in paths


def test_list_features_uses_repo_root_override(tmp_path):
    repo_root = make_repo_with_feature(tmp_path, "features/cli/sample.feature")
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
    Given a `<release>` `<machine_type>` machine with \
ubuntu-advantage-tools installed
    When I attach

    Examples: ubuntu release
      | release  | machine_type  |
      | jammy    | lxd-container |
      | resolute | lxd-vm        |

  @arm64
  Scenario Outline: Attach invalid token
    Given a `<release>` `<machine_type>` machine with \
ubuntu-advantage-tools installed
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


def test_find_scenarios_caps_matches_but_counts_them_all(tmp_path):
    repo_root = _make_repo_with_outline(tmp_path)
    service = _make_service(FakeWorkspace(repo_root=repo_root))

    result = service.find_scenarios(limit=1)

    assert len(result.matches) == 1
    assert result.total >= 2
    assert result.truncated is True
    assert result.limit_clamped is False

    with pytest.raises(BehaveServiceError, match="limit must be"):
        service.find_scenarios(limit=0)


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
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
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
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
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
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
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
    assert (
        "features.behave_combo_formatter:ComboFormatter" not in call["command"]
    )
    assert call["cwd"] == str(repo_root)
    assert call["env"]["UACLIENT_BEHAVE_CONTRACT_TOKEN"] == "token"


def test_start_scenario_defaults_install_from_to_local(tmp_path):
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    launcher = FakeLauncher()
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, log_dir=tmp_path),
        launcher=launcher,
    )

    service.start_scenario(
        "features/cli/attach.feature",
        machine_types=["lxd-container"],
    )

    assert launcher.calls[0]["env"]["UACLIENT_BEHAVE_INSTALL_FROM"] == "local"


def test_start_scenario_sets_install_from_env_var(tmp_path):
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    launcher = FakeLauncher()
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, log_dir=tmp_path),
        launcher=launcher,
    )

    service.start_scenario(
        "features/cli/attach.feature",
        machine_types=["lxd-container"],
        install_from="proposed",
    )

    assert (
        launcher.calls[0]["env"]["UACLIENT_BEHAVE_INSTALL_FROM"] == "proposed"
    )


def test_start_scenario_rejects_invalid_install_from(tmp_path):
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, log_dir=tmp_path)
    )

    with pytest.raises(BehaveServiceError, match="Unsupported install_from"):
        service.start_scenario(
            "features/cli/attach.feature",
            machine_types=["lxd-container"],
            install_from="custom",
        )


def test_start_scenario_includes_repo_state_in_response_and_record(tmp_path):
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    launcher = FakeLauncher()
    service = _make_service(
        FakeWorkspace(
            repo_root=repo_root,
            log_dir=tmp_path,
            repo_state=RepoState(
                commit="deadbeef", branch="main", dirty=False
            ),
        ),
        launcher=launcher,
    )

    result = service.start_scenario(
        "features/cli/attach.feature",
        machine_types=["lxd-container"],
    ).model_dump(mode="json")

    assert result["repo_state"] == {
        "commit": "deadbeef",
        "branch": "main",
        "dirty": False,
    }

    store = LocalJobResultStoreFactory().bind(tmp_path)
    record = store.read_record(result["job_id"])
    assert record.repo_state == RepoState(
        commit="deadbeef", branch="main", dirty=False
    )


def test_start_scenario_uses_repo_root_override(tmp_path):
    repo_root = make_repo_with_feature(tmp_path, "features/cli/sample.feature")
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


def test_start_scenario_does_not_reference_combo_formatter(tmp_path):
    repo_root = _make_repo_with_outline(tmp_path)
    launcher = FakeLauncher()
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, log_dir=tmp_path),
        launcher=launcher,
    )

    service.start_scenario(
        "features/cli/attach.feature",
        machine_types=["lxd-container", "lxd-vm"],
    ).model_dump(mode="json")

    call = launcher.calls[0]
    assert (
        "features.behave_combo_formatter:ComboFormatter" not in call["command"]
    )
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
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, log_dir=tmp_path)
    )

    with pytest.raises(BehaveServiceError, match="machine_types is required"):
        service.start_scenario("features/cli/attach.feature", [])


def test_start_scenario_rejects_cloud_machine_type_by_default(tmp_path):
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
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
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, log_dir=tmp_path),
        settings=_settings(allow_cloud=True),
    )

    result = service.start_scenario(
        "features/cli/attach.feature", machine_types=["azure.generic"]
    ).model_dump(mode="json")

    assert result["ok"] is True


def test_start_scenario_fails_fast_when_capacity_reached(tmp_path):
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    registry = InMemoryJobRegistry()
    ids = iter(["job1", "job2"])
    launcher = FakeLauncher(handle=FakeProcessHandle(returncode=None))
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
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
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
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
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


def test_wait_for_completion_unknown_job_id_is_actionable(tmp_path):
    service = _make_service(
        FakeWorkspace(repo_root=tmp_path, log_dir=tmp_path)
    )

    with pytest.raises(UnknownJobError, match="list_scenario_jobs"):
        service.wait_for_completion("nope", max_wait_seconds=1)


def test_wait_for_completion_running_to_completed(tmp_path):
    registry = InMemoryJobRegistry()
    job_id = "job12345"
    stdout_log = tmp_path / f"{job_id}_stdout.log"
    report = tmp_path / f"{job_id}_report.json"
    stdout_log.write_text("line1\nline2\n", encoding="utf-8")
    handle = FakeProcessHandle(returncode=None)
    registry.register(
        job_id,
        Job(
            job_id=job_id,
            process_handle=handle,
            log_dir=tmp_path,
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
    handle = FakeProcessHandle(returncode=2)
    registry.register(
        job_id,
        Job(
            job_id=job_id,
            process_handle=handle,
            log_dir=tmp_path,
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
    handle = FakeProcessHandle(returncode=None)
    registry.register(
        job_id,
        Job(
            job_id=job_id,
            process_handle=handle,
            log_dir=tmp_path,
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
    handle = FakeProcessHandle(returncode=0)
    registry.register(
        job_id,
        Job(
            job_id=job_id,
            process_handle=handle,
            log_dir=tmp_path,
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
            log_dir=tmp_path,
        ),
    )
    service = _make_service(
        FakeWorkspace(repo_root=tmp_path, log_dir=tmp_path),
        registry=registry,
    )

    result = service.get_logs(job_id, lines=2).model_dump(mode="json")
    assert result["text"] == "2: l2\n3: l3"
    assert result["total_lines"] == 3
    assert (result["first_line"], result["last_line"]) == (2, 3)
    assert result["truncated"] is True
    assert result["lines_clamped"] is False
    assert result["log_path"] == str(stdout_log)


def test_get_logs_searches_with_context(tmp_path):
    registry = InMemoryJobRegistry()
    job_id = "jobgrep"
    stdout_log = tmp_path / f"{job_id}_stdout.log"
    stdout_log.write_text(
        "ok\nTraceback (most recent call last):\n  x\nKeyError: k\nok\n",
        encoding="utf-8",
    )
    registry.register(
        job_id, Job(job_id=job_id, process_handle=None, log_dir=tmp_path)
    )
    service = _make_service(
        FakeWorkspace(repo_root=tmp_path, log_dir=tmp_path),
        registry=registry,
    )

    result = service.get_logs(
        job_id, pattern="traceback|error", context=0
    ).model_dump(mode="json")
    assert (
        result["text"]
        == "2: Traceback (most recent call last):\n--\n4: KeyError: k"
    )
    assert result["matches"] == 2

    with pytest.raises(BehaveServiceError, match="invalid pattern"):
        service.get_logs(job_id, pattern="(")


def test_get_logs_clamps_lines_above_max(tmp_path):
    registry = InMemoryJobRegistry()
    job_id = "jobtail"
    stdout_log = tmp_path / f"{job_id}_stdout.log"
    stdout_log.write_text("l1\nl2\nl3\n", encoding="utf-8")
    registry.register(
        job_id,
        Job(
            job_id=job_id,
            process_handle=None,
            log_dir=tmp_path,
        ),
    )
    service = _make_service(
        FakeWorkspace(repo_root=tmp_path, log_dir=tmp_path),
        registry=registry,
    )

    result = service.get_logs(
        job_id, lines=domain.MAX_LOG_LINES + 1000
    ).model_dump(mode="json")
    assert result["lines_clamped"] is True


def test_get_logs_rejects_non_positive_lines(tmp_path):
    service = _make_service(
        FakeWorkspace(repo_root=tmp_path, log_dir=tmp_path)
    )

    with pytest.raises(BehaveServiceError, match="lines must be a positive"):
        service.get_logs("whatever", lines=0)


def test_get_logs_rejects_invalid_repo_root():
    service = _make_service(
        FakeWorkspace(repo_root_error="Invalid repo_root: bad")
    )

    with pytest.raises(BehaveServiceError, match="Invalid repo_root"):
        service.get_logs("missing-job", repo_root="/bad")


def test_get_logs_unknown_job_id_is_actionable(tmp_path):
    service = _make_service(
        FakeWorkspace(repo_root=tmp_path, log_dir=tmp_path)
    )

    with pytest.raises(UnknownJobError, match="list_scenario_jobs"):
        service.get_logs("nope")


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
            log_dir=tmp_path,
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


def test_get_artifacts_unknown_job_id_is_actionable(tmp_path):
    service = _make_service(
        FakeWorkspace(repo_root=tmp_path, log_dir=tmp_path)
    )

    with pytest.raises(UnknownJobError, match="list_scenario_jobs"):
        service.get_artifacts("nope")


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
            process_handle=FakeProcessHandle(returncode=None, pid=111),
            log_dir=tmp_path,
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
    total = domain.DEFAULT_JOB_LIST_LIMIT + 5
    for i in range(total):
        job_id = f"jobold{i:03d}"
        (tmp_path / f"{job_id}_meta.json").write_text(
            json.dumps({"started_at": f"T{i:03d}"}), encoding="utf-8"
        )

    service = _make_service(
        FakeWorkspace(repo_root=tmp_path, log_dir=tmp_path),
    )

    result = service.list_jobs().model_dump(mode="json")

    assert len(result["jobs"]) == domain.DEFAULT_JOB_LIST_LIMIT
    assert result["total_completed"] == total
    assert result["truncated"] is True
    kept_ids = {job["job_id"] for job in result["jobs"]}
    assert f"jobold{total - 1:03d}" in kept_ids
    assert "jobold000" not in kept_ids


def test_list_jobs_limit_overrides_default(tmp_path):
    total = domain.DEFAULT_JOB_LIST_LIMIT + 5
    for i in range(total):
        job_id = f"jobold{i:03d}"
        (tmp_path / f"{job_id}_meta.json").write_text(
            json.dumps({"started_at": f"T{i:03d}"}), encoding="utf-8"
        )

    service = _make_service(
        FakeWorkspace(repo_root=tmp_path, log_dir=tmp_path),
    )

    result = service.list_jobs(limit=total).model_dump(mode="json")

    assert len(result["jobs"]) == total
    assert result["total_completed"] == total
    assert result["truncated"] is False


def test_list_jobs_not_truncated_when_under_limit(tmp_path):
    (tmp_path / "jobold000_meta.json").write_text(
        json.dumps({"started_at": "T000"}), encoding="utf-8"
    )

    service = _make_service(
        FakeWorkspace(repo_root=tmp_path, log_dir=tmp_path),
    )

    result = service.list_jobs().model_dump(mode="json")

    assert result["total_completed"] == 1
    assert result["truncated"] is False


def test_list_jobs_limit_clamped_when_too_high(tmp_path):
    (tmp_path / "jobold000_meta.json").write_text(
        json.dumps({"started_at": "T000"}), encoding="utf-8"
    )
    service = _make_service(
        FakeWorkspace(repo_root=tmp_path, log_dir=tmp_path),
    )

    result = service.list_jobs(
        limit=domain.MAX_JOB_LIST_LIMIT + 1000
    ).model_dump(mode="json")
    assert result["truncated"] is False
    assert result["limit_clamped"] is True


def test_list_jobs_rejects_non_positive_limit(tmp_path):
    service = _make_service(
        FakeWorkspace(repo_root=tmp_path, log_dir=tmp_path),
    )

    with pytest.raises(BehaveServiceError, match="limit must be a positive"):
        service.list_jobs(limit=0)


def test_list_jobs_rejects_invalid_repo_root():
    service = _make_service(
        FakeWorkspace(repo_root_error="Invalid repo_root: bad")
    )

    with pytest.raises(BehaveServiceError, match="Invalid repo_root"):
        service.list_jobs(repo_root="/bad")


# ---- summarize_scenario_results ----


def test_get_results_reports_one_result_per_job(tmp_path):
    repo_root = _make_repo_with_outline(tmp_path)
    handle = FakeProcessHandle(returncode=0)
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
        releases=["jammy", "resolute"],
    ).model_dump(mode="json")
    job_id = start_result["job_id"]

    report_path = Path(start_result["artifacts"]["json_report"])
    report_path.write_text(
        json.dumps(
            [
                {
                    "name": "Attach things",
                    "elements": [
                        {
                            "name": "Attach on a machine -- @1.1",
                            "location": "features/cli/attach.feature:10",
                            "steps": [
                                {
                                    "name": "step1",
                                    "result": {"status": "passed"},
                                }
                            ],
                        },
                        {
                            "name": "Attach on a machine -- @1.2",
                            "location": "features/cli/attach.feature:11",
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

    result = service.get_results(job_ids=[job_id]).model_dump(mode="json")

    assert result["total"] == 1
    assert result["truncated"] is False
    (job,) = result["results"]
    assert job["job_id"] == job_id
    assert job["status"] == "completed"
    assert job["ok"] is True
    assert job["feature_file"] == "features/cli/attach.feature"
    assert job["releases"] == ["jammy", "resolute"]
    assert job["machine_types"] == ["lxd-container", "lxd-vm"]
    assert job["summary"]["scenarios"] == {
        "total": 2,
        "passed": 1,
        "failed": 1,
        "skipped": 0,
        "unknown": 0,
    }
    assert job["failures"] == [
        {
            "scenario": "Attach on a machine -- @1.2",
            "step": "step2",
            "status": "failed",
            "error_message": "boom",
        }
    ]


def _write_meta(tmp_path, job_id, feature_file, started_at, **extra):
    (tmp_path / f"{job_id}_meta.json").write_text(
        json.dumps(
            {
                "feature_file": feature_file,
                "started_at": started_at,
                "releases": extra.get("releases", []),
                "machine_types": extra.get("machine_types", []),
            }
        ),
        encoding="utf-8",
    )


def test_get_results_filters_by_feature_file_newest_first(tmp_path):
    _write_meta(tmp_path, "jobmatch", "features/a.feature", "T1")
    _write_meta(tmp_path, "jobnewer", "features/a.feature", "T2")
    _write_meta(tmp_path, "jobother", "features/b.feature", "T3")
    service = _make_service(
        FakeWorkspace(repo_root=tmp_path, log_dir=tmp_path),
    )

    result = service.get_results(feature_file="features/a.feature")

    assert [r.job_id for r in result.results] == ["jobnewer", "jobmatch"]
    assert result.total == 2


def test_get_results_recovers_disk_only_job(tmp_path):
    job_id = "joblegacy"
    _write_meta(
        tmp_path,
        job_id,
        "features/a.feature",
        "T1",
        releases=["jammy"],
        machine_types=["lxd-container"],
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

    (job,) = service.get_results().results

    assert job.status == "completed"
    assert job.summary is not None
    assert job.summary["scenarios"]["passed"] == 1
    assert job.releases == ["jammy"]


def test_get_results_rejects_invalid_status(tmp_path):
    service = _make_service(
        FakeWorkspace(repo_root=tmp_path, log_dir=tmp_path),
    )
    with pytest.raises(BehaveServiceError, match="Invalid status filter"):
        service.get_results(status="bogus")


def test_get_results_rejects_non_positive_limit(tmp_path):
    service = _make_service(
        FakeWorkspace(repo_root=tmp_path, log_dir=tmp_path),
    )
    with pytest.raises(BehaveServiceError, match="limit must be"):
        service.get_results(limit=0)


def test_get_results_caps_and_truncates(tmp_path):
    for number in range(3):
        _write_meta(
            tmp_path, f"job{number}", "features/a.feature", f"T{number}"
        )
    service = _make_service(
        FakeWorkspace(repo_root=tmp_path, log_dir=tmp_path),
    )

    result = service.get_results(limit=2)
    assert [r.job_id for r in result.results] == ["job2", "job1"]
    assert result.total == 3
    assert result.truncated is True

    clamped = service.get_results(limit=domain.MAX_RESULTS_LIMIT + 1)
    assert clamped.limit_clamped is True
    assert clamped.truncated is False


class TestKillJob:
    """Stopping a job that has hung, so its lane does not stay open."""

    @staticmethod
    def _service_with_job(tmp_path, handle):
        registry = InMemoryJobRegistry()
        registry.register(
            "job12345",
            Job(job_id="job12345", process_handle=handle, log_dir=tmp_path),
        )
        service = _make_service(
            FakeWorkspace(repo_root=tmp_path, log_dir=tmp_path),
            registry=registry,
        )
        return service

    def test_a_running_job_is_terminated(self, tmp_path):
        handle = FakeProcessHandle(returncode=None)
        service = self._service_with_job(tmp_path, handle)

        result = service.kill_job("job12345")

        assert result.killed is True
        assert handle.terminated is True
        assert result.job_id == "job12345"

    def test_a_finished_job_is_left_alone(self, tmp_path):
        handle = FakeProcessHandle(returncode=0)
        service = self._service_with_job(tmp_path, handle)

        result = service.kill_job("job12345")

        assert result.killed is False
        assert handle.terminated is False
        assert "already finished" in result.message

    def test_a_job_without_a_live_handle_reports_rather_than_raises(
        self, tmp_path
    ):
        # What a job recovered from disk after a restart looks like: the
        # record is there, but no handle in this process.
        service = self._service_with_job(tmp_path, None)

        result = service.kill_job("job12345")

        assert result.killed is False
        assert "No live handle" in result.message

    def test_an_unknown_job_is_rejected(self, tmp_path):
        service = _make_service(
            FakeWorkspace(repo_root=tmp_path, log_dir=tmp_path)
        )

        with pytest.raises(UnknownJobError):
            service.kill_job("nope")


def test_get_errors_digests_the_log(tmp_path):
    registry = InMemoryJobRegistry()
    job_id = "joberr"
    stdout_log = tmp_path / f"{job_id}_stdout.log"
    stdout_log.write_text(
        "    Given a machine ... error in 0.1s\n"
        "Traceback (most recent call last):\n"
        '  File "steps.py", line 4, in given\n'
        "KeyError: 'base'\n"
        "\n"
        "Errored scenarios:\n"
        "  f.feature:1  s\n"
        "\n"
        "0 features passed, 0 failed, 1 error, 0 skipped\n"
        "Took 0min 0.002s\n",
        encoding="utf-8",
    )
    registry.register(
        job_id, Job(job_id=job_id, process_handle=None, log_dir=tmp_path)
    )
    service = _make_service(
        FakeWorkspace(repo_root=tmp_path, log_dir=tmp_path),
        registry=registry,
    )

    result = service.get_errors(job_id).model_dump(mode="json")

    assert result["finished"] is True
    assert result["errors_total"] == 1
    (region,) = result["errors"]
    assert region["kind"] == "traceback"
    assert region["exception"] == "KeyError: 'base'"
    assert region["step"] == {
        "line": 1,
        "text": "Given a machine ... error in 0.1s",
    }
    assert result["summary"]["first_line"] == 6
    assert result["log_path"] == str(stdout_log)


def test_get_errors_unknown_job_id_is_actionable(tmp_path):
    service = _make_service(
        FakeWorkspace(repo_root=tmp_path, log_dir=tmp_path)
    )
    with pytest.raises(UnknownJobError):
        service.get_errors("nope")
