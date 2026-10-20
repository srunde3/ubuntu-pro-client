import csv
import io
import json

from behave_campaign import export
from behave_campaign.domain import (
    Attempt,
    CampaignHeader,
    Filters,
    RepoState,
    Unit,
    UnitStatus,
)
from behave_mcp.pools import Pool

HEADER = CampaignHeader(
    at="2026-10-06T00:00:00Z",
    campaign_id="LP-1",
    repo=RepoState(root="/repo", commit="abc123", branch="main", dirty=True),
    filters=Filters(release=("noble", "jammy")),
    install_from="proposed",
    lanes={Pool.LXD: 2},
)


def status(unit, *attempts):
    """A unit's status the way reduce_units settles it."""
    passed = [a for a in attempts if a.outcome == "passed"]
    current = passed[0] if passed else attempts[-1]
    return UnitStatus(
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
A_JAMMY_VM = Unit("features/a.feature", "A", "jammy", "lxd-vm")
A_NOBLE_LXD = Unit("features/a.feature", "A", "noble", "lxd-container")
B_JAMMY_LXD = Unit("features/b.feature", "B", "jammy", "lxd-container")


class TestJobIds:
    def test_includes_superseded_attempts(self):
        statuses = [
            status(
                A_JAMMY_LXD, attempt("old", "failed"), attempt("new", "passed")
            ),
            status(B_JAMMY_LXD, attempt("b1", "failed")),
        ]

        assert export.job_ids(statuses) == ["b1", "new", "old"]


class TestUnitsCsv:
    def test_one_row_per_unit_with_its_final_job(self):
        statuses = [
            status(
                A_JAMMY_LXD,
                attempt("old", "failed", "2026-10-06T01:00:00Z"),
                attempt("new", "passed", "2026-10-06T02:00:00Z"),
            )
        ]

        rows = list(
            csv.DictReader(io.StringIO(export.render_units_csv(statuses)))
        )

        assert rows == [
            {
                "feature": "features/a.feature",
                "scenario": "A",
                "release": "jammy",
                "machine_type": "lxd-container",
                "state": "passed",
                "job_id": "new",
                "finished_at": "2026-10-06T02:00:00Z",
            }
        ]


class TestCampaignJson:
    def test_describes_scope_and_outcome_without_repo_state(self):
        statuses = [status(A_JAMMY_LXD, attempt("j1", "passed"))]

        document = json.loads(
            export.render_campaign_json("LP-1", HEADER, "complete", statuses)
        )

        assert document["install_from"] == "proposed"
        assert document["lifecycle"] == "complete"
        assert document["counts"]["passed"] == 1
        assert document["scope"]["release"] == ["noble", "jammy"]
        assert "repo" not in document
        assert "abc123" not in json.dumps(document)


class TestSummary:
    def summary(self, statuses, missing_jobs=()):
        return export.render_summary("LP-1", HEADER, statuses, missing_jobs)

    def test_passed_and_not_passed_are_separate_sections(self):
        text = self.summary(
            [
                status(A_JAMMY_LXD, attempt("j1", "passed")),
                status(B_JAMMY_LXD, attempt("b1", "error")),
            ]
        )

        passed, not_passed = text.split("## Not passed (1)")
        assert "## Passed (1)" in passed
        assert "features/a.feature" in passed
        assert "- jammy: lxd-container\n" in passed
        assert "- jammy: lxd-container (error, job b1)" in not_passed

    def test_groups_feature_then_scenario_then_release(self):
        text = self.summary(
            [
                status(A_JAMMY_LXD, attempt("j1", "passed")),
                status(A_JAMMY_VM, attempt("j2", "passed")),
                status(A_NOBLE_LXD, attempt("n1", "passed")),
            ]
        )

        assert (
            "### features/a.feature\n\n#### A\n"
            "- noble: lxd-container\n"
            "- jammy: lxd-container, lxd-vm\n"
        ) in text

    def test_never_shows_attempt_counts(self):
        text = self.summary(
            [
                status(
                    A_JAMMY_LXD,
                    attempt("old", "failed"),
                    attempt("new", "passed"),
                )
            ]
        )

        assert "old" not in text
        assert "attempt" not in text.lower()

    def test_never_shows_repo_state(self):
        text = self.summary([status(A_JAMMY_LXD, attempt("j1", "passed"))])

        assert "abc123" not in text
        assert "main" not in text

    def test_marks_units_whose_final_job_files_are_missing(self):
        text = self.summary(
            [status(A_JAMMY_LXD, attempt("j1", "passed"))],
            missing_jobs=["j1"],
        )

        assert "- jammy: lxd-container (files missing)" in text
