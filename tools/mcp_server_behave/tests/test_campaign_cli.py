import json

import pytest

from campaign.cli import main

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


def attempt(unit, state, job_id):
    feature, scenario, release, machine_type = unit
    return {
        "feature": feature,
        "scenario": scenario,
        "release": release,
        "machine_type": machine_type,
        "state": state,
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
def tracker(tmp_path):
    return tmp_path / "records.jsonl"


@pytest.fixture
def repo(tmp_path):
    features = tmp_path / "features"
    features.mkdir()
    (features / "a.feature").write_text(FEATURE_A)
    (features / "b.feature").write_text(FEATURE_B)
    return tmp_path


@pytest.fixture
def run(tmp_path, tracker, repo, capsys):
    def _run(args, payload=None, expect=0):
        if payload is not None:
            source = tmp_path / "input.json"
            source.write_text(json.dumps(payload))
            args = [*args, "--input", str(source)]
        if args[0] == "init":
            args = [*args, "--repo-root", str(repo)]
        if args[0] == "record" and "--install-from" not in args:
            args = [*args, "--install-from", "proposed"]
        code = main([*args, "--tracker", str(tracker)])
        captured = capsys.readouterr()
        assert code == expect, captured.err
        return json.loads(captured.out) if code == 0 else captured.err

    return _run


@pytest.fixture
def planned(run):
    return run(["init"])


class TestInit:
    def test_campaign_covers_every_discovered_unit(self, planned, run):
        assert planned["counts"]["unattempted"] == 3
        assert planned["running"] == []
        assert planned["problems"] == []
        history = run(["history"])
        assert [unit_of(unit) for unit in history["units"]] == [
            UNIT_A_JAMMY,
            UNIT_A_NOBLE,
            UNIT_B_JAMMY,
        ]

    def test_filters_define_the_campaign(self, run):
        result = run(["init", "--machine-type", "lxd-vm"])

        assert result["counts"]["unattempted"] == 1
        history = run(["history"])
        assert [unit_of(unit) for unit in history["units"]] == [UNIT_A_NOBLE]

    def test_initialising_twice_is_rejected(self, planned, run):
        error = run(["init"], expect=2)

        assert "already holds a campaign" in error

    def test_unknown_filter_value_is_rejected(self, run):
        error = run(["init", "--release", "bogus"], expect=2)

        assert "unknown release: bogus" in error

    def test_full_campaign_needs_no_filters(self, run, tracker):
        result = run(["init", "--campaign-id", "1234567"])

        assert result["counts"]["unattempted"] == 3
        assert result["campaign"]["filters"] == {
            "feature": [],
            "scenario": [],
            "release": [],
            "machine_type": [],
        }

    def test_campaign_header_records_how_it_was_built(self, run, repo):
        result = run(
            ["init", "--campaign-id", "1234567", "--release", "jammy"]
        )
        campaign = result["campaign"]

        assert campaign["campaign_id"] == "1234567"
        assert campaign["filters"]["release"] == ["jammy"]
        assert campaign["repo"]["root"] == str(repo)
        assert campaign["at"].endswith("Z")

    def test_campaign_header_is_the_first_record(self, planned, tracker):
        first = json.loads(tracker.read_text().splitlines()[0])

        assert first["type"] == "campaign"

    def test_status_reports_the_campaign(self, planned, run):
        result = run(["status"])

        assert result["campaign"]["filters"]["release"] == []


class TestRecord:
    def test_attempt_for_unplanned_unit_is_rejected(
        self, planned, run, tracker
    ):
        original = tracker.read_text()
        unplanned = attempt(
            ["features/a.feature", "A", "xenial", "lxd-vm"], "failed", "job-1"
        )

        error = run(["record"], [unplanned], expect=2)

        assert "unplanned units" in error
        assert tracker.read_text() == original

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
                attempt(UNIT_A_JAMMY, "running", "job-1"),
                attempt(UNIT_A_NOBLE, "running", "job-2"),
            ],
        )

        assert result["counts"]["running"] == 2

    def test_attempts_store_install_source_and_timestamp(
        self, planned, run, tracker
    ):
        run(
            ["record", "--install-from", "proposed"],
            [attempt(UNIT_A_JAMMY, "passed", "job-1")],
        )
        stored = json.loads(tracker.read_text().splitlines()[-1])

        assert stored["install_from"] == "proposed"
        assert stored["at"].endswith("Z")

    def test_unknown_install_source_is_rejected(
        self, planned, tmp_path, tracker
    ):
        source = tmp_path / "attempts.json"
        source.write_text(
            json.dumps([attempt(UNIT_A_JAMMY, "passed", "job-1")])
        )

        with pytest.raises(SystemExit):
            main(
                [
                    "record",
                    "--tracker",
                    str(tracker),
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
                attempt(UNIT_A_NOBLE, "running", "job-2"),
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

        assert result["counts"]["unattempted"] == 1
        assert result["running"] == []
        assert result["problems"] == []

    def test_status_reports_running_and_problems(self, planned, run):
        run(
            ["record"],
            [
                attempt(UNIT_A_JAMMY, "failed", "job-1"),
                attempt(UNIT_A_NOBLE, "running", "job-2"),
            ],
        )

        result = run(["status"])

        assert result["counts"]["failed"] == 1
        assert result["counts"]["running"] == 1
        assert len(result["running"]) == 1
        assert result["running"][0]["job_id"] == "job-2"
        assert len(result["problems"]) == 1
        assert result["problems"][0]["job_id"] == "job-1"
        assert result["problems"][0]["state"] == "failed"

    def test_history_lists_every_attempt(self, planned, run):
        run(["record"], [attempt(UNIT_A_JAMMY, "failed", "job-1")])
        run(["record"], [attempt(UNIT_A_JAMMY, "passed", "job-2")])

        result = run(["history", "--release", "jammy", "--scenario", "A"])

        assert result["units"][0]["attempts"] == [
            {"state": "failed", "job_id": "job-1"},
            {"state": "passed", "job_id": "job-2"},
        ]

    def test_status_on_missing_tracker_is_empty(self, run):
        result = run(["status"])

        assert result["running"] == []
        assert result["problems"] == []
        assert result["counts"]["unattempted"] == 0
