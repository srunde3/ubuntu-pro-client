import json
from pathlib import Path

import pytest

from behave_mcp.adapters import (
    LocalFeatureFileReader,
    LocalJobResultStore,
    LocalJobResultStoreFactory,
)
from behave_mcp.config import Settings
from behave_mcp.domain.limits import (
    DEFAULT_JOB_LIST_LIMIT,
    MAX_JOB_LIST_LIMIT,
    MAX_LOG_LINES,
    MAX_RESULTS_LIMIT,
)
from behave_mcp.domain.pools import Pool
from behave_mcp.messages import JobRecord, RepoState, RunStatus
from behave_mcp.ports import JobStartError
from behave_mcp.service import (
    BehaveService,
    BehaveServiceError,
    UnknownJobError,
)
from tests.conftest import (
    UNIT_FEATURE,
    UNIT_SCENARIO,
    FakeJobLauncher,
    FakeLanePool,
    FakeWorkspace,
    job_file,
    make_repo_with_feature,
)


def _settings(*, lxd=1, cloud=0, wsl=1) -> Settings:
    return Settings(
        pool_limits={Pool.LXD: lxd, Pool.CLOUD: cloud, Pool.WSL: wsl},
        campaign_poll_timeout=60,
        host="127.0.0.1",
        port=8000,
    )


def start(service, machine_type):
    """Start the unit fixture feature's scenario on ``machine_type``."""
    return service.start_scenario(
        "features/cli/attach.feature",
        scenario_name=UNIT_SCENARIO,
        release="jammy",
        machine_type=machine_type,
    ).model_dump(mode="json")


def _make_service(
    workspace,
    *,
    settings=None,
    launcher=None,
    lanes=None,
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
        lanes=lanes if lanes is not None else FakeLanePool(),
        launcher=launcher if launcher is not None else FakeJobLauncher(),
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
            scenario_name=UNIT_SCENARIO,
            release="jammy",
            machine_type="lxd-container",
        )


def test_start_scenario_accepts_normalized_listed_feature(tmp_path):
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    launcher = FakeJobLauncher()
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, log_dir=tmp_path),
        launcher=launcher,
    )

    result = service.start_scenario(
        "features/cli/../cli/attach.feature",
        scenario_name=UNIT_SCENARIO,
        release="jammy",
        machine_type="lxd-container",
    ).model_dump(mode="json")

    assert result["ok"] is True
    assert "artifacts" in result
    assert result["artifacts"]["record"].endswith("/job.json")


def test_start_scenario_refuses_a_name_the_feature_lacks(tmp_path):
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    (repo_root / "features/cli/attach.feature").write_text(
        "Feature: attach\n"
        "  Scenario: Attach command with token\n"
        "    Given a step\n",
        encoding="utf-8",
    )
    launcher = FakeJobLauncher()
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, log_dir=tmp_path),
        launcher=launcher,
    )

    with pytest.raises(BehaveServiceError, match="exact name"):
        service.start_scenario(
            "features/cli/attach.feature",
            scenario_name="Attach command",
            release="jammy",
            machine_type="lxd-container",
        )
    assert launcher.calls == []


def test_start_scenario_refuses_a_combo_the_scenario_lacks(tmp_path):
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    launcher = FakeJobLauncher()
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, log_dir=tmp_path),
        launcher=launcher,
    )

    with pytest.raises(BehaveServiceError, match="does not run"):
        service.start_scenario(
            "features/cli/attach.feature",
            scenario_name=UNIT_SCENARIO,
            release="noble",
            machine_type="lxd-container",
        )
    assert launcher.calls == []


def test_start_scenario_refuses_an_ambiguous_unit(tmp_path):
    # Two outlines share a name and the jammy lxd-container row, so behave
    # would run both: no job can name exactly one unit.
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    (repo_root / "features/cli/attach.feature").write_text(
        UNIT_FEATURE + "\n" + UNIT_FEATURE.split("\n", 1)[1],
        encoding="utf-8",
    )
    launcher = FakeJobLauncher()
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, log_dir=tmp_path),
        launcher=launcher,
    )

    with pytest.raises(BehaveServiceError, match="More than one scenario"):
        service.start_scenario(
            "features/cli/attach.feature",
            scenario_name=UNIT_SCENARIO,
            release="jammy",
            machine_type="lxd-container",
        )
    assert launcher.calls == []


def test_start_scenario_builds_command(tmp_path):
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    launcher = FakeJobLauncher()
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
        scenario_name=UNIT_SCENARIO,
        release="jammy",
        machine_type="lxd-container",
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
    launcher = FakeJobLauncher()
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, log_dir=tmp_path),
        launcher=launcher,
    )

    service.start_scenario(
        "features/cli/attach.feature",
        scenario_name=UNIT_SCENARIO,
        release="jammy",
        machine_type="lxd-container",
    )

    assert launcher.calls[0]["env"]["UACLIENT_BEHAVE_INSTALL_FROM"] == "local"


def test_start_scenario_sets_install_from_env_var(tmp_path):
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    launcher = FakeJobLauncher()
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, log_dir=tmp_path),
        launcher=launcher,
    )

    service.start_scenario(
        "features/cli/attach.feature",
        scenario_name=UNIT_SCENARIO,
        release="jammy",
        machine_type="lxd-container",
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
            scenario_name=UNIT_SCENARIO,
            release="jammy",
            machine_type="lxd-container",
            install_from="custom",
        )


def test_start_scenario_includes_repo_state_in_response_and_record(tmp_path):
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    launcher = FakeJobLauncher()
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
        scenario_name=UNIT_SCENARIO,
        release="jammy",
        machine_type="lxd-container",
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


def test_a_repo_root_override_runs_there_but_files_stay_with_the_server(
    tmp_path,
):
    other_checkout = make_repo_with_feature(
        tmp_path, "features/cli/sample.feature", name="worktree"
    )
    server_jobs = tmp_path / "server-state" / "jobs"
    server_jobs.mkdir(parents=True)
    launcher = FakeJobLauncher()
    service = _make_service(
        FakeWorkspace(repo_root=None, log_dir=server_jobs), launcher=launcher
    )

    result = service.start_scenario(
        "features/cli/sample.feature",
        scenario_name=UNIT_SCENARIO,
        release="jammy",
        machine_type="lxd-container",
        repo_root=str(other_checkout),
    ).model_dump(mode="json")

    assert result["ok"] is True
    assert launcher.calls[0]["cwd"] == str(other_checkout)
    assert (server_jobs / result["job_id"] / "job.json").is_file()
    record = json.loads(
        (job_file(server_jobs, result["job_id"], "job.json")).read_text()
    )
    assert record["repo_root"] == str(other_checkout)


def test_start_scenario_does_not_reference_combo_formatter(tmp_path):
    repo_root = _make_repo_with_outline(tmp_path)
    launcher = FakeJobLauncher()
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, log_dir=tmp_path),
        launcher=launcher,
    )

    service.start_scenario(
        "features/cli/attach.feature",
        scenario_name="Attach on a machine",
        release="jammy",
        machine_type="lxd-container",
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
            scenario_name=UNIT_SCENARIO,
            release="jammy",
            machine_type="lxd-container",
            repo_root="/bad",
        )


def test_start_scenario_refuses_an_unknown_machine_type(tmp_path):
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, log_dir=tmp_path)
    )

    with pytest.raises(BehaveServiceError, match="Unsupported machine_type"):
        service.start_scenario(
            "features/cli/attach.feature",
            scenario_name=UNIT_SCENARIO,
            release="jammy",
            machine_type="lxd-ctr",
        )


@pytest.mark.parametrize("machine_type", ["azure.generic", "aws.pro-fips"])
def test_start_scenario_rejects_cloud_machine_type_by_default(
    tmp_path, machine_type
):
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, log_dir=tmp_path)
    )

    with pytest.raises(
        BehaveServiceError,
        match="cloud machine types are disabled.*MCP_MAX_CLOUD_JOBS",
    ):
        service.start_scenario(
            "features/cli/attach.feature",
            scenario_name=UNIT_SCENARIO,
            release="jammy",
            machine_type=machine_type,
        )


def test_start_scenario_rejects_a_disabled_wsl_pool(tmp_path):
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, log_dir=tmp_path),
        settings=_settings(wsl=0),
    )

    with pytest.raises(BehaveServiceError, match="MCP_MAX_WSL_JOBS"):
        service.start_scenario(
            "features/cli/attach.feature",
            scenario_name=UNIT_SCENARIO,
            release="jammy",
            machine_type="wsl",
        )


def test_start_scenario_allows_cloud_machine_type_with_a_limit(tmp_path):
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, log_dir=tmp_path),
        settings=_settings(cloud=1),
    )

    result = service.start_scenario(
        "features/cli/attach.feature",
        scenario_name=UNIT_SCENARIO,
        release="jammy",
        machine_type="azure.generic",
    ).model_dump(mode="json")

    assert result["ok"] is True


def test_start_scenario_fails_fast_when_capacity_reached(tmp_path):
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    ids = iter(["job1", "job2"])
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, log_dir=tmp_path),
        settings=_settings(lxd=1),
        new_job_id=lambda: next(ids),
    )
    first = start(service, "lxd-container")

    second = start(service, "lxd-container")

    assert first["ok"] is True
    assert second["status"] == "capacity_exceeded"
    assert second["full_pools"] == [{"pool": "lxd", "limit": 1, "running": 1}]
    assert "MCP_MAX_LXD_JOBS" in second["error"]


def test_a_full_pool_leaves_other_pools_free(tmp_path):
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    ids = iter(["job1", "job2", "job3"])
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, log_dir=tmp_path),
        settings=_settings(lxd=2, wsl=1),
        new_job_id=lambda: next(ids),
    )
    start(service, "wsl")

    second_wsl = start(service, "wsl")
    lxd = start(service, "lxd-container")

    assert second_wsl["full_pools"] == [
        {"pool": "wsl", "limit": 1, "running": 1}
    ]
    assert lxd["ok"] is True


def test_a_finished_job_frees_its_lane(tmp_path):
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    ids = iter(["job1", "job2"])
    launcher = FakeJobLauncher(tmp_path)
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, log_dir=tmp_path),
        settings=_settings(lxd=1),
        launcher=launcher,
        new_job_id=lambda: next(ids),
    )
    start(service, "lxd-container")
    launcher.finish("job1")

    second = start(service, "lxd-container")

    assert second["ok"] is True


def test_a_launch_that_fails_frees_its_lane_and_reads_as_interrupted(
    tmp_path,
):
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    lanes = FakeLanePool()
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, log_dir=tmp_path),
        lanes=lanes,
        launcher=FakeJobLauncher(tmp_path, error=JobStartError("boom")),
        new_job_id=lambda: JOB_ID,
    )

    with pytest.raises(BehaveServiceError, match="Failed to start behave"):
        start(service, "lxd-container")

    assert lanes.busy(Pool.LXD, 1) == 0
    assert service.job_status(JOB_ID).status == "interrupted"


# ---- one job, read from its files and its lock ----


JOB_ID = "job-under-test"


OTHER_JOB_ID = "job-from-another-run"


def launched(tmp_path, job_id=JOB_ID, **record):
    """A job as the service launched it: its record, nothing more."""
    fields = dict(
        job_id=job_id,
        started_at="T0",
        feature_file="features/cli/attach.feature",
        scenario_name=UNIT_SCENARIO,
        release="jammy",
        machine_type="lxd-container",
    )
    fields.update(record)
    LocalJobResultStore(tmp_path).write_record(job_id, JobRecord(**fields))


def job_service(tmp_path, launcher, **kwargs):
    return _make_service(
        FakeWorkspace(repo_root=tmp_path, log_dir=tmp_path),
        launcher=launcher,
        **kwargs,
    )


PASSING_REPORT = [
    {
        "name": "attach feature",
        "elements": [
            {
                "name": "attach scenario",
                "steps": [
                    {"name": "do attach", "result": {"status": "passed"}}
                ],
            }
        ],
    }
]


def arrange(tmp_path, state):
    """A launched job in ``state``, and the launcher that knows of it."""
    launched(tmp_path)
    launcher = FakeJobLauncher(tmp_path)
    if state == RunStatus.RUNNING:
        launcher.running.add(JOB_ID)
    elif state == RunStatus.COMPLETED:
        launcher.finish(JOB_ID, report=PASSING_REPORT)
    job_file(tmp_path, JOB_ID, "stdout.log").write_text("line1\nline2\n")
    return launcher


# What a status read reports for a job in each state.
STATUS_OF = {
    RunStatus.RUNNING: ("running", True),
    RunStatus.COMPLETED: ("completed", True),
    RunStatus.INTERRUPTED: ("interrupted", False),
}


def test_every_job_state_has_an_expected_status():
    assert set(STATUS_OF) == set(RunStatus)


@pytest.mark.parametrize("state, expected", STATUS_OF.items(), ids=str)
def test_job_status_reads_the_state_off_disk(tmp_path, state, expected):
    service = job_service(tmp_path, arrange(tmp_path, state))

    payload = service.job_status(JOB_ID)

    assert (payload.status, payload.ok) == expected


def test_a_completed_job_reports_its_recorded_exit_and_report(tmp_path):
    launched(tmp_path)
    launcher = FakeJobLauncher(tmp_path)
    launcher.finish(JOB_ID, returncode=1, report=PASSING_REPORT)
    service = job_service(tmp_path, launcher)

    payload = service.job_status(JOB_ID).model_dump(mode="json")

    assert payload["ok"] is False
    assert payload["returncode"] == 1
    assert payload["summary"]["steps"]["passed"] == 1
    assert payload["recent_output"] is None
    assert payload["release"] == "jammy"


def test_a_completed_job_without_a_report_shows_its_output(tmp_path):
    launched(tmp_path)
    launcher = FakeJobLauncher(tmp_path)
    launcher.finish(JOB_ID, returncode=2, log="tox failed\n")
    service = job_service(tmp_path, launcher)

    payload = service.job_status(JOB_ID).model_dump(mode="json")

    assert payload["summary"] is None
    assert "tox failed" in payload["recent_output"]


def test_a_killed_job_is_not_ok(tmp_path):
    launched(tmp_path)
    launcher = FakeJobLauncher(tmp_path)
    launcher.finish(JOB_ID, returncode=0, killed=True)
    service = job_service(tmp_path, launcher)

    assert service.job_status(JOB_ID).ok is False


def test_reading_a_job_never_writes_its_exit(tmp_path):
    # The wrapper alone records an exit; the server only reads it.
    launcher = arrange(tmp_path, RunStatus.RUNNING)
    service = job_service(tmp_path, launcher)

    service.job_status(JOB_ID)
    service.list_jobs()
    service.get_results()

    assert not job_file(tmp_path, JOB_ID, "status.json").exists()


def test_wait_for_completion_returns_once_the_job_ends(tmp_path):
    launcher = arrange(tmp_path, RunStatus.RUNNING)
    service = job_service(
        tmp_path,
        launcher,
        sleep=lambda seconds: launcher.finish(JOB_ID, report=PASSING_REPORT),
    )

    result = service.wait_for_completion(
        JOB_ID, max_wait_seconds=5, poll_interval_seconds=0.01
    )

    assert result.status == "completed"
    assert result.ok is True


def test_wait_for_completion_returns_an_interrupted_job(tmp_path):
    launcher = arrange(tmp_path, RunStatus.RUNNING)
    service = job_service(
        tmp_path, launcher, sleep=lambda seconds: launcher.crash(JOB_ID)
    )

    result = service.wait_for_completion(
        JOB_ID, max_wait_seconds=5, poll_interval_seconds=0.01
    )

    assert result.status == "interrupted"
    assert "line2" in result.recent_output


def test_wait_for_completion_times_out_on_a_running_job(tmp_path):
    launcher = arrange(tmp_path, RunStatus.RUNNING)
    clock = iter([0.0, 0.0, 10.0])
    service = job_service(tmp_path, launcher, monotonic=lambda: next(clock))

    result = service.wait_for_completion(
        JOB_ID, max_wait_seconds=5, poll_interval_seconds=1
    ).model_dump(mode="json")

    assert result["status"] == "timeout"
    assert result["last_status"] == "running"
    assert "line2" in result["recent_output"]


@pytest.mark.parametrize(
    "call",
    [
        lambda service: service.wait_for_completion("nope", 1),
        lambda service: service.job_status("nope"),
        lambda service: service.get_logs("nope"),
        lambda service: service.get_errors("nope"),
        lambda service: service.get_artifacts("nope"),
        lambda service: service.kill_job("nope"),
    ],
    ids=["wait", "status", "logs", "errors", "artifacts", "kill"],
)
def test_an_unknown_job_id_is_actionable(tmp_path, call):
    service = job_service(tmp_path, FakeJobLauncher(tmp_path))

    with pytest.raises(UnknownJobError, match="list_scenario_jobs"):
        call(service)


# What kill_job does to a job in each state.
KILL_OF = {
    RunStatus.RUNNING: (True, "Termination signalled"),
    RunStatus.COMPLETED: (False, "already finished"),
    RunStatus.INTERRUPTED: (False, "not running"),
}


def test_every_job_state_has_an_expected_kill():
    assert set(KILL_OF) == set(RunStatus)


@pytest.mark.parametrize("state, expected", KILL_OF.items(), ids=str)
def test_kill_job_by_state(tmp_path, state, expected):
    launcher = arrange(tmp_path, state)
    service = job_service(tmp_path, launcher)

    result = service.kill_job(JOB_ID)

    killed, message = expected
    assert result.killed is killed
    assert message in result.message
    assert launcher.signalled == ([JOB_ID] if killed else [])


def test_a_wrapper_that_cannot_be_signalled_is_reported(tmp_path):
    launched(tmp_path)
    launcher = FakeJobLauncher(tmp_path, unsignallable={JOB_ID})
    launcher.running.add(JOB_ID)
    service = job_service(tmp_path, launcher)

    result = service.kill_job(JOB_ID)

    assert result.killed is False
    assert "could not be signalled" in result.message


# ---- logs, errors and artifacts ----


def logged(tmp_path, text):
    launched(tmp_path)
    log = job_file(tmp_path, JOB_ID, "stdout.log")
    log.write_text(text, encoding="utf-8")
    return log


def test_get_logs_returns_tail(tmp_path):
    logged(tmp_path, "l1\nl2\nl3\n")
    service = job_service(tmp_path, FakeJobLauncher(tmp_path))

    result = service.get_logs(JOB_ID, lines=2).model_dump(mode="json")

    assert result["text"] == "2: l2\n3: l3"
    assert result["total_lines"] == 3


def test_get_logs_searches_with_context(tmp_path):
    logged(
        tmp_path,
        "ok\nTraceback (most recent call last):\n  x\nKeyError: k\nok\n",
    )
    service = job_service(tmp_path, FakeJobLauncher(tmp_path))

    result = service.get_logs(
        JOB_ID, pattern="traceback|error", context=0
    ).model_dump(mode="json")

    assert (
        result["text"]
        == "2: Traceback (most recent call last):\n--\n4: KeyError: k"
    )
    assert result["matches"] == 2


def test_get_logs_rejects_an_invalid_pattern(tmp_path):
    logged(tmp_path, "ok\n")
    service = job_service(tmp_path, FakeJobLauncher(tmp_path))

    with pytest.raises(BehaveServiceError, match="invalid pattern"):
        service.get_logs(JOB_ID, pattern="(")


def test_get_logs_clamps_lines_above_max(tmp_path):
    logged(tmp_path, "l1\nl2\nl3\n")
    service = job_service(tmp_path, FakeJobLauncher(tmp_path))

    result = service.get_logs(JOB_ID, lines=MAX_LOG_LINES + 1000)

    assert result.lines_clamped is True


def test_get_logs_rejects_non_positive_lines(tmp_path):
    logged(tmp_path, "l1\n")
    service = job_service(tmp_path, FakeJobLauncher(tmp_path))

    with pytest.raises(BehaveServiceError, match="positive"):
        service.get_logs(JOB_ID, lines=0)


def test_get_logs_of_a_job_with_no_log_yet_is_an_error(tmp_path):
    launched(tmp_path)
    service = job_service(tmp_path, FakeJobLauncher(tmp_path))

    with pytest.raises(BehaveServiceError, match="No log file"):
        service.get_logs(JOB_ID)


def test_get_errors_digests_the_log(tmp_path):
    log = logged(
        tmp_path,
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
    )
    service = job_service(tmp_path, FakeJobLauncher(tmp_path))

    result = service.get_errors(JOB_ID).model_dump(mode="json")

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
    assert result["log_path"] == str(log)


def test_get_artifacts_reports_paths_record_and_exit(tmp_path):
    launched(tmp_path, feature_file="features/a.feature")
    launcher = FakeJobLauncher(tmp_path)
    launcher.finish(JOB_ID, log="line\n", report=[])
    service = job_service(tmp_path, launcher)

    result = service.get_artifacts(JOB_ID).model_dump(mode="json")

    assert result["exists"] == {
        "record": True,
        "status": True,
        "stdout_log": True,
        "json_report": True,
    }
    assert result["record"]["feature_file"] == "features/a.feature"
    assert result["exit"]["ok"] is True
    assert result["artifacts"]["job_dir"] == str(tmp_path / JOB_ID)


# ---- listing ----


def listed(tmp_path, launcher, job_id, started_at, state):
    launched(tmp_path, job_id=job_id, started_at=started_at)
    if state == RunStatus.RUNNING:
        launcher.running.add(job_id)
    elif state == RunStatus.COMPLETED:
        launcher.finish(job_id)


def test_list_jobs_reads_every_job_off_disk(tmp_path):
    launcher = FakeJobLauncher(tmp_path)
    listed(tmp_path, launcher, "a-running", "T1", RunStatus.RUNNING)
    listed(tmp_path, launcher, "b-done", "T2", RunStatus.COMPLETED)
    listed(tmp_path, launcher, "c-gone", "T3", RunStatus.INTERRUPTED)
    service = job_service(tmp_path, launcher)

    result = service.list_jobs()

    assert {(job.job_id, job.status) for job in result.jobs} == {
        ("a-running", RunStatus.RUNNING),
        ("b-done", RunStatus.COMPLETED),
        ("c-gone", RunStatus.INTERRUPTED),
    }


def test_list_jobs_keeps_running_jobs_and_caps_the_rest(tmp_path):
    launcher = FakeJobLauncher(tmp_path)
    listed(tmp_path, launcher, "running", "T0", RunStatus.RUNNING)
    for number in range(DEFAULT_JOB_LIST_LIMIT + 2):
        listed(
            tmp_path,
            launcher,
            "done-{:02d}".format(number),
            "T{:02d}".format(number + 1),
            RunStatus.COMPLETED,
        )
    service = job_service(tmp_path, launcher)

    result = service.list_jobs()

    assert result.jobs[0].job_id == "running"
    assert len(result.jobs) == DEFAULT_JOB_LIST_LIMIT + 1
    assert result.total_completed == DEFAULT_JOB_LIST_LIMIT + 2
    assert result.truncated is True


def test_list_jobs_is_not_truncated_under_the_limit(tmp_path):
    launcher = FakeJobLauncher(tmp_path)
    listed(tmp_path, launcher, "done", "T1", RunStatus.COMPLETED)
    service = job_service(tmp_path, launcher)

    result = service.list_jobs(limit=5)

    assert result.truncated is False


def test_list_jobs_limit_is_clamped_when_too_high(tmp_path):
    service = job_service(tmp_path, FakeJobLauncher(tmp_path))

    result = service.list_jobs(limit=MAX_JOB_LIST_LIMIT + 1)

    assert result.limit_clamped is True


def test_list_jobs_rejects_a_non_positive_limit(tmp_path):
    service = job_service(tmp_path, FakeJobLauncher(tmp_path))

    with pytest.raises(BehaveServiceError, match="positive"):
        service.list_jobs(limit=0)


# ---- results ----


TWO_SCENARIO_REPORT = [
    {
        "name": "Attach things",
        "elements": [
            {
                "name": "Attach on a machine -- @1.1",
                "location": "features/cli/attach.feature:10",
                "steps": [{"name": "step1", "result": {"status": "passed"}}],
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


def test_get_results_reports_one_result_per_job(tmp_path):
    launched(tmp_path, release="resolute", machine_type="lxd-vm")
    launcher = FakeJobLauncher(tmp_path)
    launcher.finish(JOB_ID, report=TWO_SCENARIO_REPORT)
    service = job_service(tmp_path, launcher)

    result = service.get_results(job_ids=[JOB_ID]).model_dump(mode="json")

    (job,) = result["results"]
    assert result["total"] == 1
    assert job["status"] == "completed"
    assert (job["release"], job["machine_type"]) == ("resolute", "lxd-vm")
    assert job["summary"]["scenarios"]["failed"] == 1
    assert job["failures"][0]["error_message"] == "boom"


def test_get_results_filters_by_feature_file_newest_first(tmp_path):
    launcher = FakeJobLauncher(tmp_path)
    for job_id, feature, started in [
        ("older", "features/a.feature", "T1"),
        ("newer", "features/a.feature", "T2"),
        ("other", "features/b.feature", "T3"),
    ]:
        launched(
            tmp_path, job_id=job_id, feature_file=feature, started_at=started
        )
        launcher.finish(job_id)
    service = job_service(tmp_path, launcher)

    result = service.get_results(feature_file="features/a.feature")

    assert [r.job_id for r in result.results] == ["newer", "older"]


def test_get_results_filters_by_status(tmp_path):
    launcher = FakeJobLauncher(tmp_path)
    launched(tmp_path, job_id=JOB_ID)
    launched(tmp_path, job_id=OTHER_JOB_ID)
    launcher.running.add(OTHER_JOB_ID)
    service = job_service(tmp_path, launcher)

    result = service.get_results(status="interrupted")

    assert [r.job_id for r in result.results] == [JOB_ID]


def test_get_results_rejects_an_invalid_status(tmp_path):
    service = job_service(tmp_path, FakeJobLauncher(tmp_path))

    with pytest.raises(BehaveServiceError, match="Invalid status filter"):
        service.get_results(status="bogus")


def test_get_results_rejects_a_non_positive_limit(tmp_path):
    service = job_service(tmp_path, FakeJobLauncher(tmp_path))

    with pytest.raises(BehaveServiceError, match="positive"):
        service.get_results(limit=0)


def test_get_results_truncates_at_the_limit(tmp_path):
    launcher = FakeJobLauncher(tmp_path)
    shown = 2
    for number in range(shown + 1):
        job_id = "job-{}".format(number)
        launched(tmp_path, job_id=job_id, started_at="T{}".format(number))
        launcher.finish(job_id)
    service = job_service(tmp_path, launcher)

    result = service.get_results(limit=shown)

    assert len(result.results) == shown
    assert result.total == shown + 1
    assert result.truncated is True


def test_get_results_limit_is_clamped_when_too_high(tmp_path):
    service = job_service(tmp_path, FakeJobLauncher(tmp_path))

    result = service.get_results(limit=MAX_RESULTS_LIMIT + 1)

    assert result.limit_clamped is True
