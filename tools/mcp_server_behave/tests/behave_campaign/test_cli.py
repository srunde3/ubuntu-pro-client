import json
import tarfile

import pytest

from behave_campaign.adapters import CampaignDirStore
from behave_campaign.cli import main
from behave_campaign.domain.records import (
    AttemptFinished,
    AttemptStarted,
    Cause,
    Intent,
    IntentRecord,
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
        AttemptStarted(
            unit=tried, job_id=job_id, install_from="proposed", at=AT
        ),
        AttemptFinished(unit=tried, job_id=job_id, outcome=outcome, at=AT),
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
    return tmp_path


@pytest.fixture
def run(tmp_path, campaign_file, repo, capsys):
    def _run(args, payload=None, expect=0):
        if payload is not None:
            source = tmp_path / "input.json"
            source.write_text(json.dumps(payload))
            args = [*args, "--input", str(source)]
        if args[0] == "create":
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
    return run(["create"])


class TestInit:
    def test_campaign_covers_every_discovered_unit(self, planned, run):
        assert planned["summary"]["counts"]["unattempted"] == 3
        assert planned["campaign"]["total_units"] == 3
        history = run(["history"])
        assert [unit_of(unit) for unit in history["units"]] == [
            UNIT_A_JAMMY,
            UNIT_A_NOBLE,
            UNIT_B_JAMMY,
        ]

    def test_filters_define_the_campaign(self, run):
        result = run(["create", "--machine-type", "lxd-vm"])

        assert result["summary"]["counts"]["unattempted"] == 1
        history = run(["history"])
        assert [unit_of(unit) for unit in history["units"]] == [UNIT_A_NOBLE]

    def test_lanes_are_set_per_pool(self, run):
        result = run(["create", "--lxd-lanes", "4"])

        assert result["campaign"]["lanes"] == {"lxd": 4}

    def test_lanes_for_a_pool_out_of_scope_are_rejected(self, run):
        error = run(["create", "--wsl-lanes", "1"], expect=2)

        assert "no unit in scope" in error

    def test_initialising_twice_is_rejected(self, planned, run):
        error = run(["create"], expect=2)

        assert "already holds a campaign" in error

    def test_unknown_filter_value_is_rejected(self, run):
        error = run(["create", "--release", "bogus"], expect=2)

        assert "unknown release: bogus" in error

    def test_full_campaign_needs_no_filters(self, run):
        result = run(["create"])

        assert result["summary"]["counts"]["unattempted"] == 3
        assert result["campaign"]["scope"] == {
            "feature": [],
            "scenario": [],
            "release": [],
            "machine_type": [],
        }

    def test_campaign_header_records_how_it_was_built(self, run, repo):
        result = run(["create", "--release", "jammy"])
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

        result = run(["units"])

        assert [(unit_of(u), u["state"]) for u in result["units"]] == [
            (UNIT_A_JAMMY, "failed"),
            (UNIT_A_NOBLE, "unattempted"),
            (UNIT_B_JAMMY, "unattempted"),
        ]
        assert result["truncated"] is False

    def test_filters_and_limit_apply(self, planned, run):
        result = run(["units", "--release", "jammy", "--limit", "1"])

        assert [unit_of(u) for u in result["units"]] == [UNIT_A_JAMMY]
        assert result["truncated"] is True

    def test_csv_is_one_row_per_unit(
        self, planned, run, capsys, campaign_file
    ):
        record(campaign_file, [attempt(UNIT_A_JAMMY, "failed", "job-1")])

        code = main(
            ["units", "--campaign", str(campaign_file), "--format", "csv"]
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

    def test_an_id_resolves_to_the_servers_file(self, run, repo):
        created = run(["create", "--campaign-id", "1234567"])
        status = run(
            ["status", "--campaign-id", "1234567", "--repo-root", str(repo)]
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
        run(["create", "--campaign-id", "1234567"])
        status = main(["status", "--campaign-id", "1234567"])

        assert status == 0

    def test_without_either_is_an_error(self, capsys):
        with pytest.raises(SystemExit) as exit:
            main(["status"])

        assert exit.value.code == 2
        assert "--campaign-id" in capsys.readouterr().err

    def test_both_is_an_error(self, campaign_file, capsys):
        with pytest.raises(SystemExit) as exit:
            main(
                [
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
        result = run(["status"])

        assert result["campaign"]["campaign_id"] == campaign_file.name

    def test_missing_repo_root_is_a_clear_error(self, monkeypatch, capsys):
        monkeypatch.delenv("UBUNTU_PRO_CLIENT_REPO", raising=False)

        code = main(["status", "--campaign-id", "1234567"])

        assert code == 2
        assert "UBUNTU_PRO_CLIENT_REPO" in capsys.readouterr().err

    def test_the_header_is_in_the_campaign_record(
        self, planned, campaign_file
    ):
        record = json.loads((campaign_file / "campaign.json").read_text())

        assert record["campaign"]["type"] == "campaign"

    def test_status_reports_the_campaign(self, planned, run):
        result = run(["status"])

        assert result["campaign"]["scope"]["release"] == []


class TestQueries:
    def test_status_filters_narrow_units_and_counts(self, planned, run):
        result = run(["status", "--machine-type", "lxd-vm"])

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

        result = run(["status"])

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

        result = run(["status", "--group-by", "scenario"])

        assert result["problems"] is None
        (row,) = result["problem_scenarios"]
        assert row["scenario"] == "A"
        assert [u["job_id"] for u in row["failed"]] == ["job-1"]
        assert [u["job_id"] for u in row["skipped"]] == ["job-2"]

    def test_history_lists_every_attempt(self, planned, run, campaign_file):
        record(campaign_file, [attempt(UNIT_A_JAMMY, "failed", "job-1")])
        record(campaign_file, [attempt(UNIT_A_JAMMY, "passed", "job-2")])

        result = run(["history", "--release", "jammy", "--scenario", "A"])

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
        for command in (["status"], ["history"], ["events"]):
            error = run(command, expect=2)
            assert str(campaign_file) in error, command

    def test_a_missing_id_is_an_error(self, run, repo):
        error = run(
            ["status", "--campaign-id", "absent", "--repo-root", str(repo)],
            expect=2,
        )

        assert "absent" in error


class TestEvents:
    """``events`` reads the events a campaign's records announce."""

    def test_reads_the_campaigns_events(self, planned, run, campaign_file):
        record(campaign_file, [attempt(UNIT_A_JAMMY, "failed", "job-1")])

        result = run(["events"])

        assert [e["kind"] for e in result["events"]] == [
            "campaign.created",
            "campaign.running",
            "lane.started",
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

        result = run(["events", "--since-seq", "4", "--kinds", "unit.*"])

        assert [e["kind"] for e in result["events"]] == ["unit.passed"]

    def test_unknown_kind_is_rejected(self, planned, run):
        error = run(["events", "--kinds", "unit.exploded"], expect=2)

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
    @pytest.fixture
    def finished(self, run, repo):
        """A campaign the server ran to completion, with its job files."""
        run(
            [
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
        assert "1234567/audit/events.jsonl" in names

    def test_an_existing_output_is_refused(
        self, finished, run, repo, tmp_path
    ):
        output = tmp_path / "taken.tar.gz"
        output.write_text("keep")

        error = run(
            [
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
        run(["create", "--campaign-id", "1234567"])

        error = run(
            ["export", "--campaign-id", "1234567", "--repo-root", str(repo)],
            expect=2,
        )

        assert "only a complete campaign can be exported" in error
