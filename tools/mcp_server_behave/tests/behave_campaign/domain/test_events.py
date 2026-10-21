"""Event kinds, presets, matching and failure details."""

import json

import pytest

from behave_campaign.domain.events import (
    ACTIONABLE_PRESET,
    STATE_EVENT,
    EventKind,
    event_matches,
    expand_event_kinds,
    failure_details,
    unit_event_kind,
)
from behave_campaign.domain.records import (
    CampaignError,
    CampaignState,
    Outcome,
)


class TestEventMatching:
    def test_no_patterns_matches_everything(self):
        assert event_matches("unit.failed", [])

    def test_an_exact_kind_matches_only_itself(self):
        assert event_matches("unit.failed", ["unit.failed"])
        assert not event_matches("unit.passed", ["unit.failed"])

    def test_a_family_matches_its_kinds(self):
        assert event_matches("unit.failed", ["unit.*"])
        assert event_matches("unit.passed", ["unit.*"])
        assert not event_matches("lane.started", ["unit.*"])

    def test_patterns_are_a_union(self):
        patterns = ["unit.*", "campaign.complete"]

        assert event_matches("unit.errored", patterns)
        assert event_matches("campaign.complete", patterns)
        assert not event_matches("campaign.paused", patterns)

    def test_a_family_does_not_match_a_longer_prefix(self):
        assert not event_matches("unitary.thing", ["unit.*"])


class TestExpandEventKinds:
    def test_known_kinds_and_families_are_accepted(self):
        assert expand_event_kinds(["unit.*", "campaign.complete"]) == (
            "unit.*",
            "campaign.complete",
        )

    def test_no_filter_is_accepted(self):
        assert expand_event_kinds([]) == ()

    def test_everything_preset_drops_every_other_pattern(self):
        assert expand_event_kinds(["unit.failed", "*"]) == ()

    def test_actionable_preset_expands_in_place(self):
        expanded = expand_event_kinds(["actionable", "unit.passed"])

        assert "unit.failed" in expanded
        assert "anomaly.*" in expanded
        assert "campaign.*" in expanded
        assert expanded[-1] == "unit.passed"

    def test_an_unknown_family_is_rejected(self):
        # A typo would otherwise look like a campaign that emits nothing,
        # which is the most confusing failure available here.
        with pytest.raises(CampaignError) as error:
            expand_event_kinds(["units.*"])

        assert "unknown event family" in str(error.value)

    def test_an_unknown_kind_is_rejected(self):
        with pytest.raises(CampaignError) as error:
            expand_event_kinds(["unit.exploded"])

        assert "unknown event kind" in str(error.value)


EVENT_FOR_OUTCOME = {
    Outcome.PASSED: EventKind.UNIT_PASSED,
    Outcome.FAILED: EventKind.UNIT_FAILED,
    Outcome.SKIPPED: EventKind.UNIT_SKIPPED,
    Outcome.ERROR: EventKind.UNIT_ERRORED,
}


def test_every_outcome_has_an_expected_event():
    assert set(EVENT_FOR_OUTCOME) == set(Outcome)


@pytest.mark.parametrize("outcome, kind", EVENT_FOR_OUTCOME.items(), ids=str)
def test_each_outcome_announces_its_own_event(outcome, kind):
    assert unit_event_kind(outcome) == kind


@pytest.mark.parametrize("outcome", list(Outcome), ids=str)
def test_an_unclassifiable_result_announces_unclassifiable(outcome):
    assert unit_event_kind(outcome, unclassifiable=True) == (
        EventKind.UNIT_UNCLASSIFIABLE
    )


# Progress is in the counts on every response, so passes, lane openings and
# a watcher's own retries are left out of what it has to react to.
ACTIONABLE = {
    EventKind.CAMPAIGN_CREATED: True,
    EventKind.CAMPAIGN_RUNNING: True,
    EventKind.CAMPAIGN_FINISHING: True,
    EventKind.CAMPAIGN_COMPLETE: True,
    EventKind.CAMPAIGN_PAUSING: True,
    EventKind.CAMPAIGN_PAUSED: True,
    EventKind.LANE_STARTED: False,
    EventKind.LANE_OVERDUE: True,
    EventKind.UNIT_PASSED: False,
    EventKind.UNIT_FAILED: True,
    EventKind.UNIT_SKIPPED: True,
    EventKind.UNIT_ERRORED: True,
    EventKind.UNIT_UNCLASSIFIABLE: True,
    EventKind.UNIT_INTERRUPTED: True,
    EventKind.UNIT_RETRIED: False,
    EventKind.ANOMALY_REPEATED_SCENARIO_FAILURE: True,
    EventKind.ANOMALY_REPEATED_SKIPS: True,
    EventKind.ANOMALY_CAPACITY_STARVED: True,
}


def test_every_event_kind_is_actionable_or_not():
    assert set(ACTIONABLE) == set(EventKind)


@pytest.mark.parametrize("kind, actionable", ACTIONABLE.items(), ids=str)
def test_the_actionable_preset(kind, actionable):
    patterns = expand_event_kinds([ACTIONABLE_PRESET])

    matched = event_matches(kind, patterns)

    assert matched is actionable


# The event announcing arrival in each named state.
EVENT_FOR_STATE = {
    CampaignState.CREATED: EventKind.CAMPAIGN_CREATED,
    CampaignState.RUNNING: EventKind.CAMPAIGN_RUNNING,
    CampaignState.FINISHING: EventKind.CAMPAIGN_FINISHING,
    CampaignState.COMPLETE: EventKind.CAMPAIGN_COMPLETE,
    CampaignState.PAUSING: EventKind.CAMPAIGN_PAUSING,
    CampaignState.PAUSED: EventKind.CAMPAIGN_PAUSED,
}


def test_every_campaign_state_has_an_expected_event():
    assert set(EVENT_FOR_STATE) == set(CampaignState)


@pytest.mark.parametrize("state, kind", EVENT_FOR_STATE.items(), ids=str)
def test_arriving_in_a_state_announces_its_event(state, kind):
    assert STATE_EVENT[state] == kind


class TestFailureDetails:
    def test_it_carries_the_failing_steps(self):
        result = {
            "failures": [
                {
                    "step": "Then I see it",
                    "status": "failed",
                    "error_message": "nope",
                }
            ]
        }

        assert failure_details(result) == [
            {
                "step": "Then I see it",
                "status": "failed",
                "error_message": "nope",
            }
        ]

    def test_it_caps_how_many_it_carries(self):
        result = {"failures": [{"step": str(n)} for n in range(10)]}

        assert len(failure_details(result)) == 3

    def test_a_long_message_is_truncated(self):
        result = {"failures": [{"error_message": "x" * 5000}]}

        assert len(failure_details(result)[0]["error_message"]) == 1200

    @pytest.mark.parametrize("result", [None, "text", {}, {"failures": 3}])
    def test_anything_unexpected_yields_nothing(self, result):
        assert failure_details(result) == []


class TestEventKindEnum:
    def test_every_kind_belongs_to_a_known_family(self):
        from behave_campaign.domain.events import EventFamily, EventKind

        for kind in EventKind:
            assert kind.family in EventFamily
            assert kind.value.startswith(kind.family.value + ".")

    def test_a_kind_reads_as_its_value(self):
        from behave_campaign.domain.events import EventKind

        # Messages and JSON say "unit.failed", not the member name.
        assert str(EventKind.UNIT_FAILED) == "unit.failed"
        assert "{}".format(EventKind.UNIT_FAILED) == "unit.failed"
        assert json.dumps(EventKind.UNIT_FAILED) == '"unit.failed"'

    def test_a_kind_compares_equal_to_its_string(self):
        from behave_campaign.domain.events import EventKind

        assert EventKind.UNIT_FAILED == "unit.failed"
        assert event_matches(EventKind.UNIT_FAILED, ["unit.*"])
