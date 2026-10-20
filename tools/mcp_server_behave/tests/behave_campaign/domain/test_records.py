"""Campaign record types, enums and their serialisation."""

import json

import pytest

from behave_campaign.domain.records import (
    WRITABLE_LIFECYCLE,
    AttemptFinished,
    AttemptStarted,
    CampaignError,
    CampaignHeader,
    Filters,
    Lifecycle,
    LifecycleRecord,
    Outcome,
    PlanRecord,
    RecordType,
    RepoState,
    RetryRecord,
    Unit,
    UnitState,
    encode_record,
    parse_record,
)

UNIT_A = Unit("features/a.feature", "A", "jammy", "lxd-container")


AT = "2026-09-10T12:00:00Z"


class TestRecordRoundTrip:
    def test_every_record_type_survives_encoding(self):
        records = [
            CampaignHeader(
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
            AttemptStarted(UNIT_A, "job-1", install_from="proposed", at=AT),
            AttemptFinished(UNIT_A, "job-1", "failed", at=AT),
            LifecycleRecord(state="paused", at=AT, reason="server_restart"),
            RetryRecord(UNIT_A, at=AT, reason="looked flaky"),
        ]

        assert [
            parse_record(encode_record(record)) for record in records
        ] == records

    def test_campaign_without_an_id_survives_encoding(self):
        campaign = CampaignHeader(at=AT, repo=RepoState(root="/repo"))

        assert parse_record(encode_record(campaign)) == campaign

    def test_unknown_install_source_is_rejected(self):
        raw = encode_record(
            AttemptStarted(UNIT_A, "job-1", install_from="proposed", at=AT)
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

    def test_unknown_outcome_is_rejected(self):
        raw = encode_record(AttemptFinished(UNIT_A, "job-1", "failed", at=AT))
        raw["outcome"] = "exploded"

        with pytest.raises(CampaignError, match="outcome must be one of"):
            parse_record(raw)

    def test_unknown_record_type_is_rejected(self):
        with pytest.raises(CampaignError, match="record type must be"):
            parse_record({"type": "note"})


class TestStringEnums:
    """Every mutually exclusive set is an enum that still reads as a string."""

    @pytest.mark.parametrize(
        "member,expected",
        [
            (RecordType.STARTED, "started"),
            (Outcome.PASSED, "passed"),
            (UnitState.UNATTEMPTED, "unattempted"),
            (Lifecycle.CANCELLED, "cancelled"),
        ],
    )
    def test_a_member_reads_and_serialises_as_its_value(
        self, member, expected
    ):
        assert str(member) == expected
        assert "{}".format(member) == expected
        assert json.dumps(member) == '"{}"'.format(expected)
        assert member == expected

    def test_outcomes_are_a_subset_of_unit_states(self):
        # A unit's state is an outcome, or one of the two conditions that no
        # job reports.
        assert {o.value for o in Outcome} < {s.value for s in UnitState}
        assert {s.value for s in UnitState} - {o.value for o in Outcome} == {
            "unattempted",
            "running",
        }

    def test_every_state_but_created_is_written(self):
        # created is the absence of a record.
        assert set(WRITABLE_LIFECYCLE) == set(Lifecycle) - {Lifecycle.CREATED}
