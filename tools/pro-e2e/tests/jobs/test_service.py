import json
from pathlib import Path

import pytest

from pro_e2e.jobs.adapters import (
    LocalJobResultStore,
    LocalJobResultStoreFactory,
)
from pro_e2e.jobs.domain.limits import (
    DEFAULT_JOB_LIST_LIMIT,
    MAX_JOB_LIST_LIMIT,
    MAX_LOG_LINES,
    MAX_RESULTS_LIMIT,
)
from pro_e2e.jobs.domain.pools import Pool
from pro_e2e.jobs.messages import (
    Failure,
    JobRecord,
    JobRequest,
    JobState,
    Outcome,
    RepoState,
)
from pro_e2e.jobs.ports import JobStartError
from pro_e2e.jobs.service import JobService, JobServiceError, UnknownJobError
from tests.conftest import (
    UNIT_FEATURE,
    UNIT_SCENARIO,
    FakeConfigStore,
    FakeJobLauncher,
    FakeLanePool,
    FakeWorkspace,
    job_file,
    make_repo_with_feature,
    make_service,
)


def _make_service(workspace, *, max_jobs=None, **ports) -> JobService:
    if max_jobs is not None:
        ports.setdefault("config", FakeConfigStore(max_jobs))
    return make_service(workspace, **ports)


def start(service, machine_type):
    """Start the unit fixture feature's scenario on ``machine_type``."""
    return service.run_job(
        "features/cli/attach.feature",
        scenario=UNIT_SCENARIO,
        release="jammy",
        machine_type=machine_type,
    ).model_dump(mode="json")


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

    with pytest.raises(JobServiceError, match="Invalid repo_root"):
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
        JobServiceError, match="Feature is not listed by list_features"
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

    with pytest.raises(JobServiceError, match="limit must be"):
        service.find_scenarios(limit=0)


def test_find_scenarios_filters_by_tag(tmp_path):
    repo_root = _make_repo_with_outline(tmp_path)
    service = _make_service(FakeWorkspace(repo_root=repo_root))

    result = service.find_scenarios(tag="arm64").model_dump(mode="json")

    assert len(result["matches"]) == 1
    match = result["matches"][0]
    assert match["scenario"] == "Attach invalid token"
    assert match["feature"] == "features/cli/attach.feature"


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


def test_run_job_rejects_unlisted_feature(tmp_path):
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, jobs_dir=tmp_path)
    )

    with pytest.raises(
        JobServiceError, match="Feature is not listed by list_features"
    ):
        service.run_job(
            "features/cli/does-not-exist.feature",
            scenario=UNIT_SCENARIO,
            release="jammy",
            machine_type="lxd-container",
        )


def test_run_job_rejects_a_feature_file_that_cannot_be_parsed(tmp_path):
    # The catalog leaves out what it cannot parse, so list_features never
    # lists such a file.
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    (repo_root / "features" / "broken.feature").write_text("not gherkin\n")
    launcher = FakeJobLauncher()
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, jobs_dir=tmp_path),
        launcher=launcher,
    )

    with pytest.raises(
        JobServiceError, match="Feature is not listed by list_features"
    ):
        service.run_job(
            "features/broken.feature",
            scenario=UNIT_SCENARIO,
            release="jammy",
            machine_type="lxd-container",
        )
    assert launcher.calls == []


def test_run_job_accepts_normalized_listed_feature(tmp_path):
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    launcher = FakeJobLauncher()
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, jobs_dir=tmp_path),
        launcher=launcher,
    )

    result = service.run_job(
        "features/cli/../cli/attach.feature",
        scenario=UNIT_SCENARIO,
        release="jammy",
        machine_type="lxd-container",
    ).model_dump(mode="json")

    assert result["state"] == "running"
    assert "artifacts" in result
    assert result["artifacts"]["record"].endswith("/job.json")


def test_run_job_refuses_a_name_the_feature_lacks(tmp_path):
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    (repo_root / "features/cli/attach.feature").write_text(
        "Feature: attach\n"
        "  Scenario: Attach command with token\n"
        "    Given a step\n",
        encoding="utf-8",
    )
    launcher = FakeJobLauncher()
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, jobs_dir=tmp_path),
        launcher=launcher,
    )

    with pytest.raises(JobServiceError, match="exact name"):
        service.run_job(
            "features/cli/attach.feature",
            scenario="Attach command",
            release="jammy",
            machine_type="lxd-container",
        )
    assert launcher.calls == []


def test_run_job_refuses_a_combo_the_scenario_lacks(tmp_path):
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    launcher = FakeJobLauncher()
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, jobs_dir=tmp_path),
        launcher=launcher,
    )

    with pytest.raises(JobServiceError, match="does not run"):
        service.run_job(
            "features/cli/attach.feature",
            scenario=UNIT_SCENARIO,
            release="noble",
            machine_type="lxd-container",
        )
    assert launcher.calls == []


def test_run_job_refuses_an_ambiguous_unit(tmp_path):
    # Two outlines share a name and the jammy lxd-container row, so behave
    # would run both: no job can name exactly one unit.
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    (repo_root / "features/cli/attach.feature").write_text(
        UNIT_FEATURE + "\n" + UNIT_FEATURE.split("\n", 1)[1],
        encoding="utf-8",
    )
    launcher = FakeJobLauncher()
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, jobs_dir=tmp_path),
        launcher=launcher,
    )

    with pytest.raises(JobServiceError, match="More than one scenario"):
        service.run_job(
            "features/cli/attach.feature",
            scenario=UNIT_SCENARIO,
            release="jammy",
            machine_type="lxd-container",
        )
    assert launcher.calls == []


def test_run_job_builds_command(tmp_path):
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    launcher = FakeJobLauncher()
    service = _make_service(
        FakeWorkspace(
            repo_root=repo_root,
            jobs_dir=tmp_path,
            env={"PATH": "/bin"},
        ),
        launcher=launcher,
    )

    result = service.run_job(
        "features/cli/attach.feature",
        scenario=UNIT_SCENARIO,
        release="jammy",
        machine_type="lxd-container",
    ).model_dump(mode="json")

    assert result["state"] == "running"
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
    assert call["env"]["PATH"] == "/bin"


def test_run_job_defaults_install_from_to_local(tmp_path):
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    launcher = FakeJobLauncher()
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, jobs_dir=tmp_path),
        launcher=launcher,
    )

    service.run_job(
        "features/cli/attach.feature",
        scenario=UNIT_SCENARIO,
        release="jammy",
        machine_type="lxd-container",
    )

    assert launcher.calls[0]["env"]["UACLIENT_BEHAVE_INSTALL_FROM"] == "local"


def test_run_job_sets_install_from_env_var(tmp_path):
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    launcher = FakeJobLauncher()
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, jobs_dir=tmp_path),
        launcher=launcher,
    )

    service.run_job(
        "features/cli/attach.feature",
        scenario=UNIT_SCENARIO,
        release="jammy",
        machine_type="lxd-container",
        install_from="proposed",
    )

    assert (
        launcher.calls[0]["env"]["UACLIENT_BEHAVE_INSTALL_FROM"] == "proposed"
    )


def test_run_job_rejects_invalid_install_from(tmp_path):
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, jobs_dir=tmp_path)
    )

    with pytest.raises(JobServiceError, match="Unsupported install_from"):
        service.run_job(
            "features/cli/attach.feature",
            scenario=UNIT_SCENARIO,
            release="jammy",
            machine_type="lxd-container",
            install_from="custom",
        )


def test_a_job_records_the_repo_it_ran_in(tmp_path):
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    launcher = FakeJobLauncher()
    service = _make_service(
        FakeWorkspace(
            repo_root=repo_root,
            jobs_dir=tmp_path,
            repo_state=RepoState(
                commit="deadbeef", branch="main", dirty=False
            ),
        ),
        launcher=launcher,
    )

    result = service.run_job(
        "features/cli/attach.feature",
        scenario=UNIT_SCENARIO,
        release="jammy",
        machine_type="lxd-container",
    ).model_dump(mode="json")

    store = LocalJobResultStoreFactory().bind(tmp_path)
    record = store.read_record(result["job_id"])
    assert record.repo == RepoState(
        root=str(repo_root), commit="deadbeef", branch="main", dirty=False
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
        FakeWorkspace(repo_root=None, jobs_dir=server_jobs), launcher=launcher
    )

    result = service.run_job(
        "features/cli/sample.feature",
        scenario=UNIT_SCENARIO,
        release="jammy",
        machine_type="lxd-container",
        repo_root=str(other_checkout),
    ).model_dump(mode="json")

    assert result["state"] == "running"
    assert launcher.calls[0]["cwd"] == str(other_checkout)
    assert (server_jobs / result["job_id"] / "job.json").is_file()
    record = json.loads(
        (job_file(server_jobs, result["job_id"], "job.json")).read_text()
    )
    assert record["repo"]["root"] == str(other_checkout)


def test_run_job_does_not_reference_combo_formatter(tmp_path):
    repo_root = _make_repo_with_outline(tmp_path)
    launcher = FakeJobLauncher()
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, jobs_dir=tmp_path),
        launcher=launcher,
    )

    service.run_job(
        "features/cli/attach.feature",
        scenario="Attach on a machine",
        release="jammy",
        machine_type="lxd-container",
    ).model_dump(mode="json")

    call = launcher.calls[0]
    assert (
        "features.behave_combo_formatter:ComboFormatter" not in call["command"]
    )
    assert call["command"][-2:] == ["-f", "plain"]


def test_run_job_rejects_invalid_repo_root():
    service = _make_service(
        FakeWorkspace(repo_root_error="Invalid repo_root: bad")
    )

    with pytest.raises(JobServiceError, match="Invalid repo_root"):
        service.run_job(
            "features/cli/attach.feature",
            scenario=UNIT_SCENARIO,
            release="jammy",
            machine_type="lxd-container",
            repo_root="/bad",
        )


def test_run_job_refuses_an_unknown_machine_type(tmp_path):
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, jobs_dir=tmp_path)
    )

    with pytest.raises(JobServiceError, match="Unsupported machine_type"):
        service.run_job(
            "features/cli/attach.feature",
            scenario=UNIT_SCENARIO,
            release="jammy",
            machine_type="lxd-ctr",
        )


@pytest.mark.parametrize("machine_type", ["azure.generic", "aws.pro-fips"])
def test_run_job_rejects_cloud_machine_type_by_default(tmp_path, machine_type):
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, jobs_dir=tmp_path)
    )

    with pytest.raises(
        JobServiceError,
        match="cloud machine types are disabled.*max_jobs.cloud",
    ):
        service.run_job(
            "features/cli/attach.feature",
            scenario=UNIT_SCENARIO,
            release="jammy",
            machine_type=machine_type,
        )


def test_run_job_rejects_a_disabled_pool_naming_the_setting(tmp_path):
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, jobs_dir=tmp_path),
        max_jobs={Pool.WSL: 0},
    )

    with pytest.raises(JobServiceError, match="max_jobs.wsl"):
        start(service, "wsl")


def test_a_job_with_a_free_lane_starts_at_once(tmp_path):
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, jobs_dir=tmp_path),
        max_jobs={Pool.CLOUD: 1},
    )

    result = start(service, "azure.generic")

    assert (result["state"], result["queue_position"]) == ("running", None)


def queue_service(tmp_path, launcher, ids, **max_jobs):
    """A service whose jobs get ``ids`` in turn, with ``max_jobs`` per
    pool."""
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    issued = iter(ids)
    return _make_service(
        FakeWorkspace(repo_root=repo_root, jobs_dir=tmp_path),
        max_jobs={Pool(pool): count for pool, count in max_jobs.items()},
        launcher=launcher,
        new_job_id=lambda: next(issued),
    )


def test_a_job_with_no_free_lane_is_queued(tmp_path):
    service = queue_service(tmp_path, FakeJobLauncher(tmp_path), ["j1", "j2"])
    start(service, "lxd-container")

    second = start(service, "lxd-container")

    assert (second["state"], second["queue_position"]) == ("queued", 1)


def test_a_full_pool_leaves_other_pools_free(tmp_path):
    service = queue_service(
        tmp_path, FakeJobLauncher(tmp_path), ["w1", "w2", "l1"], lxd=2, wsl=1
    )
    start(service, "wsl")

    second_wsl = start(service, "wsl")
    lxd = start(service, "lxd-container")

    assert second_wsl["state"] == "queued"
    assert lxd["state"] == "running"


def test_a_freed_lane_goes_to_the_oldest_queued_job(tmp_path):
    launcher = FakeJobLauncher(tmp_path)
    clock = iter(["T1", "T2", "T3", "T4", "T5", "T6"])
    service = queue_service(tmp_path, launcher, ["j1", "j2", "j3"])
    service._now_utc = lambda: next(clock)
    start(service, "lxd-container")
    start(service, "lxd-container")
    start(service, "lxd-container")

    launcher.finish("j1")
    service.reconcile()

    assert [call["job_id"] for call in launcher.calls] == ["j1", "j2"]
    assert service.job_status("j3").queue_position == 1


def test_a_launch_that_fails_ends_the_job_with_the_reason(tmp_path):
    lanes = FakeLanePool()
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, jobs_dir=tmp_path),
        lanes=lanes,
        launcher=FakeJobLauncher(tmp_path, error=JobStartError("boom")),
        new_job_id=lambda: JOB_ID,
    )

    result = start(service, "lxd-container")

    assert result["state"] == "finished"
    status = service.job_status(JOB_ID)
    assert (status.outcome, status.reason) == (
        Outcome.ERROR,
        "Could not start: boom",
    )
    assert lanes.busy(Pool.LXD) == 0


def test_a_queued_job_can_be_killed_and_never_starts(tmp_path):
    launcher = FakeJobLauncher(tmp_path)
    service = queue_service(tmp_path, launcher, ["j1", "j2"])
    start(service, "lxd-container")
    start(service, "lxd-container")

    killed = service.kill_job("j2")
    launcher.finish("j1")
    service.reconcile()

    assert killed.killed is True
    status = service.job_status("j2")
    assert (status.state, status.interruption) == ("interrupted", "killed")
    assert [call["job_id"] for call in launcher.calls] == ["j1"]


def test_a_wait_that_runs_out_returns_the_queued_job(tmp_path):
    service = queue_service(tmp_path, FakeJobLauncher(tmp_path), ["j1", "j2"])
    clock = iter([0.0, 10.0])
    service._monotonic = lambda: next(clock)
    start(service, "lxd-container")
    start(service, "lxd-container")

    result = service.wait_for_job(
        "j2", max_wait_seconds=5, poll_interval_seconds=1
    )

    assert (result.state, result.timed_out) == ("queued", True)


def test_a_job_gets_behave_settings_only_from_the_config(tmp_path):
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    launcher = FakeJobLauncher(tmp_path)
    service = _make_service(
        FakeWorkspace(
            repo_root=repo_root,
            jobs_dir=tmp_path,
            env={"PATH": "/bin", "UACLIENT_BEHAVE_CUSTOM_PPA": "inherited"},
        ),
        config=FakeConfigStore(behave={"contract_token": "secret"}),
        launcher=launcher,
        new_job_id=lambda: JOB_ID,
    )

    start(service, "lxd-container")

    env = launcher.calls[0]["env"]
    assert env["UACLIENT_BEHAVE_CONTRACT_TOKEN"] == "secret"
    assert "UACLIENT_BEHAVE_CUSTOM_PPA" not in env
    assert env["PATH"] == "/bin"


def test_a_job_records_its_config_without_secret_values(tmp_path):
    repo_root = make_repo_with_feature(tmp_path, "features/cli/attach.feature")
    service = _make_service(
        FakeWorkspace(repo_root=repo_root, jobs_dir=tmp_path),
        config=FakeConfigStore(
            behave={"contract_token": "secret", "custom_ppa": "ppa:x/y"}
        ),
        launcher=FakeJobLauncher(tmp_path),
        new_job_id=lambda: JOB_ID,
    )

    start(service, "lxd-container")

    record = LocalJobResultStore(tmp_path).read_record(JOB_ID)
    assert record.config == {
        "contract_token": "<set>",
        "custom_ppa": "ppa:x/y",
    }


def test_an_invalid_config_is_reported_by_name(tmp_path):
    service = job_service(
        tmp_path,
        FakeJobLauncher(tmp_path),
        config=FakeConfigStore(error="config.json: unknown pool"),
    )

    with pytest.raises(JobServiceError, match="unknown pool"):
        service.reconcile()


# ---- one job, read from its files and its lock ----


JOB_ID = "job-under-test"


OTHER_JOB_ID = "job-from-another-run"


def launched(tmp_path, job_id=JOB_ID, launch=True, **record):
    """A job as the service queued it, and launched it unless ``launch`` is
    false: its request, then its record."""
    fields = dict(
        job_id=job_id,
        feature="features/cli/attach.feature",
        scenario=UNIT_SCENARIO,
        release="jammy",
        machine_type="lxd-container",
    )
    fields.update(record)
    started_at = fields.pop("started_at", "T0")
    queued_at = fields.pop("queued_at", started_at)
    store = LocalJobResultStore(tmp_path)
    store.write_request(job_id, JobRequest(queued_at=queued_at, **fields))
    if launch:
        store.write_record(job_id, JobRecord(started_at=started_at, **fields))


def job_service(tmp_path, launcher, **kwargs):
    return _make_service(
        FakeWorkspace(repo_root=tmp_path, jobs_dir=tmp_path),
        launcher=launcher,
        **kwargs,
    )


# A failing step of the unit's one instance.
FAILURE = Failure(
    instance="features/cli/attach.feature:10",
    step="Then it works",
    status="failed",
    error_message="boom",
)


def arrange(tmp_path, state):
    """A job in ``state``, and the launcher that knows of it."""
    launched(tmp_path, launch=state != JobState.QUEUED)
    launcher = FakeJobLauncher(tmp_path)
    if state == JobState.RUNNING:
        launcher.running.add(JOB_ID)
    elif state == JobState.FINISHED:
        launcher.finish(JOB_ID)
    job_file(tmp_path, JOB_ID, "stdout.log").write_text("line1\nline2\n")
    return launcher


# What a status read reports for a job in each state.
STATUS_OF = {
    JobState.QUEUED: ("queued", None),
    JobState.RUNNING: ("running", None),
    JobState.FINISHED: ("finished", Outcome.PASSED),
    JobState.INTERRUPTED: ("interrupted", None),
}


def test_every_job_state_has_an_expected_status():
    assert set(STATUS_OF) == set(JobState)


@pytest.mark.parametrize("state, expected", STATUS_OF.items(), ids=str)
def test_job_status_reads_the_state_off_disk(tmp_path, state, expected):
    service = job_service(tmp_path, arrange(tmp_path, state))

    payload = service.job_status(JOB_ID)

    assert (payload.state, getattr(payload, "outcome", None)) == expected


def test_a_finished_job_reports_its_recorded_result(tmp_path):
    launched(tmp_path)
    launcher = FakeJobLauncher(tmp_path)
    launcher.finish(JOB_ID, Outcome.FAILED, returncode=1, failures=[FAILURE])
    service = job_service(tmp_path, launcher)

    payload = service.job_status(JOB_ID).model_dump(mode="json")

    assert payload["outcome"] == "failed"
    assert payload["returncode"] == 1
    assert payload["failures"] == [FAILURE.model_dump(mode="json")]
    assert payload["recent_output"] is None
    assert payload["release"] == "jammy"


def test_a_job_that_established_nothing_shows_its_output(tmp_path):
    launched(tmp_path)
    launcher = FakeJobLauncher(tmp_path)
    launcher.finish(
        JOB_ID,
        Outcome.ERROR,
        returncode=2,
        log="tox failed\n",
        reason="the job left no readable report",
    )
    service = job_service(tmp_path, launcher)

    payload = service.job_status(JOB_ID).model_dump(mode="json")

    assert payload["reason"] == "the job left no readable report"
    assert "tox failed" in payload["recent_output"]


def test_a_killed_job_is_interrupted_whatever_tox_returned(tmp_path):
    launched(tmp_path)
    launcher = FakeJobLauncher(tmp_path)
    launcher.finish(JOB_ID, returncode=0, killed=True)
    service = job_service(tmp_path, launcher)

    status = service.job_status(JOB_ID)

    assert (status.state, status.interruption, status.outcome) == (
        "interrupted",
        "killed",
        None,
    )


def test_reading_a_job_never_writes_its_result(tmp_path):
    # The job process alone records a result; the server only reads it.
    launcher = arrange(tmp_path, JobState.RUNNING)
    service = job_service(tmp_path, launcher)

    service.job_status(JOB_ID)
    service.list_jobs()
    service.job_results()

    assert not job_file(tmp_path, JOB_ID, "result.json").exists()


def test_wait_for_completion_returns_once_the_job_ends(tmp_path):
    launcher = arrange(tmp_path, JobState.RUNNING)
    service = job_service(
        tmp_path,
        launcher,
        sleep=lambda seconds: launcher.finish(JOB_ID),
    )

    result = service.wait_for_job(
        JOB_ID, max_wait_seconds=5, poll_interval_seconds=0.01
    )

    assert (result.state, result.outcome) == ("finished", Outcome.PASSED)


def test_wait_for_completion_returns_an_interrupted_job(tmp_path):
    launcher = arrange(tmp_path, JobState.RUNNING)
    service = job_service(
        tmp_path, launcher, sleep=lambda seconds: launcher.crash(JOB_ID)
    )

    result = service.wait_for_job(
        JOB_ID, max_wait_seconds=5, poll_interval_seconds=0.01
    )

    assert (result.state, result.interruption) == ("interrupted", "died")
    assert "line2" in result.recent_output


def test_wait_for_completion_times_out_on_a_running_job(tmp_path):
    launcher = arrange(tmp_path, JobState.RUNNING)
    clock = iter([0.0, 0.0, 10.0])
    service = job_service(tmp_path, launcher, monotonic=lambda: next(clock))

    result = service.wait_for_job(
        JOB_ID, max_wait_seconds=5, poll_interval_seconds=1
    ).model_dump(mode="json")

    assert (result["state"], result["timed_out"]) == ("running", True)
    assert "line2" in result["recent_output"]


@pytest.mark.parametrize(
    "call",
    [
        lambda service: service.wait_for_job("nope", 1),
        lambda service: service.job_status("nope"),
        lambda service: service.job_logs("nope"),
        lambda service: service.job_errors("nope"),
        lambda service: service.job_artifacts("nope"),
        lambda service: service.kill_job("nope"),
    ],
    ids=["wait", "status", "logs", "errors", "artifacts", "kill"],
)
def test_an_unknown_job_id_is_actionable(tmp_path, call):
    service = job_service(tmp_path, FakeJobLauncher(tmp_path))

    with pytest.raises(UnknownJobError, match="list_jobs"):
        call(service)


# What kill_job does to a job in each state.
KILL_OF = {
    JobState.QUEUED: (True, "before it started"),
    JobState.RUNNING: (True, "Kill sent"),
    JobState.FINISHED: (False, "already finished"),
    JobState.INTERRUPTED: (False, "already been interrupted"),
}


def test_every_job_state_has_an_expected_kill():
    assert set(KILL_OF) == set(JobState)


@pytest.mark.parametrize("state, expected", KILL_OF.items(), ids=str)
def test_kill_job_by_state(tmp_path, state, expected):
    launcher = arrange(tmp_path, state)
    service = job_service(tmp_path, launcher)

    result = service.kill_job(JOB_ID)

    killed, message = expected
    assert result.killed is killed
    assert message in result.message
    signalled = state == JobState.RUNNING
    assert launcher.killed == ([JOB_ID] if signalled else [])


def test_a_job_process_that_cannot_be_reached_is_reported(tmp_path):
    launched(tmp_path)
    launcher = FakeJobLauncher(tmp_path, unkillable={JOB_ID})
    launcher.running.add(JOB_ID)
    service = job_service(tmp_path, launcher)

    result = service.kill_job(JOB_ID)

    assert result.killed is False
    assert "could not be reached" in result.message


# ---- logs, errors and artifacts ----


def logged(tmp_path, text):
    launched(tmp_path)
    log = job_file(tmp_path, JOB_ID, "stdout.log")
    log.write_text(text, encoding="utf-8")
    return log


def test_get_logs_returns_tail(tmp_path):
    logged(tmp_path, "l1\nl2\nl3\n")
    service = job_service(tmp_path, FakeJobLauncher(tmp_path))

    result = service.job_logs(JOB_ID, lines=2).model_dump(mode="json")

    assert result["text"] == "2: l2\n3: l3"
    assert result["total_lines"] == 3


def test_get_logs_searches_with_context(tmp_path):
    logged(
        tmp_path,
        "ok\nTraceback (most recent call last):\n  x\nKeyError: k\nok\n",
    )
    service = job_service(tmp_path, FakeJobLauncher(tmp_path))

    result = service.job_logs(
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

    with pytest.raises(JobServiceError, match="invalid pattern"):
        service.job_logs(JOB_ID, pattern="(")


def test_get_logs_clamps_lines_above_max(tmp_path):
    logged(tmp_path, "l1\nl2\nl3\n")
    service = job_service(tmp_path, FakeJobLauncher(tmp_path))

    result = service.job_logs(JOB_ID, lines=MAX_LOG_LINES + 1000)

    assert result.lines_clamped is True


def test_get_logs_rejects_non_positive_lines(tmp_path):
    logged(tmp_path, "l1\n")
    service = job_service(tmp_path, FakeJobLauncher(tmp_path))

    with pytest.raises(JobServiceError, match="positive"):
        service.job_logs(JOB_ID, lines=0)


def test_get_logs_of_a_job_with_no_log_yet_is_an_error(tmp_path):
    launched(tmp_path)
    service = job_service(tmp_path, FakeJobLauncher(tmp_path))

    with pytest.raises(JobServiceError, match="No log file"):
        service.job_logs(JOB_ID)


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

    result = service.job_errors(JOB_ID).model_dump(mode="json")

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


def test_get_artifacts_reports_paths_record_and_result(tmp_path):
    launched(tmp_path, feature="features/a.feature")
    launcher = FakeJobLauncher(tmp_path)
    launcher.finish(JOB_ID, log="line\n")
    job_file(tmp_path, JOB_ID, "report.json").write_text("[]")
    service = job_service(tmp_path, launcher)

    result = service.job_artifacts(JOB_ID).model_dump(mode="json")

    assert result["exists"] == {
        "request": True,
        "record": True,
        "result": True,
        "stdout_log": True,
        "json_report": True,
    }
    assert result["record"]["feature"] == "features/a.feature"
    assert result["result"]["outcome"] == "passed"
    assert result["artifacts"]["job_dir"] == str(tmp_path / JOB_ID)


# ---- listing ----


def listed(tmp_path, launcher, job_id, started_at, state):
    launched(tmp_path, job_id=job_id, started_at=started_at)
    if state == JobState.RUNNING:
        launcher.running.add(job_id)
    elif state == JobState.FINISHED:
        launcher.finish(job_id)


def test_list_jobs_reads_every_job_off_disk(tmp_path):
    launcher = FakeJobLauncher(tmp_path)
    listed(tmp_path, launcher, "a-running", "T1", JobState.RUNNING)
    listed(tmp_path, launcher, "b-done", "T2", JobState.FINISHED)
    listed(tmp_path, launcher, "c-gone", "T3", JobState.INTERRUPTED)
    service = job_service(tmp_path, launcher)

    result = service.list_jobs()

    assert {(job.job_id, job.state) for job in result.jobs} == {
        ("a-running", JobState.RUNNING),
        ("b-done", JobState.FINISHED),
        ("c-gone", JobState.INTERRUPTED),
    }


def test_list_jobs_keeps_running_jobs_and_caps_the_rest(tmp_path):
    launcher = FakeJobLauncher(tmp_path)
    listed(tmp_path, launcher, "running", "T0", JobState.RUNNING)
    for number in range(DEFAULT_JOB_LIST_LIMIT + 2):
        listed(
            tmp_path,
            launcher,
            "done-{:02d}".format(number),
            "T{:02d}".format(number + 1),
            JobState.FINISHED,
        )
    service = job_service(tmp_path, launcher)

    result = service.list_jobs()

    assert result.jobs[0].job_id == "running"
    assert len(result.jobs) == DEFAULT_JOB_LIST_LIMIT + 1
    assert result.total_ended == DEFAULT_JOB_LIST_LIMIT + 2
    assert result.truncated is True


def test_list_jobs_is_not_truncated_under_the_limit(tmp_path):
    launcher = FakeJobLauncher(tmp_path)
    listed(tmp_path, launcher, "done", "T1", JobState.FINISHED)
    service = job_service(tmp_path, launcher)

    result = service.list_jobs(limit=5)

    assert result.truncated is False


def test_list_jobs_limit_is_clamped_when_too_high(tmp_path):
    service = job_service(tmp_path, FakeJobLauncher(tmp_path))

    result = service.list_jobs(limit=MAX_JOB_LIST_LIMIT + 1)

    assert result.limit_clamped is True


def test_list_jobs_rejects_a_non_positive_limit(tmp_path):
    service = job_service(tmp_path, FakeJobLauncher(tmp_path))

    with pytest.raises(JobServiceError, match="positive"):
        service.list_jobs(limit=0)


# ---- results ----


def test_get_results_reports_one_result_per_job(tmp_path):
    launched(tmp_path, release="resolute", machine_type="lxd-vm")
    launcher = FakeJobLauncher(tmp_path)
    launcher.finish(JOB_ID, Outcome.FAILED, failures=[FAILURE])
    service = job_service(tmp_path, launcher)

    result = service.job_results(job_ids=[JOB_ID]).model_dump(mode="json")

    (job,) = result["results"]
    assert result["total"] == 1
    assert job["state"] == "finished"
    assert (job["release"], job["machine_type"]) == ("resolute", "lxd-vm")
    assert job["outcome"] == "failed"
    assert job["failures"][0]["error_message"] == "boom"


def test_get_results_filters_by_feature_file_newest_first(tmp_path):
    launcher = FakeJobLauncher(tmp_path)
    for job_id, feature, started in [
        ("older", "features/a.feature", "T1"),
        ("newer", "features/a.feature", "T2"),
        ("other", "features/b.feature", "T3"),
    ]:
        launched(tmp_path, job_id=job_id, feature=feature, started_at=started)
        launcher.finish(job_id)
    service = job_service(tmp_path, launcher)

    result = service.job_results(feature="features/a.feature")

    assert [r.job_id for r in result.results] == ["newer", "older"]


def test_get_results_filters_by_status(tmp_path):
    launcher = FakeJobLauncher(tmp_path)
    launched(tmp_path, job_id=JOB_ID)
    launched(tmp_path, job_id=OTHER_JOB_ID)
    launcher.running.add(OTHER_JOB_ID)
    service = job_service(tmp_path, launcher)

    result = service.job_results(state="interrupted")

    assert [r.job_id for r in result.results] == [JOB_ID]


def test_get_results_rejects_an_invalid_state(tmp_path):
    service = job_service(tmp_path, FakeJobLauncher(tmp_path))

    with pytest.raises(JobServiceError, match="Invalid state filter"):
        service.job_results(state="bogus")


def test_get_results_rejects_a_non_positive_limit(tmp_path):
    service = job_service(tmp_path, FakeJobLauncher(tmp_path))

    with pytest.raises(JobServiceError, match="positive"):
        service.job_results(limit=0)


def test_get_results_truncates_at_the_limit(tmp_path):
    launcher = FakeJobLauncher(tmp_path)
    shown = 2
    for number in range(shown + 1):
        job_id = "job-{}".format(number)
        launched(tmp_path, job_id=job_id, started_at="T{}".format(number))
        launcher.finish(job_id)
    service = job_service(tmp_path, launcher)

    result = service.job_results(limit=shown)

    assert len(result.results) == shown
    assert result.total == shown + 1
    assert result.truncated is True


def test_get_results_limit_is_clamped_when_too_high(tmp_path):
    service = job_service(tmp_path, FakeJobLauncher(tmp_path))

    result = service.job_results(limit=MAX_RESULTS_LIMIT + 1)

    assert result.limit_clamped is True
