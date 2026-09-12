"""The event log: numbering, filtering, durability, and blocking reads."""

import json
import threading
import time

import pytest

from behave_campaign.adapters import JsonlEventLog, NullEventLog
from behave_campaign.domain import (
    CampaignError,
    NewEvent,
    event_matches,
    failure_details,
    unit_event_kind,
    validate_event_kinds,
)

AT = "2026-09-12T12:00:00Z"


def new(kind, **data):
    return NewEvent(kind=kind, at=AT, data=data)


@pytest.fixture
def log(tmp_path):
    return JsonlEventLog(tmp_path / "campaigns")


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


class TestValidateEventKinds:
    def test_known_kinds_and_families_are_accepted(self):
        assert validate_event_kinds(["unit.*", "campaign.complete"]) == (
            "unit.*",
            "campaign.complete",
        )

    def test_no_filter_is_accepted(self):
        assert validate_event_kinds([]) == ()

    def test_an_unknown_family_is_rejected(self):
        # A typo would otherwise look like a campaign that emits nothing,
        # which is the most confusing failure available here.
        with pytest.raises(CampaignError) as error:
            validate_event_kinds(["units.*"])

        assert "unknown event family" in str(error.value)

    def test_an_unknown_kind_is_rejected(self):
        with pytest.raises(CampaignError) as error:
            validate_event_kinds(["unit.exploded"])

        assert "unknown event kind" in str(error.value)


class TestUnitEventKind:
    @pytest.mark.parametrize(
        "state,kind",
        [
            ("passed", "unit.passed"),
            ("failed", "unit.failed"),
            ("skipped", "unit.skipped"),
            ("error", "unit.errored"),
        ],
    )
    def test_each_outcome_has_its_own_kind(self, state, kind):
        assert unit_event_kind(state) == kind

    def test_an_unclassifiable_outcome_has_its_own_kind(self):
        assert unit_event_kind("error", unclassifiable=True) == (
            "unit.unclassifiable"
        )


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


class TestJsonlEventLog:
    def test_events_are_numbered_from_one(self, log):
        stored = log.append(
            "c1", [new("campaign.started"), new("lane.started")]
        )

        assert [event.seq for event in stored] == [1, 2]

    def test_numbering_continues_across_appends(self, log):
        log.append("c1", [new("campaign.started")])

        stored = log.append("c1", [new("lane.started")])

        assert [event.seq for event in stored] == [2]

    def test_each_campaign_is_numbered_separately(self, log):
        log.append("c1", [new("campaign.started")])

        stored = log.append("c2", [new("campaign.started")])

        assert stored[0].seq == 1

    def test_appending_nothing_stores_nothing(self, log):
        assert log.append("c1", []) == []
        assert log.latest_seq("c1") == 0

    def test_read_returns_events_after_the_cursor(self, log):
        log.append("c1", [new("campaign.started"), new("lane.started")])

        found = log.read("c1", since_seq=1, kinds=[], limit=10)

        assert [event.kind for event in found] == ["lane.started"]

    def test_read_filters_by_kind(self, log):
        log.append(
            "c1",
            [new("campaign.started"), new("unit.failed"), new("unit.passed")],
        )

        found = log.read("c1", since_seq=0, kinds=["unit.*"], limit=10)

        assert [event.kind for event in found] == [
            "unit.failed",
            "unit.passed",
        ]

    def test_read_respects_the_limit(self, log):
        log.append("c1", [new("unit.passed") for _ in range(5)])

        assert len(log.read("c1", since_seq=0, kinds=[], limit=2)) == 2

    def test_an_unknown_campaign_simply_has_no_events(self, log):
        assert log.read("absent", since_seq=0, kinds=[], limit=10) == []
        assert log.latest_seq("absent") == 0

    def test_latest_seq_tracks_the_last_event(self, log):
        log.append("c1", [new("unit.passed"), new("unit.failed")])

        assert log.latest_seq("c1") == 2

    def test_events_are_written_to_disk(self, log, tmp_path):
        log.append("c1", [new("unit.failed", release="jammy")])

        path = tmp_path / "campaigns" / "c1.events.jsonl"
        stored = json.loads(path.read_text().splitlines()[0])

        assert stored["seq"] == 1
        assert stored["kind"] == "unit.failed"
        assert stored["campaign_id"] == "c1"
        assert stored["data"] == {"release": "jammy"}

    def test_a_cursor_survives_a_restart(self, log, tmp_path):
        log.append("c1", [new("campaign.started"), new("unit.passed")])

        # A fresh process reads the file rather than any memory.
        reopened = JsonlEventLog(tmp_path / "campaigns")

        assert reopened.latest_seq("c1") == 2
        assert [
            e.kind
            for e in reopened.read("c1", since_seq=1, kinds=[], limit=10)
        ] == ["unit.passed"]

    def test_numbering_continues_after_a_restart(self, log, tmp_path):
        log.append("c1", [new("campaign.started")])

        reopened = JsonlEventLog(tmp_path / "campaigns")
        stored = reopened.append("c1", [new("unit.passed")])

        assert stored[0].seq == 2

    def test_a_corrupt_event_line_names_the_file(self, log, tmp_path):
        log.append("c1", [new("campaign.started")])
        path = tmp_path / "campaigns" / "c1.events.jsonl"
        with path.open("a") as stream:
            stream.write('{"seq": "no"}\n')

        with pytest.raises(CampaignError) as error:
            JsonlEventLog(tmp_path / "campaigns").read(
                "c1", since_seq=0, kinds=[], limit=10
            )

        assert "c1.events.jsonl" in str(error.value)
        assert "line 2" in str(error.value)


class TestWaiting:
    def test_it_returns_at_once_when_something_already_matches(self, log):
        log.append("c1", [new("unit.failed")])

        started = time.monotonic()
        found = log.wait("c1", since_seq=0, kinds=[], limit=10, timeout=5.0)

        assert [e.kind for e in found] == ["unit.failed"]
        assert time.monotonic() - started < 1.0

    def test_it_returns_empty_on_timeout_rather_than_raising(self, log):
        # A quiet campaign is not an error.
        found = log.wait("c1", since_seq=0, kinds=[], limit=10, timeout=0.05)

        assert found == []

    def test_an_append_wakes_a_waiting_reader(self, log):
        found = []

        def reader():
            found.extend(
                log.wait("c1", since_seq=0, kinds=[], limit=10, timeout=10.0)
            )

        thread = threading.Thread(target=reader)
        thread.start()
        time.sleep(0.05)
        log.append("c1", [new("unit.passed")])
        thread.join(timeout=10)

        assert [event.kind for event in found] == ["unit.passed"]

    def test_a_reader_keeps_waiting_through_events_it_filtered_out(self, log):
        found = []

        def reader():
            found.extend(
                log.wait(
                    "c1",
                    since_seq=0,
                    kinds=["unit.*"],
                    limit=10,
                    timeout=10.0,
                )
            )

        thread = threading.Thread(target=reader)
        thread.start()
        time.sleep(0.05)
        log.append("c1", [new("lane.started")])
        time.sleep(0.05)
        log.append("c1", [new("unit.failed")])
        thread.join(timeout=10)

        assert [event.kind for event in found] == ["unit.failed"]


class TestNullEventLog:
    def test_it_discards_everything(self):
        log = NullEventLog()

        assert log.append("c1", [new("unit.passed")]) == []
        assert log.read("c1", since_seq=0, kinds=[], limit=10) == []
        assert (
            log.wait("c1", since_seq=0, kinds=[], limit=10, timeout=0.01) == []
        )
        assert log.latest_seq("c1") == 0
