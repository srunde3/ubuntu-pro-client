import json

import pytest

from behave_campaign.adapters import (
    FileCampaignRunLock,
    JsonlCampaignStore,
    SingleFileCampaignStore,
)
from behave_campaign.domain import (
    AttemptFinished,
    AttemptStarted,
    CampaignError,
    CampaignHeader,
    Filters,
    PlanRecord,
    RepoState,
    Unit,
)
from behave_campaign.ports import (
    CampaignExistsError,
    CampaignLockedError,
    CampaignNotFoundError,
)

UNIT_A = Unit("features/a.feature", "A", "jammy", "lxd-container")
UNIT_B = Unit("features/b.feature", "B", "noble", "lxd-vm")
AT = "2026-09-12T12:00:00Z"


def header(**overrides):
    fields = {
        "at": AT,
        "campaign_id": "1234567",
        "repo": RepoState(root="/repo", commit="abc", branch="main"),
        "filters": Filters(release=("jammy",)),
    }
    fields.update(overrides)
    return CampaignHeader(**fields)


def _stored_header(drop=None, **overrides):
    """A campaign header as it appears on disk, for malformed-input tests."""
    stored = {
        "type": "campaign",
        "at": AT,
        "campaign_id": "1234567",
        "repo": {
            "root": "/repo",
            "commit": None,
            "branch": None,
            "dirty": None,
        },
        "filters": {
            "feature": [],
            "scenario": [],
            "release": [],
            "machine_type": [],
        },
        "install_from": "local",
        "max_lanes": 1,
    }
    stored.update(overrides)
    if drop is not None:
        del stored[drop]
    return stored


def plans(*units):
    return [PlanRecord(unit=unit, at=AT) for unit in units]


class TestJsonlCampaignStore:
    def test_create_then_replay_round_trips(self, tmp_path):
        store = JsonlCampaignStore(tmp_path)
        store.create("1234567", header(), plans(UNIT_A, UNIT_B))

        records = store.replay("1234567")

        assert records[0] == header()
        assert records[1:] == plans(UNIT_A, UNIT_B)

    def test_create_writes_one_record_per_line(self, tmp_path):
        store = JsonlCampaignStore(tmp_path)
        store.create("1234567", header(), plans(UNIT_A, UNIT_B))

        lines = (tmp_path / "1234567.jsonl").read_text().splitlines()

        assert len(lines) == 3
        assert json.loads(lines[0])["type"] == "campaign"
        assert json.loads(lines[1])["type"] == "plan"

    def test_creating_the_same_campaign_twice_is_rejected(self, tmp_path):
        store = JsonlCampaignStore(tmp_path)
        store.create("1234567", header(), plans(UNIT_A))

        with pytest.raises(CampaignExistsError):
            store.create("1234567", header(), plans(UNIT_B))

    def test_creating_twice_leaves_the_first_campaign_intact(self, tmp_path):
        store = JsonlCampaignStore(tmp_path)
        store.create("1234567", header(), plans(UNIT_A))
        original = (tmp_path / "1234567.jsonl").read_text()

        with pytest.raises(CampaignExistsError):
            store.create("1234567", header(), plans(UNIT_B))

        assert (tmp_path / "1234567.jsonl").read_text() == original

    def test_append_adds_attempts_after_the_plan(self, tmp_path):
        store = JsonlCampaignStore(tmp_path)
        store.create("1234567", header(), plans(UNIT_A))
        started = AttemptStarted(
            unit=UNIT_A, job_id="job1", install_from="proposed", at=AT
        )
        finished = AttemptFinished(
            unit=UNIT_A, job_id="job1", outcome="passed", at=AT
        )

        store.append("1234567", [started, finished])

        assert store.replay("1234567")[-2:] == [started, finished]

    def test_append_of_nothing_leaves_the_file_alone(self, tmp_path):
        store = JsonlCampaignStore(tmp_path)
        store.create("1234567", header(), plans(UNIT_A))
        original = (tmp_path / "1234567.jsonl").read_text()

        store.append("1234567", [])

        assert (tmp_path / "1234567.jsonl").read_text() == original

    def test_replay_of_unknown_campaign_is_rejected(self, tmp_path):
        with pytest.raises(CampaignNotFoundError):
            JsonlCampaignStore(tmp_path).replay("nope")

    def test_append_to_unknown_campaign_is_rejected(self, tmp_path):
        with pytest.raises(CampaignNotFoundError):
            JsonlCampaignStore(tmp_path).append("nope", plans(UNIT_A))

    def test_a_corrupt_line_names_the_file_and_line(self, tmp_path):
        store = JsonlCampaignStore(tmp_path)
        store.create("1234567", header(), plans(UNIT_A))
        with (tmp_path / "1234567.jsonl").open("a") as stream:
            stream.write('{"type": "plan"}\n')

        with pytest.raises(CampaignError) as error:
            store.replay("1234567")

        assert "1234567.jsonl" in str(error.value)
        assert "line 3" in str(error.value)

    def test_list_ids_is_sorted_and_skips_event_files(self, tmp_path):
        store = JsonlCampaignStore(tmp_path)
        store.create("222", header(), plans(UNIT_A))
        store.create("111", header(), plans(UNIT_A))
        (tmp_path / "111.events.jsonl").write_text("{}\n")
        (tmp_path / "111.lock").write_text("")

        assert store.list_ids() == ["111", "222"]

    def test_list_ids_on_a_missing_directory_is_empty(self, tmp_path):
        assert JsonlCampaignStore(tmp_path / "absent").list_ids() == []

    def test_exists_reflects_the_file(self, tmp_path):
        store = JsonlCampaignStore(tmp_path)

        assert not store.exists("1234567")
        store.create("1234567", header(), plans(UNIT_A))
        assert store.exists("1234567")

    def test_a_header_missing_install_from_is_rejected(self, tmp_path):
        path = tmp_path / "1234567.jsonl"
        path.write_text(json.dumps(_stored_header(drop="install_from")) + "\n")

        with pytest.raises(CampaignError) as error:
            JsonlCampaignStore(tmp_path).replay("1234567")

        assert "install_from" in str(error.value)

    def test_a_header_missing_max_lanes_is_rejected(self, tmp_path):
        path = tmp_path / "1234567.jsonl"
        path.write_text(json.dumps(_stored_header(drop="max_lanes")) + "\n")

        with pytest.raises(CampaignError) as error:
            JsonlCampaignStore(tmp_path).replay("1234567")

        assert "max_lanes" in str(error.value)

    def test_the_header_round_trips_both_fields(self, tmp_path):
        store = JsonlCampaignStore(tmp_path)
        store.create(
            "1234567",
            header(install_from="proposed", max_lanes=8),
            plans(UNIT_A),
        )

        replayed = store.replay("1234567")[0]

        assert replayed.install_from == "proposed"
        assert replayed.max_lanes == 8

    def test_the_header_always_states_them_on_disk(self, tmp_path):
        store = JsonlCampaignStore(tmp_path)
        store.create("1234567", header(), plans(UNIT_A))

        stored = json.loads(
            (tmp_path / "1234567.jsonl").read_text().splitlines()[0]
        )

        assert stored["install_from"] == "local"
        assert stored["max_lanes"] == 1

    def test_a_non_positive_max_lanes_on_disk_is_rejected(self, tmp_path):
        path = tmp_path / "1234567.jsonl"
        path.write_text(json.dumps(_stored_header(max_lanes=0)) + "\n")

        with pytest.raises(CampaignError) as error:
            JsonlCampaignStore(tmp_path).replay("1234567")

        assert "max_lanes" in str(error.value)


class TestSingleFileCampaignStore:
    def test_it_writes_to_the_exact_path_it_was_given(self, tmp_path):
        # The CLI is pointed at a file, so a name that is not "<id>.jsonl"
        # must still be the file that gets written.
        path = tmp_path / "records.txt"
        store = SingleFileCampaignStore(path)

        store.create("ignored", header(), plans(UNIT_A))

        assert path.is_file()
        assert not (tmp_path / "ignored.jsonl").exists()

    def test_replay_of_a_missing_file_is_empty(self, tmp_path):
        store = SingleFileCampaignStore(tmp_path / "absent.jsonl")

        assert store.replay("") == []

    def test_campaign_id_comes_from_the_file_stem(self, tmp_path):
        store = SingleFileCampaignStore(tmp_path / "1234567.jsonl")

        assert store.campaign_id == "1234567"

    def test_list_ids_is_empty_until_the_file_exists(self, tmp_path):
        store = SingleFileCampaignStore(tmp_path / "1234567.jsonl")

        assert store.list_ids() == []
        store.create("", header(), plans(UNIT_A))
        assert store.list_ids() == ["1234567"]


class TestFileCampaignRunLock:
    def test_acquiring_marks_it_held_for_other_holders(self, tmp_path):
        mine = FileCampaignRunLock(tmp_path)
        theirs = FileCampaignRunLock(tmp_path)

        mine.acquire("1234567")

        assert theirs.held_by_other("1234567")
        assert not mine.held_by_other("1234567")

    def test_a_second_holder_is_refused(self, tmp_path):
        # The held descriptor *is* the lock, so the first holder has to stay
        # referenced -- letting it be collected would release it.
        first = FileCampaignRunLock(tmp_path)
        first.acquire("1234567")

        with pytest.raises(CampaignLockedError):
            FileCampaignRunLock(tmp_path).acquire("1234567")

    def test_releasing_frees_it(self, tmp_path):
        mine = FileCampaignRunLock(tmp_path)
        mine.acquire("1234567")

        mine.release("1234567")

        assert not FileCampaignRunLock(tmp_path).held_by_other("1234567")
        FileCampaignRunLock(tmp_path).acquire("1234567")

    def test_acquiring_twice_from_one_holder_is_allowed(self, tmp_path):
        mine = FileCampaignRunLock(tmp_path)
        mine.acquire("1234567")

        mine.acquire("1234567")

        assert not mine.held_by_other("1234567")

    def test_releasing_what_was_never_held_is_a_no_op(self, tmp_path):
        FileCampaignRunLock(tmp_path).release("1234567")

    def test_an_unlocked_campaign_is_not_held(self, tmp_path):
        assert not FileCampaignRunLock(tmp_path).held_by_other("1234567")


class TestSystemNow:
    def test_it_is_a_second_precision_utc_stamp(self):
        from datetime import datetime

        from behave_campaign.adapters import system_now

        stamp = system_now()

        # One shape for campaign records, whoever wrote them: no fractional
        # seconds and a literal Z rather than an offset.
        assert stamp.endswith("Z")
        assert "." not in stamp
        assert datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
