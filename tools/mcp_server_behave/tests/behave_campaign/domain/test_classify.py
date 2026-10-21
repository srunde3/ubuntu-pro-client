"""What a finished MCP job established for its unit."""

import pytest

from behave_campaign.domain.classify import classify_result, finished_from_mcp
from behave_campaign.domain.records import CampaignError, Outcome, Unit

AT = "2026-09-12T12:00:00Z"


UNIT_A = Unit("features/a.feature", "A", "jammy", "lxd-container")


def completed(
    passed=1, failed=0, skipped=0, unknown=0, ok=True, job_id="job1"
):
    scenarios = {"passed": passed, "failed": failed, "skipped": skipped}
    if unknown:
        scenarios["unknown"] = unknown
    return {
        "status": "completed",
        "ok": ok,
        "job_id": job_id,
        "summary": {"scenarios": scenarios, "features": {}},
    }


CLASSIFY_CASES = [
    pytest.param(completed(passed=2), Outcome.PASSED, False, id="clean-pass"),
    # A skipped Examples row next to a passing one: that run passed.
    pytest.param(
        completed(passed=1, skipped=1),
        Outcome.PASSED,
        False,
        id="pass-and-skip",
    ),
    pytest.param(
        completed(passed=1, failed=1), Outcome.FAILED, False, id="any-failure"
    ),
    pytest.param(
        completed(passed=0, skipped=2), Outcome.SKIPPED, False, id="only-skips"
    ),
    pytest.param(
        {"status": "completed", "ok": False, "job_id": "j", "summary": None},
        Outcome.ERROR,
        False,
        id="no-report",
    ),
    # The CLI raises on these. A scheduler has nobody to raise at, so it
    # records an error with the reason and keeps going.
    pytest.param(
        completed(passed=1, unknown=1),
        Outcome.ERROR,
        True,
        id="unknown-scenario-status",
    ),
    pytest.param(
        completed(passed=1, ok=False),
        Outcome.ERROR,
        True,
        id="pass-contradicted-by-ok-false",
    ),
    pytest.param("not a payload", Outcome.ERROR, True, id="junk"),
]


def test_every_outcome_is_reached_by_a_case():
    assert {case.values[1] for case in CLASSIFY_CASES} == set(Outcome)


@pytest.mark.parametrize("result, outcome, flagged", CLASSIFY_CASES)
def test_classify_result(result, outcome, flagged):
    classified = classify_result(UNIT_A, result, AT)

    assert classified.outcome == outcome
    assert bool(classified.problem) is flagged


def test_a_classified_result_keeps_the_job_id_and_time():
    classified = classify_result(UNIT_A, completed(passed=2), AT)

    assert classified.job_id == "job1"
    assert classified.at == AT


def test_an_unclassifiable_result_keeps_the_job_id_and_says_why():
    classified = classify_result(
        UNIT_A, completed(passed=1, unknown=1, job_id="job-xyz"), AT
    )

    assert classified.job_id == "job-xyz"
    assert "classify" in classified.problem


class TestMcpResultAdapter:
    def completed(self, scenarios, **overrides):
        result = {
            "status": "completed",
            "ok": True,
            "job_id": "job-1",
            "returncode": 0,
            "summary": {"scenarios": scenarios, "features": {}},
            "failures": [],
        }
        result.update(overrides)
        return result

    @pytest.mark.parametrize("status", ["started", "timeout"])
    def test_a_job_still_in_flight_has_no_outcome_to_record(self, status):
        # The scheduler owns an unfinished job: it wrote the start itself
        # and will write the finish. Recording one here would be inventing
        # a result nobody has.
        with pytest.raises(CampaignError) as error:
            finished_from_mcp(
                UNIT_A, {"status": status, "ok": True, "job_id": "job-1"}
            )

        assert "only a completed job" in str(error.value)

    def test_clean_pass_is_passed(self):
        result = self.completed({"passed": 2})

        assert finished_from_mcp(UNIT_A, result).outcome == "passed"

    def test_any_failure_is_failed(self):
        result = self.completed({"passed": 1, "failed": 1}, ok=False)

        assert finished_from_mcp(UNIT_A, result).outcome == "failed"

    def test_only_skips_is_skipped(self):
        result = self.completed({"skipped": 2})

        assert finished_from_mcp(UNIT_A, result).outcome == "skipped"

    def test_missing_report_is_error(self):
        result = self.completed({"passed": 1}, summary=None, ok=False)

        assert finished_from_mcp(UNIT_A, result).outcome == "error"

    def test_mixed_pass_and_skip_is_passed(self):
        result = self.completed({"passed": 1, "skipped": 1})

        assert finished_from_mcp(UNIT_A, result).outcome == "passed"

    def test_scenario_counts_with_total_is_supported(self):
        result = self.completed(
            {"total": 6, "passed": 1, "skipped": 5, "failed": 0}
        )

        assert finished_from_mcp(UNIT_A, result).outcome == "passed"

    def test_unknown_scenario_status_is_rejected(self):
        result = self.completed({"passed": 1, "unknown": 1})

        with pytest.raises(CampaignError, match="cannot classify"):
            finished_from_mcp(UNIT_A, result)

    def test_pass_contradicted_by_return_code_is_rejected(self):
        result = self.completed({"passed": 1}, ok=False)

        with pytest.raises(CampaignError, match="ok=false"):
            finished_from_mcp(UNIT_A, result)

    def test_capacity_exceeded_is_not_an_attempt(self):
        rejected = {
            "status": "capacity_exceeded",
            "ok": False,
            "error": "full",
        }

        with pytest.raises(CampaignError, match="capacity_exceeded"):
            finished_from_mcp(UNIT_A, rejected)
