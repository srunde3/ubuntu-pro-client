"""The rules that decide a unit's result from what its job's files show."""

import pytest
from pydantic import ValidationError

from pro_behave.jobs.domain.results import (
    ObservedInstance,
    decide,
    finished_result,
    killed_result,
    start_failure,
    unreadable_result,
)
from pro_behave.jobs.messages import (
    Failure,
    Instance,
    InstanceStatus,
    JobResult,
    Outcome,
)

PASSED = InstanceStatus.PASSED
FAILED = InstanceStatus.FAILED
SKIPPED = InstanceStatus.SKIPPED
ERROR = InstanceStatus.ERROR

# The unit's two instances, and a row of another combo.
ROW_1 = Instance(id="features/a.feature:10", name="A -- @1.1 rows")
ROW_2 = Instance(id="features/a.feature:11", name="A -- @1.2 rows")
OTHER = Instance(id="features/a.feature:12", name="A -- @1.3 rows")
UNIT = [ROW_1, ROW_2]


def seen(instance, status, reason=""):
    failures = ()
    if status == FAILED:
        failures = (
            Failure(
                instance=instance.id,
                step="Then it works",
                status="failed",
                error_message="it did not",
            ),
        )
    return ObservedInstance(
        id=instance.id,
        name=instance.name,
        status=status,
        reason=reason,
        failures=failures,
    )


# Each rule, as (what the report shows, the exit code) -> outcome. The first
# rule that matches decides.
RULES = [
    pytest.param(None, 0, Outcome.ERROR, id="no-readable-report"),
    pytest.param(
        [seen(OTHER, PASSED)], 0, Outcome.ERROR, id="none-of-the-unit-ran"
    ),
    pytest.param(
        [seen(ROW_1, PASSED)], 0, Outcome.ERROR, id="an-instance-is-missing"
    ),
    pytest.param(
        [seen(ROW_1, PASSED), seen(ROW_1, PASSED), seen(ROW_2, PASSED)],
        0,
        Outcome.ERROR,
        id="an-instance-appears-twice",
    ),
    pytest.param(
        [
            seen(ROW_1, PASSED),
            ObservedInstance(ROW_2.id, "renamed", PASSED),
        ],
        0,
        Outcome.ERROR,
        id="an-instance-was-renamed",
    ),
    pytest.param(
        [seen(ROW_1, PASSED), seen(ROW_2, FAILED)],
        1,
        Outcome.FAILED,
        id="a-step-failed",
    ),
    pytest.param(
        [seen(ROW_1, PASSED), seen(ROW_2, ERROR, "HOOK-ERROR in after")],
        1,
        Outcome.ERROR,
        id="the-machinery-broke",
    ),
    pytest.param(
        [seen(ROW_1, PASSED), seen(ROW_2, PASSED)],
        1,
        Outcome.ERROR,
        id="a-non-zero-exit-over-passes",
    ),
    pytest.param(
        [seen(ROW_1, SKIPPED, "no token"), seen(ROW_2, SKIPPED, "no token")],
        0,
        Outcome.SKIPPED,
        id="every-instance-skipped",
    ),
    pytest.param(
        [seen(ROW_1, PASSED), seen(ROW_2, SKIPPED, "no token")],
        0,
        Outcome.PASSED,
        id="a-pass-and-a-skip",
    ),
    pytest.param(
        [seen(ROW_1, PASSED), seen(ROW_2, PASSED), seen(OTHER, SKIPPED)],
        0,
        Outcome.PASSED,
        id="another-combo-s-row-is-ignored",
    ),
]


def test_every_outcome_is_reached_by_a_rule():
    assert {case.values[2] for case in RULES} == set(Outcome)


@pytest.mark.parametrize("observed, returncode, expected", RULES)
def test_the_first_matching_rule_decides(observed, returncode, expected):
    decided = decide(UNIT, observed, returncode)

    assert decided.outcome == expected


def test_a_decided_result_lists_every_instance_of_the_unit():
    decided = decide(
        UNIT,
        [
            seen(ROW_1, PASSED),
            seen(ROW_2, SKIPPED, "no token"),
            seen(OTHER, SKIPPED),
        ],
        0,
    )

    assert [(i.id, i.status, i.reason) for i in decided.instances] == [
        (ROW_1.id, PASSED, ""),
        (ROW_2.id, SKIPPED, "no token"),
    ]


def test_a_failure_of_another_combo_s_row_is_not_the_unit_s():
    decided = decide(
        UNIT,
        [seen(ROW_1, PASSED), seen(ROW_2, PASSED), seen(OTHER, FAILED)],
        1,
    )

    assert decided.failures == ()


def test_a_non_zero_exit_names_the_row_that_broke():
    broken = seen(OTHER, ERROR, "HOOK-ERROR in after_scenario")

    decided = decide(
        UNIT, [seen(ROW_1, PASSED), seen(ROW_2, PASSED), broken], 1
    )

    assert decided.reason == (
        "the command exited 1, at A -- @1.3 rows: error "
        "HOOK-ERROR in after_scenario"
    )


def test_a_changed_feature_says_what_changed():
    decided = decide(UNIT, [seen(ROW_1, PASSED)], 0)

    assert decided.reason == (
        "the feature changed since the job was queued: "
        "A -- @1.2 rows is missing"
    )


def test_a_finished_result_carries_the_decision():
    result = finished_result(
        "T1", 1, UNIT, [seen(ROW_1, PASSED), seen(ROW_2, FAILED)]
    )

    assert (result.outcome, result.returncode, result.killed) == (
        Outcome.FAILED,
        1,
        False,
    )
    assert [f.instance for f in result.failures] == [ROW_2.id]


# Every way a job ends, and whether it has an outcome.
ENDINGS = {
    "finished": finished_result(
        "T1", 0, UNIT, [seen(ROW_1, PASSED), seen(ROW_2, PASSED)]
    ),
    "killed": killed_result("T1", returncode=-15),
    "could-not-start": start_failure("T1", "no tox"),
    "unreadable": unreadable_result("T1", 0, ValueError("bad")),
}


@pytest.mark.parametrize("ending", ENDINGS.values(), ids=list(ENDINGS))
def test_a_result_has_an_outcome_exactly_when_not_killed(ending):
    assert (ending.outcome is None) is ending.killed


@pytest.mark.parametrize(
    "fields",
    [{"killed": True, "outcome": Outcome.PASSED}, {"killed": False}],
    ids=["killed-with-an-outcome", "not-killed-without-one"],
)
def test_a_result_that_breaks_the_rule_is_refused(fields):
    with pytest.raises(ValidationError):
        JobResult(**fields)
