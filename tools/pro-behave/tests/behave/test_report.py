"""Reading a behave job's report and log into observations."""

import json

import pytest

from pro_behave.behave.report import STATUS_OF, observe
from pro_behave.jobs.messages import Failure, InstanceStatus

FEATURE = "features/cli/attach.feature"
ROW = "Attach -- @1.1 ubuntu release"
OTHER_ROW = "Attach -- @1.2 ubuntu release"


def element(name, line, status, steps=(), type="scenario"):
    return {
        "type": type,
        "keyword": "Scenario Outline",
        "name": name,
        "tags": [],
        "location": "{}:{}".format(FEATURE, line),
        "steps": list(steps),
        "status": status,
    }


def step(name, status, error_message=None):
    result = {"status": status, "duration": 0.1}
    if error_message is not None:
        result["error_message"] = error_message
    return {
        "keyword": "Then",
        "step_type": "then",
        "name": name,
        "result": result,
    }


def write(tmp_path, elements, log=""):
    report = tmp_path / "report.json"
    report.write_text(json.dumps([{"name": "Attach", "elements": elements}]))
    stdout = tmp_path / "stdout.log"
    stdout.write_text(log)
    return report, stdout


# Every scenario status behave writes, and what it says about the unit.
BEHAVE_STATUSES = {
    "passed": InstanceStatus.PASSED,
    "failed": InstanceStatus.FAILED,
    "error": InstanceStatus.FAILED,
    "skipped": InstanceStatus.SKIPPED,
    "hook_error": InstanceStatus.ERROR,
    "cleanup_error": InstanceStatus.ERROR,
    "undefined": InstanceStatus.ERROR,
    "pending": InstanceStatus.ERROR,
    "untested": InstanceStatus.ERROR,
    None: InstanceStatus.ERROR,
}


def test_every_status_is_reached():
    assert set(BEHAVE_STATUSES.values()) == set(InstanceStatus)


def test_every_mapped_status_has_an_expectation():
    assert set(STATUS_OF) <= set(BEHAVE_STATUSES)


@pytest.mark.parametrize("raw, expected", BEHAVE_STATUSES.items(), ids=str)
def test_each_behave_status(tmp_path, raw, expected):
    report, log = write(tmp_path, [element(ROW, 10, raw)])

    (observed,) = observe(report, log)

    assert observed.status == expected


def test_a_run_is_identified_by_its_location(tmp_path):
    report, log = write(tmp_path, [element(ROW, 10, "passed")])

    (observed,) = observe(report, log)

    assert (observed.id, observed.name) == ("{}:10".format(FEATURE), ROW)


def test_backgrounds_are_not_runs(tmp_path):
    report, log = write(
        tmp_path,
        [
            element("", 3, "passed", type="background"),
            element(ROW, 10, "passed"),
        ],
    )

    observed = observe(report, log)

    assert [o.name for o in observed] == [ROW]


def test_failing_steps_are_the_failures(tmp_path):
    report, log = write(
        tmp_path,
        [
            element(
                ROW,
                10,
                "failed",
                [
                    step("it attaches", "passed"),
                    step("it works", "failed", "boom"),
                ],
            )
        ],
    )

    (observed,) = observe(report, log)

    assert observed.failures == (
        Failure(
            instance="{}:10".format(FEATURE),
            step="it works",
            status="failed",
            error_message="boom",
        ),
    )


def test_skip_reasons_come_from_the_log_in_order(tmp_path):
    # Two outlines may share a name; behave logs their skips in run order.
    log = (
        "WARNING:behave:SKIP Scenario {}: release is bionic - skipping.\n"
        "WARNING:behave:SKIP Scenario {}: release is focal - skipping.\n"
    ).format(ROW, ROW)
    report, stdout = write(
        tmp_path,
        [element(ROW, 10, "skipped"), element(ROW, 40, "skipped")],
        log,
    )

    observed = observe(report, stdout)

    assert [o.reason for o in observed] == [
        "release is bionic - skipping.",
        "release is focal - skipping.",
    ]


def test_a_broken_run_takes_its_reason_from_the_log_s_hook_error(tmp_path):
    log = "HOOK-ERROR in after_scenario: InstanceNotFoundError\n\n"
    report, stdout = write(tmp_path, [element(ROW, 10, "hook_error")], log)

    (observed,) = observe(report, stdout)

    assert (
        observed.reason
        == "HOOK-ERROR in after_scenario: InstanceNotFoundError"
    )


def test_a_broken_run_with_nothing_in_the_log_names_its_status(tmp_path):
    report, log = write(tmp_path, [element(ROW, 10, "cleanup_error")])

    (observed,) = observe(report, log)

    assert observed.reason == "behave status cleanup_error"


@pytest.mark.parametrize("text", ["", "not json", "{}"], ids=repr)
def test_no_readable_report_is_none(tmp_path, text):
    report = tmp_path / "report.json"
    if text:
        report.write_text(text)

    assert observe(report, tmp_path / "stdout.log") is None


def test_rows_of_other_combos_are_observed_too(tmp_path):
    # Deciding which runs are the unit's is the rules' job, not the reader's.
    report, log = write(
        tmp_path,
        [element(ROW, 10, "passed"), element(OTHER_ROW, 11, "skipped")],
    )

    observed = observe(report, log)

    assert [o.name for o in observed] == [ROW, OTHER_ROW]
