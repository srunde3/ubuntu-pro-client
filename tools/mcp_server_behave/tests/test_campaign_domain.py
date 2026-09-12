import pytest

from campaign.domain import (
    AttemptRecord,
    CampaignError,
    CampaignRecord,
    Filters,
    PlanRecord,
    RepoState,
    Unit,
    attempt_from_mcp,
    attempts_from_mcp,
    count_states,
    encode_record,
    parse_record,
    problems,
    reduce_units,
    running,
    select_next,
)

UNIT_A = Unit("features/a.feature", "A", "jammy", "lxd-container")
UNIT_B = Unit("features/b.feature", "B", "noble", "lxd-vm")
AT = "2026-09-10T12:00:00Z"


def attempt(unit, state, job_id):
    return AttemptRecord(unit=unit, state=state, job_id=job_id)


class TestReduceUnits:
    def test_planned_unit_without_attempts_is_unattempted(self):
        statuses = reduce_units([PlanRecord(UNIT_A)])

        assert statuses[0].state == "unattempted"
        assert statuses[0].job_id is None

    def test_passing_attempt_wins_over_earlier_and_later_failures(self):
        statuses = reduce_units(
            [
                PlanRecord(UNIT_A),
                attempt(UNIT_A, "failed", "job-1"),
                attempt(UNIT_A, "passed", "job-2"),
                attempt(UNIT_A, "failed", "job-3"),
            ]
        )

        assert statuses[0].state == "passed"
        assert statuses[0].job_id == "job-2"
        assert len(statuses[0].attempts) == 3

    def test_latest_attempt_wins_until_a_unit_passes(self):
        statuses = reduce_units(
            [
                PlanRecord(UNIT_A),
                attempt(UNIT_A, "failed", "job-1"),
                attempt(UNIT_A, "skipped", "job-2"),
            ]
        )

        assert statuses[0].state == "skipped"
        assert statuses[0].job_id == "job-2"

    def test_units_are_ordered_and_deduplicated(self):
        statuses = reduce_units(
            [PlanRecord(UNIT_B), PlanRecord(UNIT_A), PlanRecord(UNIT_A)]
        )

        assert [status.unit for status in statuses] == [UNIT_A, UNIT_B]

    def test_attempts_for_unplanned_units_are_not_reported(self):
        statuses = reduce_units([attempt(UNIT_A, "failed", "job-1")])

        assert statuses == []


class TestSelectNext:
    def test_unattempted_units_come_before_retryable_problems(self):
        statuses = reduce_units(
            [
                PlanRecord(UNIT_A),
                PlanRecord(UNIT_B),
                attempt(UNIT_A, "failed", "job-1"),
            ]
        )

        selected = select_next(statuses, limit=5)

        assert [status.unit for status in selected] == [UNIT_B, UNIT_A]

    def test_passed_and_running_units_are_excluded(self):
        statuses = reduce_units(
            [
                PlanRecord(UNIT_A),
                PlanRecord(UNIT_B),
                attempt(UNIT_A, "passed", "job-1"),
                attempt(UNIT_B, "running", "job-2"),
            ]
        )

        assert select_next(statuses, limit=5) == []

    def test_limit_must_be_positive(self):
        with pytest.raises(CampaignError):
            select_next([], limit=0)


class TestRecordRoundTrip:
    def test_every_record_type_survives_encoding(self):
        records = [
            CampaignRecord(
                at=AT,
                campaign_id="1234567",
                repo=RepoState(
                    root="/repo",
                    commit="abc123",
                    branch="main",
                    dirty=False,
                ),
                filters=Filters(release=("jammy",)),
            ),
            PlanRecord(UNIT_A, at=AT),
            AttemptRecord(
                UNIT_A, "failed", "job-1", install_from="proposed", at=AT
            ),
        ]

        assert [
            parse_record(encode_record(record)) for record in records
        ] == records

    def test_campaign_without_an_id_survives_encoding(self):
        campaign = CampaignRecord(at=AT, repo=RepoState(root="/repo"))

        assert parse_record(encode_record(campaign)) == campaign

    def test_unknown_install_source_is_rejected(self):
        raw = encode_record(
            AttemptRecord(
                UNIT_A, "failed", "job-1", install_from="proposed", at=AT
            )
        )
        raw["install_from"] = "somewhere"

        with pytest.raises(
            CampaignError, match="install source must be one of"
        ):
            parse_record(raw)

    def test_record_without_a_timestamp_is_rejected(self):
        raw = encode_record(PlanRecord(UNIT_A, at=AT))
        raw["at"] = ""

        with pytest.raises(CampaignError, match="'at' must be a non-empty"):
            parse_record(raw)

    def test_unknown_state_is_rejected(self):
        raw = encode_record(
            AttemptRecord(
                UNIT_A, "failed", "job-1", install_from="proposed", at=AT
            )
        )
        raw["state"] = "exploded"

        with pytest.raises(CampaignError, match="state must be one of"):
            parse_record(raw)

    def test_unknown_record_type_is_rejected(self):
        with pytest.raises(CampaignError, match="record type must be"):
            parse_record({"type": "note"})


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

    def test_started_and_timeout_payloads_are_running(self):
        started = attempt_from_mcp(
            UNIT_A, {"status": "started", "ok": True, "job_id": "job-1"}
        )
        timed_out = attempt_from_mcp(
            UNIT_A, {"status": "timeout", "ok": False, "job_id": "job-1"}
        )

        assert started.state == "running"
        assert timed_out.state == "running"
        assert started.job_id == "job-1"

    def test_clean_pass_is_passed(self):
        result = self.completed({"passed": 2})

        assert attempt_from_mcp(UNIT_A, result).state == "passed"

    def test_any_failure_is_failed(self):
        result = self.completed({"passed": 1, "failed": 1}, ok=False)

        assert attempt_from_mcp(UNIT_A, result).state == "failed"

    def test_only_skips_is_skipped(self):
        result = self.completed({"skipped": 2})

        assert attempt_from_mcp(UNIT_A, result).state == "skipped"

    def test_missing_report_is_error(self):
        result = self.completed({"passed": 1}, summary=None, ok=False)

        assert attempt_from_mcp(UNIT_A, result).state == "error"

    def test_mixed_pass_and_skip_is_passed(self):
        result = self.completed({"passed": 1, "skipped": 1})

        assert attempt_from_mcp(UNIT_A, result).state == "passed"

    def test_scenario_counts_with_total_is_supported(self):
        result = self.completed(
            {"total": 6, "passed": 1, "skipped": 5, "failed": 0}
        )

        assert attempt_from_mcp(UNIT_A, result).state == "passed"

    def test_unknown_scenario_status_is_rejected(self):
        result = self.completed({"passed": 1, "unknown": 1})

        with pytest.raises(CampaignError, match="cannot classify"):
            attempt_from_mcp(UNIT_A, result)

    def test_pass_contradicted_by_return_code_is_rejected(self):
        result = self.completed({"passed": 1}, ok=False)

        with pytest.raises(CampaignError, match="ok=false"):
            attempt_from_mcp(UNIT_A, result)

    def test_capacity_exceeded_is_not_an_attempt(self):
        rejected = {
            "status": "capacity_exceeded",
            "ok": False,
            "error": "full",
        }

        with pytest.raises(CampaignError, match="capacity_exceeded"):
            attempt_from_mcp(UNIT_A, rejected)

    def test_entries_pair_units_with_results(self):
        attempts = attempts_from_mcp(
            [
                {
                    "unit": UNIT_A.as_dict(),
                    "result": self.completed({"passed": 1}),
                },
                {
                    "unit": UNIT_B.as_dict(),
                    "result": {
                        "status": "started",
                        "job_id": "job-2",
                    },
                },
            ]
        )

        assert [(a.unit, a.state) for a in attempts] == [
            (UNIT_A, "passed"),
            (UNIT_B, "running"),
        ]

    def test_entry_without_a_unit_is_rejected(self):
        with pytest.raises(CampaignError, match="missing fields: unit"):
            attempts_from_mcp([{"result": self.completed({"passed": 1})}])


class TestFilters:
    def test_state_filter_applies_to_current_state(self):
        statuses = reduce_units(
            [
                PlanRecord(UNIT_A),
                PlanRecord(UNIT_B),
                attempt(UNIT_A, "failed", "job-1"),
            ]
        )
        filters = Filters(state=("failed",))

        kept = [s.unit for s in statuses if filters.matches(s)]

        assert kept == [UNIT_A]

    def test_counts_cover_every_state(self):
        counts = count_states(reduce_units([PlanRecord(UNIT_A)]))

        assert counts["unattempted"] == 1
        assert counts["passed"] == 0

    def test_running_and_problems_extract_matching_statuses(self):
        statuses = reduce_units(
            [
                PlanRecord(UNIT_A),
                PlanRecord(UNIT_B),
                attempt(UNIT_A, "running", "job-1"),
                attempt(UNIT_B, "failed", "job-2"),
            ]
        )

        assert [s.unit for s in running(statuses)] == [UNIT_A]
        assert [s.unit for s in problems(statuses)] == [UNIT_B]
