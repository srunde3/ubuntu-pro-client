"""What a campaign's evidence bundle holds."""

from behave_campaign.domain.bundle import job_ids
from behave_campaign.domain.records import Attempt, Unit, UnitHistory


def status(unit, *attempts):
    """A unit's status the way reduce_units settles it."""
    passed = [a for a in attempts if a.outcome == "passed"]
    current = passed[0] if passed else attempts[-1]
    return UnitHistory(
        unit=unit,
        state=current.outcome,
        job_id=current.job_id,
        attempts=tuple(attempts),
    )


def attempt(job_id, outcome, finished_at="2026-10-06T01:00:00Z"):
    return Attempt(
        job_id=job_id,
        install_from="proposed",
        started_at="2026-10-06T00:30:00Z",
        outcome=outcome,
        finished_at=finished_at,
    )


A_JAMMY_LXD = Unit("features/a.feature", "A", "jammy", "lxd-container")


B_JAMMY_LXD = Unit("features/b.feature", "B", "jammy", "lxd-container")


class TestJobIds:
    def test_includes_superseded_attempts(self):
        statuses = [
            status(
                A_JAMMY_LXD, attempt("old", "failed"), attempt("new", "passed")
            ),
            status(B_JAMMY_LXD, attempt("b1", "failed")),
        ]

        assert job_ids(statuses) == ["b1", "new", "old"]
