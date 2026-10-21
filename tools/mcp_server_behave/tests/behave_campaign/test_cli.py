import io
import json
import tarfile

import pytest

from behave_campaign.adapters import CampaignDirStore
from behave_campaign.cli import main
from behave_campaign.domain.records import (
    Cause,
    FinishedRecord,
    Intent,
    IntentRecord,
    StartedRecord,
    Unit,
)

pytestmark = pytest.mark.integration

UNIT_A_JAMMY = ["features/a.feature", "A", "jammy", "lxd-container"]
UNIT_A_NOBLE = ["features/a.feature", "A", "noble", "lxd-vm"]
UNIT_B_JAMMY = ["features/b.feature", "B", "jammy", "lxd-container"]
AT = "2026-10-06T00:00:00Z"

FEATURE_A = """Feature: A feature

  Scenario Outline: A
    Given a `<release>` `<machine_type>` machine
    Then I verify that `esm-infra` is enabled

    Examples: ubuntu release
      | release | machine_type  |
      | jammy   | lxd-container |
      | noble   | lxd-vm        |
"""

FEATURE_B = """Feature: B feature

  Scenario Outline: B
    Given a `<release>` `<machine_type>` machine
    Then I verify that `esm-apps` is enabled

    Examples: ubuntu release
      | release | machine_type  |
      | jammy   | lxd-container |
"""


def attempt(unit, outcome, job_id):
    """One completed try: its start and its finish."""
    tried = Unit(*unit)
    return [
        StartedRecord(
            unit=tried, job_id=job_id, install_from="proposed", at=AT
        ),
        FinishedRecord(unit=tried, job_id=job_id, outcome=outcome, at=AT),
    ]


def record(campaign_file, attempts):
    """Write attempts as a runner does: intent run, then each try."""
    CampaignDirStore(campaign_file).append(
        campaign_file.name,
        [
            IntentRecord(Intent.RUN, Cause.RUN, AT),
            *(item for tried in attempts for item in tried),
        ],
    )


def unit_of(reported):
    return [
        reported["feature"],
        reported["scenario"],
        reported["release"],
        reported["machine_type"],
    ]


@pytest.fixture
def campaign_file(tmp_path):
    return tmp_path / "records"


@pytest.fixture
def repo(tmp_path):
    features = tmp_path / "features"
    features.mkdir()
    (features / "a.feature").write_text(FEATURE_A)
    (features / "b.feature").write_text(FEATURE_B)
    (tmp_path / "tox.ini").write_text("[tox]\n")
    return tmp_path


@pytest.fixture
def run(tmp_path, campaign_file, repo, capsys):
    def _run(args, payload=None, expect=0):
        if payload is not None:
            source = tmp_path / "input.json"
            source.write_text(json.dumps(payload))
            args = [*args, "--input", str(source)]
        if args[1] == "create":
            args = [*args, "--repo-root", str(repo)]
        if "--campaign-id" not in args:
            args = [*args, "--campaign", str(campaign_file)]
        code = main(args)
        captured = capsys.readouterr()
        assert code == expect, captured.err
        return json.loads(captured.out) if code == 0 else captured.err

    return _run


@pytest.fixture
def planned(run):
    return run(["campaign", "create"])


class TestInit:
    def test_campaign_covers_every_discovered_unit(self, planned, run):
        assert planned["summary"]["counts"]["unattempted"] == 3
        assert planned["campaign"]["total_units"] == 3
        history = run(["campaign", "history"])
        assert [unit_of(unit) for unit in history["units"]] == [
            UNIT_A_JAMMY,
            UNIT_A_NOBLE,
            UNIT_B_JAMMY,
        ]

    def test_filters_define_the_campaign(self, run):
        result = run(["campaign", "create", "--machine-type", "lxd-vm"])

        assert result["summary"]["counts"]["unattempted"] == 1
        history = run(["campaign", "history"])
        assert [unit_of(unit) for unit in history["units"]] == [UNIT_A_NOBLE]

    def test_max_jobs_are_set_per_pool(self, run, repo, capsys):
        # Within the checkout's own max jobs, which create checks.
        main(["config", "--repo-root", str(repo), "set", "max_jobs.lxd", "4"])
        capsys.readouterr()

        result = run(["campaign", "create", "--max-lxd-jobs", "4"])

        assert result["campaign"]["max_jobs"] == {"lxd": 4}

    def test_max_jobs_for_a_pool_out_of_scope_are_rejected(self, run):
        error = run(["campaign", "create", "--max-wsl-jobs", "1"], expect=2)

        assert "no unit in scope" in error

    def test_initialising_twice_is_rejected(self, planned, run):
        error = run(["campaign", "create"], expect=2)

        assert "already holds a campaign" in error

    def test_unknown_filter_value_is_rejected(self, run):
        error = run(["campaign", "create", "--release", "bogus"], expect=2)

        assert "unknown release: bogus" in error

    def test_full_campaign_needs_no_filters(self, run):
        result = run(["campaign", "create"])

        assert result["summary"]["counts"]["unattempted"] == 3
        assert result["campaign"]["scope"] == {
            "feature": [],
            "scenario": [],
            "release": [],
            "machine_type": [],
        }

    def test_campaign_header_records_how_it_was_built(self, run, repo):
        result = run(["campaign", "create", "--release", "jammy"])
        campaign = result["campaign"]

        assert campaign["campaign_id"] == "records"
        assert campaign["scope"]["release"] == ["jammy"]
        assert campaign["repo"]["root"] == str(repo)
        assert campaign["created_at"].endswith("Z")


class TestUnits:
    def test_lists_every_unit_with_its_state(
        self, planned, run, campaign_file
    ):
        record(campaign_file, [attempt(UNIT_A_JAMMY, "failed", "job-1")])

        result = run(["campaign", "units"])

        assert [(unit_of(u), u["state"]) for u in result["units"]] == [
            (UNIT_A_JAMMY, "failed"),
            (UNIT_A_NOBLE, "unattempted"),
            (UNIT_B_JAMMY, "unattempted"),
        ]
        assert result["truncated"] is False

    def test_filters_and_limit_apply(self, planned, run):
        result = run(
            ["campaign", "units", "--release", "jammy", "--limit", "1"]
        )

        assert [unit_of(u) for u in result["units"]] == [UNIT_A_JAMMY]
        assert result["truncated"] is True

    def test_csv_is_one_row_per_unit(
        self, planned, run, capsys, campaign_file
    ):
        record(campaign_file, [attempt(UNIT_A_JAMMY, "failed", "job-1")])

        code = main(
            [
                "campaign",
                "units",
                "--campaign",
                str(campaign_file),
                "--format",
                "csv",
            ]
        )
        out = capsys.readouterr().out.splitlines()

        assert code == 0
        assert out[0] == (
            "feature,scenario,release,machine_type,state,job_id,attempt_count"
        )
        assert (
            out[1] == "features/a.feature,A,jammy,lxd-container,failed,job-1,1"
        )
        assert out[2] == "features/a.feature,A,noble,lxd-vm,unattempted,,0"
        assert len(out) == 4

    def test_csv_warns_when_cut_by_the_limit(
        self, planned, capsys, campaign_file
    ):
        main(
            [
                "campaign",
                "units",
                "--campaign",
                str(campaign_file),
                "--format",
                "csv",
                "--limit",
                "1",
            ]
        )
        captured = capsys.readouterr()

        assert len(captured.out.splitlines()) == 2
        assert "--limit 1" in captured.err

    def test_history_csv_is_one_row_per_attempt(
        self, planned, run, capsys, campaign_file
    ):
        record(
            campaign_file,
            [
                attempt(UNIT_A_JAMMY, "failed", "job-1"),
                attempt(UNIT_A_JAMMY, "passed", "job-2"),
            ],
        )

        main(
            [
                "campaign",
                "history",
                "--campaign",
                str(campaign_file),
                "--format",
                "csv",
                "--feature",
                "features/a.feature",
            ]
        )
        out = capsys.readouterr().out.splitlines()

        assert out[0].startswith(
            "feature,scenario,release,machine_type,state,"
        )
        assert out[0].endswith(
            "attempt,job_id,install_from,started_at,outcome,finished_at,"
            "interrupted"
        )
        # Two attempts at A/jammy, then A/noble with none.
        assert [row.split(",")[5:7] for row in out[1:]] == [
            ["1", "job-1"],
            ["2", "job-2"],
            ["", ""],
        ]


class TestAddressing:
    """A campaign is named the way the server names it, or by its file."""

    @pytest.fixture(autouse=True)
    def default_state_dir(self, monkeypatch):
        """These tests are about the state directory a checkout has when
        nothing overrides it."""
        monkeypatch.delenv("MCP_STATE_DIR")

    def test_an_id_resolves_to_the_servers_file(self, run, repo):
        created = run(["campaign", "create", "--campaign-id", "1234567"])
        status = run(
            [
                "campaign",
                "status",
                "--campaign-id",
                "1234567",
                "--repo-root",
                str(repo),
            ]
        )

        expected = repo / ".mcp_server_behave" / "campaigns" / "1234567"
        assert (expected / "campaign.json").is_file()
        assert created["campaign"]["campaign_id"] == "1234567"
        assert status["campaign"]["campaign_id"] == "1234567"

    def test_repo_root_falls_back_to_the_environment(
        self, run, repo, monkeypatch
    ):
        monkeypatch.setenv("UBUNTU_PRO_CLIENT_REPO", str(repo))

        # create's run() adds --repo-root; status must find the same file
        # from the environment alone.
        run(["campaign", "create", "--campaign-id", "1234567"])
        status = main(["campaign", "status", "--campaign-id", "1234567"])

        assert status == 0

    def test_without_either_is_an_error(self, capsys):
        with pytest.raises(SystemExit) as exit:
            main(["campaign", "status"])

        assert exit.value.code == 2
        assert "--campaign-id" in capsys.readouterr().err

    def test_both_is_an_error(self, campaign_file, capsys):
        with pytest.raises(SystemExit) as exit:
            main(
                [
                    "campaign",
                    "status",
                    "--campaign-id",
                    "1234567",
                    "--campaign",
                    str(campaign_file),
                ]
            )

        assert exit.value.code == 2
        assert "not allowed with" in capsys.readouterr().err

    def test_a_directory_names_its_own_id(self, planned, run, campaign_file):
        result = run(["campaign", "status"])

        assert result["campaign"]["campaign_id"] == campaign_file.name

    def test_missing_repo_root_is_a_clear_error(self, monkeypatch, capsys):
        monkeypatch.delenv("UBUNTU_PRO_CLIENT_REPO", raising=False)

        code = main(["campaign", "status", "--campaign-id", "1234567"])

        assert code == 2
        assert "UBUNTU_PRO_CLIENT_REPO" in capsys.readouterr().err

    def test_the_header_is_in_the_campaign_record(
        self, planned, campaign_file
    ):
        record = json.loads((campaign_file / "campaign.json").read_text())

        assert record["campaign"]["type"] == "campaign"

    def test_status_reports_the_campaign(self, planned, run):
        result = run(["campaign", "status"])

        assert result["campaign"]["scope"]["release"] == []


class TestQueries:
    def test_status_filters_narrow_units_and_counts(self, planned, run):
        result = run(["campaign", "status", "--machine-type", "lxd-vm"])

        assert result["summary"]["counts"]["unattempted"] == 1
        assert result["in_flight"] == []
        assert result["problems"] == []

    def test_status_reports_problems(self, planned, run, campaign_file):
        record(
            campaign_file,
            [
                attempt(UNIT_A_JAMMY, "failed", "job-1"),
                attempt(UNIT_A_NOBLE, "skipped", "job-2"),
            ],
        )

        result = run(["campaign", "status"])

        assert result["summary"]["counts"]["failed"] == 1
        assert result["summary"]["counts"]["skipped"] == 1
        assert result["in_flight"] == []
        assert {u["job_id"] for u in result["problems"]} == {"job-1", "job-2"}
        assert {u["state"] for u in result["problems"]} == {
            "failed",
            "skipped",
        }

    def test_status_groups_problems_by_scenario(
        self, planned, run, campaign_file
    ):
        record(
            campaign_file,
            [
                attempt(UNIT_A_JAMMY, "failed", "job-1"),
                attempt(UNIT_A_NOBLE, "skipped", "job-2"),
            ],
        )

        result = run(["campaign", "status", "--group-by", "scenario"])

        assert result["problems"] is None
        (row,) = result["problem_scenarios"]
        assert row["scenario"] == "A"
        assert [u["job_id"] for u in row["failed"]] == ["job-1"]
        assert [u["job_id"] for u in row["skipped"]] == ["job-2"]

    def test_history_lists_every_attempt(self, planned, run, campaign_file):
        record(campaign_file, [attempt(UNIT_A_JAMMY, "failed", "job-1")])
        record(campaign_file, [attempt(UNIT_A_JAMMY, "passed", "job-2")])

        result = run(
            ["campaign", "history", "--release", "jammy", "--scenario", "A"]
        )

        attempts = result["units"][0]["attempts"]

        # Two tries, so two attempts -- not four records' worth.
        assert [(a["outcome"], a["job_id"]) for a in attempts] == [
            ("failed", "job-1"),
            ("passed", "job-2"),
        ]
        assert all(a["install_from"] == "proposed" for a in attempts)
        assert all(a["started_at"].endswith("Z") for a in attempts)
        assert all(a["finished_at"].endswith("Z") for a in attempts)

    def test_a_missing_campaign_is_an_error_not_an_empty_record(
        self, run, campaign_file
    ):
        # An absent file used to read as a fresh campaign with header
        # defaults -- no lanes, no repo -- which looks like a real one.
        for command in (
            ["campaign", "status"],
            ["campaign", "history"],
            ["campaign", "events"],
        ):
            error = run(command, expect=2)
            assert str(campaign_file) in error, command

    def test_a_missing_id_is_an_error(self, run, repo):
        error = run(
            [
                "campaign",
                "status",
                "--campaign-id",
                "absent",
                "--repo-root",
                str(repo),
            ],
            expect=2,
        )

        assert "absent" in error


class TestEvents:
    """``events`` reads the events a campaign's records announce."""

    def test_reads_the_campaigns_events(self, planned, run, campaign_file):
        record(campaign_file, [attempt(UNIT_A_JAMMY, "failed", "job-1")])

        result = run(["campaign", "events"])

        assert [e["kind"] for e in result["events"]] == [
            "campaign.created",
            "campaign.running",
            "job.started",
            "unit.failed",
        ]
        assert result["events"][3]["data"]["job_id"] == "job-1"
        assert result["next_seq"] == 4
        assert "campaign_id" not in result["events"][0]

    def test_cursor_and_kinds_narrow_the_batch(
        self, planned, run, campaign_file
    ):
        record(
            campaign_file,
            [
                attempt(UNIT_A_JAMMY, "failed", "job-1"),
                attempt(UNIT_A_NOBLE, "passed", "job-2"),
            ],
        )

        result = run(
            ["campaign", "events", "--since-seq", "4", "--kinds", "unit.*"]
        )

        assert [e["kind"] for e in result["events"]] == ["unit.passed"]

    def test_unknown_kind_is_rejected(self, planned, run):
        error = run(
            ["campaign", "events", "--kinds", "unit.exploded"], expect=2
        )

        assert "unit.exploded" in error

    def test_follow_stops_once_the_campaign_is_complete(
        self, planned, capsys, campaign_file
    ):
        # Every unit has an outcome, so a follower prints what there is and
        # returns.
        record(
            campaign_file,
            [
                attempt(UNIT_A_JAMMY, "passed", "j1"),
                attempt(UNIT_A_NOBLE, "passed", "j2"),
                attempt(UNIT_B_JAMMY, "passed", "j3"),
            ],
        )

        code = main(
            [
                "campaign",
                "events",
                "--campaign",
                str(campaign_file),
                "--follow",
                "--limit",
                "1",
                "--interval",
                "0",
            ]
        )
        out = capsys.readouterr().out

        assert code == 0
        batches = [json.loads(line) for line in out.splitlines()]
        seqs = [b["events"][0]["seq"] for b in batches]
        assert seqs == list(range(1, len(batches) + 1))
        assert batches[-1]["events"][0]["kind"] == "campaign.complete"
        assert batches[-1]["state"] == "complete"


class TestExport:

    @pytest.fixture(autouse=True)
    def default_state_dir(self, monkeypatch):
        """These tests are about the state directory a checkout has when
        nothing overrides it."""
        monkeypatch.delenv("MCP_STATE_DIR")

    @pytest.fixture
    def finished(self, run, repo):
        """A campaign the server ran to completion, with its job files."""
        run(
            [
                "campaign",
                "create",
                "--campaign-id",
                "1234567",
                "--install-from",
                "proposed",
            ]
        )
        state = repo / ".mcp_server_behave"
        attempts = []
        for unit, job_id in (
            (UNIT_A_JAMMY, "j1"),
            (UNIT_A_NOBLE, "n1"),
            (UNIT_B_JAMMY, "b1"),
        ):
            attempts.append(attempt(unit, "passed", job_id))
            for name in (
                "job.json",
                "exit.json",
                "report.json",
                "stdout.log",
            ):
                path = state / "jobs" / job_id / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("[]" if name == "report.json" else "x")
        record(state / "campaigns" / "1234567", attempts)
        return state

    def export(self, run, repo, *extra):
        return run(
            [
                "campaign",
                "export",
                "--campaign-id",
                "1234567",
                "--repo-root",
                str(repo),
                *extra,
            ]
        )

    def test_writes_the_bundle_to_the_state_dir(self, finished, run, repo):
        result = self.export(run, repo)

        output = finished / "exports" / "1234567.tar.gz"
        assert result["path"] == str(output)
        assert result["counts"]["passed"] == 3
        assert result["missing_files"] == []
        with tarfile.open(output) as archive:
            names = archive.getnames()
        assert "1234567/SUMMARY.md" in names
        assert "1234567/jobs/j1/stdout.log" in names
        assert "1234567/audit/campaign.json" in names
        assert "1234567/audit/records.jsonl" in names

    def test_an_existing_output_is_refused(
        self, finished, run, repo, tmp_path
    ):
        output = tmp_path / "taken.tar.gz"
        output.write_text("keep")

        error = run(
            [
                "campaign",
                "export",
                "--campaign-id",
                "1234567",
                "--repo-root",
                str(repo),
                "--output",
                str(output),
            ],
            expect=2,
        )

        assert "already exists" in error
        assert output.read_text() == "keep"

    def test_an_unfinished_campaign_is_refused(self, run, repo):
        run(["campaign", "create", "--campaign-id", "1234567"])

        error = run(
            [
                "campaign",
                "export",
                "--campaign-id",
                "1234567",
                "--repo-root",
                str(repo),
            ],
            expect=2,
        )

        assert "only a complete campaign can be exported" in error


class TestConfig:
    @pytest.fixture
    def config(self, repo, capsys, monkeypatch):
        def _config(*action, stdin=None, expect=0):
            if stdin is not None:
                monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
            code = main(["config", "--repo-root", str(repo), *action])
            captured = capsys.readouterr()
            assert code == expect, captured.err
            return json.loads(captured.out) if code == 0 else captured.err

        return _config

    def test_with_nothing_set_it_shows_the_defaults(self, config):
        shown = config()

        assert {pool["pool"]: pool["max_jobs"] for pool in shown["pools"]} == {
            "lxd": 1,
            "cloud": 0,
            "wsl": 1,
        }
        assert shown["behave"] == {}

    def test_max_jobs_can_be_set_and_unset(self, config):
        config("set", "max_jobs.cloud", "2")
        raised = config()
        config("unset", "max_jobs.cloud")
        restored = config()

        assert {p["pool"]: p["max_jobs"] for p in raised["pools"]}[
            "cloud"
        ] == 2
        assert {p["pool"]: p["max_jobs"] for p in restored["pools"]}[
            "cloud"
        ] == 0

    def test_a_secret_read_from_stdin_is_never_shown(self, config):
        shown = config("set", "behave.contract_token", "-", stdin="secret\n")

        assert shown["behave"] == {"contract_token": "<set>"}
        assert "secret" not in json.dumps(config())

    @pytest.mark.parametrize(
        "key, value, message",
        [
            pytest.param("max_jobs.wsl", "2", "wsl max jobs", id="above-cap"),
            pytest.param("max_jobs.moon", "1", "Unknown pool", id="pool"),
            pytest.param("behave.releases", "jammy", "per job", id="owned"),
            pytest.param("color", "blue", "Unknown config key", id="key"),
        ],
    )
    def test_a_bad_setting_is_refused_by_name(
        self, config, key, value, message
    ):
        error = config("set", key, value, expect=2)

        assert message in error


@pytest.fixture
def command(repo, capsys):
    """Run one ``behave-tests`` command on the fixture checkout."""

    def _command(*args, expect=0):
        code = main([*args, "--repo-root", str(repo)])
        captured = capsys.readouterr()
        assert code == expect, captured.err
        return json.loads(captured.out) if code == 0 else captured.err

    return _command


class TestCatalogue:
    """The same commands as the catalogue tools, through the job service."""

    def test_feature_list(self, command):
        listed = command("feature", "list")

        assert [f["path"] for f in listed["features"]] == [
            "features/a.feature",
            "features/b.feature",
        ]

    def test_feature_describe(self, command):
        described = command("feature", "describe", "features/a.feature")

        assert [s["name"] for s in described["scenarios"]] == ["A"]

    def test_scenario_find(self, command):
        found = command("scenario", "find", "--release", "noble")

        assert [m["scenario"] for m in found["matches"]] == ["A"]

    def test_dimension_list(self, command):
        dimensions = command("dimension", "list")

        assert [r["name"] for r in dimensions["releases"]] == [
            "jammy",
            "noble",
        ]


class TestJobs:
    def test_a_checkout_with_no_jobs_lists_none(self, command):
        listed = command("job", "list")

        assert listed["jobs"] == []
        assert [p["pool"] for p in listed["pools"]] == ["lxd", "cloud", "wsl"]

    def test_results_of_no_jobs_are_empty(self, command):
        assert command("job", "results")["results"] == []

    def test_an_unknown_job_is_an_error(self, command):
        error = command("job", "kill", "no-such-job", expect=2)

        assert "Unknown job_id" in error


class TestCampaignCommands:
    """run, pause, retry and list, on a checkout that starts no jobs."""

    @pytest.fixture
    def created(self, command):
        command("campaign", "create", "--campaign-id", "1234567")
        # The lxd pool is disabled after the campaign exists, so a run
        # queues nothing and no real job starts.
        command("config", "set", "max_jobs.lxd", "0")
        return "1234567"

    def test_list_shows_the_checkout_s_campaigns(self, command, created):
        listed = command("campaign", "list")

        assert [c["campaign_id"] for c in listed["campaigns"]] == [created]

    def test_run_sets_the_intent_and_reports_a_refusal(self, command, created):
        ran = command("campaign", "run", "--campaign-id", created)
        events = command(
            "campaign",
            "events",
            "--campaign-id",
            created,
            "--kinds",
            "anomaly.starved",
        )

        assert ran["intent"] == "run"
        assert [e["data"]["pool"] for e in events["events"]] == ["lxd"]

    def test_pause_holds_it(self, command, created):
        command("campaign", "run", "--campaign-id", created)

        paused = command("campaign", "pause", "--campaign-id", created)

        assert (paused["intent"], paused["state"]) == ("hold", "paused")

    def test_retry_with_nothing_to_retry_is_an_error(self, command, created):
        error = command(
            "campaign", "retry", "--campaign-id", created, expect=2
        )

        assert "nothing was re-queued" in error

    def test_a_campaign_named_by_directory_cannot_run(
        self, capsys, campaign_file, run
    ):
        run(["campaign", "create"])

        code = main(["campaign", "run", "--campaign", str(campaign_file)])

        assert code == 2
        assert "job service" in capsys.readouterr().err
