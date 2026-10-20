import json
import os
import tarfile

import pytest

from behave_campaign.adapters import (
    FileCampaignRunLock,
    JobDirFiles,
    JsonlCampaignStore,
    SingleFileCampaignStore,
    TarBundleWriter,
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
from behave_campaign.export import BundleEntry
from behave_campaign.ports import (
    CampaignExistsError,
    CampaignLockedError,
    CampaignNotFoundError,
)
from behave_mcp.pools import Pool

UNIT_A = Unit("features/a.feature", "A", "jammy", "lxd-container")
UNIT_B = Unit("features/b.feature", "B", "noble", "lxd-vm")
AT = "2026-09-12T12:00:00Z"


def header(**overrides):
    fields = {
        "at": AT,
        "campaign_id": "1234567",
        "repo": RepoState(root="/repo", commit="abc", branch="main"),
        "filters": Filters(release=("jammy",)),
        "lanes": {Pool.LXD: 1},
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
        "lanes": {"lxd": 1},
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

    def test_a_header_missing_lanes_is_rejected(self, tmp_path):
        path = tmp_path / "1234567.jsonl"
        path.write_text(json.dumps(_stored_header(drop="lanes")) + "\n")

        with pytest.raises(CampaignError) as error:
            JsonlCampaignStore(tmp_path).replay("1234567")

        assert "lanes" in str(error.value)

    def test_the_header_round_trips_both_fields(self, tmp_path):
        store = JsonlCampaignStore(tmp_path)
        store.create(
            "1234567",
            header(install_from="proposed", lanes={Pool.LXD: 8, Pool.WSL: 1}),
            plans(UNIT_A),
        )

        replayed = store.replay("1234567")[0]

        assert replayed.install_from == "proposed"
        assert replayed.lanes == {Pool.LXD: 8, Pool.WSL: 1}

    def test_the_header_always_states_them_on_disk(self, tmp_path):
        store = JsonlCampaignStore(tmp_path)
        store.create("1234567", header(), plans(UNIT_A))

        stored = json.loads(
            (tmp_path / "1234567.jsonl").read_text().splitlines()[0]
        )

        assert stored["install_from"] == "local"
        assert stored["lanes"] == {"lxd": 1}

    @pytest.mark.parametrize(
        "lanes, message",
        [
            ({"lxd": 0}, "lxd lanes must be a positive integer"),
            ({"lxd": True}, "lxd lanes must be a positive integer"),
            ({"wsl": 2}, "wsl lanes may not exceed 1"),
            ({"toaster": 1}, "pool must be one of"),
            ([1], "must be an object"),
        ],
    )
    def test_bad_lanes_on_disk_are_rejected(self, tmp_path, lanes, message):
        path = tmp_path / "1234567.jsonl"
        path.write_text(json.dumps(_stored_header(lanes=lanes)) + "\n")

        with pytest.raises(CampaignError, match=message):
            JsonlCampaignStore(tmp_path).replay("1234567")


class TestSingleFileCampaignStore:
    def test_it_writes_to_the_exact_path_it_was_given(self, tmp_path):
        # The CLI is pointed at a file, so a name that is not "<id>.jsonl"
        # must still be the file that gets written.
        path = tmp_path / "records.txt"
        store = SingleFileCampaignStore(path)

        store.create("ignored", header(), plans(UNIT_A))

        assert path.is_file()
        assert not (tmp_path / "ignored.jsonl").exists()

    def test_a_missing_file_is_not_a_campaign(self, tmp_path):
        store = SingleFileCampaignStore(tmp_path / "absent.jsonl")

        with pytest.raises(CampaignNotFoundError, match="absent.jsonl"):
            store.replay("")
        with pytest.raises(CampaignNotFoundError, match="absent.jsonl"):
            store.append("", [])

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


class TestJobDirFiles:
    def test_lists_only_the_files_a_job_wrote(self, tmp_path):
        (tmp_path / "abc_meta.json").write_text("{}")
        (tmp_path / "abc_stdout.log").write_text("log")

        found = JobDirFiles(tmp_path).files("abc")

        assert found == {
            "meta.json": tmp_path / "abc_meta.json",
            "stdout.log": tmp_path / "abc_stdout.log",
        }


class TestTarBundleWriter:
    def entries(self, tmp_path):
        source = tmp_path / "source.log"
        source.write_text("copied")
        return [
            BundleEntry(name="b.txt", data=b"rendered"),
            BundleEntry(name="jobs/x/stdout.log", source=source),
        ]

    def test_writes_entries_under_one_top_directory(self, tmp_path):
        output = tmp_path / "out" / "bundle.tar.gz"

        skipped = TarBundleWriter().write(
            output, "LP-1", self.entries(tmp_path)
        )

        assert skipped == []
        with tarfile.open(output) as archive:
            assert archive.getnames() == [
                "LP-1/b.txt",
                "LP-1/jobs/x/stdout.log",
            ]
            member = archive.extractfile("LP-1/jobs/x/stdout.log")
            assert member.read() == b"copied"

    def test_same_entries_give_the_same_bytes(self, tmp_path):
        first, second = tmp_path / "1.tar.gz", tmp_path / "2.tar.gz"
        entries = self.entries(tmp_path)

        TarBundleWriter().write(first, "LP-1", entries)
        TarBundleWriter().write(second, "LP-1", list(reversed(entries)))

        assert first.read_bytes() == second.read_bytes()

    def test_missing_source_is_skipped_and_reported(self, tmp_path):
        output = tmp_path / "bundle.tar.gz"
        entries = [BundleEntry(name="gone", source=tmp_path / "absent")]

        assert TarBundleWriter().write(output, "LP-1", entries) == ["gone"]

    def test_never_replaces_an_existing_file(self, tmp_path):
        output = tmp_path / "bundle.tar.gz"
        output.write_bytes(b"precious")

        with pytest.raises(CampaignError, match="already exists"):
            TarBundleWriter().write(output, "LP-1", self.entries(tmp_path))

        assert output.read_bytes() == b"precious"

    def test_leaves_no_temporary_file(self, tmp_path):
        output = tmp_path / "out" / "bundle.tar.gz"

        TarBundleWriter().write(output, "LP-1", self.entries(tmp_path))

        assert [path.name for path in output.parent.iterdir()] == [
            "bundle.tar.gz"
        ]

    def test_file_is_readable_by_others_under_the_umask(self, tmp_path):
        output = tmp_path / "bundle.tar.gz"
        previous = os.umask(0o022)
        try:
            TarBundleWriter().write(output, "LP-1", self.entries(tmp_path))
        finally:
            os.umask(previous)

        assert output.stat().st_mode & 0o777 == 0o644
