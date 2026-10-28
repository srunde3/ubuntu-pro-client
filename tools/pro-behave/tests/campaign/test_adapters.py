import itertools
import json
import os
import tarfile
import threading
import time

import pytest

from pro_behave.behave.framework import BehaveFramework
from pro_behave.campaign import adapters as adapters_module
from pro_behave.campaign.adapters import (
    CampaignDirStore,
    CatalogFeatureReader,
    JobDirFiles,
    JsonlCampaignStore,
    ServiceJobRunner,
    TarBundleWriter,
)
from pro_behave.campaign.domain.bundle import BundleEntry
from pro_behave.campaign.domain.records import (
    CampaignError,
    CampaignRecord,
    CampaignState,
    FinishedRecord,
    PlanRecord,
    RepoState,
    Scope,
    StartedRecord,
    Unit,
)
from pro_behave.campaign.domain.state import state_of
from pro_behave.campaign.ports import (
    CampaignExistsError,
    CampaignNotFoundError,
    QueueRefused,
)
from pro_behave.campaign.service import CampaignService
from pro_behave.jobs.domain import catalog
from pro_behave.jobs.domain.pools import Pool
from pro_behave.jobs.service import JobServiceError, UnknownJobError
from tests.conftest import (
    FakeConfigStore,
    FakeJobLauncher,
    FakeWorkspace,
    job_file,
    make_service,
)

UNIT_A = Unit("features/a.feature", "A", "jammy", "lxd-container")
UNIT_B = Unit("features/b.feature", "B", "noble", "lxd-vm")
AT = "2026-09-12T12:00:00Z"


def header(**overrides):
    fields = {
        "at": AT,
        "campaign_id": "1234567",
        "repo": RepoState(root="/repo", commit="abc", branch="main"),
        "scope": Scope(release=("jammy",)),
        "max_jobs": {Pool.LXD: 1},
    }
    fields.update(overrides)
    return CampaignRecord(**fields)


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
        "scope": {
            "feature": [],
            "scenario": [],
            "release": [],
            "machine_type": [],
        },
        "install_from": "local",
        "max_jobs": {"lxd": 1},
    }
    stored.update(overrides)
    if drop is not None:
        del stored[drop]
    return stored


def started(unit, job_id):
    return StartedRecord(unit=unit, job_id=job_id, install_from="local", at=AT)


def plans(*units):
    return [PlanRecord(unit=unit, at=AT) for unit in units]


CAMPAIGN = "1234567"


def stored(tmp_path, campaign_id=CAMPAIGN):
    """The campaign's files, as the store lays them out."""
    campaign_dir = tmp_path / campaign_id
    return (
        campaign_dir / "campaign.json",
        campaign_dir / "records.jsonl",
    )


def write_record(tmp_path, campaign_header, units=()):
    """A campaign.json written by hand, for malformed-input tests."""
    record, _ = stored(tmp_path)
    record.parent.mkdir(parents=True)
    record.write_text(
        json.dumps({"campaign": campaign_header, "units": list(units)})
    )


class TestJsonlCampaignStore:
    def test_create_then_replay_round_trips(self, tmp_path):
        store = JsonlCampaignStore(tmp_path)
        store.create(CAMPAIGN, header(), plans(UNIT_A, UNIT_B))

        records = store.read_records(CAMPAIGN)

        assert records[0] == header()
        assert records[1:] == plans(UNIT_A, UNIT_B)

    def test_create_writes_the_header_and_units_once(self, tmp_path):
        store = JsonlCampaignStore(tmp_path)
        store.create(CAMPAIGN, header(), plans(UNIT_A, UNIT_B))

        record, log = stored(tmp_path)
        document = json.loads(record.read_text())

        assert document["campaign"]["type"] == "campaign"
        assert document["units"] == [UNIT_A.as_dict(), UNIT_B.as_dict()]
        assert not log.exists()

    def test_creating_the_same_campaign_twice_is_rejected(self, tmp_path):
        store = JsonlCampaignStore(tmp_path)
        store.create(CAMPAIGN, header(), plans(UNIT_A))

        with pytest.raises(CampaignExistsError):
            store.create(CAMPAIGN, header(), plans(UNIT_B))

    def test_creating_twice_leaves_the_first_campaign_intact(self, tmp_path):
        store = JsonlCampaignStore(tmp_path)
        store.create(CAMPAIGN, header(), plans(UNIT_A))
        record, _ = stored(tmp_path)
        original = record.read_text()

        with pytest.raises(CampaignExistsError):
            store.create(CAMPAIGN, header(), plans(UNIT_B))

        assert record.read_text() == original

    def test_append_adds_records_to_the_log(self, tmp_path):
        store = JsonlCampaignStore(tmp_path)
        store.create(CAMPAIGN, header(), plans(UNIT_A))
        finished = FinishedRecord(
            unit=UNIT_A, job_id="job1", outcome="passed", at=AT
        )

        store.append(CAMPAIGN, [started(UNIT_A, "job1"), finished])

        _, log = stored(tmp_path)
        assert len(log.read_text().splitlines()) == 2
        assert store.read_records(CAMPAIGN)[-2:] == [
            started(UNIT_A, "job1"),
            finished,
        ]

    def test_append_of_nothing_writes_nothing(self, tmp_path):
        store = JsonlCampaignStore(tmp_path)
        store.create(CAMPAIGN, header(), plans(UNIT_A))

        store.append(CAMPAIGN, [])

        _, log = stored(tmp_path)
        assert not log.exists()

    def test_a_torn_last_line_is_ignored(self, tmp_path):
        # A crash mid-append leaves part of a line and no newline.
        store = JsonlCampaignStore(tmp_path)
        store.create(CAMPAIGN, header(), plans(UNIT_A))
        store.append(CAMPAIGN, [started(UNIT_A, "job1")])
        _, log = stored(tmp_path)
        with log.open("a") as stream:
            stream.write('{"type": "fini')

        records = store.read_records(CAMPAIGN)

        assert records[-1] == started(UNIT_A, "job1")

    def test_the_next_append_cuts_off_a_torn_line(self, tmp_path):
        store = JsonlCampaignStore(tmp_path)
        store.create(CAMPAIGN, header(), plans(UNIT_A))
        store.append(CAMPAIGN, [started(UNIT_A, "job1")])
        _, log = stored(tmp_path)
        with log.open("a") as stream:
            stream.write('{"type": "fini')

        store.append(CAMPAIGN, [started(UNIT_A, "job2")])

        assert store.read_records(CAMPAIGN)[-2:] == [
            started(UNIT_A, "job1"),
            started(UNIT_A, "job2"),
        ]
        assert "fini" not in log.read_text()

    def test_replay_of_unknown_campaign_is_rejected(self, tmp_path):
        with pytest.raises(CampaignNotFoundError):
            JsonlCampaignStore(tmp_path).read_records("nope")

    def test_append_to_unknown_campaign_is_rejected(self, tmp_path):
        with pytest.raises(CampaignNotFoundError):
            JsonlCampaignStore(tmp_path).append("nope", plans(UNIT_A))

    def test_a_corrupt_line_names_the_file_and_line(self, tmp_path):
        store = JsonlCampaignStore(tmp_path)
        store.create(CAMPAIGN, header(), plans(UNIT_A))
        store.append(CAMPAIGN, [started(UNIT_A, "job1")])
        _, log = stored(tmp_path)
        with log.open("a") as stream:
            stream.write('{"type": "plan"}\n')

        with pytest.raises(CampaignError) as error:
            store.read_records(CAMPAIGN)

        assert "records.jsonl" in str(error.value)
        assert "line 2" in str(error.value)

    def test_list_ids_is_sorted_and_needs_a_record(self, tmp_path):
        store = JsonlCampaignStore(tmp_path)
        store.create("222", header(), plans(UNIT_A))
        store.create("111", header(), plans(UNIT_A))
        (tmp_path / "half-made").mkdir()

        assert store.list_ids() == ["111", "222"]

    def test_list_ids_on_a_missing_directory_is_empty(self, tmp_path):
        assert JsonlCampaignStore(tmp_path / "absent").list_ids() == []

    def test_exists_reflects_the_record(self, tmp_path):
        store = JsonlCampaignStore(tmp_path)

        assert not store.exists(CAMPAIGN)
        store.create(CAMPAIGN, header(), plans(UNIT_A))
        assert store.exists(CAMPAIGN)

    def test_path_for_is_the_campaign_directory(self, tmp_path):
        store = JsonlCampaignStore(tmp_path)

        assert store.path_for(CAMPAIGN) == tmp_path / CAMPAIGN

    def test_a_header_missing_install_from_is_rejected(self, tmp_path):
        write_record(tmp_path, _stored_header(drop="install_from"))

        with pytest.raises(CampaignError) as error:
            JsonlCampaignStore(tmp_path).read_records(CAMPAIGN)

        assert "install_from" in str(error.value)

    def test_a_header_missing_max_jobs_is_rejected(self, tmp_path):
        write_record(tmp_path, _stored_header(drop="max_jobs"))

        with pytest.raises(CampaignError) as error:
            JsonlCampaignStore(tmp_path).read_records(CAMPAIGN)

        assert "max_jobs" in str(error.value)

    def test_an_unreadable_record_names_its_file(self, tmp_path):
        record, _ = stored(tmp_path)
        record.parent.mkdir(parents=True)
        record.write_text("not json")

        with pytest.raises(CampaignError, match="campaign.json"):
            JsonlCampaignStore(tmp_path).read_records(CAMPAIGN)

    def test_the_header_round_trips_both_fields(self, tmp_path):
        store = JsonlCampaignStore(tmp_path)
        store.create(
            CAMPAIGN,
            header(
                install_from="proposed", max_jobs={Pool.LXD: 8, Pool.WSL: 1}
            ),
            plans(UNIT_A),
        )

        replayed = store.read_records(CAMPAIGN)[0]

        assert replayed.install_from == "proposed"
        assert replayed.max_jobs == {Pool.LXD: 8, Pool.WSL: 1}

    def test_the_header_always_states_them_on_disk(self, tmp_path):
        store = JsonlCampaignStore(tmp_path)
        store.create(CAMPAIGN, header(), plans(UNIT_A))

        record, _ = stored(tmp_path)
        campaign = json.loads(record.read_text())["campaign"]

        assert campaign["install_from"] == "local"
        assert campaign["max_jobs"] == {"lxd": 1}

    @pytest.mark.parametrize(
        "max_jobs, message",
        [
            ({"lxd": 0}, "lxd max jobs must be a positive integer"),
            ({"lxd": True}, "lxd max jobs must be a positive integer"),
            ({"wsl": 2}, "wsl max jobs may not exceed 1"),
            ({"toaster": 1}, "pool must be one of"),
            ([1], "must be an object"),
        ],
    )
    def test_bad_max_jobs_on_disk_are_rejected(
        self, tmp_path, max_jobs, message
    ):
        write_record(tmp_path, _stored_header(max_jobs=max_jobs))

        with pytest.raises(CampaignError, match=message):
            JsonlCampaignStore(tmp_path).read_records(CAMPAIGN)


class TestCampaignDirStore:
    def test_it_writes_to_the_directory_it_was_given(self, tmp_path):
        campaign_dir = tmp_path / "anywhere"
        store = CampaignDirStore(campaign_dir)

        store.create("ignored", header(), plans(UNIT_A))

        assert (campaign_dir / "campaign.json").is_file()
        assert not (tmp_path / "ignored").exists()

    def test_a_missing_directory_is_not_a_campaign(self, tmp_path):
        store = CampaignDirStore(tmp_path / "absent")

        with pytest.raises(CampaignNotFoundError, match="absent"):
            store.read_records("")
        with pytest.raises(CampaignNotFoundError, match="absent"):
            store.append("", [])

    def test_campaign_id_comes_from_the_directory_name(self, tmp_path):
        store = CampaignDirStore(tmp_path / CAMPAIGN)

        assert store.campaign_id == CAMPAIGN

    def test_list_ids_is_empty_until_the_campaign_exists(self, tmp_path):
        store = CampaignDirStore(tmp_path / CAMPAIGN)

        assert store.list_ids() == []
        store.create("", header(), plans(UNIT_A))
        assert store.list_ids() == [CAMPAIGN]


class TestCatalogFeatureReader:
    def test_units_come_from_the_catalog_it_was_given(self, tmp_path):
        feature = catalog.FeatureDetail(
            path="features/a.feature",
            title="A",
            tags=[],
            requires_config=[],
            scenarios=[
                catalog.ScenarioSummary(
                    name="A",
                    type="scenario_outline",
                    tags=[],
                    requires_config=[],
                    example_columns=[],
                    combos=[catalog.Combo("jammy", "lxd-container")],
                )
            ],
        )
        reader = CatalogFeatureReader(lambda repo_root: [feature])

        units = reader.discover_units(tmp_path, Scope())

        assert units == [
            Unit("features/a.feature", "A", "jammy", "lxd-container")
        ]

    def test_an_empty_catalog_is_rejected(self, tmp_path):
        reader = CatalogFeatureReader(lambda repo_root: [])

        with pytest.raises(CampaignError, match="no features found"):
            reader.discover_units(tmp_path, Scope())


class TestSystemNow:
    def test_it_is_a_microsecond_utc_stamp(self):
        from datetime import datetime

        from pro_behave.campaign.adapters import system_now

        stamp = system_now()

        # One shape for every timestamp this tool writes: microseconds and a
        # literal Z rather than an offset.
        assert datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%fZ")


class TestJobDirFiles:
    def test_lists_only_the_files_a_job_wrote(self, tmp_path):
        (job_file(tmp_path, "abc", "job.json")).write_text("{}")
        (job_file(tmp_path, "abc", "stdout.log")).write_text("log")

        found = JobDirFiles(tmp_path).files("abc")

        assert found == {
            "job.json": job_file(tmp_path, "abc", "job.json"),
            "stdout.log": job_file(tmp_path, "abc", "stdout.log"),
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


# ---- waiting for a campaign to grow ----


class TestStoreWait:
    # Short enough that the tests stay quick, long enough to be sure a
    # wait that should return at once did not wait it out.
    PATIENCE = 5.0

    @pytest.fixture
    def store(self, tmp_path):
        store = JsonlCampaignStore(tmp_path)
        store.create("c1", header(), plans(UNIT_A))
        return store

    def test_it_returns_at_once_when_records_were_added(self, store):
        seen = len(store.read_records("c1"))
        store.append("c1", [started(UNIT_A, "job-1")])

        began = time.monotonic()
        store.wait("c1", seen, timeout=self.PATIENCE)

        assert time.monotonic() - began < 1.0

    def test_it_returns_on_timeout_rather_than_raising(self, store):
        # A quiet campaign is not an error.
        store.wait("c1", len(store.read_records("c1")), timeout=0.05)

    def test_an_append_wakes_a_waiting_reader(self, store):
        seen = len(store.read_records("c1"))
        woke = threading.Event()

        def reader():
            store.wait("c1", seen, timeout=self.PATIENCE)
            woke.set()

        thread = threading.Thread(target=reader)
        thread.start()
        store.append("c1", [started(UNIT_A, "job-1")])

        assert woke.wait(self.PATIENCE)
        thread.join(self.PATIENCE)

    def test_another_writer_is_noticed_by_polling(
        self, store, tmp_path, monkeypatch
    ):
        # A second store object stands for another process: its append
        # cannot wake this one's readers, so they poll.
        monkeypatch.setattr(adapters_module._Changes, "POLL_SECONDS", 0.01)
        seen = len(store.read_records("c1"))
        JsonlCampaignStore(tmp_path).append("c1", [started(UNIT_A, "job-1")])

        began = time.monotonic()
        store.wait("c1", seen, timeout=self.PATIENCE)

        assert time.monotonic() - began < 1.0


FEATURE_A = """Feature: A feature

  Scenario Outline: A
    Given a `<release>` `<machine_type>` machine

    Examples: ubuntu release
      | release | machine_type  |
      | jammy   | lxd-container |
"""


class TestServiceJobRunner:
    """A campaign's jobs queued, read and killed through a real
    JobService."""

    @staticmethod
    def job_runner(tmp_path, launcher, **ports):
        repo_root = tmp_path / "repo"
        (repo_root / "features").mkdir(parents=True)
        (repo_root / "features" / "a.feature").write_text(FEATURE_A)
        (repo_root / "tox.ini").write_text("[tox]\n")
        ports.setdefault("new_job_id", lambda: "job-a")
        service = make_service(
            FakeWorkspace(repo_root=repo_root, jobs_dir=tmp_path),
            launcher=launcher,
            now_utc=lambda: AT,
            **ports,
        )
        runner = ServiceJobRunner(
            service, refusal=JobServiceError, unknown_job=UnknownJobError
        )
        return runner, repo_root

    @staticmethod
    def queue(runner, repo_root, record=lambda: None, unit=UNIT_A):
        runner.queue(
            "job-a",
            unit,
            repo_root=repo_root,
            install_from="local",
            campaign_id="1234567",
            record=record,
        )
        runner.reconcile_jobs()
        return "job-a"

    def test_a_queued_job_belongs_to_its_campaign(self, tmp_path):
        launcher = FakeJobLauncher(tmp_path)
        runner, repo_root = self.job_runner(tmp_path, launcher)

        job_id = self.queue(runner, repo_root)

        request = json.loads((tmp_path / job_id / "request.json").read_text())
        assert request["campaign_id"] == "1234567"

    def test_the_attempt_is_recorded_before_the_request_exists(self, tmp_path):
        launcher = FakeJobLauncher(tmp_path)
        runner, repo_root = self.job_runner(tmp_path, launcher)
        seen = []

        self.queue(
            runner,
            repo_root,
            record=lambda: seen.append(
                (tmp_path / "job-a" / "request.json").exists()
            ),
        )

        assert seen == [False]

    def test_a_refused_unit_records_nothing(self, tmp_path):
        launcher = FakeJobLauncher(tmp_path)
        runner, repo_root = self.job_runner(
            tmp_path, launcher, config=FakeConfigStore({Pool.LXD: 0})
        )
        recorded = []

        with pytest.raises(QueueRefused) as refused:
            self.queue(runner, repo_root, record=lambda: recorded.append(1))

        assert refused.value.pool == Pool.LXD
        assert recorded == []

    def test_a_running_job_reads_as_running(self, tmp_path):
        launcher = FakeJobLauncher(tmp_path)
        runner, repo_root = self.job_runner(tmp_path, launcher)
        job_id = self.queue(runner, repo_root)

        assert runner.poll(job_id)["state"] == "running"

    def test_an_ended_job_reports_how_it_ended(self, tmp_path):
        launcher = FakeJobLauncher(tmp_path)
        runner, repo_root = self.job_runner(tmp_path, launcher)
        job_id = self.queue(runner, repo_root)
        launcher.crash(job_id)

        result = runner.poll(job_id)

        assert (result["state"], result["reason"]) == ("interrupted", "died")

    def test_a_job_never_queued_reads_as_died(self, tmp_path):
        runner, _ = self.job_runner(tmp_path, FakeJobLauncher(tmp_path))

        result = runner.poll("never-queued")

        assert (result["state"], result["reason"]) == ("interrupted", "died")

    def test_a_running_job_is_killed(self, tmp_path):
        launcher = FakeJobLauncher(tmp_path)
        runner, repo_root = self.job_runner(tmp_path, launcher)
        job_id = self.queue(runner, repo_root)

        assert runner.kill(job_id) is True
        assert launcher.killed == [job_id]

    def test_a_finished_job_is_not_killed(self, tmp_path):
        launcher = FakeJobLauncher(tmp_path)
        runner, repo_root = self.job_runner(tmp_path, launcher)
        job_id = self.queue(runner, repo_root)
        launcher.finish(job_id)

        assert runner.kill(job_id) is False
        assert launcher.killed == []


class TestCampaignsShareTheJobQueue:
    """Two campaigns and a one-off job on one pool, through the real job
    service: one lane, so one job runs at a time, whoever queued it."""

    def test_they_take_turns_on_the_one_lane(self, tmp_path):
        repo_root = tmp_path / "repo"
        (repo_root / "features").mkdir(parents=True)
        (repo_root / "features" / "a.feature").write_text(FEATURE_A)
        (repo_root / "tox.ini").write_text("[tox]\n")
        launcher = FakeJobLauncher(tmp_path / "jobs")
        numbers = itertools.count(1)
        service = make_service(
            FakeWorkspace(repo_root=repo_root, jobs_dir=tmp_path / "jobs"),
            launcher=launcher,
            new_job_id=lambda: "job{}".format(next(numbers)),
        )
        store = JsonlCampaignStore(tmp_path / "campaigns")
        campaigns = CampaignService(
            store=store,
            features=CatalogFeatureReader(BehaveFramework().catalog),
            now=lambda: AT,
            repo_state=lambda root: RepoState(root=str(root)),
            jobs=ServiceJobRunner(
                service,
                refusal=JobServiceError,
                unknown_job=UnknownJobError,
            ),
        )
        for campaign_id in ("111", "222"):
            campaigns.create_campaign(
                campaign_id=campaign_id, repo_root=repo_root
            )
            campaigns.run(campaign_id=campaign_id)
        one_off = service.run_job(
            "features/a.feature", "A", "jammy", "lxd-container"
        )

        # The first campaign's job holds the lane; the rest wait.
        assert len(launcher.running) == 1
        assert one_off.state == "queued"

        for _ in range(3):
            (running,) = launcher.running
            launcher.finish(running)
            campaigns.reconcile()

        assert [call["job_id"] for call in launcher.calls] == [
            "job1",
            "job2",
            "job3",
        ]
        for campaign_id in ("111", "222"):
            records = store.read_records(campaign_id)
            assert state_of(records) == CampaignState.COMPLETE
