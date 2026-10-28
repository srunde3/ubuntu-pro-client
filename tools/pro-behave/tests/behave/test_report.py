"""Reading behave's JSON report."""

import pytest

from pro_behave.behave.report import (
    BehaveScenarioStatus,
    scenario_status_from_element,
    summarize_report,
)

SCENARIO_STATUS_FOR_BEHAVE = {
    BehaveScenarioStatus.PASSED: "passed",
    BehaveScenarioStatus.FAILED: "failed",
    BehaveScenarioStatus.ERROR: "failed",
    BehaveScenarioStatus.HOOK_ERROR: "failed",
    BehaveScenarioStatus.CLEANUP_ERROR: "failed",
    BehaveScenarioStatus.UNDEFINED: "failed",
    BehaveScenarioStatus.SKIPPED: "skipped",
}


def test_every_behave_scenario_status_has_an_expectation():
    assert set(SCENARIO_STATUS_FOR_BEHAVE) == set(BehaveScenarioStatus)


@pytest.mark.parametrize(
    "reported, expected", SCENARIO_STATUS_FOR_BEHAVE.items(), ids=str
)
def test_a_reported_scenario_status_wins_over_the_steps(reported, expected):
    scenario = {
        "status": reported.value,
        "steps": [{"name": "step", "result": {"status": "passed"}}],
    }

    assert scenario_status_from_element(scenario) == expected


def test_scenario_status_from_element_falls_back_to_steps_when_absent():
    scenario = {
        "steps": [{"name": "step", "result": {"status": "passed"}}],
    }
    assert scenario_status_from_element(scenario) == "passed"


def test_scenario_status_from_element_falls_back_for_unknown_reported_status():
    scenario = {
        "status": "some-future-behave-status",
        "steps": [{"name": "step", "result": {"status": "passed"}}],
    }
    assert scenario_status_from_element(scenario) == "passed"


def test_summarize_report_classifies_skipped_scenarios_correctly():
    report_data = [
        {
            "name": "feature",
            "elements": [
                {
                    "name": "passing scenario",
                    "status": "passed",
                    "steps": [
                        {"name": "step", "result": {"status": "passed"}}
                    ],
                },
                {
                    "name": "skipped scenario",
                    "status": "skipped",
                    "steps": [],
                },
            ],
        }
    ]

    result = summarize_report(report_data)

    assert result.summary["scenarios"]["passed"] == 1
    assert result.summary["scenarios"]["skipped"] == 1
    assert result.summary["scenarios"]["unknown"] == 0
