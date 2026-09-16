import json

import pytest

from behave_campaign.cli import main

UNIT_A_JAMMY = ["features/a.feature", "A", "jammy", "lxd-container"]
UNIT_A_NOBLE = ["features/a.feature", "A", "noble", "lxd-vm"]
UNIT_B_JAMMY = ["features/b.feature", "B", "jammy", "lxd-container"]

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
    """One completed try, as the CLI's record command takes it."""
    feature, scenario, release, machine_type = unit
    return {
        "feature": feature,
        "scenario": scenario,
        "release": release,
        "machine_type": machine_type,
        "outcome": outcome,
        "job_id": job_id,
    }


def unit_of(reported):
    return [
        reported["feature"],
        reported["scenario"],
        reported["release"],
        reported["machine_type"],
    ]


@pytest.fixture
def campaign_file(tmp_path):
    return tmp_path / "records.jsonl"


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
        if args[0] == "record" and "--install-from" not in args:
            args = [*args, "--install-from", "proposed"]
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
        assert planned["state"]["counts"]["unattempted"] == 3
        assert planned["campaign"]["total_units"] == 3
        history = run(["history"])
        assert [unit_of(unit) for unit in history["units"]] == [
            UNIT_A_JAMMY,
            UNIT_A_NOBLE,
            UNIT_B_JAMMY,
        ]

    def test_filters_define_the_campaign(self, run):
        result = run(["create", "--machine-type", "lxd-vm"])

        assert result["state"]["counts"]["unattempted"] == 1
        history = run(["history"])
        assert [unit_of(unit) for unit in history["units"]] == [UNIT_A_NOBLE]

    def test_initialising_twice_is_rejected(self, planned, run):
        error = run(["create"], expect=2)

        assert "already holds a campaign" in error

    def test_unknown_filter_value_is_rejected(self, run):
        error = run(["create", "--release", "bogus"], expect=2)

        assert "unknown release: bogus" in error

    def test_full_campaign_needs_no_filters(self, run):
        result = run(["create"])

        assert result["state"]["counts"]["unattempted"] == 3
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
    def test_lists_every_unit_with_its_state(self, planned, run):
        run(["record"], [attempt(UNIT_A_JAMMY, "failed", "job-1")])

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
        run(["record"], [attempt(UNIT_A_JAMMY, "failed", "job-1")])

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
        run(
            ["record"],
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
            "attempt,job_id,install_from,started_at,outcome,finished_at"
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

        expected = repo / ".mcp_server_behave" / "campaigns" / "1234567.jsonl"
        assert expected.is_file()
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

    def test_a_file_names_its_own_id(self, planned, run, campaign_file):
        result = run(["status"])

        assert result["campaign"]["campaign_id"] == campaign_file.stem

    def test_missing_repo_root_is_a_clear_error(self, monkeypatch, capsys):
        monkeypatch.delenv("UBUNTU_PRO_CLIENT_REPO", raising=False)

        code = main(["status", "--campaign-id", "1234567"])

        assert code == 2
        assert "UBUNTU_PRO_CLIENT_REPO" in capsys.readouterr().err

    def test_campaign_header_is_the_first_record(self, planned, campaign_file):
        first = json.loads(campaign_file.read_text().splitlines()[0])

        assert first["type"] == "campaign"

    def test_status_reports_the_campaign(self, planned, run):
        result = run(["status"])

        assert result["campaign"]["scope"]["release"] == []


class TestRecord:
    def test_attempt_for_unplanned_unit_is_rejected(
        self, planned, run, campaign_file
    ):
        original = campaign_file.read_text()
        unplanned = attempt(
            ["features/a.feature", "A", "xenial", "lxd-vm"], "failed", "job-1"
        )

        error = run(["record"], [unplanned], expect=2)

        assert "unplanned units" in error
        assert campaign_file.read_text() == original

    def test_recorded_pass_supersedes_earlier_failure(self, planned, run):
        run(["record"], [attempt(UNIT_A_JAMMY, "failed", "job-1")])
        result = run(["record"], [attempt(UNIT_A_JAMMY, "passed", "job-2")])

        assert result["counts"]["passed"] == 1
        assert result["counts"]["failed"] == 0
        assert result["problems"] == []

    def test_batches_record_every_attempt(self, planned, run):
        result = run(
            ["record"],
            [
                attempt(UNIT_A_JAMMY, "failed", "job-1"),
                attempt(UNIT_A_NOBLE, "passed", "job-2"),
            ],
        )

        assert result["recorded"] == 2
        assert result["counts"]["failed"] == 1
        assert result["counts"]["passed"] == 1

    def test_a_job_still_in_flight_cannot_be_recorded(self, planned, run):
        # The scheduler owns unfinished jobs: it wrote the start and will
        # write the finish. The CLI records completed tries only.
        error = run(
            ["record"], [attempt(UNIT_A_JAMMY, "running", "job-1")], expect=2
        )

        assert "outcome must be one of" in error

    def test_attempts_store_install_source_and_timestamp(
        self, planned, run, campaign_file
    ):
        run(
            ["record", "--install-from", "proposed"],
            [attempt(UNIT_A_JAMMY, "passed", "job-1")],
        )
        lines = campaign_file.read_text().splitlines()
        started = json.loads(lines[-2])
        finished = json.loads(lines[-1])

        # One completed try is written as both halves; the start is what
        # carries the install source.
        assert started["type"] == "started"
        assert started["install_from"] == "proposed"
        assert started["at"].endswith("Z")
        assert finished["type"] == "finished"
        assert finished["outcome"] == "passed"
        assert finished["job_id"] == started["job_id"]

    def test_unknown_install_source_is_rejected(
        self, planned, tmp_path, campaign_file
    ):
        source = tmp_path / "attempts.json"
        source.write_text(
            json.dumps([attempt(UNIT_A_JAMMY, "passed", "job-1")])
        )

        with pytest.raises(SystemExit):
            main(
                [
                    "record",
                    "--campaign",
                    str(campaign_file),
                    "--input",
                    str(source),
                    "--install-from",
                    "somewhere",
                ]
            )

    def test_mcp_payloads_are_translated_into_attempts(self, planned, run):
        feature, scenario, release, machine_type = UNIT_A_JAMMY
        result = run(
            ["record", "--from-mcp"],
            [
                {
                    "unit": {
                        "feature": feature,
                        "scenario": scenario,
                        "release": release,
                        "machine_type": machine_type,
                    },
                    "result": {
                        "status": "completed",
                        "ok": True,
                        "job_id": "job-abc",
                        "returncode": 0,
                        "summary": {
                            "scenarios": {"passed": 1},
                            "features": {"passed": 1},
                        },
                        "failures": [],
                    },
                }
            ],
        )

        assert result["counts"]["passed"] == 1
        history = run(["history"])
        assert history["units"][0]["job_id"] == "job-abc"

    def test_unclassifiable_mcp_result_is_rejected(self, planned, run):
        feature, scenario, release, machine_type = UNIT_A_JAMMY
        error = run(
            ["record", "--from-mcp"],
            [
                {
                    "unit": {
                        "feature": feature,
                        "scenario": scenario,
                        "release": release,
                        "machine_type": machine_type,
                    },
                    "result": {
                        "status": "completed",
                        "ok": True,
                        "job_id": "job-abc",
                        "summary": {
                            "scenarios": {"passed": 1, "unknown": 1},
                            "features": {},
                        },
                        "failures": [],
                    },
                }
            ],
            expect=2,
        )

        assert "cannot classify" in error


class TestQueries:
    def test_next_returns_unattempted_before_problems(self, planned, run):
        run(
            ["record"],
            [
                attempt(UNIT_A_JAMMY, "failed", "job-1"),
                attempt(UNIT_A_NOBLE, "passed", "job-2"),
            ],
        )

        result = run(["next", "--limit", "5"])

        assert [unit_of(unit) for unit in result["units"]] == [
            UNIT_B_JAMMY,
            UNIT_A_JAMMY,
        ]

    def test_next_respects_filters_and_limit(self, planned, run):
        run(["record"], [attempt(UNIT_B_JAMMY, "passed", "job-1")])

        result = run(["next", "--limit", "1", "--release", "jammy"])

        assert [unit_of(unit) for unit in result["units"]] == [UNIT_A_JAMMY]

    def test_status_filters_narrow_units_and_counts(self, planned, run):
        result = run(["status", "--machine-type", "lxd-vm"])

        assert result["state"]["counts"]["unattempted"] == 1
        assert result["running"] == []
        assert result["problems"] == []

    def test_status_reports_problems(self, planned, run):
        run(
            ["record"],
            [
                attempt(UNIT_A_JAMMY, "failed", "job-1"),
                attempt(UNIT_A_NOBLE, "skipped", "job-2"),
            ],
        )

        result = run(["status"])

        assert result["state"]["counts"]["failed"] == 1
        assert result["state"]["counts"]["skipped"] == 1
        assert result["running"] == []
        assert {u["job_id"] for u in result["problems"]} == {"job-1", "job-2"}
        assert {u["state"] for u in result["problems"]} == {
            "failed",
            "skipped",
        }

    def test_status_groups_problems_by_scenario(self, planned, run):
        run(
            ["record"],
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

    def test_history_lists_every_attempt(self, planned, run):
        run(["record"], [attempt(UNIT_A_JAMMY, "failed", "job-1")])
        run(["record"], [attempt(UNIT_A_JAMMY, "passed", "job-2")])

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
        # defaults -- max_lanes 1, no repo -- which looks like a real one.
        for command in (["status"], ["next"], ["history"], ["events"]):
            error = run(command, expect=2)
            assert str(campaign_file) in error, command

        error = run(
            ["record"],
            payload=[attempt(UNIT_A_JAMMY, "passed", "job-1")],
            expect=2,
        )
        assert str(campaign_file) in error

    def test_a_missing_id_is_an_error(self, run, repo):
        error = run(
            ["status", "--campaign-id", "absent", "--repo-root", str(repo)],
            expect=2,
        )

        assert "absent" in error


class TestEvents:
    """``events`` reads the log a server wrote beside the campaign file."""

    @pytest.fixture
    def log(self, campaign_file):
        from behave_campaign.adapters import JsonlEventLog

        return JsonlEventLog(campaign_file.parent)

    @staticmethod
    def new(kind, **data):
        from behave_campaign.domain import NewEvent

        return NewEvent(kind=kind, at="2026-01-01T00:00:00Z", data=data)

    def test_reads_the_servers_events(self, planned, run, log):
        log.append(
            "records",
            [self.new("campaign.started"), self.new("unit.failed", x=1)],
        )

        result = run(["events"])

        assert [e["kind"] for e in result["events"]] == [
            "campaign.started",
            "unit.failed",
        ]
        assert result["events"][1]["data"] == {"x": 1}
        assert result["next_seq"] == 2
        assert "campaign_id" not in result["events"][0]

    def test_cursor_and_kinds_narrow_the_batch(self, planned, run, log):
        log.append(
            "records",
            [
                self.new("campaign.started"),
                self.new("lane.started"),
                self.new("unit.failed"),
                self.new("unit.passed"),
            ],
        )

        result = run(["events", "--since-seq", "1", "--kinds", "unit.*"])

        assert [e["seq"] for e in result["events"]] == [3, 4]

    def test_unknown_kind_is_rejected(self, planned, run):
        error = run(["events", "--kinds", "unit.exploded"], expect=2)

        assert "unit.exploded" in error

    def test_follow_stops_once_the_campaign_is_settled(
        self, planned, run, log, capsys, campaign_file
    ):
        from behave_campaign.adapters import SingleFileCampaignStore
        from behave_campaign.domain import Lifecycle, LifecycleRecord

        # Started, every unit attempted, nothing in flight: complete, so a
        # follower prints what there is and returns.
        SingleFileCampaignStore(campaign_file).append(
            "records",
            [
                LifecycleRecord(
                    state=Lifecycle.RUNNING, at="2026-01-01T00:00:00Z"
                )
            ],
        )
        run(
            ["record"],
            payload=[
                attempt(UNIT_A_JAMMY, "passed", "j1"),
                attempt(UNIT_A_NOBLE, "passed", "j2"),
                attempt(UNIT_B_JAMMY, "passed", "j3"),
            ],
        )
        log.append(
            "records", [self.new("unit.passed"), self.new("unit.passed")]
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
        assert [b["events"][0]["seq"] for b in batches] == [1, 2]
        assert batches[-1]["lifecycle"] == "complete"
